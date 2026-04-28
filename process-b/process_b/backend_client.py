"""HTTP client for the ShelfAware backend (Cloudflare Worker).

Thin wrapper around :class:`httpx.AsyncClient` that:

- Injects ``Authorization: Bearer <PI_API_KEY>`` and ``X-Shelf-Id`` headers.
- Validates request/response payloads with pydantic v2 models.
- Maps HTTP outcomes to two domain exceptions:

  * :class:`TransientBackendError` — 5xx, network errors, timeouts. Caller
    (the drainer) should back off and retry. Rows are not touched.
  * :class:`PermanentBackendError` — 4xx and malformed-but-200 responses.
    Caller should bump ``reject_count`` (drainer) or log + drop (poller).

The client itself does not retry. The drainer owns the retry loop and the
backoff state; keeping retry policy out of the transport makes both layers
simpler to reason about and test.
"""

from __future__ import annotations

from collections.abc import Sequence
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class BackendError(Exception):
    """Base for HTTP-layer failures surfaced to the drainer/poller."""

    def __init__(self, message: str, *, status: int | None = None, body: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class TransientBackendError(BackendError):
    """5xx, network error, or timeout. Caller should back off and retry."""


class PermanentBackendError(BackendError):
    """4xx or malformed-but-200 response. Caller should not retry."""


class ReadingPayload(BaseModel):
    """One reading as sent on the wire to ``POST /telemetry``."""

    model_config = ConfigDict(extra="forbid")

    reading_id: str
    scale_index: int
    est_grams: float


class TelemetryRequest(BaseModel):
    """Request envelope for ``POST /telemetry``."""

    model_config = ConfigDict(extra="forbid")

    shelf_id: str
    sampled_at: str
    metadata: dict[str, object] | None = None
    readings: list[ReadingPayload]


class SkippedReading(BaseModel):
    """One entry of the ``skipped`` array in a 2xx telemetry response."""

    model_config = ConfigDict(extra="allow")

    reading_id: str
    reason: str


class TelemetryResponse(BaseModel):
    """Parsed 2xx body from ``POST /telemetry``."""

    model_config = ConfigDict(extra="allow")

    accepted: int
    skipped: list[SkippedReading] = Field(default_factory=list)


class Command(BaseModel):
    """One command from ``GET /commands``."""

    model_config = ConfigDict(extra="allow")

    id: str
    command: str
    payload: object | None = None
    expires_at: str


class CommandsResponse(BaseModel):
    """Parsed 2xx body from ``GET /commands``."""

    model_config = ConfigDict(extra="allow")

    commands: list[Command] = Field(default_factory=list)


_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)


class BackendClient:
    """Async HTTP client for the ShelfAware Cloudflare Worker.

    Use as an async context manager so the underlying ``httpx.AsyncClient`` is
    closed on shutdown::

        async with BackendClient(base_url=..., api_key=...) as client:
            await client.post_telemetry(...)
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout: httpx.Timeout | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout or _DEFAULT_TIMEOUT,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def post_telemetry(
        self,
        *,
        shelf_id: str,
        sampled_at: str,
        metadata: dict[str, object] | None,
        readings: Sequence[ReadingPayload],
    ) -> TelemetryResponse:
        """POST a batch of readings for a single shelf. Returns the parsed body.

        Raises
        ------
        TransientBackendError
            5xx, network error, or timeout. Drainer should back off and retry
            without touching the rows.
        PermanentBackendError
            4xx (whole-batch reject — bump ``reject_count``) or
            malformed-but-200 (treat as 4xx so the row eventually quarantines
            instead of looping forever).
        """
        body = TelemetryRequest(
            shelf_id=shelf_id,
            sampled_at=sampled_at,
            metadata=metadata,
            readings=list(readings),
        ).model_dump(mode="json")

        response = await self._request(
            "POST",
            "/telemetry",
            shelf_id=shelf_id,
            json=body,
        )
        return _parse(response, TelemetryResponse)

    async def get_commands(self, *, shelf_id: str) -> list[Command]:
        """GET pending commands for one shelf. Returns the list (possibly empty).

        Raises the same domain exceptions as :meth:`post_telemetry`.
        """
        response = await self._request("GET", "/commands", shelf_id=shelf_id)
        parsed = _parse(response, CommandsResponse)
        return parsed.commands

    async def _request(
        self,
        method: str,
        path: str,
        *,
        shelf_id: str,
        json: object | None = None,
    ) -> httpx.Response:
        """Issue one HTTP request. Maps transport errors to TransientBackendError."""
        url = f"{self._base_url}{path}"
        headers = {"X-Shelf-Id": shelf_id}
        try:
            return await self._client.request(method, url, headers=headers, json=json)
        except httpx.TimeoutException as exc:
            raise TransientBackendError(f"{method} {path}: timeout") from exc
        except httpx.NetworkError as exc:
            raise TransientBackendError(f"{method} {path}: network error: {exc}") from exc
        except httpx.RemoteProtocolError as exc:
            raise TransientBackendError(f"{method} {path}: protocol error: {exc}") from exc


def _parse(response: httpx.Response, model: type[BaseModel]) -> "BaseModel":  # noqa: F821
    """Map an httpx response to a parsed model or a domain exception.

    - 2xx with a valid body → returns the parsed model.
    - 2xx with a malformed body → ``PermanentBackendError``. Treating this as
      transient would loop forever; treating it as permanent at least lets a
      row quarantine after 3 attempts, which is a finite escape hatch.
    - 4xx → ``PermanentBackendError``.
    - 5xx → ``TransientBackendError``.
    """
    status = response.status_code
    text_preview = response.text[:500] if response.text else ""

    if 200 <= status < 300:
        try:
            return model.model_validate(response.json())
        except ValidationError as exc:
            raise PermanentBackendError(
                f"malformed response body: {exc}", status=status, body=text_preview
            ) from exc
        except ValueError as exc:
            raise PermanentBackendError(
                f"non-JSON response body: {exc}", status=status, body=text_preview
            ) from exc

    if 400 <= status < 500:
        raise PermanentBackendError(
            f"backend rejected request: HTTP {status}", status=status, body=text_preview
        )

    raise TransientBackendError(
        f"backend transient failure: HTTP {status}", status=status, body=text_preview
    )
