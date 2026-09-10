"""Create or delete the Option 1 Managed Compute deployment.

    uv run python 01-managed-compute/deploy_managed_compute.py
    uv run python 01-managed-compute/deploy_managed_compute.py --accelerator A100_80GB
    uv run python 01-managed-compute/deploy_managed_compute.py --delete     # STOP THE BILLING

WHY THIS IS PYTHON AND NOT BICEP
Managed Compute deployments are
Microsoft.CognitiveServices/accounts/managedComputeDeployments, and that type
has no ARM/Bicep representation yet. The only supported programmatic path is
the beta management SDK (azure-mgmt-cognitiveservices 15.0.0b2), which is why
this one resource sits outside infra/. Everything else in the repository is
Bicep.

WHAT IT COSTS
This is the expensive option, and the only one that bills for *existing*
rather than for *being used*. A single H100_80GB is roughly $8/hour - about
$190/day - whether you send one request or none. A100_80GB is roughly half
that. Deploy it last, demo it, then delete it. The --delete flag is the most
important line in this file.

WHAT MAKES IT INTERESTING
Unlike the old hub-based managed online endpoints, which spoke an AzureML
`input_data` envelope and needed a translation shim, the new Managed Compute
path is natively OpenAI-compatible: it is served from the account's /openai/v1
route. That is the whole reason a single unmodified APIM policy can front it
alongside a partner API, a VM, and a laptop.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError  # noqa: E402
from azure.identity import AzureCliCredential  # noqa: E402
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient  # noqa: E402
from azure.mgmt.cognitiveservices.models import (  # noqa: E402
    ManagedComputeDeployment,
    ManagedComputeDeploymentProperties,
    Sku,
)

from shared.config import REPO_ROOT, load_settings  # noqa: E402

ENV_PATH = REPO_ROOT / ".env"
DEFAULT_DEPLOYMENT_NAME = "managed-compute"
DEFAULT_MODEL = "azureml://registries/azureml/models/Phi-4-mini-instruct/versions/1"
ACCELERATOR_PREFERENCE = ["A100_80GB", "MI300_192GB", "H100_80GB", "H200_141GB"]

# Indicative on-demand rates, USD per accelerator-hour. Shown as a warning, not
# used for billing. Verify against the Azure pricing page before quoting these.
INDICATIVE_HOURLY = {
    "A100_80GB": 3.67,
    "H100_80GB": 7.91,
    "H200_141GB": 9.80,
    "MI300_192GB": 5.00,
}


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


def pick_accelerator(mgmt: CognitiveServicesManagementClient, location: str) -> str:
    """Cheapest accelerator that has both quota and free fleet capacity."""
    free: dict[str, int] = {}
    for usage in mgmt.managed_compute_usages_operation_group.list(location):
        data = usage.as_dict()
        name = (data.get("name") or {}).get("value", "").rsplit(".", 1)[-1]
        free[name] = int(data.get("limit") or 0) - int(data.get("currentValue") or 0)

    for accelerator in ACCELERATOR_PREFERENCE:
        if free.get(accelerator, 0) > 0:
            return accelerator
    raise SystemExit(
        f"No Managed Compute quota available in {location}.\n"
        "Run 01-managed-compute/list_templates.py to see the numbers, then request\n"
        "quota in the Foundry portal under Management centre > Quota."
    )


def show_deployment(deployment: ManagedComputeDeployment) -> dict:
    data = deployment.as_dict()
    props = data.get("properties", {})
    return {
        "name": data.get("name"),
        "state": props.get("provisioningState"),
        "model": props.get("model"),
        "template": props.get("deploymentTemplate"),
        "accelerator": props.get("acceleratorType"),
        "acceleratorsPerInstance": props.get("acceleratorsPerInstance"),
        "totalAccelerators": props.get("totalAccelerators"),
        "instances": (data.get("sku") or {}).get("capacity"),
        "routes": props.get("routes") or {},
        "details": props.get("provisioningDetails") or {},
    }


def delete(mgmt: CognitiveServicesManagementClient, rg: str, account: str, name: str) -> int:
    print(f"[info] Deleting managed compute deployment {name}")
    try:
        mgmt.managed_compute_deployments.begin_delete(rg, account, name).result()
    except ResourceNotFoundError:
        print(f"[ok] {name} does not exist - nothing to bill")
        return 0
    print(f"[ok] {name} deleted. GPU billing has stopped.")
    update_env({"MC_DEPLOYMENT_NAME": ""})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Deploy or delete the Option 1 Managed Compute endpoint.",
    )
    parser.add_argument("--name", help=f"Deployment name. Default {DEFAULT_DEPLOYMENT_NAME}.")
    parser.add_argument("--model", help="AzureML asset URI. Overrides MC_MODEL_URI.")
    parser.add_argument("--template", help="Pin a deployment template URI. Optional - the service picks a default.")
    parser.add_argument("--accelerator", choices=ACCELERATOR_PREFERENCE,
                        help="Accelerator family. Defaults to the cheapest with free quota.")
    parser.add_argument("--instances", type=int, default=1, help="Instance count (sku.capacity).")
    parser.add_argument("--delete", action="store_true", help="Delete the deployment and stop the billing.")
    parser.add_argument("--status", action="store_true", help="Show current state and exit.")
    parser.add_argument("--yes", action="store_true", help="Skip the cost confirmation prompt.")
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.subscription_id:
        raise SystemExit("AZURE_SUBSCRIPTION_ID is not set. Copy .env.example to .env first.")

    account = f"aif-spectrum-{settings.name_suffix}"
    name = args.name or settings.raw.get("MC_DEPLOYMENT_NAME") or DEFAULT_DEPLOYMENT_NAME
    mgmt = CognitiveServicesManagementClient(AzureCliCredential(), settings.subscription_id)

    if args.delete:
        return delete(mgmt, settings.resource_group, account, name)

    if args.status:
        try:
            current = mgmt.managed_compute_deployments.get(settings.resource_group, account, name)
        except ResourceNotFoundError:
            print(f"{name} does not exist.")
            return 1
        info = show_deployment(current)
        print(json.dumps(info, indent=2) if args.json else info)
        return 0

    model = args.model or settings.raw.get("MC_MODEL_URI") or DEFAULT_MODEL
    accelerator = args.accelerator or pick_accelerator(mgmt, settings.location)
    hourly = INDICATIVE_HOURLY.get(accelerator)

    print("Option 1 - Foundry Managed Compute")
    print(f"  account     : {account}")
    print(f"  deployment  : {name}")
    print(f"  model       : {model}")
    print(f"  accelerator : {accelerator}")
    print(f"  instances   : {args.instances}")
    if args.template:
        print(f"  template    : {args.template}")
    if hourly:
        print(f"\n  COST: about ${hourly:.2f} per accelerator-hour, roughly "
              f"${hourly * 24:.0f}/day, billed with zero traffic.")
        print("  Delete it as soon as the demo is done:")
        print("    uv run python 01-managed-compute/deploy_managed_compute.py --delete")

    if not args.yes:
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted. Nothing was created.")
            return 1

    properties = ManagedComputeDeploymentProperties(
        model=model,
        accelerator_type=accelerator,
    )
    if args.template:
        properties.deployment_template = args.template

    resource = ManagedComputeDeployment(
        properties=properties,
        # GlobalManagedCompute is the Foundry-hosted fleet. VmManagedCompute
        # would place the model on compute you own and needs compute_id.
        sku=Sku(name="GlobalManagedCompute", capacity=args.instances),
    )

    print("\n[info] Creating - GPU allocation and model download usually take 10-25 minutes")
    started = time.perf_counter()
    try:
        poller = mgmt.managed_compute_deployments.begin_create_or_update(
            settings.resource_group, account, name, resource
        )
        result = poller.result()
    except HttpResponseError as exc:
        print(f"\n[fail] {exc.status_code}: {exc.message}")
        print("\nCommon causes:")
        print("  * Model asset URI wrong - copy it from the model card in the Foundry portal.")
        print("  * No template for this model on this accelerator - try --accelerator H100_80GB.")
        print("  * Quota or capacity exhausted - run 01-managed-compute/list_templates.py.")
        return 1

    info = show_deployment(result)
    elapsed = (time.perf_counter() - started) / 60
    print(f"\n[ok] {info['state']} after {elapsed:.1f} minutes")
    print(f"     template     : {info['template']}")
    print(f"     GPUs         : {info['acceleratorsPerInstance']}/instance, "
          f"{info['totalAccelerators']} total")

    route = (info["routes"] or {}).get("chatCompletionsScoringPath")
    if route:
        print(f"     inference    : {settings.foundry_endpoint.rstrip('/')}/{route.lstrip('/')}")

    update_env({
        "MC_DEPLOYMENT_NAME": name,
        "MC_MODEL_URI": model,
        "MC_ACCELERATOR": accelerator,
    })

    print("\nNext:")
    print("  uv run python shared/client.py --option 1")
    print("\nWhen you are done, stop the billing:")
    print("  uv run python 01-managed-compute/deploy_managed_compute.py --delete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
