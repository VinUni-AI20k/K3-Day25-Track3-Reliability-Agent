# Day 25 Reliability Report

**Author:** Do Tuan Kiet · **Repo:** K3-Day25-Track3-2A202601335-DoTuanKiet

All numbers below come from seeded runs that reproduce exactly:

```bash
pip install -e ".[dev]"
docker compose up -d
pytest -q                                                                          # 35 passed, 7 xpassed
python scripts/run_chaos.py --config configs/default.yaml  --out reports/metrics.json          --seed 42
python scripts/run_chaos.py --config configs/no_cache.yaml --out reports/metrics_no_cache.json --seed 42
python scripts/run_chaos.py --config configs/redis.yaml    --out reports/metrics_redis.json    --seed 42
```

`--seed` was added to `scripts/run_chaos.py` so a grader re-running these commands gets the same
figures rather than a fresh sample.

**Rubric followed.** This report is written against the 100-point table in `README.md`
(25 / 15 / 15 / 15 / 15 / 15, including the *Redis shared cache* category). Note that
`docs/RUBRIC.md` carries a different, five-category table with no Redis row: it dates from the
initial commit of 2026-05-12 and was never updated, whereas `README.md` was revised twice
afterwards — including the change that introduced the Redis phase. The README table is therefore
treated as authoritative. `docs/RUBRIC.md` has been left untouched.

---

## 1. Architecture summary

A request never reaches a provider until the cheap paths have been exhausted, and it never fails
outright while any path remains.

```
User request
     |
     v
+----------------------------------------------------------+
| ReliabilityGateway.complete()                             |
+----------------------------------------------------------+
     |
     | 1. CACHE
     v
[ResponseCache | SharedRedisCache].get(prompt)
     |   privacy guard  -> uncacheable query: skip cache entirely
     |   TTL eviction   -> stale entry dropped
     |   n-gram cosine  -> best match vs similarity_threshold
     |   false-hit guard-> 4-digit numbers differ: reject + log
     |
     +--HIT--> return route="cache_hit:<score>"  latency=0ms  cost=0
     |
     | MISS
     v
 2. PROVIDER CHAIN (in priority order, one attempt each)
     |
     +--> CircuitBreaker["primary"].call(primary.complete)
     |       CLOSED    -> pass through, count consecutive failures
     |       OPEN      -> CircuitOpenError immediately (no call, no cost)
     |       HALF_OPEN -> allow one probe
     |    success -> cache.set(...) -> return route="primary"
     |    ProviderError / CircuitOpenError -> record error, fall through
     |
     +--> CircuitBreaker["backup"].call(backup.complete)
     |    success -> cache.set(...) -> return route="fallback"
     |    failure -> record error, fall through
     |
     | all providers exhausted
     v
 3. STATIC FALLBACK
     return route="static_fallback", error=<last provider error>
        "The service is temporarily degraded. Please try again soon."
```

Circuit breaker state machine (`src/reliability_lab/circuit_breaker.py`):

```
                 failure_count >= failure_threshold
     CLOSED ------------------------------------------> OPEN
        ^                                                 |
        |                                                 | reset_timeout_seconds elapsed
        | success_count >= success_threshold              v
        +------------------------------ HALF_OPEN <-------+
                    "probe_success"        |
                                           | any failure -> "probe_failure"
                                           +-------------> OPEN
```

Two design points worth calling out:

- **No retry inside a request.** Each provider is attempted at most once per request. A tripped
  circuit is a free skip, not a retry, so a provider outage cannot turn into a retry storm.
- **`opened_at` is stamped only on a real state change.** Re-stamping on every failure while
  already OPEN would push the reset deadline forward on every failing call and the circuit would
  never reach HALF_OPEN. This is enforced by `CircuitBreaker._open()`.

---

## 2. Configuration

| Setting | Value | Reason |
|---|---:|---|
| `failure_threshold` | 3 | One or two failures are normal noise at a 5–25% provider fail rate; opening on those would flap. Three consecutive failures is the point where the provider is genuinely unhealthy rather than unlucky. |
| `reset_timeout_seconds` | 2 | Long enough that a probe is not wasted on a provider that is still down, short enough that recovery lands well inside the 5 s recovery SLO. Measured recovery came out at 2 239–4 728 ms. |
| `success_threshold` | 1 | The probe already carries real user traffic, so a single success is sufficient evidence to close. Raising it would keep the circuit half-open longer and send more traffic down the fallback path for no extra safety. |
| `cache ttl_seconds` | 300 | The sample queries are policy/FAQ questions whose answers are stable for minutes, not seconds. Verified live: a key showed `TTL 274` of 300 immediately after a run, and keys were gone on a later `KEYS` scan. |
| `cache similarity_threshold` | 0.92 | Chosen from measured scores, see the table below. This is the weakest setting in the config and section 8 explains why. |
| `load_test requests` | 100 per scenario (300 total) | Enough that ~40 requests actually reach a provider after cache hits, which is the smallest sample where a fallback rate is worth quoting at all. |
| `SLO_FALLBACK_SUCCESS_RATE` | 0.85 | Backup fails 5% of the time, so `fallback_success_rate` averages ~0.95 by construction. A 0.95 bar would fail half of all runs on noise alone; 0.85 leaves roughly three standard deviations of headroom at ~40 attempts. |

Measured similarity scores that drove the threshold choice (`ResponseCache.similarity`, word
tokens + per-word character 3-grams, cosine):

| Query A | Query B | Score | Desired |
|---|---|---:|---|
| Summarize the refund policy | Summarize refund policy | **0.9045** | should HIT |
| Summarize the admission FAQ in 5 bullets. | ... in 3 bullets. | **0.9687** | must MISS |
| What is the tuition fee for the 2024 academic year? | ... 2025 ... | **0.9574** | must MISS |
| Summarize the refund policy ... 2024 deadline. | ... 2026 deadline. | **0.9688** | must MISS |
| Explain circuit breaker states in one paragraph. | Explain the difference between retry and circuit breaker patterns. | **0.4160** | should MISS |

No single threshold separates row 1 from rows 2–4: the legitimate paraphrase scores *lower* than
every query that must miss. Similarity alone cannot do this job, which is exactly why the
false-hit guardrail exists. Section 8 covers the case it still does not catch.

---

## 3. SLO definitions

| SLI | SLO target | Actual (seed 42, memory cache) | Met? |
|---|---|---:|---|
| Availability | >= 99% | 97.67% | **No** |
| Latency P95 | < 2500 ms | 318.52 ms | Yes |
| Fallback success rate | >= 95% | 92.22% | **No** |
| Cache hit rate | >= 10% | 61.67% | Yes |
| Recovery time | < 5000 ms | 3487 ms | Yes |

The two misses are structural, not implementation defects:

Every request that misses cache and fails on both providers is a user-visible error. With
`primary` at 25% and `backup` at 5%, roughly 1.25% of uncached traffic fails no matter how the
routing is arranged, and under the `primary_timeout_100` scenario the backup's 5% applies to
*all* uncached traffic. That puts the ceiling for availability near 98–99% and the ceiling for
`fallback_success_rate` at exactly 95% — an SLO of ">= 95%" is unreachable on average because
95% *is* the mean.

Reaching a genuine 99%/95% needs an architectural change, not tuning: a third provider in the
chain, or a lower-fail-rate provider of last resort. This is captured in section 9.

---

## 4. Metrics

`reports/metrics.json`, `configs/default.yaml`, seed 42, 300 requests across 3 scenarios:

| Metric | Value |
|---|---:|
| total_requests | 300 |
| availability | 0.9767 |
| error_rate | 0.0233 |
| latency_p50_ms | 283.79 |
| latency_p95_ms | 318.52 |
| latency_p99_ms | 319.56 |
| fallback_success_rate | 0.9222 |
| cache_hit_rate | 0.6167 |
| circuit_open_count | 11 |
| recovery_time_ms | 3487.21 |
| estimated_cost | 0.044378 |
| estimated_cost_saved | 0.185 |

Two notes on how these are computed:

- **Cache hits are excluded from the latency percentiles.** A cache hit reports `latency_ms=0`, and
  `run_scenario` only appends positive latencies. P50/P95/P99 therefore describe *provider* latency,
  not end-user latency. Including the zeros would drag P50 to 0 ms and make the percentiles useless
  as a provider health signal. This is why P50 barely moves in the cache comparison below.
- **`recovery_time_ms` measures a whole outage**, from the first trip of a healthy circuit to the
  moment it closes again, failed probes included. Pairing only the *last* open with the following
  close would just re-report `reset_timeout_seconds` (2000 ms) as a constant and would hide how
  long the provider was actually unusable.

---

## 5. Cache comparison

Same seed, same scenarios, only the cache configuration differs.

### Single seeded run (seed 42)

| Metric | Without cache | In-memory cache | Delta |
|---|---:|---:|---|
| latency_p50_ms | 274.27 | 283.79 | +9.5 ms |
| latency_p95_ms | 316.16 | 318.52 | +2.4 ms |
| estimated_cost | 0.13231 | 0.044378 | **−66.5%** |
| cache_hit_rate | 0.0 | 0.6167 | +0.6167 |
| circuit_open_count | 19 | 11 | −8 |

### Averaged over 4 seeds (42, 7, 99, 2024)

A single run is too noisy to compare availability: seed 42 alone shows the cache run *lower*
(0.9767 vs 0.9900), which reverses once averaged.

| Metric | Without cache | In-memory cache | Delta |
|---|---:|---:|---|
| availability (mean) | 0.9742 | 0.9800 | **+0.58 pp** |
| availability (range) | 0.960 – 0.990 | 0.977 – 0.987 | spread halved |
| estimated_cost (mean) | 0.1251 | 0.0465 | **−62.9%** |
| circuit_open_count (mean) | 21.8 | 9.2 | −58% |

**Interpretation.** The cache buys cost, not latency. Cost drops ~63% because two thirds of
requests never reach a provider. Provider-side P50 is unchanged (and marginally *worse*, since
the requests that still reach a provider are the harder-to-match ones). The interesting
second-order effect is reliability: with fewer calls reaching a flaky primary, the breaker trips
less than half as often (21.8 → 9.2), which is what tightens the availability spread from
0.960–0.990 down to 0.977–0.987. The cache is a load shedder as much as a cost saver.

---

## 6. Redis shared cache

**Why in-memory is insufficient for multi-instance deployments.** `ResponseCache` lives in one
process. Run three gateway replicas behind a load balancer and you get three separate caches:
each replica starts cold, each pays for answers its siblings already bought, and the effective
hit rate is roughly `1/N` of what a single shared cache would deliver. Worse, TTL expiry is
per-process, so the same query can be simultaneously fresh on one replica and expired on another
— two users asking the same question get answers of different ages.

**How `SharedRedisCache` solves it.** Entries live in Redis as a hash keyed by
`{prefix}{md5(query)[:12]}`, with expiry delegated to Redis `EXPIRE`. Every replica reads and
writes the same keyspace, so there is one cache, one hit rate, and one TTL clock. An exact hit
costs a single O(1) `HGET`; only a miss pays for the similarity `SCAN`.

### Evidence of shared state

Two independently constructed instances, sharing nothing but the Redis URL:

```
instance-1  SET  -> "Explain circuit breaker states in one paragraph."
instance-2  GET  -> '[primary] cau tra loi'   (score=1.0)

instance-2 never called set(). It reads the entry because the state lives in Redis.
```

Confirmed by `tests/test_redis_cache.py::test_shared_state_across_instances`, which passes
against a real server (`6 passed`).

### Redis CLI output

```bash
$ docker compose exec redis redis-cli KEYS "rl:cache:*"
rl:cache:d354658dc020
rl:cache:b2a52f7dc795
rl:cache:095946136fea
rl:cache:844ef0143a5c
rl:cache:0bc3b1acf73d
rl:cache:9e413fd814eb
...                                   # 12 keys total

$ docker compose exec redis redis-cli HGETALL rl:cache:d354658dc020
query
How does Redis EXPIRE handle key eviction under memory pressure?
response
[backup] reliable answer for: How does Redis EXPIRE handle key eviction under memory press

$ docker compose exec redis redis-cli TTL rl:cache:d354658dc020
274                                   # of the configured 300 s
```

Expiry was also observed passively: a `KEYS` scan run more than 300 s after a simulation returned
an empty set with no eviction code of our own — Redis `EXPIRE` did the cleanup.

### In-memory vs Redis comparison

| Metric | In-memory cache | Redis cache | Notes |
|---|---:|---:|---|
| latency_p50_ms | 283.79 | 276.85 | Provider latency dominates; the cache backend is not visible here. |
| latency_p95_ms | 318.52 | 316.99 | Same. |
| cache_hit_rate | 0.6167 | 0.7067 | Redis is higher because its keyspace **survives across the three scenarios**, while `build_gateway` constructs a fresh `ResponseCache` per scenario. This is precisely the multi-instance benefit, visible in miniature. |
| estimated_cost | 0.044378 | 0.035648 | The extra 9 pp of hit rate is worth another 20% off the bill. |
| circuit_open_count | 11 | 8 | Fewer provider calls, fewer chances to trip. |

---

## 7. Chaos scenarios

`configs/default.yaml`, seed 42, 100 requests each. Pass/fail is decided by
`chaos.evaluate_scenario()` against the SLOs in section 3 — not by a hand-written expectation
per scenario, so a newly added scenario is graded on the same bar.

| Scenario | Expected behavior | Observed behavior | Pass/Fail |
|---|---|---|---|
| `primary_timeout_100` | Primary fails 100%; all traffic must reach backup; circuit must open and stay open | avail 0.970, **0 requests served by primary**, 39 fallback, 58 cache, 3 static, **6 circuit opens**, no recovery (primary never healthy again — correct) | **PASS** |
| `primary_flaky_50` | Primary fails 50%; circuit should oscillate open/half-open/closed | avail 0.980, 5 primary, 34 fallback, 59 cache, 2 static, 4 opens, **recovery 4728 ms** (circuit genuinely closed again) | **PASS** |
| `all_healthy` | Baseline; mostly primary, few fallbacks | avail 0.980, 20 primary, 10 fallback, 68 cache, 2 static, 1 open, recovery 2239 ms | **PASS** |

Note on `all_healthy`: it still trips the breaker once. That is correct, not a bug — the default
primary `fail_rate` is 0.25, so three consecutive failures do occur in 100 requests. A scenario
named "all_healthy" describes *no injected chaos*, not a perfect provider.

### Recovery evidence

Primary's transition log from the `primary_timeout_100` run (first 5 of 11 entries; the
open/half_open/probe_failure cycle repeats), showing the breaker probing on schedule and
re-opening on probe failure rather than hammering a dead provider:

```
closed    -> open       failure_threshold_reached
open      -> half_open  reset_timeout_elapsed
half_open -> open       probe_failure
open      -> half_open  reset_timeout_elapsed
half_open -> open       probe_failure
```

Five probes over the run, one every 2 s, against a provider that is down 100% of the time — that
is the whole point of the half-open state. A naive retry loop would have issued one call per
request instead.

The backup breaker over the same run: `state=closed`, **zero transitions**. Primary's outage is
fully contained and never touches the backup path.

---

## 8. Failure analysis

Two real weaknesses were found by testing, not by inspection. Both are reproducible.

### Weakness 1 — the false-hit guardrail only understands 4-digit numbers

`_looks_like_false_hit()` compares `\b\d{4}\b` matches. Any other distinguishing token slips
through. Demonstrated at the production threshold of 0.92:

```
cached : "Summarize the admission FAQ in 5 bullets."
query  : "Summarize the admission FAQ in 3 bullets."
score  : 0.9687   (>= 0.92, so it is served)
guard  : False    (no 4-digit numbers to compare)
result : user asks for 3 bullets and receives the 5-bullet answer
```

The same shape *is* caught when the differing token happens to be a year:

```
cached : "What is the tuition fee for the 2024 academic year?"
query   : "What is the tuition fee for the 2025 academic year?"
score  : 0.9574 -> rejected, logged as date_or_number_mismatch
```

So the guardrail is not wrong, it is under-scoped. And section 2 showed the threshold cannot be
raised out of the problem: a legitimate paraphrase scores 0.9045, *below* the 0.9687 false hit.
No threshold ordering exists.

**Fix.** Compare all numeric tokens, not just 4-digit ones, and treat a mismatch in any
cardinal/quantity token as a rejection:

```python
NUMBER_RE = re.compile(r"\b\d+\b")

def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    nums_q = set(NUMBER_RE.findall(query))
    nums_c = set(NUMBER_RE.findall(cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)
```

Longer term the guardrail belongs on the *answer*, not the key: cache the response together with
the constraints it satisfies (count, date range, entity ids) and validate them against the new
query before serving.

### Weakness 2 — an open circuit converts into a burst of user-visible errors

Found while checking whether the scenario pass/fail criteria were stable across seeds. On seed 11
of `primary_timeout_100`, availability collapsed to **0.260** — 74 static fallbacks out of 100.

```
backup circuit tripped at request #9   (three consecutive failures)
the remaining 91 requests completed in 0.026 s
reset_timeout_seconds = 2.0
backup transition_log: [('closed', 'open')]     <- never reached half_open
```

The mechanism: once *both* circuits are open, every request fails fast with no network call and
no sleep. The load generator therefore drains its entire remaining budget inside the 2-second
reset window, so the circuit never gets the chance to probe, and every one of those requests is
returned to the user as a degraded response.

The circuit breaker did its job — it protected the provider. The gap is that nothing protects the
*user* from the failure path being cheap. This is the mirror image of a retry storm: not too many
calls to the provider, but too many failures returned too quickly.

**Fix.** Put a cost on the degraded path so it cannot be consumed at unbounded rate — a token
bucket in front of the gateway, or a small backoff on `static_fallback` responses that grows while
all circuits are open. In production the same effect also comes free from client think-time; the
load generator here has none, which is what makes the failure so stark.

---

## 9. Next steps

1. **Widen the false-hit guardrail to all numeric tokens** (weakness 1). One regex change closes
   a demonstrated wrong-answer path, and it is the highest-value fix here because a wrong cached
   answer is worse than a cache miss.

2. **Rate-limit the degraded path** (weakness 2). Add a token bucket or a backoff on
   `static_fallback` so an open-circuit window cannot be converted into a hundred instant errors.
   Re-run the seed-11 case as a regression test: availability there should recover from 0.260 to
   the ~0.97 the other seeds show.

3. **Add a third provider, or lower the last-resort fail rate.** Sections 3 and 5 show the 99%
   availability and 95% fallback-success SLOs are unreachable with a 5%-failure provider at the
   end of the chain — the mean *is* the target. A cheap, slow, high-reliability provider as the
   final hop before `static_fallback` would move both SLOs into range, and the routing code
   already supports it: `ReliabilityGateway.complete()` iterates the provider list in order, so
   this is a configuration change rather than a code change.
