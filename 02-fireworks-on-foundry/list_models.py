"""Show which Fireworks models can actually be deployed, and how.

    uv run python 02-fireworks-on-foundry/list_models.py
    uv run python 02-fireworks-on-foundry/list_models.py --all
    uv run python 02-fireworks-on-foundry/list_models.py --json

Option 2 is the "someone else's premium runtime" end of the spectrum: Fireworks
AI serves the model, Microsoft bills it, and it appears on your own Foundry
account as an ordinary deployment resource. No Marketplace SaaS offer, no
`az term accept`.

The trap this script exists to expose: almost every small Fireworks model is
provisioned-throughput only. A DataZoneProvisionedManaged deployment reserves
capacity and bills whether or not you send a request - tens of thousands of
dollars a month. Only models offering DataZoneStandard or GlobalStandard are
pay-per-token, and those are the ones a demo can afford.

The other trap: the value you put in the request body's `model` field is the
name of the *deployment* you create, not the FW- catalog id.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.identity import AzureCliCredential  # noqa: E402
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient  # noqa: E402

from shared.config import load_settings  # noqa: E402

PAY_AS_YOU_GO_SKUS = {"Standard", "GlobalStandard", "DataZoneStandard"}


@dataclass
class FireworksModel:
    name: str
    version: str
    skus: set[str] = field(default_factory=set)

    @property
    def payg_skus(self) -> list[str]:
        return sorted(self.skus & PAY_AS_YOU_GO_SKUS)

    @property
    def is_pay_as_you_go(self) -> bool:
        return bool(self.payg_skus)

    @property
    def recommended_sku(self) -> str | None:
        for preferred in ("DataZoneStandard", "GlobalStandard", "Standard"):
            if preferred in self.skus:
                return preferred
        return None


def collect(location: str, subscription_id: str) -> list[FireworksModel]:
    client = CognitiveServicesManagementClient(AzureCliCredential(), subscription_id)
    found: dict[tuple[str, str], FireworksModel] = {}
    for entry in client.models.list(location):
        model = entry.as_dict().get("model", {})
        if model.get("format") != "Fireworks":
            continue
        key = (model.get("name", ""), str(model.get("version", "")))
        record = found.setdefault(key, FireworksModel(name=key[0], version=key[1]))
        for sku in model.get("skus") or []:
            if sku.get("name"):
                record.skus.add(sku["name"])
    return sorted(found.values(), key=lambda m: m.name)


def main() -> int:
    parser = argparse.ArgumentParser(description="List deployable Fireworks models on Foundry.")
    parser.add_argument("--all", action="store_true", help="Include provisioned-throughput-only models.")
    parser.add_argument("--location", help="Region to query. Defaults to AZURE_LOCATION.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    args = parser.parse_args()

    settings = load_settings()
    location = args.location or settings.location
    if not settings.subscription_id:
        raise SystemExit("AZURE_SUBSCRIPTION_ID is not set. Copy .env.example to .env first.")

    models = collect(location, settings.subscription_id)
    if not models:
        raise SystemExit(
            f"No Fireworks models found in {location}.\n"
            "Pay-as-you-go Fireworks is US-only, and the subscription needs the\n"
            "Fireworks.EnableDeploy feature registered. Run infra/scripts/preflight.py --fix."
        )

    payg = [m for m in models if m.is_pay_as_you_go]
    shown = models if args.all else payg

    if args.json:
        print(json.dumps(
            [{"name": m.name, "version": m.version, "skus": sorted(m.skus),
              "payAsYouGo": m.is_pay_as_you_go, "recommendedSku": m.recommended_sku} for m in shown],
            indent=2,
        ))
        return 0

    print(f"Fireworks models in {location}")
    print(f"  {len(models)} in the catalogue, {len(payg)} pay-per-token\n")
    print(f"{'Model':<40}{'Ver':<5}{'Deployable as':<22}Billing")
    print("-" * 92)
    for model in shown:
        if model.is_pay_as_you_go:
            billing = "per token"
            skus = ", ".join(model.payg_skus)
        else:
            billing = "RESERVED CAPACITY - bills at zero traffic"
            skus = "provisioned only"
        print(f"{model.name:<40}{model.version:<5}{skus:<22}{billing}")

    if not args.all:
        hidden = len(models) - len(payg)
        print(f"\n{hidden} provisioned-throughput-only model(s) hidden. Use --all to see them.")

    configured = settings.raw.get("FIREWORKS_MODEL", "")
    if configured:
        match = next((m for m in models if m.name == configured), None)
        print(f"\nConfigured FIREWORKS_MODEL={configured}")
        if match is None:
            print("  NOT IN THE CATALOGUE. Per-token Fireworks models are retired on 15 days'")
            print("  notice - pick a replacement from the list above and update .env.")
        elif not match.is_pay_as_you_go:
            print(f"  WARNING: only deployable as {', '.join(sorted(match.skus))} - reserved capacity.")
        else:
            print(f"  OK - deploy as {match.recommended_sku}.")

    print("\nRemember: the request body's \"model\" is the deployment name you choose")
    print("(FIREWORKS_DEPLOYMENT_NAME), not the FW- catalogue id above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
