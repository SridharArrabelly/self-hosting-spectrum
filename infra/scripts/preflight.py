"""Preflight checks for self-hosting-spectrum.

Run this before deploying anything. It answers, in order:

1.  Am I pointed at the right tenant and subscription?
2.  Are the tools installed that later phases need?
3.  Are the resource providers registered?
4.  Is the Fireworks preview feature registered?  (~30 min to propagate - this is
    the long pole, so it is registered as early as possible.)
5.  Do I have Managed Compute accelerator quota?  This is a *separate* quota
    namespace from VM/AML GPU quota, so a subscription with zero GPU cores can
    still run Option 1.
6.  Does the chosen VM size have quota in this region?

Nothing here creates billable resources.

    uv run python infra/scripts/preflight.py
    uv run python infra/scripts/preflight.py --fix     # register what is missing
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import load_settings  # noqa: E402

REQUIRED_PROVIDERS = [
    "Microsoft.CognitiveServices",
    "Microsoft.ApiManagement",
    "Microsoft.Compute",
    "Microsoft.Network",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.Storage",
]

FIREWORKS_FEATURE = "Fireworks.EnableDeploy"

# Tools: (executable, why it is needed, whether a missing one is fatal)
TOOLS = [
    ("az", "Azure CLI - deploys all infrastructure", True),
    ("uv", "Python environment for clients and agents", True),
    ("git", "Source control", False),
    ("devtunnel", "Option 4 - publishes Foundry Local to APIM", False),
    ("foundry", "Option 4 - the local inference runtime", False),
]

OK = "  [ok]  "
WARN = "  [warn]"
FAIL = "  [FAIL]"
INFO = "  [info]"


@dataclass
class Report:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        print(f"{FAIL} {msg}")
        self.failures.append(msg)

    def warn(self, msg: str) -> None:
        print(f"{WARN} {msg}")
        self.warnings.append(msg)

    @staticmethod
    def ok(msg: str) -> None:
        print(f"{OK} {msg}")

    @staticmethod
    def info(msg: str) -> None:
        print(f"{INFO} {msg}")


def _az_executable() -> str:
    """Resolve the az launcher.

    On Windows ``az`` is ``az.cmd``; subprocess with shell=False will not find it
    by bare name, so resolve the full path once via PATHEXT-aware lookup.
    """
    resolved = shutil.which("az")
    if not resolved:
        raise RuntimeError("Azure CLI not found on PATH. Install it, then re-run preflight.")
    return resolved


def az(*args: str, check: bool = True) -> object:
    """Run an az command and parse JSON output.

    Uses a list argv so JMESPath queries containing '||' are never seen by a shell.
    """
    cmd = [_az_executable(), *args, "-o", "json"]
    proc = subprocess.run(cmd, capture_output=True, text=True, shell=False)
    if proc.returncode != 0:
        if check:
            raise RuntimeError(f"az {' '.join(args)} failed:\n{proc.stderr.strip()}")
        return None
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # A few subcommands (notably 'az bicep version') ignore -o json.
        return out


def header(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 62 - len(title)))


# --------------------------------------------------------------------------- #
# checks
# --------------------------------------------------------------------------- #


def check_tools(rep: Report) -> None:
    header("Tooling")
    for exe, why, fatal in TOOLS:
        if shutil.which(exe):
            rep.ok(f"{exe:<10} found - {why}")
        elif fatal:
            rep.fail(f"{exe:<10} MISSING - {why}")
        else:
            rep.warn(f"{exe:<10} missing - {why} (install before that option)")

    if shutil.which("az"):
        bicep = az("bicep", "version", check=False)
        if bicep is None:
            rep.warn("bicep  not installed - run 'az bicep install'")
        else:
            rep.ok("bicep      available via Azure CLI")


def check_account(rep: Report, want_tenant: str, want_sub: str) -> None:
    header("Azure context")
    acct = az("account", "show", check=False)
    if acct is None:
        rep.fail("Not logged in. Run: az login --tenant <tenant-id>")
        return

    sub_id, tenant_id = acct["id"], acct["tenantId"]
    rep.info(f"subscription : {acct['name']} ({sub_id})")
    rep.info(f"tenant       : {tenant_id}")
    rep.info(f"signed in as : {acct.get('user', {}).get('name', '?')}")

    if want_tenant and tenant_id.lower() != want_tenant.lower():
        rep.fail(f"Wrong tenant. Expected {want_tenant}. Run: az login --tenant {want_tenant}")
    else:
        rep.ok("tenant matches AZURE_TENANT_ID")

    if want_sub and sub_id.lower() != want_sub.lower():
        rep.fail(f"Wrong subscription. Run: az account set --subscription {want_sub}")
    else:
        rep.ok("subscription matches AZURE_SUBSCRIPTION_ID")


def check_providers(rep: Report, fix: bool) -> None:
    header("Resource providers")
    for ns in REQUIRED_PROVIDERS:
        info = az("provider", "show", "-n", ns, "--query", "registrationState", check=False)
        state = info if isinstance(info, str) else "Unknown"
        if state == "Registered":
            rep.ok(f"{ns:<32} {state}")
        elif state == "Registering":
            rep.warn(f"{ns:<32} {state} - still propagating, this is fine")
        elif fix:
            az("provider", "register", "-n", ns, check=False)
            rep.warn(f"{ns:<32} {state} -> registration requested")
        else:
            rep.fail(f"{ns:<32} {state} - rerun with --fix")


def check_fireworks(rep: Report, fix: bool) -> None:
    header("Fireworks preview feature (Option 2)")
    feat = az(
        "feature", "show",
        "--namespace", "Microsoft.CognitiveServices",
        "--name", FIREWORKS_FEATURE,
        "--query", "properties.state",
        check=False,
    )
    state = feat if isinstance(feat, str) else "NotRegistered"

    if state == "Registered":
        rep.ok(f"{FIREWORKS_FEATURE} Registered")
        rep.info("If Option 2 deployment still fails, the flag may not have propagated to the")
        rep.info("resource provider yet. Re-run: az provider register -n Microsoft.CognitiveServices")
    elif state == "Pending":
        rep.warn(f"{FIREWORKS_FEATURE} Pending - approval in progress")
    elif fix:
        az("feature", "register",
           "--namespace", "Microsoft.CognitiveServices",
           "--name", FIREWORKS_FEATURE, check=False)
        az("provider", "register", "-n", "Microsoft.CognitiveServices", check=False)
        rep.warn(f"{FIREWORKS_FEATURE} registration requested - allow ~30 minutes to propagate")
    else:
        rep.fail(f"{FIREWORKS_FEATURE} {state} - Option 2 will fail. Rerun with --fix")


def check_managed_compute_quota(rep: Report, location: str) -> None:
    header(f"Managed Compute accelerator quota ({location})")
    rep.info("This is a separate namespace from VM/AML GPU quota - zero VM GPU cores")
    rep.info("does not block Option 1.")

    usages = az("cognitiveservices", "usage", "list", "-l", location, check=False)
    if not usages:
        rep.warn("Could not read Cognitive Services usage for this region.")
        rep.warn("Check the Foundry portal quota page before deploying Option 1.")
        return

    accel = [
        u for u in usages
        if any(tag in (u.get("name", {}).get("value") or "").upper()
               for tag in ("A100", "H100", "MI300", "ACCELERATOR", "MANAGEDCOMPUTE"))
    ]
    if not accel:
        rep.warn("No accelerator quota entries returned for this region.")
        rep.warn("Quota is granted per Foundry account - re-check after the account exists.")
        return

    for u in accel:
        name = u.get("name", {}).get("value", "?")
        used, limit = u.get("currentValue", 0), u.get("limit", 0)
        if limit and limit > 0:
            rep.ok(f"{name:<44} {used:g}/{limit:g}")
        else:
            rep.warn(f"{name:<44} {used:g}/{limit:g} - request quota to use this accelerator")


def check_vm_quota(rep: Report, location: str, vm_size: str) -> None:
    header(f"VM quota for Option 3 ({vm_size} in {location})")

    # Do not guess the quota family from the SKU name - Standard_D4s_v5 lives under
    # 'standardDSv5Family', which no string transform of the name produces. Ask the
    # SKU API for the real family, then match the usage entry exactly.
    skus = az("vm", "list-skus", "-l", location, "--size", vm_size,
              "--query", "[].{name:name, family:family}", check=False)
    match = next((s for s in (skus or []) if s.get("name") == vm_size), None)
    if match is None:
        rep.warn(f"{vm_size} is not offered in {location}. Pick another size or region.")
        return

    family = match["family"]
    usages = az("vm", "list-usage", "-l", location, check=False)
    if not usages:
        rep.warn("Could not read VM usage for this region.")
        return

    entry = next((u for u in usages if u.get("name", {}).get("value") == family), None)
    if entry is None:
        rep.warn(f"No quota entry for family '{family}'.")
    else:
        # az returns these as strings in some CLI versions.
        used = int(entry.get("currentValue", 0) or 0)
        limit = int(entry.get("limit", 0) or 0)
        label = entry.get("localName", family)
        if limit > 0:
            rep.ok(f"{label:<44} {used}/{limit}")
        else:
            rep.fail(f"{label:<44} {used}/{limit} - no quota, Option 3 cannot deploy")

    total = next((u for u in usages if u.get("name", {}).get("value") == "cores"), None)
    if total:
        rep.info(f"Total regional vCPUs: {total.get('currentValue')}/{total.get('limit')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Preflight checks for self-hosting-spectrum.")
    parser.add_argument("--fix", action="store_true",
                        help="Register missing providers and preview features.")
    args = parser.parse_args()

    settings = load_settings()
    rep = Report()

    print("self-hosting-spectrum preflight")
    print(f"region: {settings.location}   resource group: {settings.resource_group}")

    check_tools(rep)
    check_account(rep, settings.tenant_id, settings.subscription_id)

    if any("Not logged in" in f or "Wrong tenant" in f for f in rep.failures):
        print("\nFix the Azure context above, then re-run.")
        return 1

    check_providers(rep, args.fix)
    check_fireworks(rep, args.fix)
    check_managed_compute_quota(rep, settings.location)
    check_vm_quota(rep, settings.location, settings.raw.get("VM_SIZE", "Standard_D4s_v7"))

    header("Summary")
    if rep.failures:
        print(f"{len(rep.failures)} blocking issue(s):")
        for f in rep.failures:
            print(f"  - {f}")
    if rep.warnings:
        print(f"{len(rep.warnings)} warning(s) - review before the affected option.")
    if not rep.failures:
        print("\nPreflight passed. Next: uv run python infra/scripts/deploy.py")
    return 1 if rep.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
