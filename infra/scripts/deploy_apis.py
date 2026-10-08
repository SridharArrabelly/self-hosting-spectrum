"""Publish the four APIM routes and write the gateway key back to .env.

    uv run python infra/scripts/deploy_apis.py
    uv run python infra/scripts/deploy_apis.py --what-if
    uv run python infra/scripts/deploy_apis.py --tokens-per-minute 5000

Run this after infra/scripts/deploy.py, and again whenever:

  * a policy in infra/policies/ changes,
  * Option 1's managed compute deployment is created,
  * Option 3's VM gets a new address,
  * Option 4's Dev Tunnel URL rotates.

It is a seconds-long deployment, unlike main.bicep. Backend URLs live in APIM
named values, so a route can also be repointed without redeploying at all -
see --set-backend.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import OPTIONS, REPO_ROOT, load_settings  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"
BICEP_FILE = REPO_ROOT / "infra" / "apis.bicep"
BICEP_PARAMS = REPO_ROOT / "infra" / "apis.bicepparam"

# APIM named value <- .env key, for --set-backend.
BACKEND_NAMED_VALUES = {
    "managed-compute": "mc-backend-url",
    "fireworks": "fireworks-backend-url",
    "azure-vm": "vm-backend-url",
    "foundry-local": "foundry-local-backend-url",
}


def az_exe() -> str:
    exe = shutil.which("az")
    if not exe:
        raise SystemExit("Azure CLI not found on PATH.")
    return exe


def run_az(args: list[str], *, capture: bool = True) -> object:
    cmd = [az_exe(), *args]
    if capture:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise SystemExit(f"\naz {' '.join(args)} failed:\n{proc.stderr.strip()}")
        out = proc.stdout.strip()
        try:
            return json.loads(out) if out else None
        except json.JSONDecodeError:
            return out
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(f"\naz {' '.join(args)} failed with exit code {proc.returncode}.")
    return None


def update_env_file(values: dict[str, str]) -> None:
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
    for key in values:
        print(f"[ok] {key} written to .env")


def set_named_value(subscription_id: str, rg: str, apim_name: str, name: str, value: str) -> None:
    """Repoint a backend without redeploying anything."""
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{rg}/providers/Microsoft.ApiManagement/service/{apim_name}"
        f"/namedValues/{name}?api-version=2024-05-01"
    )
    body = json.dumps({"properties": {"displayName": name, "value": value}})
    run_az(["rest", "--method", "put", "--url", url, "--body", body, "-o", "none"])
    print(f"[ok] named value {name} -> {value}")


def fetch_gateway_key(subscription_id: str, rg: str, apim_name: str) -> str | None:
    """The 'spectrum-demo' subscription key, which is what the policy checks."""
    url = (
        f"https://management.azure.com/subscriptions/{subscription_id}"
        f"/resourceGroups/{rg}/providers/Microsoft.ApiManagement/service/{apim_name}"
        f"/subscriptions/spectrum-demo/listSecrets?api-version=2024-05-01"
    )
    result = run_az(["rest", "--method", "post", "--url", url])
    return result.get("primaryKey") if isinstance(result, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish the four APIM routes.")
    parser.add_argument("--what-if", action="store_true", help="Preview changes without applying.")
    parser.add_argument("--tokens-per-minute", type=int, help="Override the per-route token budget.")
    parser.add_argument(
        "--audience",
        choices=["https://cognitiveservices.azure.com", "https://ai.azure.com"],
        help="Entra audience APIM requests when calling Foundry. Flip this if Option 1 or 2 returns 401.",
    )
    parser.add_argument(
        "--set-backend",
        nargs=2,
        metavar=("ROUTE", "URL"),
        help="Repoint one route's backend named value and exit. ROUTE is e.g. foundry-local.",
    )
    parser.add_argument("--name", default="spectrum-apis", help="Deployment name.")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.subscription_id:
        raise SystemExit("AZURE_SUBSCRIPTION_ID is not set. Copy .env.example to .env first.")

    apim_name = f"apim-spectrum-{settings.name_suffix}"
    run_az(["account", "set", "--subscription", settings.subscription_id])

    if args.set_backend:
        route, url = args.set_backend
        named_value = BACKEND_NAMED_VALUES.get(route)
        if not named_value:
            valid = ", ".join(BACKEND_NAMED_VALUES)
            raise SystemExit(f"Unknown route {route!r}. Valid routes: {valid}")
        set_named_value(settings.subscription_id, settings.resource_group, apim_name, named_value, url)
        return 0

    if args.tokens_per_minute:
        os.environ["APIM_TOKENS_PER_MINUTE"] = str(args.tokens_per_minute)
    if args.audience:
        os.environ["FOUNDRY_AAD_AUDIENCE"] = args.audience

    print(f"Publishing routes onto {apim_name}")
    print(f"  auth mode     : {settings.gateway_auth_mode}")
    print(f"  tokens/minute : {os.environ.get('APIM_TOKENS_PER_MINUTE', '20000')} per route")
    print(f"  Foundry aud   : {os.environ.get('FOUNDRY_AAD_AUDIENCE', 'https://cognitiveservices.azure.com')}")
    for label, env_key in (("Option 3 VM", "VM_BACKEND_URL"), ("Option 4 local", "FOUNDRY_LOCAL_BACKEND_URL")):
        value = os.environ.get(env_key, "")
        print(f"  {label:<14}: {value or '(not wired yet - route will be stubbed)'}")

    verb = "what-if" if args.what_if else "create"
    run_az([
        "deployment", "group", verb,
        "-g", settings.resource_group,
        "-n", args.name,
        "-f", str(BICEP_FILE),
        "-p", str(BICEP_PARAMS),
    ], capture=False)

    if args.what_if:
        return 0

    if settings.gateway_auth_mode.strip().lower() in {"key", "both"}:
        key = fetch_gateway_key(settings.subscription_id, settings.resource_group, apim_name)
        if key:
            update_env_file({"APIM_SUBSCRIPTION_KEY": key})
        else:
            print("[warn] Could not read the spectrum-demo subscription key.")

    gateway = settings.apim_gateway_url or f"https://{apim_name}.azure-api.net"
    print("\nRoutes now live:")
    for number, option in sorted(OPTIONS.items()):
        print(f"  {number}. {option.title:<32} {gateway}{option.route}/chat/completions")

    print("\nNext:")
    if settings.uses_entra:
        print("  uv run python infra/scripts/setup_entra.py    # allow-list the callers")
    print("  uv run python shared/client.py --option 2      # cheapest end-to-end check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
