"""Run the same Foundry agent against whichever runtime you pick.

    uv run python shared/agent.py --option 2
    uv run python shared/agent.py --all
    uv run python shared/agent.py --option 4 --prompt "Explain vLLM in one sentence."
    uv run python shared/agent.py --cleanup

shared/client.py proves the *gateway* is uniform. This proves the layer above
it is uniform too: a Foundry Agent Service agent whose model happens to be a
rented A100, a partner API, a VM you own, or your own laptop - with the same
agent definition every time.

The only thing that changes between options is one string::

    model = "<connection-name>/<model-name>"      e.g. apim-azure-vm/qwen2.5:1.5b

That connection is an ApiManagement Bring Your Own Model connection created by
shared/register_connections.py. Foundry appends `chat/completions` to the
connection target, which is why the APIM routes are shaped /v1/<option>.

Caveat worth knowing before you demo this: BYOM is supported for *prompt*
agents. Agents are created here through the control plane, then invoked over
the project's OpenAI-compatible endpoint. If your project has not yet been
enabled for agent invocation, --dry-run still creates and lists the agents so
you can show the wiring.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from azure.ai.projects import AIProjectClient  # noqa: E402
from azure.ai.projects.models import PromptAgentDefinition  # noqa: E402
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError  # noqa: E402
from azure.identity import AzureCliCredential  # noqa: E402

from shared.config import OPTIONS, Option, Settings, get_option, load_settings  # noqa: E402

DEFAULT_PROMPT = "In one sentence, what is the difference between a model and a model deployment?"

INSTRUCTIONS = (
    "You are the self-hosting-spectrum demo agent. Answer in one or two short "
    "sentences. Be concrete and skip preamble."
)


def agent_name(option: Option) -> str:
    return f"spectrum-{option.key}"


def project_client(settings: Settings) -> AIProjectClient:
    endpoint = settings.foundry_project_endpoint or settings.foundry_endpoint
    if not endpoint:
        raise SystemExit(
            "FOUNDRY_PROJECT_ENDPOINT is not set. Run infra/scripts/deploy.py, "
            "which writes it into .env."
        )
    return AIProjectClient(endpoint=endpoint, credential=AzureCliCredential())


def ensure_agent(client: AIProjectClient, settings: Settings, option: Option):
    """Create or update the prompt agent for one option.

    Creating a new *version* rather than a new agent is deliberate: rerunning
    this script re-points the same agent at the same connection instead of
    littering the project with duplicates.
    """
    model = f"{option.connection_name}/{settings.model_for(option)}"
    return client.agents.create_version(
        agent_name=agent_name(option),
        definition=PromptAgentDefinition(model=model, instructions=INSTRUCTIONS),
    )


def invoke(client: AIProjectClient, option: Option, prompt: str) -> tuple[str, float]:
    """Run the agent and return (answer, seconds)."""
    openai_client = client.get_openai_client()
    started = time.perf_counter()
    response = openai_client.responses.create(
        input=prompt,
        extra_body={"agent": {"name": agent_name(option), "type": "agent_reference"}},
    )
    elapsed = time.perf_counter() - started
    text = getattr(response, "output_text", None)
    if not text:
        text = str(response)
    return text.strip(), elapsed


def run_one(client: AIProjectClient, settings: Settings, option: Option,
            prompt: str, dry_run: bool) -> bool:
    print(f"\n=== Option {option.number}: {option.title} ===")
    print(f"    runtime owner : {option.runtime_owner}")
    print(f"    connection    : {option.connection_name}")
    print(f"    model         : {option.connection_name}/{settings.model_for(option)}")

    try:
        version = ensure_agent(client, settings, option)
    except HttpResponseError as exc:
        first = (exc.message or "").splitlines()[0]
        print(f"    [fail] could not create agent: HTTP {exc.status_code} {first}")
        if exc.status_code == 404:
            print("           The BYOM connection is probably missing. Run:")
            print(f"           uv run python shared/register_connections.py --option {option.number}")
        return False

    print(f"    agent         : {agent_name(option)} (version {getattr(version, 'version', '?')})")

    if dry_run:
        print("    [dry-run] agent created; skipping invocation.")
        return True

    try:
        answer, elapsed = invoke(client, option, prompt)
    except Exception as exc:  # noqa: BLE001 - the run path varies by project config
        print(f"    [fail] agent run: {type(exc).__name__}: {exc}")
        print("           The agent exists. Verify the route directly with:")
        print(f"           uv run python shared/client.py --option {option.number}")
        return False

    print(f"    latency       : {elapsed:.2f}s")
    print(f"    answer        : {answer}")
    return True


def cleanup(client: AIProjectClient, options: list[Option]) -> None:
    for option in options:
        try:
            client.agents.delete(agent_name(option))
            print(f"[ok] deleted agent {agent_name(option)}")
        except ResourceNotFoundError:
            print(f"[skip] {agent_name(option)} does not exist")
        except HttpResponseError as exc:
            print(f"[warn] {agent_name(option)}: HTTP {exc.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a Foundry prompt agent backed by any of the four hosting options."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--option", type=int, choices=sorted(OPTIONS))
    group.add_argument("--all", action="store_true", help="Run every option in turn.")
    group.add_argument("--cleanup", action="store_true", help="Delete the demo agents.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--dry-run", action="store_true",
                        help="Create the agents but do not invoke them.")
    args = parser.parse_args()

    settings = load_settings()
    client = project_client(settings)

    if args.cleanup:
        cleanup(client, list(OPTIONS.values()))
        return 0

    targets = list(OPTIONS.values()) if args.all else [get_option(args.option)]
    results = [run_one(client, settings, opt, args.prompt, args.dry_run) for opt in targets]

    if len(targets) > 1:
        ok = sum(results)
        print(f"\n{ok}/{len(results)} agents responded. "
              "Same agent definition, same prompt, four different runtimes.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
