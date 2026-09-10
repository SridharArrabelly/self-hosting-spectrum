"""What can Managed Compute actually run right now, and on what.

    uv run python 01-managed-compute/list_templates.py
    uv run python 01-managed-compute/list_templates.py --model azureml://registries/azureml/models/Phi-4-mini-instruct/versions/1
    uv run python 01-managed-compute/list_templates.py --json

Run this before deploy_managed_compute.py. It answers the three questions that
decide whether Option 1 is viable today:

  1. Quota. Managed Compute has its own quota namespace, granted per
     accelerator family per region. It is completely separate from VM quota and
     from Azure ML quota - a subscription with zero GPU vCPU can still have
     Managed Compute quota, and usually does. This is the single most
     misunderstood thing about the product.
  2. Fleet capacity. Quota is permission; capacity is whether the GPUs are free
     right now. Both have to be non-zero.
  3. Whether a specific model asset can land on an accelerator you have.

Deployment templates (the vLLM/SGLang/TRT-LLM recipe and its context length)
are not enumerable through the management SDK. They do not have to be: the
template is optional at creation, and the service picks the default for the
model and accelerator. Pass --template only to pin a specific recipe, e.g. a
256K-context variant.

Cost note: A100_80GB is roughly half the hourly rate of H100_80GB and is
plenty for a small model, so this script sorts A100 first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.core.exceptions import HttpResponseError  # noqa: E402
from azure.identity import AzureCliCredential  # noqa: E402
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient  # noqa: E402

from shared.config import load_settings  # noqa: E402

# Cheapest first. Managed Compute bills per accelerator-hour whether or not a
# request is ever sent, so the accelerator choice is the cost decision.
ACCELERATOR_PREFERENCE = ["A100_80GB", "MI300_192GB", "H100_80GB", "H200_141GB"]

# Small, openly licensed models that suit a portability demo. These are asset
# URIs in public AzureML registries, not Foundry deployment names.
CANDIDATE_MODELS = [
    "azureml://registries/azureml/models/Phi-4-mini-instruct/versions/1",
    "azureml://registries/azureml/models/Phi-3.5-mini-instruct/versions/1",
    "azureml://registries/azureml-meta/models/Llama-3.2-3B-Instruct/versions/1",
    "azureml://registries/azureml/models/Mistral-7B-Instruct-v0.2/versions/1",
]


def client(subscription_id: str) -> CognitiveServicesManagementClient:
    return CognitiveServicesManagementClient(AzureCliCredential(), subscription_id)


def quota(mgmt: CognitiveServicesManagementClient, location: str) -> list[dict]:
    rows = []
    for usage in mgmt.managed_compute_usages_operation_group.list(location):
        data = usage.as_dict()
        name = (data.get("name") or {}).get("value", "")
        rows.append({
            "accelerator": name.rsplit(".", 1)[-1],
            "scope": data.get("offerScope", ""),
            "used": int(data.get("currentValue") or 0),
            "limit": int(data.get("limit") or 0),
        })
    rows.sort(key=lambda r: (ACCELERATOR_PREFERENCE.index(r["accelerator"])
                             if r["accelerator"] in ACCELERATOR_PREFERENCE else 99))
    return rows


def capacity(mgmt: CognitiveServicesManagementClient, model: str) -> list[dict]:
    rows = []
    for item in mgmt.managed_compute_capacities.list(offer=model):
        props = item.as_dict().get("properties", {})
        sizes = {
            int(s["modelInstanceAcceleratorCount"]): int(s["largestDeploymentCapacity"])
            for s in props.get("deploymentSizeCapacities", [])
        }
        rows.append({
            "accelerator": props.get("acceleratorType", ""),
            "available": int(props.get("availableAccelerators") or 0),
            "sizes": sizes,
        })
    rows.sort(key=lambda r: (ACCELERATOR_PREFERENCE.index(r["accelerator"])
                             if r["accelerator"] in ACCELERATOR_PREFERENCE else 99))
    return rows


def existing(mgmt: CognitiveServicesManagementClient, rg: str, account: str) -> list[dict]:
    try:
        deployments = list(mgmt.managed_compute_deployments.list(rg, account))
    except HttpResponseError as exc:
        if exc.status_code == 404:
            return []
        raise
    rows = []
    for dep in deployments:
        data = dep.as_dict()
        props = data.get("properties", {})
        rows.append({
            "name": data.get("name"),
            "model": props.get("model"),
            "accelerator": props.get("acceleratorType"),
            "instances": (data.get("sku") or {}).get("capacity"),
            "totalAccelerators": props.get("totalAccelerators"),
            "state": props.get("provisioningState"),
            "route": (props.get("routes") or {}).get("chatCompletionsScoringPath"),
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Inspect Managed Compute quota, capacity and deployments.")
    parser.add_argument("--model", help="AzureML asset URI to check capacity for.")
    parser.add_argument("--all-candidates", action="store_true", help="Check every built-in candidate model.")
    parser.add_argument("--location", help="Region to query. Defaults to AZURE_LOCATION.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of tables.")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.subscription_id:
        raise SystemExit("AZURE_SUBSCRIPTION_ID is not set. Copy .env.example to .env first.")
    location = args.location or settings.location
    account = f"aif-spectrum-{settings.name_suffix}"

    mgmt = client(settings.subscription_id)
    quota_rows = quota(mgmt, location)
    deployment_rows = existing(mgmt, settings.resource_group, account)

    models = CANDIDATE_MODELS if args.all_candidates else [
        args.model or settings.raw.get("MC_MODEL_URI") or CANDIDATE_MODELS[0]
    ]
    capacity_rows = {}
    for model in models:
        try:
            capacity_rows[model] = capacity(mgmt, model)
        except HttpResponseError as exc:
            capacity_rows[model] = {"error": f"HTTP {exc.status_code}: {exc.message.splitlines()[0]}"}

    if args.json:
        print(json.dumps({
            "location": location,
            "quota": quota_rows,
            "capacity": capacity_rows,
            "deployments": deployment_rows,
        }, indent=2))
        return 0

    print(f"Managed Compute in {location}\n")

    print("Quota - your permission to allocate accelerators")
    print("  (separate namespace from VM and Azure ML quota; zero GPU vCPU quota is irrelevant here)")
    print(f"\n  {'Accelerator':<16}{'Scope':<10}{'Used':>6}{'Limit':>7}   Free")
    print("  " + "-" * 50)
    for row in quota_rows:
        free = row["limit"] - row["used"]
        print(f"  {row['accelerator']:<16}{row['scope']:<10}{row['used']:>6}{row['limit']:>7}{free:>7}")
    if all(r["limit"] == 0 for r in quota_rows):
        print("\n  No Managed Compute quota in this region. Request it in the Foundry portal")
        print("  under Management centre > Quota, or use a different region.")

    for model, rows in capacity_rows.items():
        print(f"\nFleet capacity for {model}")
        if isinstance(rows, dict):
            print(f"  {rows['error']}")
            print("  The asset URI is probably wrong. Copy it from the model card in the Foundry portal.")
            continue
        if not rows:
            print("  No accelerator has free capacity for this model right now.")
            continue
        print(f"\n  {'Accelerator':<16}{'Free now':>10}   Largest single deployment by GPUs/instance")
        print("  " + "-" * 72)
        for row in rows:
            sizes = "  ".join(f"{k}x:{v}" for k, v in sorted(row["sizes"].items()))
            print(f"  {row['accelerator']:<16}{row['available']:>10}   {sizes}")

    print(f"\nExisting deployments on {account}")
    if not deployment_rows:
        print("  none - nothing is burning GPU hours")
    else:
        for row in deployment_rows:
            print(f"  {row['name']} [{row['state']}] {row['accelerator']} "
                  f"x{row['instances']} instance(s) = {row['totalAccelerators']} GPU(s)")
            print(f"    model : {row['model']}")
            print(f"    route : {row['route']}")
        print("\n  These bill per accelerator-hour with zero traffic.")
        print("  Delete with: uv run python 01-managed-compute/deploy_managed_compute.py --delete")

    best = next((r for r in quota_rows if r["limit"] - r["used"] > 0), None)
    if best and not deployment_rows:
        print(f"\nSuggested: deploy on {best['accelerator']} "
              f"({best['limit'] - best['used']} accelerator(s) of quota free)")
        print("  uv run python 01-managed-compute/deploy_managed_compute.py "
              f"--accelerator {best['accelerator']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
