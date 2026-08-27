from __future__ import annotations

import hashlib
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)

NGRAM_SIZE = 3
WORD_RE = re.compile(r"\w+")


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


def _tokenize(text: str) -> list[str]:
    """Split text into word tokens plus per-word character n-grams.

    N-grams are taken inside each word rather than across the whole string so
    that filler words ("the") shift the score far less than they would if
    whitespace were part of the grams.
    """
    words = WORD_RE.findall(text.lower())
    tokens: list[str] = list(words)
    for word in words:
        for i in range(len(word) - NGRAM_SIZE + 1):
            tokens.append(word[i : i + NGRAM_SIZE])
    return tokens


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """In-memory semantic cache with privacy and false-hit guardrails.

    Lookups match on n-gram cosine similarity rather than exact keys, so two
    guardrails sit in front of the result: privacy-sensitive queries are never
    stored or served, and a match whose 4-digit numbers differ from the query
    (years, IDs) is rejected as a false hit.  For production, replace with
    SharedRedisCache.
    """

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response by semantic similarity."""
        if _is_uncacheable(query):
            return None, 0.0

        self._evict_expired()

        best: CacheEntry | None = None
        best_score = 0.0
        for entry in self._entries:
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best = entry

        if best is None or best_score < self.similarity_threshold:
            return None, best_score

        if _looks_like_false_hit(query, best.key):
            self.false_hit_log.append(
                {
                    "reason": "date_or_number_mismatch",
                    "query": query,
                    "cached_key": best.key,
                    "score": best_score,
                }
            )
            return None, best_score

        return best.value, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response, skipping privacy-sensitive queries entirely."""
        if _is_uncacheable(query):
            return
        # Replace any existing entry for the same key so repeated misses on the
        # same query cannot grow the scan list without bound.
        self._entries = [e for e in self._entries if e.key != query]
        self._entries.append(
            CacheEntry(
                key=query,
                value=value,
                created_at=time.time(),
                metadata=metadata or {},
            )
        )

    def _evict_expired(self) -> None:
        """Drop entries older than ttl_seconds."""
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Cosine similarity over word tokens + character n-grams."""
        if a == b:
            return 1.0

        vec_a: Counter[str] = Counter(_tokenize(a))
        vec_b: Counter[str] = Counter(_tokenize(b))
        if not vec_a or not vec_b:
            return 0.0

        # Iterate the smaller vector; tokens missing from the other contribute 0.
        if len(vec_a) > len(vec_b):
            vec_a, vec_b = vec_b, vec_a
        dot = sum(count * vec_b[token] for token, count in vec_a.items())
        if dot == 0:
            return 0.0

        norm_a = math.sqrt(sum(c * c for c in vec_a.values()))
        norm_b = math.sqrt(sum(c * c for c in vec_b.values()))
        return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    ResponseCache keeps entries in one process, so a second gateway instance
    starts cold and pays for answers the first one already bought.  Holding the
    entries in Redis gives every instance one cache, one hit rate, and one TTL.

    Data model:
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    Similarity lookup SCANs the keys under self.prefix, HGETs each entry's
    "query" field, and scores locally with ResponseCache.similarity(), so the
    same guardrails as the in-memory cache apply to shared entries.
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:  # noqa: BLE001 - a health probe must survive any client error
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        Exact hits are a single O(1) HGET.  Only a miss pays for the similarity
        scan, which reads every key under the prefix, so scan cost grows with
        the size of the cache; a vector index would replace it in production.
        """
        if _is_uncacheable(query):
            return None, 0.0

        exact_key = f"{self.prefix}{self._query_hash(query)}"
        exact = self._redis.hget(exact_key, "response")
        if exact is not None:
            return str(exact), 1.0

        best_response: str | None = None
        best_query = ""
        best_score = 0.0
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            cached_query = self._redis.hget(key, "query")
            if cached_query is None:
                continue
            score = ResponseCache.similarity(query, str(cached_query))
            if score > best_score:
                cached_response = self._redis.hget(key, "response")
                if cached_response is None:
                    # Expired between SCAN and HGET; nothing to serve.
                    continue
                best_score = score
                best_query = str(cached_query)
                best_response = str(cached_response)

        if best_response is None or best_score < self.similarity_threshold:
            return None, best_score

        if _looks_like_false_hit(query, best_query):
            self.false_hit_log.append(
                {
                    "reason": "date_or_number_mismatch",
                    "query": query,
                    "cached_key": best_query,
                    "score": best_score,
                }
            )
            return None, best_score

        return best_response, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis under a TTL.

        Expiry is delegated to Redis EXPIRE, so unlike the in-memory cache there
        is no eviction pass to run and every instance sees the same deadline.
        ``metadata`` is accepted for interface parity with ResponseCache but is
        not persisted; the hash keeps only the fields lookup needs.
        """
        if _is_uncacheable(query):
            return
        key = f"{self.prefix}{self._query_hash(query)}"
        self._redis.hset(key, mapping={"query": query, "response": value})
        self._redis.expire(key, self.ttl_seconds)

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
