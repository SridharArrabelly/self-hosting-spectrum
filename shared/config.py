"""Central configuration and the option -> APIM route table.

Every one of the four hosting options is reachable at::

    {APIM_GATEWAY_URL}/v1/{route}/chat/completions

The point of this module is that *nothing else in the repository* needs to know
where a model actually runs. Pick an option number, get a base URL and a model
name, call it like OpenAI.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Option:
    """One hosting pattern."""

    number: int
    key: str
    """Stable slug, also the APIM API path segment."""

    title: str
    runtime_owner: str
    location: str
    model_env_var: str
    """Env var holding the model/deployment name the backend expects."""

    default_model: str
    notes: str

    @property
    def route(self) -> str:
        """APIM path prefix, e.g. ``/v1/managed-compute``."""
        return f"/v1/{self.key}"

    @property
    def connection_name(self) -> str:
        """Foundry BYOM connection name for this option."""
        return f"apim-{self.key}"


OPTIONS: dict[int, Option] = {
    1: Option(
        number=1,
        key="managed-compute",
        title="Foundry Managed Compute",
        runtime_owner="Microsoft Foundry",
        location="Azure - dedicated GPUs",
        model_env_var="MC_DEPLOYMENT_NAME",
        default_model="managed-compute",
        notes="Dedicated A100/H100 capacity. Billed hourly even at zero traffic - tear down after the demo.",
    ),
    2: Option(
        number=2,
        key="fireworks",
        title="Fireworks on Foundry",
        runtime_owner="Fireworks AI (partner)",
        location="Azure - shared serverless",
        model_env_var="FIREWORKS_DEPLOYMENT_NAME",
        default_model="fireworks",
        notes="Pay per token. The request body 'model' is the deployment name, not the FW- catalog id.",
    ),
    3: Option(
        number=3,
        key="azure-vm",
        title="Customer-managed Azure VM",
        runtime_owner="You",
        location="Azure - your VM",
        model_env_var="VM_MODEL",
        default_model="qwen2.5:1.5b-instruct",
        notes="Ollama or vLLM on a VM you own. Reached privately by APIM; never exposed to the internet.",
    ),
    4: Option(
        number=4,
        key="foundry-local",
        title="Foundry Local",
        runtime_owner="You",
        location="Your own machine",
        model_env_var="FOUNDRY_LOCAL_MODEL",
        default_model="qwen2.5-0.5b",
        notes="Runs on local CPU/GPU/NPU. Published to APIM through a Dev Tunnel.",
    ),
}


def get_option(number: int) -> Option:
    try:
        return OPTIONS[number]
    except KeyError:
        valid = ", ".join(str(n) for n in OPTIONS)
        raise SystemExit(f"Unknown option {number!r}. Valid options: {valid}") from None


@dataclass
class Settings:
    """Values loaded from .env, with just enough validation to fail helpfully."""

    tenant_id: str
    subscription_id: str
    location: str
    resource_group: str
    name_suffix: str

    apim_gateway_url: str
    apim_subscription_key: str

    foundry_endpoint: str
    foundry_project_endpoint: str

    raw: dict[str, str]

    def model_for(self, option: Option) -> str:
        return self.raw.get(option.model_env_var) or option.default_model

    def base_url_for(self, option: Option) -> str:
        """OpenAI-SDK ``base_url`` for this option - the SDK appends /chat/completions."""
        if not self.apim_gateway_url:
            raise SystemExit(
                "APIM_GATEWAY_URL is not set. Run infra/scripts/deploy.py first, "
                "or copy the value from the deployment outputs into .env."
            )
        return f"{self.apim_gateway_url.rstrip('/')}{option.route}"


def load_settings(env_file: str | Path | None = None) -> Settings:
    """Load .env (falling back to .env.example for non-secret defaults)."""
    path = Path(env_file) if env_file else REPO_ROOT / ".env"
    if path.exists():
        load_dotenv(path, override=False)
    else:
        example = REPO_ROOT / ".env.example"
        if example.exists():
            load_dotenv(example, override=False)
            print(f"[warn] {path.name} not found - using defaults from .env.example.")

    raw = dict(os.environ)
    return Settings(
        tenant_id=raw.get("AZURE_TENANT_ID", ""),
        subscription_id=raw.get("AZURE_SUBSCRIPTION_ID", ""),
        location=raw.get("AZURE_LOCATION", "eastus2"),
        resource_group=raw.get("RESOURCE_GROUP", "rg-self-hosting-spectrum"),
        name_suffix=raw.get("NAME_SUFFIX", "shs01"),
        apim_gateway_url=raw.get("APIM_GATEWAY_URL", ""),
        apim_subscription_key=raw.get("APIM_SUBSCRIPTION_KEY", ""),
        foundry_endpoint=raw.get("FOUNDRY_ENDPOINT", ""),
        foundry_project_endpoint=raw.get("FOUNDRY_PROJECT_ENDPOINT", ""),
        raw=raw,
    )
