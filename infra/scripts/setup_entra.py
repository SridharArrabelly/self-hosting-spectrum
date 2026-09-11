"""Turn the gateway keyless: work out who is allowed through, and tell APIM.

    uv run python infra/scripts/setup_entra.py
    uv run python infra/scripts/setup_entra.py --show
    uv run python infra/scripts/setup_entra.py --add-client-id <app-id>

Run this once after infra/scripts/deploy.py, then again only if the Foundry
project is recreated.

Why a script rather than Bicep
------------------------------
The gateway allow-list is a list of Entra *application* IDs, and neither of the
two entries is knowable at template-authoring time:

  * Azure CLI has a fixed, well-known application ID, but it is a magic constant
    that deserves to be named and explained rather than pasted into a template.
  * The Foundry project's managed identity gets a fresh application ID every
    time the project is created, and ARM only ever hands back its *object* ID.
    Converting one to the other is a directory lookup, which Bicep cannot do.

Both then land in a single APIM named value, so changing who may call the
gateway never requires redeploying a policy.

Object ID vs application ID
---------------------------
These are different GUIDs for the same identity and mixing them up produces a
403 that looks exactly like a broken policy. ARM outputs the object ID (the
service principal's identity in the directory). The token's `appid` claim
carries the application ID. `az ad sp show` is the bridge.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import REPO_ROOT, load_settings  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"

# Azure CLI's first-party application ID. Identical in every tenant, published
# by Microsoft, and not a secret. When a human runs `az login` and client.py
# asks for a token, this is the value that arrives in the appid claim - so
# without it on the allow-list, developers get a 403 from their own gateway.
AZURE_CLI_APP_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"

NAMED_VALUE = "spectrum-entra-client-ids"


def az_exe() -> str:
    exe = shutil.which("az")
    if not exe:
        raise SystemExit("Azure CLI not found on PATH.")
    return exe


def run_az(args: list[str], *, allow_fail: bool = False) -> object:
    proc = subprocess.run([az_exe(), *args], capture_output=True, text=True)
    if proc.returncode != 0:
        if allow_fail:
            return None
        raise SystemExit(f"\naz {' '.join(args)} failed:\n{proc.stderr.strip()}")
    out = proc.stdout.strip()
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return out


def project_principal_id(settings, account: str, project: str) -> str | None:
    """Object ID of the Foundry project's system-assigned managed identity."""
    result = run_az([
        "rest", "--method", "get",
        "--url",
        f"https://management.azure.com/subscriptions/{settings.subscription_id}"
        f"/resourceGroups/{settings.resource_group}"
        f"/providers/Microsoft.CognitiveServices/accounts/{account}"
        f"/projects/{project}?api-version=2025-06-01",
    ], allow_fail=True)

    if not isinstance(result, dict):
        return None
    return (result.get("identity") or {}).get("principalId")


def app_id_for_principal(principal_id: str) -> str | None:
    """Object ID -> application (client) ID, via the directory."""
    result = run_az(["ad", "sp", "show", "--id", principal_id, "-o", "json"], allow_fail=True)
    return result.get("appId") if isinstance(result, dict) else None


def read_named_value(settings, apim_name: str) -> str:
    result = run_az([
        "rest", "--method", "post",
        "--url",
        f"https://management.azure.com/subscriptions/{settings.subscription_id}"
        f"/resourceGroups/{settings.resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{apim_name}"
        f"/namedValues/{NAMED_VALUE}/listValue?api-version=2024-05-01",
    ], allow_fail=True)
    return (result.get("value") or "").strip() if isinstance(result, dict) else ""


def write_named_value(settings, apim_name: str, value: str) -> None:
    """Push the allow-list straight into APIM.

    Deliberately not a template deployment. The list changes when identities
    change, which has nothing to do with the policy, and a named value update
    takes effect on the next request with no redeploy and no downtime.
    """
    url = (
        f"https://management.azure.com/subscriptions/{settings.subscription_id}"
        f"/resourceGroups/{settings.resource_group}"
        f"/providers/Microsoft.ApiManagement/service/{apim_name}"
        f"/namedValues/{NAMED_VALUE}?api-version=2024-05-01"
    )
    body = json.dumps({
        "properties": {
            "displayName": NAMED_VALUE,
            "value": value,
            "tags": ["spectrum"],
        }
    })
    run_az(["rest", "--method", "put", "--url", url, "--body", body, "-o", "none"])


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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover the gateway's allowed Entra applications and publish the list to APIM."
    )
    parser.add_argument("--show", action="store_true",
                        help="Print the current allow-list and exit without changing anything.")
    parser.add_argument("--add-client-id", action="append", default=[], metavar="APP_ID",
                        help="Also allow this application ID. Repeatable.")
    parser.add_argument("--open", action="store_true",
                        help="Clear the allow-list, accepting any caller in the tenant with a "
                             "valid token for the audience.")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.subscription_id:
        raise SystemExit("AZURE_SUBSCRIPTION_ID is not set. Copy .env.example to .env first.")

    apim_name = f"apim-spectrum-{settings.name_suffix}"
    account = f"aif-spectrum-{settings.name_suffix}"
    project = settings.raw.get("FOUNDRY_PROJECT_NAME", "proj-spectrum")

    run_az(["account", "set", "--subscription", settings.subscription_id])

    if args.show:
        current = read_named_value(settings, apim_name)
        print(f"Gateway   : {settings.apim_gateway_url or apim_name}")
        print(f"Audience  : {settings.entra_audience}")
        print(f"Auth mode : {settings.gateway_auth_mode}")
        if current:
            print("Allowed application IDs:")
            for app_id in (x.strip() for x in current.split(",") if x.strip()):
                label = " (Azure CLI)" if app_id == AZURE_CLI_APP_ID else ""
                print(f"  {app_id}{label}")
        else:
            print("Allow-list: empty - any caller in the tenant with a valid token is accepted.")
        return 0

    if args.open:
        # The policy treats a blank list as "no allow-list", so a single space
        # is the way to express that: APIM rejects empty named values.
        write_named_value(settings, apim_name, " ")
        print(f"[ok] {NAMED_VALUE} cleared. Any tenant caller with a valid token is now accepted.")
        return 0

    print(f"Resolving callers for {apim_name}\n")

    allowed: list[str] = [AZURE_CLI_APP_ID]
    print(f"[ok] Azure CLI              {AZURE_CLI_APP_ID}")
    print("       covers a developer running client.py, agent.py, or benchmark.py")

    principal_id = project_principal_id(settings, account, project)
    if not principal_id:
        print(f"[warn] {account}/{project} has no managed identity yet.")
        print("       Redeploy infra/main.bicep, or add one in the portal under")
        print("       Foundry > your project > Identity, then re-run this script.")
    else:
        app_id = app_id_for_principal(principal_id)
        if not app_id:
            print(f"[warn] Could not resolve object ID {principal_id} to an application ID.")
            print("       Directory replication can lag a minute or two after the project is")
            print("       created. Wait, then re-run. Agents will 403 until this is on the list.")
        else:
            allowed.append(app_id)
            print(f"[ok] Foundry project MI     {app_id}")
            print(f"       object id {principal_id}")
            print("       covers agents calling the gateway through a BYOM connection")
            update_env_file({"FOUNDRY_PROJECT_MI_CLIENT_ID": app_id})

    for extra in args.add_client_id:
        if extra not in allowed:
            allowed.append(extra)
            print(f"[ok] Additional caller      {extra}")

    value = ",".join(allowed)
    write_named_value(settings, apim_name, value)
    print(f"\n[ok] {NAMED_VALUE} -> {value}")
    update_env_file({"ENTRA_ALLOWED_CLIENT_IDS": value})

    print("\nThe gateway now accepts Entra tokens only. No key is required anywhere.")
    print("\nNext:")
    print("  uv run python shared/register_connections.py   # re-register as managed identity")
    print("  uv run python shared/client.py --option 2      # cheapest end-to-end check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
