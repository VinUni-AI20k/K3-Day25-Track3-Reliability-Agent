# Day 25 Reliability Report

Author: Nguyen Thanh Tung (ngtung2004@gmail.com)
Repro: `pip install -e ".[dev]" && docker compose up -d && make test && make run-chaos && make report`

## 1. Architecture summary

The gateway routes every request through a cache layer, then a per-provider
circuit-breaker fallback chain, then a static degraded response.

```
User Request
    |
    v
[ReliabilityGateway.complete(prompt)]
    |
    v
[Cache check]  ResponseCache / SharedRedisCache
    |  hit  ---> return GatewayResponse(route="cache_hit:<score>", cache_hit=True, cost=0)
    |  miss
    v
[CircuitBreaker: primary] --allow?--> primary.complete(prompt)
    |  OPEN (fail fast) / ProviderError            |
    |                                              success ---> cache.set(...) ; route="primary"
    v
[CircuitBreaker: backup]  --allow?--> backup.complete(prompt)
    |  OPEN (fail fast) / ProviderError            |
    |                                              success ---> cache.set(...) ; route="fallback"
    v
[Static fallback]  route="static_fallback", error=<last provider error>
    "The service is temporarily degraded. Please try again soon."
```

Circuit breaker is a 3-state machine:

- **CLOSED** — calls pass through, consecutive failures counted. At
  `failure_count >= failure_threshold` it opens with reason
  `failure_threshold_reached`.
- **OPEN** — calls fail fast with `CircuitOpenError`. After
  `reset_timeout_seconds` (measured with `time.monotonic()`) the next
  `allow_request()` moves it to HALF_OPEN.
- **HALF_OPEN** — a single probe is allowed. A success path that reaches
  `success_threshold` closes the circuit (`probe_success`); any failure
  re-opens it immediately with the distinct reason `probe_failure`.

The HALF_OPEN failure and the threshold failure are handled with `if/elif`
so the transition log carries the correct cause for each.

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| failure_threshold | 3 | Tolerates transient blips; 3 consecutive failures on a provider with 0.25 base fail-rate is a strong signal the provider is unhealthy, not unlucky. |
| reset_timeout_seconds | 2 | Long enough to let a struggling provider recover, short enough that recovery time stays well under the 5 s SLO. Observed recovery ~2.3 s. |
| success_threshold | 1 | One good probe is enough to close in this workload; the FakeLLMProvider has no slow warm-up. Raise for real providers with cold caches. |
| cache TTL | 300 s | FAQ / policy answers are stable for minutes; 5 min bounds staleness while still absorbing bursts of repeated queries. |
| similarity_threshold | 0.92 | Char-3-gram + word cosine. Tested 0.85 — produced false hits between "refund policy 2024" and "refund policy 2026" style queries. 0.92 keeps only near-duplicate phrasings; the 4-digit-number guard catches the rest. |
| load_test requests | 100 per scenario (300 total) | Enough samples for stable P95/P99 without making each run longer than a coffee break. |
| backend | memory (default) / redis | Redis used for the multi-instance evidence in section 6. |

## 3. SLO definitions

| SLI | SLO target | Actual value | Met? |
|---|---|---:|---|
| Availability | >= 99% | 99.33% | Yes |
| Latency P95 | < 2500 ms | 325.9 ms | Yes |
| Fallback success rate | >= 95% | 97.06% | Yes |
| Cache hit rate | >= 10% | 61.0% | Yes |
| Recovery time | < 5000 ms | 2297 ms | Yes |

Values from `reports/metrics.json` (memory backend, default config).

**Run-to-run variance:** failures and query selection are randomised, so each
`make run-chaos` differs. Across 4 runs, availability stayed 0.98–0.99,
fallback success rate 0.94–0.97, and recovery time 2.2–2.5 s. One unlucky run
dipped to 0.88 availability when both providers' breakers opened at once during
`primary_timeout_100` — the motivation for the shared breaker state proposed in
section 8.

## 4. Metrics

Source: `reports/metrics.json` — `make run-chaos` (3 scenarios x 100 requests).

| Metric | Value |
|---|---:|
| total_requests | 300 |
| availability | 0.9933 |
| error_rate | 0.0067 |
| latency_p50_ms | 276.74 |
| latency_p95_ms | 325.87 |
| latency_p99_ms | 363.0 |
| fallback_success_rate | 0.9706 |
| cache_hit_rate | 0.61 |
| estimated_cost | 0.051328 |
| estimated_cost_saved | 0.183 |
| circuit_open_count | 7 |
| recovery_time_ms | 2297.45 |

Also exported as `reports/metrics.csv` (single row, scenarios flattened to
`scenario_<name>` columns).

## 5. Cache comparison

Same config, `cache.enabled` toggled. `reports/metrics_nocache.json` vs
`reports/metrics.json`.

| Metric | Without cache | With cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 275.48 | 276.74 | +1.26 ms |
| latency_p95_ms | 315.16 | 325.87 | +10.7 ms |
| estimated_cost | 0.128384 | 0.051328 | -0.077056 (-60%) |
| cache_hit_rate | 0.0 | 0.61 | +0.61 |
| circuit_open_count | 21 | 7 | -14 |
| availability | 0.98 | 0.9933 | +0.013 |

The cache's biggest wins are **cost** (~60% cheaper) and **stability**:
serving 61% of traffic from cache means far less load on the providers, so the
primary breaker trips 21 -> 7 times and availability rises. Latency is
essentially flat here — even slightly higher at P95 — because the
FakeLLMProvider is fast (~250 ms) so a cache lookup saves little; against a
real multi-second LLM the P50/P95 improvement from a 61% hit rate would be
large.

## 6. Redis shared cache

- **Why in-memory cache is insufficient for multi-instance deployments:** each
  gateway process holds its own `ResponseCache` list. With N instances behind a
  load balancer, a query cached on instance A is a cold miss on instances B..N,
  so the effective hit rate falls roughly to `1/N` and every instance pays to
  warm its own copy. Cache state is also lost on every deploy / restart.
- **How `SharedRedisCache` solves this:** responses are stored in a single
  Redis instance as a hash (`{query, response}`) under key
  `rl:cache:<md5(query)[:12]>` with a Redis `EXPIRE` TTL (no manual eviction).
  Any instance can serve any other instance's cached answer. Exact lookups are
  one `HGET`; semantic lookups `SCAN` the prefix and score each candidate with
  the same n-gram cosine `similarity()`. Privacy and false-hit guardrails run
  identically to the in-memory path.

### Evidence of shared state

`tests/test_redis_cache.py::test_shared_state_across_instances` — two
independent `SharedRedisCache` objects (separate connections), one writes, the
other reads the same value:

```
$ pytest tests/test_redis_cache.py -q
......                                                                   [100%]
6 passed
```

### Redis CLI output

```bash
$ docker compose exec -T redis redis-cli KEYS "rl:cache:*"
rl:cache:844ef0143a5c
rl:cache:fff10da1c72c
rl:cache:3dab98c0e49e
rl:cache:9e413fd814eb
rl:cache:734852f3cf4a
rl:cache:b2a52f7dc795
rl:cache:0bc3b1acf73d
rl:cache:d354658dc020
rl:cache:dacb2b833659
rl:cache:095946136fea
...
$ docker compose exec -T redis redis-cli DBSIZE
(integer) 12
```

### In-memory vs Redis latency comparison

`reports/metrics.json` vs `reports/metrics_redis.json` (chaos run, cache on).

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 276.74 | 285.74 | Redis adds a network round-trip per lookup + a SCAN on misses. |
| latency_p95_ms | 325.87 | 319.88 | Difference small on localhost; would grow with a remote Redis. |
| cache_hit_rate | 0.61 | 0.6933 | Comparable; run-to-run randomness dominates the gap. |
| estimated_cost | 0.051328 | 0.032878 | Both far below the no-cache 0.128. |

Redis trades a few ms of lookup latency for cache state that survives restarts
and is shared across every instance — the right trade for production.

## 7. Chaos scenarios

Pass criteria: scenario "passes" if it produced at least one successful
response and the observed behaviour matches the design intent.

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| primary_timeout_100 | Primary fails 100%; breaker opens after 3 failures, all subsequent traffic fails fast to backup; fallback_success_rate high | Primary breaker opens (`failure_threshold_reached`), traffic served by backup via `route="fallback"`; combined fallback_success_rate 0.98 | Pass |
| primary_flaky_50 | Primary fails ~50%; breaker oscillates OPEN <-> HALF_OPEN <-> CLOSED; mix of `primary` and `fallback` routes | `circuit_open_count` > 0 with matching close transitions; recovery_time_ms ~2.4 s per cycle; both routes observed | Pass |
| all_healthy | Both providers healthy; nearly all traffic via `primary`; few or no circuit opens | Almost all responses `route="primary"` (or `cache_hit`); no static fallbacks | Pass |
| no_cache (custom, section 5) | Disabling cache raises provider load; more breaker trips, higher cost | circuit_open_count 6 -> 21, estimated_cost 0.043 -> 0.128, hit rate 0 | Pass |

## 8. Failure analysis

**What could still go wrong:** the circuit-breaker state lives in process
memory. In a multi-instance deployment each gateway learns a provider is down
independently — instance B keeps hammering a dead provider for its own 3
failures even though instance A already opened its breaker. During a real
outage this means N x `failure_threshold` wasted calls and N separate recovery
probes hitting the provider the instant it comes back (a small thundering
herd).

**What I would change before production:** move the breaker counters and
`opened_at` into Redis (`INCR` on failure with a short `EXPIRE`, a shared
`opened_at` key), so all instances share one view of provider health and only
one probe is sent during HALF_OPEN (guard the probe with `SET NX`). The cache
already proves the shared-Redis pattern works here.

Secondary weakness: `_is_uncacheable()` is a regex allowlist of keywords
(`balance`, `ssn`, ...). It will miss PII phrased differently. A production
system should classify cacheability upstream (per-endpoint policy or a proper
PII detector) rather than pattern-matching the prompt string.

## 9. Next steps

1. **Redis-backed circuit state** — shared failure counters + single HALF_OPEN
   probe via `SET NX`, so breaker decisions are consistent across instances.
2. **Cost-aware routing** — track cumulative spend; past 80% of budget route to
   the cheaper `backup` model, at 100% serve cache-only / static fallback.
3. **Concurrency + quality SLO** — drive `run_scenario` with a
   `ThreadPoolExecutor` to measure P95 under real concurrent load, and add a
   response-quality SLI (e.g. reject empty / truncated provider responses
   before caching them).
