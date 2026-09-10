"""Register each APIM route as a Bring Your Own Model connection in Foundry.

    uv run python shared/register_connections.py
    uv run python shared/register_connections.py --option 3
    uv run python shared/register_connections.py --list
    uv run python shared/register_connections.py --delete

This is what turns four gateway routes into four Foundry agents.

Foundry Agent Service used to require an Azure OpenAI deployment. It now
accepts any endpoint that implements the OpenAI chat completions API through
the ApiManagement and ModelGateway connection categories - so the model behind
an agent can be a rented GPU, a partner API, a VM, or a laptop, and the agent
definition is identical in all four cases.

Two details that decide whether this works:

  * Foundry appends `chat/completions` to whatever base URL is registered. The
    route shape in this repo (/v1/<option>) was chosen so that append produces
    exactly the right URL. Leave "include deployment name in URL path" off.
  * The agent then references the model as `<connection-name>/<model-name>`.
    shared/config.py owns both halves of that string so nothing else has to
    guess it.

Responsible AI note: BYOM models are Non-Microsoft Products. Azure's built-in
content filters do not apply to them, and you own the mitigations. That is the
argument for putting llm-content-safety in the gateway policy rather than
trusting each backend.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.core.exceptions import HttpResponseError, ResourceNotFoundError  # noqa: E402
from azure.identity import AzureCliCredential  # noqa: E402
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient  # noqa: E402
from azure.mgmt.cognitiveservices.models import (  # noqa: E402
    ApiKeyAuthConnectionProperties,
    ConnectionApiKey,
    ConnectionPropertiesV2BasicResource,
)

from shared.config import OPTIONS, Option, Settings, get_option, load_settings  # noqa: E402


def mgmt_client(subscription_id: str) -> CognitiveServicesManagementClient:
    return CognitiveServicesManagementClient(AzureCliCredential(), subscription_id)


def build_connection(settings: Settings, option: Option) -> ConnectionPropertiesV2BasicResource:
    """An ApiManagement connection pointing at one route.

    The target deliberately has no trailing /chat/completions - Foundry adds it.
    """
    return ConnectionPropertiesV2BasicResource(
        properties=ApiKeyAuthConnectionProperties(
            category="ApiManagement",
            target=settings.base_url_for(option),
            credentials=ConnectionApiKey(key=settings.apim_subscription_key),
            is_shared_to_all=True,
            metadata={
                "option": str(option.number),
                "hostingPattern": option.title,
                "runtimeOwner": option.runtime_owner,
                "model": settings.model_for(option),
            },
        )
    )


def register(settings: Settings, option: Option, account: str, project: str) -> bool:
    client = mgmt_client(settings.subscription_id)
    target = settings.base_url_for(option)
    try:
        client.project_connections.create(
            settings.resource_group,
            account,
            project,
            option.connection_name,
            build_connection(settings, option),
        )
    except HttpResponseError as exc:
        print(f"[fail] {option.connection_name}: HTTP {exc.status_code} {exc.message.splitlines()[0]}")
        return False

    print(f"[ok] {option.connection_name}")
    print(f"       target : {target}")
    print(f"       model  : {option.connection_name}/{settings.model_for(option)}")
    return True


def delete(settings: Settings, option: Option, account: str, project: str) -> None:
    client = mgmt_client(settings.subscription_id)
    try:
        client.project_connections.delete(
            settings.resource_group, account, project, option.connection_name
        )
        print(f"[ok] deleted {option.connection_name}")
    except ResourceNotFoundError:
        print(f"[skip] {option.connection_name} does not exist")


def show(settings: Settings, account: str, project: str) -> None:
    client = mgmt_client(settings.subscription_id)
    found = list(client.project_connections.list(settings.resource_group, account, project))
    if not found:
        print("No connections on this project yet.")
        return
    print(f"{'Connection':<24}{'Category':<16}Target")
    print("-" * 96)
    for item in found:
        props = item.as_dict().get("properties", {})
        print(f"{item.name:<24}{props.get('category', ''):<16}{props.get('target', '')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Register APIM routes as Foundry BYOM connections.")
    parser.add_argument("--option", type=int, choices=sorted(OPTIONS),
                        help="Register just one option. Default is all four.")
    parser.add_argument("--list", action="store_true", help="Show existing connections and exit.")
    parser.add_argument("--delete", action="store_true", help="Remove the connections instead of creating them.")
    args = parser.parse_args()

    settings = load_settings()
    if not settings.subscription_id:
        raise SystemExit("AZURE_SUBSCRIPTION_ID is not set. Copy .env.example to .env first.")

    account = f"aif-spectrum-{settings.name_suffix}"
    project = settings.raw.get("FOUNDRY_PROJECT_NAME", "proj-spectrum")

    if args.list:
        show(settings, account, project)
        return 0

    targets = [get_option(args.option)] if args.option else list(OPTIONS.values())

    if args.delete:
        for option in targets:
            delete(settings, option, account, project)
        return 0

    if not settings.apim_subscription_key:
        raise SystemExit(
            "APIM_SUBSCRIPTION_KEY is not set. Run infra/scripts/deploy_apis.py first."
        )

    print(f"Registering {len(targets)} connection(s) on {account}/{project}\n")
    results = [register(settings, option, account, project) for option in targets]

    print("\nUse them from an agent with:")
    for option in targets:
        print(f"  option {option.number}: model=\"{option.connection_name}/{settings.model_for(option)}\"")
    print("\n  uv run python shared/agent.py --option 2")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
