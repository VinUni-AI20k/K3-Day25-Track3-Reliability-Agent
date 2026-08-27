from __future__ import annotations

from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError, ProviderResponse

STATIC_FALLBACK_TEXT = "The service is temporarily degraded. Please try again soon."


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers."""

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache

    def complete(self, prompt: str) -> GatewayResponse:
        """Return a reliable response or a static fallback.

        Pipeline: cache lookup → provider chain guarded by circuit breakers →
        static degraded message.  A provider that fails or whose circuit is open
        never blocks the chain; the next provider is tried immediately, so a
        single request costs at most one attempt per provider (no retry storm).
        """
        # 1. Cache — a hit costs no latency and no money.
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0,
                )

        # 2. Provider chain, in configured priority order.
        last_error: str | None = None
        for index, provider in enumerate(self.providers):
            breaker = self.breakers[provider.name]
            try:
                response: ProviderResponse = breaker.call(provider.complete, prompt)
            except (ProviderError, CircuitOpenError) as exc:
                # Circuit-open is a fast, free skip; a provider error already cost
                # one attempt.  Either way, move on rather than retrying here.
                last_error = f"{provider.name}: {exc}"
                continue

            if self.cache is not None:
                self.cache.set(prompt, response.text, {"provider": provider.name})

            return GatewayResponse(
                text=response.text,
                route="primary" if index == 0 else "fallback",
                provider=provider.name,
                cache_hit=False,
                latency_ms=response.latency_ms,
                estimated_cost=response.estimated_cost,
            )

        # 3. Every provider is down or tripped — degrade instead of raising.
        return GatewayResponse(
            text=STATIC_FALLBACK_TEXT,
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error,
        )
