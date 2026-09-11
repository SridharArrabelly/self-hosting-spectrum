"""Measure the four options against each other.

    uv run python shared/benchmark.py
    uv run python shared/benchmark.py --options 2 3 --runs 5
    uv run python shared/benchmark.py --csv results.csv

The interesting number is not "which is fastest". It is the shape of the
trade-off: dedicated GPUs give you low, flat latency at a high fixed cost;
serverless partner endpoints give you good latency at zero fixed cost but
per-token billing; a small VM is cheap and slow; a laptop is free and slowest.
This script puts numbers on that so the README's cost table is not hand-waving.

Every request goes through the same APIM gateway, so the comparison includes
identical gateway overhead and is therefore fair.

Cost columns come from shared/pricing.py and are estimates for illustration -
they are not a billing source of truth.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from shared.auth import describe as describe_auth  # noqa: E402
from shared.auth import gateway_credential  # noqa: E402
from shared.config import OPTIONS, Option, Settings, get_option, load_settings  # noqa: E402

PROMPT = "List three benefits of running an LLM on your own infrastructure. Be brief."

# Illustrative rates. Managed compute and the VM bill by the hour whether or not
# anyone is talking to them, which is the whole point of showing them here.
HOURLY_COST: dict[int, float] = {
    1: 7.91,   # H100 80GB managed compute (A100 80GB is 3.67)
    2: 0.00,   # Fireworks: per token only
    3: 0.19,   # Standard_D4s_v5
    4: 0.00,   # your own hardware
}
PER_MTOK_COST: dict[int, tuple[float, float]] = {
    1: (0.0, 0.0),
    2: (0.06, 0.22),  # FW-Nemotron-Lightning-3.5-30B-A3B
    3: (0.0, 0.0),
    4: (0.0, 0.0),
}


@dataclass
class Result:
    option: Option
    latencies: list[float] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return len(self.latencies)

    @property
    def mean(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else float("nan")

    @property
    def p95(self) -> float:
        if not self.latencies:
            return float("nan")
        if len(self.latencies) < 3:
            return max(self.latencies)
        return statistics.quantiles(self.latencies, n=20)[-1]

    @property
    def tokens_per_second(self) -> float:
        total = sum(self.latencies)
        return self.completion_tokens / total if total and self.completion_tokens else float("nan")

    def cost_per_1k_calls(self) -> float:
        """Rough $ for 1000 calls at this measured shape."""
        if not self.latencies:
            return float("nan")
        rate_in, rate_out = PER_MTOK_COST[self.option.number]
        token_cost = (self.prompt_tokens * rate_in + self.completion_tokens * rate_out) / 1_000_000
        per_call_tokens = token_cost / self.ok
        hourly = HOURLY_COST[self.option.number]
        per_call_compute = hourly * self.mean / 3600
        return (per_call_tokens + per_call_compute) * 1000


def one_call(client: httpx.Client, settings: Settings, option: Option) -> tuple[float, dict]:
    url = f"{settings.base_url_for(option)}/chat/completions"
    body = {
        "model": settings.model_for(option),
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 120,
        "temperature": 0.0,
    }
    started = time.perf_counter()
    response = client.post(url, json=body)
    elapsed = time.perf_counter() - started
    response.raise_for_status()
    return elapsed, response.json()


def measure(settings: Settings, option: Option, runs: int, warmup: bool, pause: float = 0.0) -> Result:
    result = Result(option=option)
    credential, auth_headers = gateway_credential(settings)
    headers = {
        **auth_headers,
        "Authorization": f"Bearer {credential}",
        "Content-Type": "application/json",
    }
    label = f"Option {option.number} ({option.key})"
    with httpx.Client(timeout=600.0, headers=headers) as client:
        if warmup:
            print(f"  {label}: warmup...", end="", flush=True)
            try:
                one_call(client, settings, option)
                print(" ok")
            except Exception as exc:  # noqa: BLE001
                print(f" failed ({type(exc).__name__})")
                result.errors.append(f"warmup: {exc}")
                return result

        for i in range(runs):
            # Per-token backends throttle on requests-per-minute as well as
            # tokens: Fireworks returns 429 for back-to-back calls. A short
            # pause measures steady-state latency rather than throttle recovery.
            if pause and i:
                time.sleep(pause)
            print(f"  {label}: run {i + 1}/{runs}...", end="", flush=True)
            try:
                elapsed, payload = one_call(client, settings, option)
            except httpx.HTTPStatusError as exc:
                print(f" HTTP {exc.response.status_code}")
                result.errors.append(f"HTTP {exc.response.status_code}: {exc.response.text[:160]}")
                continue
            except Exception as exc:  # noqa: BLE001
                print(f" {type(exc).__name__}")
                result.errors.append(str(exc))
                continue

            result.latencies.append(elapsed)
            usage = payload.get("usage") or {}
            result.prompt_tokens += usage.get("prompt_tokens", 0)
            result.completion_tokens += usage.get("completion_tokens", 0)
            print(f" {elapsed:.2f}s")
    return result


def render(results: list[Result]) -> None:
    print()
    header = (f"{'#':<3}{'Option':<26}{'ok':<5}{'mean s':>9}{'p95 s':>9}"
              f"{'tok/s':>9}{'out tok':>10}{'$/1k calls':>13}")
    print(header)
    print("-" * len(header))
    for r in results:
        if not r.latencies:
            print(f"{r.option.number:<3}{r.option.key:<26}{'0':<5}{'--':>9}{'--':>9}"
                  f"{'--':>9}{'--':>10}{'--':>13}")
            continue
        print(f"{r.option.number:<3}{r.option.key:<26}{r.ok:<5}"
              f"{r.mean:>9.2f}{r.p95:>9.2f}{r.tokens_per_second:>9.1f}"
              f"{r.completion_tokens:>10}{r.cost_per_1k_calls():>13.4f}")

    failed = [r for r in results if r.errors]
    if failed:
        print("\nErrors:")
        for r in failed:
            print(f"  option {r.option.number}: {r.errors[0]}")

    print("\nFixed cost while idle (this is the part people forget):")
    for r in results:
        hourly = HOURLY_COST[r.option.number]
        note = "free" if hourly == 0 else f"${hourly:.2f}/hr  = ${hourly * 24:.2f}/day"
        print(f"  option {r.option.number} {r.option.key:<20} {note}")


def write_csv(results: list[Result], path: Path) -> None:
    import csv

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["option", "key", "title", "runs_ok", "mean_s", "p95_s",
                         "tokens_per_s", "prompt_tokens", "completion_tokens",
                         "usd_per_1k_calls", "idle_usd_per_hour"])
        for r in results:
            writer.writerow([
                r.option.number, r.option.key, r.option.title, r.ok,
                f"{r.mean:.4f}" if r.latencies else "",
                f"{r.p95:.4f}" if r.latencies else "",
                f"{r.tokens_per_second:.2f}" if r.latencies else "",
                r.prompt_tokens, r.completion_tokens,
                f"{r.cost_per_1k_calls():.6f}" if r.latencies else "",
                HOURLY_COST[r.option.number],
            ])
    print(f"\nWrote {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark the four hosting options through APIM.")
    parser.add_argument("--options", type=int, nargs="+", choices=sorted(OPTIONS),
                        default=sorted(OPTIONS))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--pause", type=float, default=0.0,
                        help="Seconds between runs. Raise it if a backend throttles back-to-back "
                             "calls, so the numbers measure latency rather than throttle recovery.")
    parser.add_argument("--no-warmup", action="store_true",
                        help="Skip the discarded first call. Cold starts will skew the mean.")
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    settings = load_settings()

    print(f"Gateway: {settings.apim_gateway_url}")
    print(describe_auth(settings))
    print(f"Prompt : {PROMPT}\n")

    results = [measure(settings, get_option(n), args.runs, not args.no_warmup, args.pause)
               for n in args.options]
    render(results)

    if args.csv:
        write_csv(results, args.csv)
    if args.json:
        args.json.write_text(json.dumps([
            {"option": r.option.number, "key": r.option.key, "runs_ok": r.ok,
             "mean_s": r.mean if r.latencies else None,
             "p95_s": r.p95 if r.latencies else None,
             "completion_tokens": r.completion_tokens,
             "prompt_tokens": r.prompt_tokens,
             "usd_per_1k_calls": r.cost_per_1k_calls() if r.latencies else None,
             "idle_usd_per_hour": HOURLY_COST[r.option.number],
             "errors": r.errors}
            for r in results
        ], indent=2), encoding="utf-8")
        print(f"Wrote {args.json}")

    return 0 if any(r.latencies for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
