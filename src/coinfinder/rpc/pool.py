"""Fault-tolerant JSON-RPC client for free public endpoints.

Free endpoints fail constantly and in boring ways: 429s, random 5xx, silent
truncation, and per-provider caps on ``eth_getLogs`` ranges. The pool handles
all four so that callers can pretend they are talking to one reliable node.

Design notes
------------
* Every endpoint gets its own token bucket, so one slow provider cannot stall
  the others.
* Failures put an endpoint into exponential cooldown rather than removing it;
  public endpoints usually recover within a minute.
* ``RangeTooLarge`` is raised as a distinct error because the correct response
  is to retry with a smaller block window, not to switch provider.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
import orjson
import structlog

log = structlog.get_logger(__name__)

# Substrings providers use to say "your getLogs window is too wide". There is
# no standard error code for this, so matching on text is unavoidable.
_RANGE_ERROR_MARKERS = (
    "block range",
    "range is too large",
    "more than 10000 results",
    "query returned more than",
    "limit exceeded",
    "response size exceeded",
    "too many results",
    "exceed maximum block range",
    "logs matched by query exceeds",
)

_RATE_LIMIT_MARKERS = ("rate limit", "too many requests", "429", "capacity", "throttl")


class RpcError(RuntimeError):
    """A JSON-RPC level error that is not worth retrying on another node."""


class RangeTooLarge(RpcError):
    """The requested block range exceeded the provider's cap."""


class AllEndpointsFailed(RuntimeError):
    """Every endpoint in the pool refused or failed the request."""


@dataclass(slots=True)
class _Bucket:
    """Simple token bucket. ``rate`` is refills per second."""

    rate: float
    capacity: float
    tokens: float = field(default=0.0)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def take(self) -> float:
        """Consume one token, returning how long the caller should wait first."""
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return 0.0
        return (1.0 - self.tokens) / self.rate


@dataclass(slots=True)
class _Endpoint:
    url: str
    bucket: _Bucket
    failures: int = 0
    cooldown_until: float = 0.0
    total_calls: int = 0
    total_errors: int = 0

    def available_in(self) -> float:
        return max(0.0, self.cooldown_until - time.monotonic())

    def penalise(self, seconds: float | None = None) -> None:
        self.failures += 1
        self.total_errors += 1
        backoff = seconds if seconds is not None else min(60.0, 2.0**self.failures)
        # Jitter stops every worker from retrying the same node in lockstep.
        self.cooldown_until = time.monotonic() + backoff * (0.75 + random.random() * 0.5)

    def reward(self) -> None:
        self.failures = 0
        self.cooldown_until = 0.0


class RpcPool:
    """Round-robin JSON-RPC pool over several endpoints for one chain."""

    def __init__(
        self,
        urls: list[str],
        *,
        requests_per_second: float = 4.0,
        timeout_seconds: float = 20.0,
        max_attempts: int = 4,
    ) -> None:
        if not urls:
            raise ValueError("RpcPool needs at least one URL")
        self._endpoints = [
            _Endpoint(url=u, bucket=_Bucket(rate=requests_per_second, capacity=requests_per_second))
            for u in urls
        ]
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._max_attempts = max_attempts
        self._session: aiohttp.ClientSession | None = None
        self._cursor = 0
        self._id = 0

    async def __aenter__(self) -> RpcPool:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                json_serialize=lambda o: orjson.dumps(o).decode(),
                # trust_env keeps the session working behind an HTTPS_PROXY.
                trust_env=True,
            )

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _next_endpoint(self) -> tuple[_Endpoint, float]:
        """Pick the endpoint that can serve soonest."""
        n = len(self._endpoints)
        best: _Endpoint | None = None
        best_wait = float("inf")
        for i in range(n):
            ep = self._endpoints[(self._cursor + i) % n]
            wait = max(ep.available_in(), ep.bucket.take() if ep.available_in() == 0 else 0.0)
            if wait < best_wait:
                best, best_wait = ep, wait
            if wait == 0.0:
                self._cursor = (self._cursor + i + 1) % n
                break
        assert best is not None
        return best, best_wait

    def stats(self) -> list[dict[str, Any]]:
        return [
            {
                "url": ep.url,
                "calls": ep.total_calls,
                "errors": ep.total_errors,
                "cooldown_s": round(ep.available_in(), 1),
            }
            for ep in self._endpoints
        ]

    async def _post(self, endpoint: _Endpoint, payload: Any) -> Any:
        await self.start()
        assert self._session is not None
        async with self._session.post(endpoint.url, json=payload) as resp:
            if resp.status == 429:
                retry_after = resp.headers.get("Retry-After")
                endpoint.penalise(float(retry_after) if retry_after else None)
                raise RpcError(f"429 from {endpoint.url}")
            if resp.status >= 500:
                endpoint.penalise()
                raise RpcError(f"{resp.status} from {endpoint.url}")
            resp.raise_for_status()
            return orjson.loads(await resp.read())

    @staticmethod
    def _classify(message: str) -> type[RpcError]:
        low = message.lower()
        if any(m in low for m in _RANGE_ERROR_MARKERS):
            return RangeTooLarge
        return RpcError

    async def call(self, method: str, params: list[Any] | None = None) -> Any:
        """Issue one JSON-RPC call, retrying across endpoints on transport errors."""
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or []}
        last: Exception | None = None

        for _ in range(self._max_attempts):
            endpoint, wait = self._next_endpoint()
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
            try:
                body = await self._post(endpoint, payload)
            except (TimeoutError, aiohttp.ClientError, RpcError) as exc:
                if not isinstance(exc, RpcError):
                    endpoint.penalise()
                last = exc
                continue

            endpoint.total_calls += 1
            if isinstance(body, dict) and body.get("error"):
                msg = str(body["error"].get("message", body["error"]))
                kind = self._classify(msg)
                if kind is RangeTooLarge:
                    # Caller-fixable: do not burn other endpoints on it.
                    raise RangeTooLarge(msg)
                if any(m in msg.lower() for m in _RATE_LIMIT_MARKERS):
                    endpoint.penalise()
                    last = RpcError(msg)
                    continue
                endpoint.reward()
                raise RpcError(f"{method}: {msg}")

            endpoint.reward()
            return body.get("result") if isinstance(body, dict) else body

        raise AllEndpointsFailed(f"{method} failed on all endpoints: {last}")

    async def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Issue several calls in one HTTP round-trip.

        Some free providers silently ignore batch requests, so a non-list
        response falls back to sequential calls rather than failing.
        """
        if not calls:
            return []
        payload = []
        base_id = self._id + 1
        for offset, (method, params) in enumerate(calls):
            payload.append(
                {"jsonrpc": "2.0", "id": base_id + offset, "method": method, "params": params}
            )
        self._id = base_id + len(calls) - 1

        for _ in range(self._max_attempts):
            endpoint, wait = self._next_endpoint()
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
            try:
                body = await self._post(endpoint, payload)
            except (TimeoutError, aiohttp.ClientError, RpcError):
                endpoint.penalise()
                continue

            endpoint.total_calls += 1
            if not isinstance(body, list):
                log.warning("rpc.batch_unsupported", url=endpoint.url)
                return [await self.call(m, p) for m, p in calls]

            endpoint.reward()
            by_id = {item.get("id"): item for item in body}
            out: list[Any] = []
            for offset in range(len(calls)):
                item = by_id.get(base_id + offset) or {}
                out.append(None if item.get("error") else item.get("result"))
            return out

        raise AllEndpointsFailed("batch failed on all endpoints")

    # -- convenience wrappers ------------------------------------------

    async def block_number(self) -> int:
        return int(await self.call("eth_blockNumber"), 16)

    async def get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        address: str | list[str] | None = None,
        topics: list[Any] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"fromBlock": hex(from_block), "toBlock": hex(to_block)}
        if address:
            params["address"] = address
        if topics:
            params["topics"] = topics
        return await self.call("eth_getLogs", [params]) or []

    async def get_block_timestamps(self, block_numbers: list[int]) -> dict[int, int]:
        """Fetch timestamps for several blocks in one batch."""
        uniq = sorted(set(block_numbers))
        results = await self.batch([("eth_getBlockByNumber", [hex(b), False]) for b in uniq])
        out: dict[int, int] = {}
        for block, res in zip(uniq, results, strict=True):
            if isinstance(res, dict) and res.get("timestamp"):
                out[block] = int(res["timestamp"], 16)
        return out
