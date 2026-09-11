"""One client, four radically different runtimes.

    uv run python shared/client.py --option 2
    uv run python shared/client.py --all
    uv run python shared/client.py --option 3 --prompt "Explain vLLM in one sentence."

This is the proof the repository exists to make. The same OpenAI client object,
the same request body, the same credential - only the path segment changes - and
behind it the model is running on Foundry-managed GPUs, on a partner's
serverless capacity, on a VM you own, or on the laptop this script is typed on.

Authentication is keyless by default: `az login` is the only credential, and the
token works against all four routes identically.

If this script needs an `if option == ...` branch to talk to a backend, the
gateway is not doing its job. It does not have one.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import APIStatusError, OpenAI  # noqa: E402

from shared.auth import describe as describe_auth  # noqa: E402
from shared.auth import gateway_credential  # noqa: E402
from shared.config import OPTIONS, Option, Settings, get_option, load_settings  # noqa: E402

DEFAULT_PROMPT = "In one sentence, what is the difference between renting a GPU and renting tokens?"


@dataclass
class Result:
    option: Option
    ok: bool
    answer: str = ""
    latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    error: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def build_client(settings: Settings, option: Option, timeout: float) -> OpenAI:
    """An ordinary OpenAI client pointed at an APIM route.

    Nothing here knows how the caller is authenticated. shared/auth.py returns
    whatever credential the gateway is configured for, and by default that is a
    short-lived Entra token obtained from `az login` - no shared secret exists
    anywhere in the system.
    """
    credential, auth_headers = gateway_credential(settings)

    return OpenAI(
        base_url=settings.base_url_for(option),
        api_key=credential,
        timeout=timeout,
        max_retries=0,
        default_headers={
            **auth_headers,
            # Read by llm-emit-token-metric so App Insights can split cost and
            # latency by hosting option in a single chart.
            "x-shs-option": option.key,
        },
    )

def call(settings: Settings, option: Option, prompt: str, max_tokens: int, timeout: float) -> Result:
    model = settings.model_for(option)
    client = build_client(settings, option, timeout)

    started = time.perf_counter()
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.2,
            extra_headers={"x-shs-model": model},
        )
    except APIStatusError as exc:
        return Result(
            option=option,
            ok=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            model=model,
            error=f"HTTP {exc.status_code}: {_short(exc.response.text)}",
        )
    except Exception as exc:  # noqa: BLE001 - surface anything the gateway does
        return Result(
            option=option,
            ok=False,
            latency_ms=(time.perf_counter() - started) * 1000,
            model=model,
            error=f"{type(exc).__name__}: {exc}",
        )

    elapsed_ms = (time.perf_counter() - started) * 1000
    usage = response.usage
    return Result(
        option=option,
        ok=True,
        answer=(response.choices[0].message.content or "").strip(),
        latency_ms=elapsed_ms,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
        model=response.model or model,
    )


def _short(text: str, limit: int = 300) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def print_result(result: Result, verbose: bool) -> None:
    option = result.option
    header = f"[{option.number}] {option.title}"
    print(f"\n{header}")
    print("-" * len(header))
    print(f"  runtime owner : {option.runtime_owner}")
    print(f"  runs on       : {option.location}")
    print(f"  route         : {option.route}/chat/completions")
    print(f"  model         : {result.model}")

    if not result.ok:
        print(f"  status        : FAILED after {result.latency_ms:.0f} ms")
        print(f"  error         : {result.error}")
        if verbose:
            print(f"  hint          : {option.notes}")
        return

    print(f"  latency       : {result.latency_ms:.0f} ms")
    print(f"  tokens        : {result.prompt_tokens} in / {result.completion_tokens} out")
    print(f"\n  {result.answer}")


def print_summary(results: list[Result]) -> None:
    print("\n" + "=" * 78)
    print(f"{'#':<3}{'Option':<28}{'Status':<10}{'Latency':>10}{'Tokens':>10}")
    print("-" * 78)
    for result in results:
        status = "ok" if result.ok else "FAILED"
        latency = f"{result.latency_ms:.0f} ms"
        tokens = str(result.total_tokens) if result.ok else "-"
        print(f"{result.option.number:<3}{result.option.title:<28}{status:<10}{latency:>10}{tokens:>10}")
    print("=" * 78)
    print("Same client, same payload, same gateway. Only the path segment changed.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call any of the four hosting options through the APIM gateway.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(
            f"  {n}  {o.title:<30} {o.location}" for n, o in sorted(OPTIONS.items())
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--option", type=int, choices=sorted(OPTIONS), help="Which hosting option to call.")
    group.add_argument("--all", action="store_true", help="Call all four in sequence and compare.")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="The question to ask.")
    parser.add_argument("--max-tokens", type=int, default=512, help="Response length cap.")
    parser.add_argument("--timeout", type=float, default=180.0, help="Per-request timeout in seconds.")
    parser.add_argument("--verbose", action="store_true", help="Show per-option notes on failure.")
    args = parser.parse_args()

    settings = load_settings()
    targets = list(OPTIONS.values()) if args.all else [get_option(args.option)]

    print(f'Prompt: "{args.prompt}"')
    print(f"Gateway: {settings.apim_gateway_url or '(APIM_GATEWAY_URL not set)'}")
    print(describe_auth(settings))

    results = [call(settings, option, args.prompt, args.max_tokens, args.timeout) for option in targets]
    for result in results:
        print_result(result, args.verbose)

    if len(results) > 1:
        print_summary(results)

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
