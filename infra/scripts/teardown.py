"""Tear down the self-hosting-spectrum infrastructure, most expensive thing first.

    uv run python infra/scripts/teardown.py                # interactive, full teardown
    uv run python infra/scripts/teardown.py --managed-compute-only
    uv run python infra/scripts/teardown.py --stop-billing  # MC delete + VM deallocate
    uv run python infra/scripts/teardown.py --yes           # no prompts

Order matters, and not for tidiness:

  1. Managed compute deployments bill per accelerator-hour whether or not anything
     calls them - about $7.91/hr for an H100. Everything else in this repo is
     either cents per hour or per-token. So managed compute is deleted first,
     before any prompt that a human might walk away from.
  2. The VM is deallocated (not deleted) under --stop-billing, because the disk
     holds a multi-GB pulled model and re-pulling it takes longer than the demo.
  3. The resource group delete is last and is what actually removes APIM, the
     Foundry account, networking, and observability.

Deleting the resource group does remove managed compute deployments too, but it
is asynchronous and can take many minutes - during which the GPU keeps billing.
Deleting the deployment explicitly first closes that window.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from shared.config import load_settings  # noqa: E402

TERMINAL_STATES = {"Succeeded", "Failed", "Canceled", "Deleted"}

# Every az call is pinned to this subscription. The CLI's default context is
# frequently a different tenant, and a teardown script is the worst possible
# place to discover that.
_SUBSCRIPTION = ""


def az_exe() -> str:
    exe = shutil.which("az")
    if not exe:
        raise SystemExit("Azure CLI not found on PATH.")
    return exe


def run_az(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    if _SUBSCRIPTION and "--subscription" not in args:
        args = [*args, "--subscription", _SUBSCRIPTION]
    proc = subprocess.run(
        [az_exe(), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if check and proc.returncode != 0:
        raise SystemExit(f"az {' '.join(args)} failed:\n{proc.stderr.strip()}")
    return proc


def confirm(question: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        print(f"  {question} -> yes (--yes)")
        return True
    return input(f"  {question} [y/N] ").strip().lower() in {"y", "yes"}


# --------------------------------------------------------------------------- #
# 1. Managed compute - the only thing here that can run up a real bill
# --------------------------------------------------------------------------- #


def managed_compute_client(subscription_id: str):
    try:
        from azure.identity import AzureCliCredential
        from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
    except ImportError:  # pragma: no cover
        raise SystemExit("Run 'uv sync' first - azure-mgmt-cognitiveservices is missing.")
    return CognitiveServicesManagementClient(AzureCliCredential(), subscription_id)


def delete_managed_compute(settings, *, assume_yes: bool, wait: bool) -> None:
    account = f"aif-spectrum-{settings.name_suffix}"
    print("\n[1/4] Managed compute deployments (billed per accelerator-hour)")

    try:
        client = managed_compute_client(settings.subscription_id)
        deployments = list(
            client.managed_compute_deployments.list(settings.resource_group, account)
        )
    except Exception as exc:  # account may already be gone - that is fine
        print(f"      could not list deployments: {exc}")
        print("      (if the Foundry account is already deleted this is expected)")
        return

    if not deployments:
        print("      none found - nothing billing.")
        return

    for dep in deployments:
        state = (dep.as_dict().get("properties") or {}).get("provisioningState", "?")
        print(f"      {dep.name}  [{state}]")

    if not confirm(f"Delete {len(deployments)} managed compute deployment(s)?", assume_yes=assume_yes):
        print("      SKIPPED - these are still billing.")
        return

    for dep in deployments:
        # A deployment cannot be deleted while it is still Creating; the API
        # returns RequestConflict. Wait it out rather than failing the teardown.
        if wait:
            wait_for_terminal(client, settings.resource_group, account, dep.name)
        print(f"      deleting {dep.name} ...")
        try:
            poller = client.managed_compute_deployments.begin_delete(
                settings.resource_group, account, dep.name
            )
            poller.result()
            print(f"      {dep.name} deleted - billing stopped.")
        except Exception as exc:
            print(f"      FAILED to delete {dep.name}: {exc}")
            print("      Delete it in the portal now - it is still billing.")


def wait_for_terminal(client, rg: str, account: str, name: str, timeout_s: int = 2400) -> None:
    deadline = time.time() + timeout_s
    warned = False
    while time.time() < deadline:
        try:
            dep = client.managed_compute_deployments.get(rg, account, name)
        except Exception:
            return  # already gone
        state = (dep.as_dict().get("properties") or {}).get("provisioningState", "")
        if state in TERMINAL_STATES:
            return
        if not warned:
            print(f"      {name} is '{state}' - cannot delete yet, waiting for a terminal state.")
            warned = True
        time.sleep(30)
    print(f"      gave up waiting for {name}; attempting delete anyway.")


# --------------------------------------------------------------------------- #
# 2. VM
# --------------------------------------------------------------------------- #


def deallocate_vm(settings, *, assume_yes: bool) -> None:
    vm = f"vm-inference-{settings.name_suffix}"
    print(f"\n[2/4] VM {vm} (~$0.19/hr while running)")

    probe = run_az(
        ["vm", "show", "-g", settings.resource_group, "-n", vm, "-o", "none"], check=False
    )
    if probe.returncode != 0:
        print("      not found - nothing to do.")
        return

    if not confirm(f"Deallocate {vm}? (keeps the disk and the pulled model)", assume_yes=assume_yes):
        print("      SKIPPED.")
        return

    print("      deallocating ...")
    run_az(["vm", "deallocate", "-g", settings.resource_group, "-n", vm, "-o", "none"])
    print("      deallocated - compute billing stopped, disk still charged.")


# --------------------------------------------------------------------------- #
# 3. Everything else
# --------------------------------------------------------------------------- #


def delete_resource_group(settings, *, assume_yes: bool, wait: bool) -> None:
    rg = settings.resource_group
    print(f"\n[3/4] Resource group {rg} (APIM, Foundry, VM, networking, observability)")

    probe = run_az(["group", "show", "-n", rg, "-o", "none"], check=False)
    if probe.returncode != 0:
        print("      not found - already gone.")
        return

    print("      This is irreversible and removes every resource in the group.")
    if not confirm(f"Delete resource group {rg}?", assume_yes=assume_yes):
        print("      SKIPPED.")
        return

    args = ["group", "delete", "-n", rg, "--yes"]
    if not wait:
        args.append("--no-wait")
    print("      deleting ..." + ("" if wait else " (async - not waiting)"))
    run_az(args)
    print("      resource group delete " + ("completed." if wait else "started."))


# --------------------------------------------------------------------------- #
# 4. Purge the soft-deleted Foundry account
# --------------------------------------------------------------------------- #


def purge_foundry(settings, *, wait: bool) -> None:
    """Remove the tombstone a deleted Cognitive Services account leaves behind.

    Deleting the resource group does not fully delete a Foundry account. It goes
    into a soft-deleted state for 48 hours, and the name stays reserved. Redeploy
    into the same resource group and Bicep fails with FlagMustBeSetForRestore -
    which reads like a template bug and is really a leftover tombstone.

    Purging is unconditional here: the whole point of running teardown is to be
    able to deploy again tomorrow.
    """
    account = f"aif-spectrum-{settings.name_suffix}"
    print(f"\n[4/4] Soft-deleted Foundry account {account}")

    if not wait:
        print("      skipped: --no-wait means the group delete has not finished, and")
        print("      the account cannot be purged until it has. Re-run teardown later,")
        print("      or purge manually:")
        print(f"      az cognitiveservices account purge -n {account} "
              f"-g {settings.resource_group} -l {settings.location}")
        return

    probe = run_az([
        "cognitiveservices", "account", "purge",
        "-n", account,
        "-g", settings.resource_group,
        "-l", settings.location,
    ], check=False)

    if probe.returncode == 0:
        print("      purged - the name is free to reuse immediately.")
    else:
        # Nothing to purge is the common case and is not an error.
        print("      nothing to purge (or already gone).")


def local_cleanup_hints(settings) -> None:
    print("\nLocal resources are not touched by this script:")
    print("  devtunnel delete shs-foundry-local     # remove the Option 4 tunnel")
    print("  foundry service stop                   # stop the local runtime")
    print(f"  Foundry connections in project 'proj-spectrum' are removed with the account.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="assume yes to every prompt")
    ap.add_argument(
        "--managed-compute-only",
        action="store_true",
        help="delete only managed compute deployments (the expensive part)",
    )
    ap.add_argument(
        "--stop-billing",
        action="store_true",
        help="delete managed compute and deallocate the VM, but keep the infrastructure",
    )
    ap.add_argument(
        "--no-wait",
        action="store_true",
        help="do not block waiting for the resource group delete to finish",
    )
    args = ap.parse_args()

    settings = load_settings()
    if not settings.subscription_id:
        raise SystemExit("AZURE_SUBSCRIPTION_ID is not set - check .env")

    global _SUBSCRIPTION
    _SUBSCRIPTION = settings.subscription_id

    print("self-hosting-spectrum teardown")
    print(f"  subscription   : {settings.subscription_id}")
    print(f"  resource group : {settings.resource_group}")

    delete_managed_compute(settings, assume_yes=args.yes, wait=not args.no_wait)
    if args.managed_compute_only:
        print("\nDone (managed compute only).")
        return 0

    deallocate_vm(settings, assume_yes=args.yes)
    if args.stop_billing:
        print("\nDone. Billing reduced to APIM (~$48/mo) plus disk storage.")
        return 0

    delete_resource_group(settings, assume_yes=args.yes, wait=not args.no_wait)
    purge_foundry(settings, wait=not args.no_wait)
    local_cleanup_hints(settings)
    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
