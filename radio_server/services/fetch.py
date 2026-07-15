"""HTTP JSON fetch seam for network-backed voice services (weather, astronomy).

Some services read a LAN HTTP endpoint (e.g. a weather station) rather than only the clock. `Fetcher`
is the one-method seam — mirroring `TtsEngine` / `DtmfDecoder` — so tests drive those services with
canned JSON and no network, and the real network call is isolated in one place.

`UrllibFetcher` is the real implementation over the standard library (no new dependency). It is the
single network-dependent code path (marked, not unit-asserted, like `PiperTts._synthesize_raw`): a
weather/astronomy handler runs synchronously inside `controller.step`, so the GET is bounded by a short
timeout and any failure is turned into a `FetchError` that the service catches and speaks a graceful
"unavailable" line for — a dead station must never crash the controller loop.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

#: Default per-request timeout (seconds). Short on purpose: the fetch blocks the controller/RX loop
#: (ADR 0031), so a dead LAN station must fail fast rather than freeze received audio.
DEFAULT_FETCH_TIMEOUT = 3.0


class FetchError(RuntimeError):
    """A network or parse failure fetching JSON. Services catch this and speak an "unavailable" line
    rather than letting it propagate into the controller loop."""


@runtime_checkable
class Fetcher(Protocol):
    """Fetches and parses a JSON object from a URL. One method, so a stub stands in for tests."""

    def fetch_json(self, url: str) -> Mapping[str, Any]: ...


class UrllibFetcher:
    """Real JSON GET over stdlib ``urllib`` (implements `Fetcher`).

    The only network-dependent path here (guardrail 1 — verify against the real endpoint). Wraps every
    failure mode — connection, timeout, non-JSON, non-object — as `FetchError`, so callers have exactly
    one exception to handle.
    """

    def __init__(self, timeout: float = DEFAULT_FETCH_TIMEOUT) -> None:
        self._timeout = timeout

    def fetch_json(self, url: str) -> Mapping[str, Any]:
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310 (LAN URL)
                raw = resp.read()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise FetchError(f"could not fetch {url}: {exc}") from exc
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FetchError(f"invalid JSON from {url}: {exc}") from exc
        if not isinstance(data, Mapping):
            raise FetchError(f"expected a JSON object from {url}, got {type(data).__name__}")
        return data


class StubFetcher:
    """A canned `Fetcher` for tests — returns a preset payload (no network), or raises `FetchError`.

    ``payloads`` maps an exact URL to its JSON object; ``default`` answers any other URL. With
    ``fail=True`` every call raises, to exercise the services' graceful-degradation path. Records each
    requested URL in :attr:`calls`.
    """

    def __init__(
        self,
        payloads: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        default: Mapping[str, Any] | None = None,
        fail: bool = False,
    ) -> None:
        self._payloads = dict(payloads or {})
        self._default = default
        self._fail = fail
        self.calls: list[str] = []

    def fetch_json(self, url: str) -> Mapping[str, Any]:
        self.calls.append(url)
        if self._fail:
            raise FetchError(f"stub configured to fail for {url}")
        if url in self._payloads:
            return self._payloads[url]
        if self._default is not None:
            return self._default
        raise FetchError(f"no stub payload for {url}")
