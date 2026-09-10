"""Bring up Foundry Local and pin it to a known port.

    uv run python 04-foundry-local/bootstrap.py
    uv run python 04-foundry-local/bootstrap.py --model qwen3-0.6b
    uv run python 04-foundry-local/bootstrap.py --status

Option 4 is the only one where the runtime is entirely yours: no Azure
resource, no hourly charge, no network hop for the inference itself. Foundry
Local runs an OpenAI-compatible server against the ONNX runtime on whatever
accelerator this machine actually has - NPU, GPU, or CPU - and it returns a
`usage` object, which is what lets the APIM token policies count it exactly
like the three cloud options.

Two details this script exists to handle:

  * The daemon picks a random port by default. That is fine for a laptop app
    and useless for a stable tunnel, so the port is pinned here and written to
    .env for tunnel.py to pick up.
  * The name in the catalogue ("qwen2.5-0.5b") is an alias. The id the server
    actually answers to is a hardware-specific variant such as
    "qwen2.5-0.5b-instruct-generic-cpu", and that is what must go in the
    request body. This script resolves it from /v1/models rather than guessing,
    because the right variant depends on the machine.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config import REPO_ROOT, get_option, load_settings  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"
DEFAULT_PORT = 39839


def foundry_exe() -> str:
    exe = shutil.which("foundry")
    if not exe:
        raise SystemExit(
            "Foundry Local is not installed.\n"
            "  winget install Microsoft.FoundryLocal\n"
            "Then re-run this script."
        )
    return exe


def foundry(args: list[str], *, capture: bool = True, check: bool = True) -> str:
    cmd = [foundry_exe(), *args]
    if not capture:
        proc = subprocess.run(cmd)
        if check and proc.returncode != 0:
            raise SystemExit(f"foundry {' '.join(args)} failed with exit code {proc.returncode}.")
        return ""
    # The CLI draws box-art tables, and on Windows Python would otherwise decode
    # that as cp1252 and raise UnicodeDecodeError before we see any output.
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise SystemExit(f"foundry {' '.join(args)} failed:\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout or ""


def running_endpoint() -> str | None:
    """Return the daemon's base URL if it is up, else None."""
    out = foundry(["server", "status"], check=False)
    match = re.search(r"https?://[0-9.]+:\d+", out)
    if match and "Ready" in out:
        return match.group(0)
    return None


def start_server(port: int) -> str:
    current = running_endpoint()
    if current:
        if current.endswith(f":{port}"):
            print(f"[ok] Foundry Local already listening on {current}")
            return current
        # A daemon on a random port is the default state; restart it on ours so
        # the tunnel URL stays valid across reboots.
        print(f"[info] Daemon is on {current}, restarting on port {port}")
        foundry(["server", "stop"], check=False)

    print(f"[info] Starting Foundry Local on port {port} with no idle timeout")
    foundry(["server", "start", "--port", str(port), "--idle-timeout", "0"], capture=False)

    endpoint = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            httpx.get(f"{endpoint}/v1/models", timeout=2.0).raise_for_status()
            return endpoint
        except Exception:  # noqa: BLE001 - daemon is still coming up
            time.sleep(1)
    raise SystemExit(f"Foundry Local did not become ready on {endpoint}.")


def ensure_model(model: str) -> None:
    cached = foundry(["model", "list"], check=False)
    # The table wraps long names, so match on the leading fragment.
    if re.search(rf"^\|\s*{re.escape(model)}\b.*●\s*\|$", cached, re.MULTILINE):
        print(f"[ok] {model} is already cached")
        return
    print(f"[info] Downloading {model} - first run only")
    foundry(["model", "download", model], capture=False)


def resolve_served_model(endpoint: str, alias: str) -> str:
    """Map a catalogue alias onto the variant id the server answers to.

    /v1/models lists what is *cached*, and the ids there are hardware variants
    ("qwen2.5-0.5b" is served as "qwen2.5-0.5b-instruct-openvino-npu" on a
    machine with an NPU). The variant id is what has to go in the request body,
    which is why this lookup exists rather than passing the alias straight
    through. The "parent" field carries the alias, so match on either.
    """
    models = httpx.get(f"{endpoint}/v1/models", timeout=10.0).json()
    entries = models.get("data", [])
    if not entries:
        raise SystemExit(f"{endpoint}/v1/models returned no models. Download one with: foundry model download {alias}")
    for entry in entries:
        if entry.get("parent") == alias or entry["id"] == alias:
            return entry["id"]
    for entry in entries:
        if entry["id"].startswith(alias):
            return entry["id"]
    ids = [e["id"] for e in entries]
    print(f"[warn] No served model matched {alias!r}. Available: {', '.join(ids)}")
    return ids[0]


def load_model(model: str) -> None:
    """Make the model resident in the daemon.

    Cached is not the same as loaded. A model that is present on disk but not
    resident returns HTTP 400 "Model ... is not loaded" from
    /v1/chat/completions, which reads like a malformed request rather than a
    missing warm-up step.
    """
    print(f"[info] Loading {model} into the daemon")
    foundry(["model", "load", model], capture=False)


def smoke_test(endpoint: str, model: str) -> None:
    print(f"[info] Smoke testing {model} - the first call loads the model into memory")
    started = time.perf_counter()
    response = httpx.post(
        f"{endpoint}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Reply with the single word: ready"}],
            "max_tokens": 16,
            "temperature": 0.0,
        },
        timeout=300.0,
    )
    response.raise_for_status()
    body = response.json()
    elapsed = time.perf_counter() - started
    answer = body["choices"][0]["message"]["content"].strip()
    usage = body.get("usage") or {}
    print(f"[ok] {answer!r} in {elapsed:.1f}s")
    if usage:
        print(
            f"[ok] usage reported: {usage.get('prompt_tokens')} in / "
            f"{usage.get('completion_tokens')} out - APIM token policies will count exactly"
        )
    else:
        print("[warn] No usage object returned; APIM token counts will be estimates.")


def update_env(values: dict[str, str]) -> None:
    if not ENV_PATH.exists():
        print("[warn] .env not found - skipping write-back.")
        return
    text = ENV_PATH.read_text(encoding="utf-8")
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        text, n = pattern.subn(f"{key}={value}", text)
        if n == 0:
            text = text.rstrip("\n") + f"\n{key}={value}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    for key, value in values.items():
        print(f"[ok] {key}={value} written to .env")


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Foundry Local for Option 4.")
    parser.add_argument("--model", help="Catalogue alias, e.g. qwen2.5-0.5b. Defaults to FOUNDRY_LOCAL_MODEL.")
    parser.add_argument("--port", type=int, help=f"Port to pin the daemon to. Default {DEFAULT_PORT}.")
    parser.add_argument("--status", action="store_true", help="Report state and exit without changing anything.")
    parser.add_argument("--stop", action="store_true", help="Stop the daemon.")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable output.")
    args = parser.parse_args()

    settings = load_settings()
    option = get_option(4)
    alias = args.model or settings.model_for(option)
    port = args.port or int(settings.raw.get("FOUNDRY_LOCAL_PORT", DEFAULT_PORT))

    if args.stop:
        foundry(["server", "stop"], capture=False, check=False)
        print("[ok] Foundry Local stopped")
        return 0

    if args.status:
        endpoint = running_endpoint()
        state = {"running": bool(endpoint), "endpoint": endpoint, "model": alias}
        if endpoint:
            try:
                state["served"] = [m["id"] for m in httpx.get(f"{endpoint}/v1/models", timeout=5).json()["data"]]
            except Exception as exc:  # noqa: BLE001
                state["error"] = str(exc)
        print(json.dumps(state, indent=2) if args.json else state)
        return 0 if endpoint else 1

    print("Option 4 - Foundry Local")
    print(f"  model alias : {alias}")
    print(f"  port        : {port}\n")

    ensure_model(alias)
    endpoint = start_server(port)
    served = resolve_served_model(endpoint, alias)
    if served != alias:
        print(f"[info] Alias {alias!r} is served as {served!r} on this hardware")
    load_model(served)
    smoke_test(endpoint, served)

    update_env({
        "FOUNDRY_LOCAL_MODEL": served,
        "FOUNDRY_LOCAL_PORT": str(port),
        "FOUNDRY_LOCAL_ENDPOINT": endpoint,
    })

    print("\nNext:")
    print("  uv run python 04-foundry-local/tunnel.py     # publish this port to APIM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
