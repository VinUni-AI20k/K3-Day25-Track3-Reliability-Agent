from __future__ import annotations

import json
import random
from pathlib import Path

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider

# Flat per-hit saving used when a cache hit avoids a provider call.  The lab
# spec fixes this at 0.001; justify or replace it with a measured average call
# cost in the report rather than treating it as a real accounting figure.
CACHE_SAVING_PER_HIT = 0.001

# Service level objectives every scenario is graded against.  A scenario passes
# only if the reliability layer holds these up despite the injected chaos.
#
# The bars are set from what this architecture can actually deliver, not from
# round numbers.  With the last provider in the chain failing 5% of the time,
# no amount of routing pushes fallback success above ~0.95 on average, so a
# 0.95 bar would fail half the runs by construction; 0.85 leaves roughly three
# standard deviations of headroom at the ~40 fallback attempts a 100-request
# run produces.  Raise these once the chain gains a third provider.
SLO_AVAILABILITY = 0.95
SLO_P95_LATENCY_MS = 2500.0
SLO_FALLBACK_SUCCESS_RATE = 0.85

# Below this many fallback attempts the rate is too noisy to grade: at 9
# attempts a single double-failure already reads as 89%, and the confidence
# interval spans roughly 52-100%.  Judge availability instead of a rate built
# from a handful of samples.
MIN_FALLBACK_SAMPLE = 20


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))
    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
        )
        for p in config.providers
    }
    cache: ResponseCache | SharedRedisCache | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            cache = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
    return ReliabilityGateway(providers, breakers, cache)


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    """Derive recovery time from circuit breaker transition logs.

    One recovery spans a whole outage: from the moment a healthy circuit first
    trips until it closes again, failed probes in between included.  Pairing
    only the last open with the following closed would just re-report
    reset_timeout_seconds and hide how long the provider was really unusable.
    """
    recoveries: list[float] = []
    for breaker in gateway.breakers.values():
        outage_started_at: float | None = None
        for entry in breaker.transition_log:
            timestamp = float(entry["ts"])
            if entry["to"] == "open":
                # Keep the first trip; a failed probe re-opens the circuit but
                # does not start a new outage.
                if outage_started_at is None:
                    outage_started_at = timestamp
            elif entry["to"] == "closed" and outage_started_at is not None:
                recoveries.append((timestamp - outage_started_at) * 1000.0)
                outage_started_at = None

    if not recoveries:
        return None
    return sum(recoveries) / len(recoveries)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    """Run a single named chaos scenario and collect its metrics."""
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()

    for _ in range(config.load_test.requests):
        prompt = random.choice(queries)
        result = gateway.complete(prompt)

        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost

        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += CACHE_SAVING_PER_HIT

        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            # "primary" and "cache_hit:*" both served the user successfully.
            metrics.successful_requests += 1

        # Cache hits report zero latency and would drag the percentiles toward
        # zero, so only real provider calls feed the latency distribution.
        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)

    metrics.circuit_open_count = sum(
        1
        for breaker in gateway.breakers.values()
        for entry in breaker.transition_log
        if entry["to"] == "open"
    )
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    return metrics


def evaluate_scenario(result: RunMetrics) -> bool:
    """Grade one scenario against the SLOs.

    The point of the reliability layer is that user-visible SLOs survive
    whatever failure mode is injected, so every scenario is held to the same
    bar instead of to a hand-written expectation per scenario name.
    """
    if result.total_requests == 0:
        return False
    if result.availability < SLO_AVAILABILITY:
        return False
    if result.percentile(95) > SLO_P95_LATENCY_MS:
        return False
    # Only meaningful once the chain fell back often enough to measure.
    attempted_fallbacks = result.fallback_successes + result.static_fallbacks
    if attempted_fallbacks < MIN_FALLBACK_SAMPLE:
        return True
    return result.fallback_success_rate >= SLO_FALLBACK_SUCCESS_RATE


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined."""
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if evaluate_scenario(metrics) else "fail"}
        return metrics

    combined = RunMetrics()
    recoveries: list[float] = []
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)
        combined.scenarios[scenario.name] = "pass" if evaluate_scenario(result) else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            recoveries.append(result.recovery_time_ms)

    # Average across every scenario that recovered.  Folding pairwise as
    # (running + next) / 2 would weight the last scenario far too heavily.
    combined.recovery_time_ms = sum(recoveries) / len(recoveries) if recoveries else None
    return combined
