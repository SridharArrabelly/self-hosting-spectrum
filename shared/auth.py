"""How callers prove who they are to the gateway.

The default is keyless. Nothing in this repository has to hold a shared secret
to call a model: `az login` is the credential, and Microsoft Entra issues a
short-lived token that APIM validates on the way in.

That matters more here than in a normal app, because there are two very
different callers hitting the same four routes:

  * a human running `client.py`, authenticated as themselves
  * Foundry Agent Service calling on an agent's behalf, authenticated as the
    project's managed identity

Both present an ordinary Entra bearer token for the same audience, and the
gateway policy treats them identically. Neither needs a key.

Key mode still exists, selected with `GATEWAY_AUTH_MODE=key`, because the demo
has to be honest about migration: most people arrive with keys, and the point
is that switching is a configuration change rather than a rewrite.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared.config import Settings  # noqa: E402

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}

# Refresh a little before the token actually expires. A benchmark run can
# straddle the boundary, and an expired token surfaces as a bare 401 that looks
# exactly like a misconfigured gateway.
_EXPIRY_SKEW_SECONDS = 300


def _credential():
    """Resolve a credential, preferring the CLI login.

    DefaultAzureCredential is the usual answer, but it probes environment
    variables and managed identity endpoints first. On a developer laptop that
    is slow and, worse, can silently pick up a stale service principal from an
    unrelated project. AzureCliCredential is tried first so `az account show`
    predicts what this script will do, with DefaultAzureCredential as the
    fallback for CI, containers, and anywhere without the CLI.
    """
    from azure.identity import AzureCliCredential, ChainedTokenCredential, DefaultAzureCredential

    return ChainedTokenCredential(AzureCliCredential(), DefaultAzureCredential())


def get_token(settings: Settings) -> str:
    """A bearer token for the gateway audience, cached until it nearly expires."""
    scope = f"{settings.entra_audience.rstrip('/')}/.default"

    cached = _TOKEN_CACHE.get(scope)
    if cached and cached[1] - _EXPIRY_SKEW_SECONDS > time.time():
        return cached[0]

    try:
        token = _credential().get_token(scope)
    except Exception as exc:  # noqa: BLE001 - the fix is always the same
        raise SystemExit(
            f"Could not get an Entra token for {scope}.\n"
            f"  {type(exc).__name__}: {exc}\n\n"
            f"Run:  az login --tenant {settings.tenant_id or '<tenant-id>'}\n"
            "Or set GATEWAY_AUTH_MODE=key in .env to fall back to the APIM "
            "subscription key."
        ) from exc

    _TOKEN_CACHE[scope] = (token.token, float(token.expires_on))
    return token.token


def gateway_credential(settings: Settings) -> tuple[str, dict[str, str]]:
    """Return ``(api_key, extra_headers)`` for an OpenAI client.

    In Entra mode the token is handed to the SDK as its `api_key`. That is not a
    trick: the OpenAI SDK's only job with `api_key` is to send
    `Authorization: Bearer <value>`, which is exactly the header
    `validate-azure-ad-token` reads. So the keyless path needs no custom
    transport, no header plumbing, and no branch anywhere in client.py.

    In key mode the key is sent twice - as `Authorization` by the SDK and as
    `Ocp-Apim-Subscription-Key` by convention - so the same client also works
    against a stock Azure OpenAI endpoint or a raw Ollama server unchanged.
    """
    if settings.uses_entra:
        return get_token(settings), {}

    if not settings.apim_subscription_key:
        raise SystemExit(
            "GATEWAY_AUTH_MODE=key but APIM_SUBSCRIPTION_KEY is not set.\n"
            "Run infra/scripts/deploy_apis.py, which writes it into .env - or "
            "switch back to the keyless default with GATEWAY_AUTH_MODE=entra."
        )

    return settings.apim_subscription_key, {
        "Ocp-Apim-Subscription-Key": settings.apim_subscription_key
    }


def describe(settings: Settings) -> str:
    """One line for the console, so a failing run says which mode it was in."""
    if settings.uses_entra:
        return f"Auth: Entra bearer token (keyless), audience {settings.entra_audience}"
    return "Auth: APIM subscription key"
