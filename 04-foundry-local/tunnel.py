"""Publish the local Foundry Local port to APIM through a Dev Tunnel.

    uv run python 04-foundry-local/tunnel.py            # create, host, wire up APIM
    uv run python 04-foundry-local/tunnel.py --url-only # print the URL and exit
    uv run python 04-foundry-local/tunnel.py --delete

The problem this solves: APIM runs in Azure, Foundry Local runs on this
machine at 127.0.0.1. The gateway cannot route to a loopback address on a
laptop behind NAT.

A Dev Tunnel fixes that from the inside out. The CLI dials *out* to a Microsoft
relay and gets back a stable HTTPS URL that forwards to the local port. No
inbound firewall rule, no public IP, no VPN. APIM treats the URL as an
ordinary backend and none of its policies change.

The alternative - the APIM self-hosted gateway - runs the APIM data plane as a
container on this machine so inference traffic never leaves it at all. That is
the right answer when data residency is the requirement rather than the demo;
it needs Docker and the Developer or Premium SKU. See the README.

Two things worth knowing:

  * The tunnel is created with a fixed id so the URL survives restarts.
    Anonymous tunnels get a new hostname every time, which means rewiring APIM
    every session.
  * Dev Tunnels serve an anti-phishing HTML interstitial to anything that looks
    like a browser. The APIM policy sends X-Tunnel-Skip-AntiPhishing-Page, so
    clients never see it - but a raw curl against the tunnel URL will.
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

from shared.config import REPO_ROOT, load_settings  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"
TUNNEL_ID = "shs-foundry-local"
DEFAULT_PORT = 39839
SKIP_INTERSTITIAL = {"X-Tunnel-Skip-AntiPhishing-Page": "true"}


def devtunnel_exe() -> str:
    exe = shutil.which("devtunnel")
    if not exe:
        raise SystemExit(
            "Dev Tunnels CLI is not installed.\n"
            "  winget install Microsoft.devtunnel\n"
            "Then run: devtunnel user login"
        )
    return exe


def devtunnel(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run([devtunnel_exe(), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise SystemExit(
            f"devtunnel {' '.join(args)} failed:\n{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def ensure_logged_in() -> None:
    proc = devtunnel(["user", "show"], check=False)
    if proc.returncode != 0 or "Logged in" not in proc.stdout:
        raise SystemExit(
            "Not signed in to Dev Tunnels.\n"
            "  devtunnel user login\n"
            "Use the same tenant as the rest of the deployment."
        )
    print(f"[ok] {proc.stdout.strip().splitlines()[0]}")


def ensure_tunnel(port: int, anonymous: bool) -> None:
    existing = devtunnel(["show", TUNNEL_ID], check=False)
    if existing.returncode != 0:
        print(f"[info] Creating persistent tunnel {TUNNEL_ID}")
        create = ["create", TUNNEL_ID, "--description", "self-hosting-spectrum Option 4"]
        if anonymous:
            create.append("--allow-anonymous")
        devtunnel(create)
    else:
        print(f"[ok] Tunnel {TUNNEL_ID} already exists")
        if anonymous:
            devtunnel(["access", "create", TUNNEL_ID, "--anonymous"], check=False)

    ports = devtunnel(["port", "list", TUNNEL_ID], check=False)
    if str(port) not in ports.stdout:
        print(f"[info] Adding port {port} to the tunnel")
        devtunnel(["port", "create", TUNNEL_ID, "-p", str(port), "--protocol", "http"])
    else:
        print(f"[ok] Port {port} already published")


def tunnel_url(port: int) -> str:
    proc = devtunnel(["show", TUNNEL_ID, "--json"])
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        match = re.search(r"https://[a-z0-9-]+\.[a-z0-9-]+\.devtunnels\.ms[^\s]*", proc.stdout)
        if not match:
            raise SystemExit(f"Could not find the tunnel URL in:\n{proc.stdout}") from None
        return match.group(0).rstrip("/")

    tunnel = data.get("tunnel", data)
    for entry in tunnel.get("ports", []):
        if int(entry.get("portNumber", 0)) == port:
            url = entry.get("portUri") or entry.get("portForwardingUris", [None])[0]
            if url:
                return url.rstrip("/")

    # Dev Tunnels URLs are deterministic, and the cluster is the suffix of the
    # tunnel id: "shs-foundry-local.inc1" is hosted in cluster "inc1" and
    # published at https://shs-foundry-local-39839.inc1.devtunnels.ms.
    # `devtunnel show --json` does not return the URL itself, so derive it.
    raw_id = tunnel.get("tunnelId") or TUNNEL_ID
    cluster = tunnel.get("clusterId")
    name = raw_id
    if "." in raw_id:
        name, _, suffix = raw_id.partition(".")
        cluster = cluster or suffix
    if cluster:
        return f"https://{name}-{port}.{cluster}.devtunnels.ms"
    raise SystemExit(f"Could not determine the tunnel URL from:\n{proc.stdout}")


def start_host() -> subprocess.Popen[str]:
    """Host the tunnel. This process must stay alive for the route to work."""
    print(f"[info] Hosting {TUNNEL_ID} - leave this running while you demo Option 4")
    return subprocess.Popen(
        [devtunnel_exe(), "host", TUNNEL_ID],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def verify(url: str, timeout: float = 60.0) -> bool:
    """Confirm the tunnel forwards to Foundry Local and returns JSON, not HTML."""
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            response = httpx.get(f"{url}/v1/models", headers=SKIP_INTERSTITIAL, timeout=10.0)
            if response.status_code == 200 and response.headers.get("content-type", "").startswith(
                "application/json"
            ):
                ids = [m["id"] for m in response.json().get("data", [])]
                print(f"[ok] Tunnel serving Foundry Local: {', '.join(ids) or '(no models loaded)'}")
                return True
            last = f"HTTP {response.status_code}, content-type {response.headers.get('content-type')}"
            if "text/html" in response.headers.get("content-type", ""):
                last += " - anti-phishing interstitial; the APIM policy sends the skip header"
        except Exception as exc:  # noqa: BLE001 - tunnel may still be connecting
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    print(f"[warn] Tunnel did not serve JSON within {timeout:.0f}s. Last: {last}")
    return False


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


def push_to_apim(url: str) -> None:
    """Repoint the APIM named value. No redeployment, no policy edit."""
    script = REPO_ROOT / "infra" / "scripts" / "deploy_apis.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--set-backend", "foundry-local", f"{url}/v1"],
        text=True,
    )
    if proc.returncode != 0:
        print("[warn] Could not update the APIM named value. Run this manually:")
        print(f"  uv run python infra/scripts/deploy_apis.py --set-backend foundry-local {url}/v1")


def main() -> int:
    parser = argparse.ArgumentParser(description="Expose Foundry Local to APIM via a Dev Tunnel.")
    parser.add_argument("--port", type=int, help=f"Local port to forward. Default {DEFAULT_PORT}.")
    parser.add_argument("--url-only", action="store_true", help="Print the tunnel URL and exit.")
    parser.add_argument("--no-host", action="store_true", help="Set everything up but do not host.")
    parser.add_argument("--delete", action="store_true", help="Delete the tunnel and exit.")
    parser.add_argument(
        "--authenticated",
        action="store_true",
        help="Require a Dev Tunnels login to reach the URL. Default is anonymous - "
             "the URL is unguessable and APIM is the real auth boundary.",
    )
    args = parser.parse_args()

    settings = load_settings()
    port = args.port or int(settings.raw.get("FOUNDRY_LOCAL_PORT", DEFAULT_PORT))

    if args.delete:
        devtunnel(["delete", TUNNEL_ID, "--force"], check=False)
        print(f"[ok] Tunnel {TUNNEL_ID} deleted")
        return 0

    ensure_logged_in()

    if args.url_only:
        print(tunnel_url(port))
        return 0

    ensure_tunnel(port, anonymous=not args.authenticated)
    url = tunnel_url(port)
    print(f"[ok] Tunnel URL: {url}")

    update_env({"FOUNDRY_LOCAL_TUNNEL_URL": url, "FOUNDRY_LOCAL_BACKEND_URL": f"{url}/v1"})
    push_to_apim(url)

    if args.no_host:
        print(f"\nHost it when you are ready:\n  devtunnel host {TUNNEL_ID}")
        return 0

    process = start_host()
    try:
        verify(url)
        print("\nOption 4 is live. In another terminal:")
        print("  uv run python shared/client.py --option 4")
        print("\nCtrl+C here stops the tunnel and takes the route offline.")
        process.wait()
    except KeyboardInterrupt:
        print("\n[info] Stopping tunnel")
    finally:
        process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
