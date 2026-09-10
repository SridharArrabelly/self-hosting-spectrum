"""Deploy the self-hosting-spectrum infrastructure and write the outputs back to .env.

    uv run python infra/scripts/deploy.py
    uv run python infra/scripts/deploy.py --what-if     # preview, change nothing
    uv run python infra/scripts/deploy.py --skip-vm     # gateway + Foundry only

Why this exists rather than a bare 'az deployment group create':

  * main.bicepparam reads its values from environment variables so that no
    secrets live in a committed file. This script loads .env and exports them.
  * The deployment emits values later phases need (gateway URL, Foundry project
    endpoint, VM backend URL). Copying those by hand is where demos break, so
    they are written straight back into .env.
  * The APIM subscription key is not a deployment output at all - it has to be
    fetched from the management API afterwards.

APIM on the Developer SKU takes 30-45 minutes on first creation. That is normal.
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

from shared.config import REPO_ROOT, load_settings  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"
BICEP_FILE = REPO_ROOT / "infra" / "main.bicep"
BICEP_PARAMS = REPO_ROOT / "infra" / "main.bicepparam"
DEFAULT_KEY_PATH = Path.home() / ".ssh" / "shs_spectrum"

# Deployment output name -> .env key
OUTPUT_TO_ENV = {
    "apimGatewayUrl": "APIM_GATEWAY_URL",
    "foundryEndpoint": "FOUNDRY_ENDPOINT",
    "foundryProjectEndpoint": "FOUNDRY_PROJECT_ENDPOINT",
    "vmBackendUrl": "VM_BACKEND_URL",
    "fireworksDeploymentName": "FIREWORKS_DEPLOYMENT_NAME",
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
    # Streamed: the deployment itself, so progress is visible.
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        raise SystemExit(f"\naz {' '.join(args)} failed with exit code {proc.returncode}.")
    return None


def ensure_ssh_key() -> str:
    """Return an SSH public key, generating one if the user has not supplied one."""
    existing = os.environ.get("VM_ADMIN_PUBLIC_KEY", "").strip()
    if existing:
        return existing

    pub = DEFAULT_KEY_PATH.with_suffix(".pub")
    if not pub.exists():
        DEFAULT_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        print(f"[info] No VM_ADMIN_PUBLIC_KEY set - generating {DEFAULT_KEY_PATH}")
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(DEFAULT_KEY_PATH),
             "-N", "", "-C", "self-hosting-spectrum"],
            check=True, capture_output=True, text=True,
        )
    return pub.read_text(encoding="utf-8").strip()


def update_env_file(values: dict[str, str]) -> None:
    """Rewrite matching KEY= lines in .env, preserving comments and order."""
    if not ENV_PATH.exists():
        print("[warn] .env not found - skipping write-back.")
        return

    text = ENV_PATH.read_text(encoding="utf-8")
    for key, value in values.items():
        pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
        line = f"{key}={value}"
        text, n = pattern.subn(line, text)
        if n == 0:
            text = text.rstrip("\n") + f"\n{line}\n"
    ENV_PATH.write_text(text, encoding="utf-8")
    print(f"\n[ok] Wrote {len(values)} value(s) into {ENV_PATH.name}:")
    for key, value in values.items():
        shown = value if len(value) < 60 else value[:57] + "..."
        print(f"       {key}={shown}")


def fetch_apim_subscription_key(resource_group: str, apim_name: str) -> str | None:
    """The built-in 'master' subscription key. Not available as a Bicep output."""
    keys = run_az([
        "rest", "--method", "post",
        "--url",
        f"https://management.azure.com/subscriptions/{os.environ['AZURE_SUBSCRIPTION_ID']}"
        f"/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{apim_name}"
        f"/subscriptions/master/listSecrets?api-version=2024-05-01",
    ])
    if isinstance(keys, dict):
        return keys.get("primaryKey")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy self-hosting-spectrum infrastructure.")
    parser.add_argument("--what-if", action="store_true", help="Preview changes without applying.")
    parser.add_argument("--skip-vm", action="store_true", help="Do not deploy the Option 3 VM.")
    parser.add_argument("--skip-fireworks", action="store_true", help="Do not deploy Option 2.")
    parser.add_argument("--name", default="spectrum-infra", help="Deployment name.")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.subscription_id:
        raise SystemExit("AZURE_SUBSCRIPTION_ID is not set. Copy .env.example to .env first.")

    # main.bicepparam reads these via readEnvironmentVariable().
    os.environ["DEPLOY_VM"] = "false" if args.skip_vm else "true"
    os.environ["DEPLOY_FIREWORKS"] = "false" if args.skip_fireworks else "true"
    if not args.skip_vm:
        os.environ["VM_ADMIN_PUBLIC_KEY"] = ensure_ssh_key()

    print("self-hosting-spectrum deployment")
    print(f"  subscription   : {settings.subscription_id}")
    print(f"  resource group : {settings.resource_group}")
    print(f"  region         : {settings.location}")
    print(f"  APIM SKU       : {os.environ.get('APIM_SKU', 'Developer')}")

    run_az(["account", "set", "--subscription", settings.subscription_id])

    print(f"\n[1/3] Resource group {settings.resource_group}")
    run_az([
        "group", "create",
        "-n", settings.resource_group,
        "-l", settings.location,
        "--tags", "project=self-hosting-spectrum",
        "-o", "none",
    ])

    verb = "what-if" if args.what_if else "create"
    print(f"\n[2/3] Deployment ({verb})")
    if not args.what_if:
        print("      APIM on Developer takes 30-45 minutes on first creation.")

    run_az([
        "deployment", "group", verb,
        "-g", settings.resource_group,
        "-n", args.name,
        "-f", str(BICEP_FILE),
        "-p", str(BICEP_PARAMS),
    ], capture=False)

    if args.what_if:
        return 0

    print("\n[3/3] Collecting outputs")
    result = run_az([
        "deployment", "group", "show",
        "-g", settings.resource_group,
        "-n", args.name,
        "--query", "properties.outputs",
        "-o", "json",
    ])
    outputs = result if isinstance(result, dict) else {}

    env_updates: dict[str, str] = {}
    for out_key, env_key in OUTPUT_TO_ENV.items():
        value = (outputs.get(out_key) or {}).get("value")
        if value:
            env_updates[env_key] = str(value)

    apim_name = (outputs.get("apimName") or {}).get("value")
    if apim_name:
        key = fetch_apim_subscription_key(settings.resource_group, apim_name)
        if key:
            env_updates["APIM_SUBSCRIPTION_KEY"] = key
        else:
            print("[warn] Could not read the APIM subscription key - set it manually.")

    update_env_file(env_updates)

    print("\nNext:")
    print("  uv run python infra/scripts/deploy_apis.py     # publish the 4 routes")
    print("  uv run python shared/client.py --option 2      # cheapest end-to-end check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
