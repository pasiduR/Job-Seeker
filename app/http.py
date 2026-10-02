"""Shared synchronous HTTP client with bounded retries and source rate limits."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import monotonic, sleep
from typing import Any, Protocol


MAX_ATTEMPTS = 3
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes | None = None


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


class HttpRequestError(RuntimeError):
    pass


class HttpStatusError(HttpRequestError):
    def __init__(self, response: HttpResponse) -> None:
        super().__init__(f"HTTP request failed with status {response.status}")
        self.response = response


Transport = Callable[[HttpRequest, float], HttpResponse]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


class RateLimiter(Protocol):
    def wait(self, source: str) -> None: ...


class SourceRateLimiter:
    """Thread-safe minimum request intervals keyed by configured source name."""

    def __init__(
        self,
        minimum_intervals: Mapping[str, float],
        *,
        sleeper: Sleeper = sleep,
        clock: Clock = monotonic,
    ) -> None:
        self._minimum_intervals = dict(minimum_intervals)
        self._sleep = sleeper
        self._clock = clock
        self._last_request: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, source: str) -> None:
        minimum_interval = self._minimum_intervals.get(source, 0.0)
        if minimum_interval <= 0:
            return

        with self._lock:
            now = self._clock()
            previous = self._last_request.get(source)
            if previous is not None:
                remaining = minimum_interval - (now - previous)
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request[source] = self._clock()


class HttpClient:
    def __init__(
        self,
        *,
        timeout_seconds: float,
        source_minimum_intervals: Mapping[str, float],
        transport: Transport | None = None,
        sleeper: Sleeper = sleep,
        clock: Clock = monotonic,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = timeout_seconds
        self._transport = transport or _urllib_transport
        self._sleep = sleeper
        self._rate_limiter = rate_limiter or SourceRateLimiter(
            source_minimum_intervals,
            sleeper=sleeper,
            clock=clock,
        )

    def request(
        self,
        source: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        request = HttpRequest(
            method=method.upper(),
            url=url,
            headers=headers or {},
            body=body,
        )
        last_error: OSError | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._rate_limiter.wait(source)
            try:
                response = self._transport(request, self._timeout_seconds)
            except OSError as exc:
                last_error = exc
                if attempt == MAX_ATTEMPTS:
                    break
                self._sleep(_backoff_seconds(attempt, None))
                continue

            if response.status in _RETRYABLE_STATUSES and attempt < MAX_ATTEMPTS:
                self._sleep(_backoff_seconds(attempt, response))
                continue
            if response.status >= 400:
                raise HttpStatusError(response)
            return response

        raise HttpRequestError(
            f"HTTP request failed after {MAX_ATTEMPTS} attempts"
        ) from last_error


def _backoff_seconds(attempt: int, response: HttpResponse | None) -> float:
    if response is not None:
        retry_after = next(
            (
                value
                for key, value in response.headers.items()
                if key.lower() == "retry-after"
            ),
            None,
        )
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
    return float(2 ** (attempt - 1))


def _urllib_transport(request: HttpRequest, timeout_seconds: float) -> HttpResponse:
    native_request = urllib.request.Request(
        request.url,
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with urllib.request.urlopen(native_request, timeout=timeout_seconds) as response:
            return HttpResponse(
                status=response.status,
                headers=dict(response.headers.items()),
                body=response.read(),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(
            status=exc.code,
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=exc.read(),
        )
