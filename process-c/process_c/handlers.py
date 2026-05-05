"""HTTP handlers for Process C's endpoints.

The handlers are intentionally thin: validation lives in pydantic models,
state mutation lives in :mod:`process_c.device_state`, and the actual
WiFi work lives in :mod:`process_c.provisioner`. This keeps each handler
under ~25 lines and makes them trivially unit-testable with a stub
WifiBackend.

Endpoints
---------
GET  /
    Captive-portal landing page. Serves the credential form HTML so the
    user can provision the shelf from a browser without needing a native
    app. Same-origin POST to /provision avoids the mixed-content blocking
    that hits HTTPS web apps trying to talk to http://192.168.4.1.

GET  /health
    Used by the frontend to detect "I'm talking to a Pi in setup mode."
    Always 200 OK; no auth.

POST /provision
    Body: ``{"ssid", "password", "user_id"}`` as JSON, OR the same fields
    as ``application/x-www-form-urlencoded`` (so the HTML form works even
    if JavaScript is blocked). On success persists device.json (with a
    freshly generated shelf_id) and schedules the background WiFi switch.
    Returns 202 (JSON) or 303 redirect (form) on success.

POST /reset
    Wipes device.json. Disabled by default — only registered when
    ``cfg.allow_reset_endpoint`` is true. Useful for dev / re-provisioning.

GET  /generate_204, /hotspot-detect.html, /ncsi.txt
    Captive-portal probes used by Android, iOS, and Windows respectively
    to detect "is this a real internet connection?". We return responses
    that *deliberately* fail the probes so the OS pops its captive-portal
    browser and loads ``/`` automatically. Without these, the user has to
    manually navigate to http://192.168.4.1.
"""

from __future__ import annotations

import json
import logging
import uuid
from importlib import resources
from typing import Any

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from process_c import __version__, device_state
from process_c.config import Config
from process_c.provisioner import schedule_provision
from process_c.wifi_backend import WifiBackend

logger = logging.getLogger(__name__)


# Loaded once at import time. The HTML is ~5KB and never changes at
# runtime, so caching it avoids re-reading the file on every request.
_PROVISION_HTML: str | None = None


def _load_provision_html() -> str:
    """Read the captive-portal HTML once and cache it.

    Uses ``importlib.resources`` so it works whether the package is
    installed editable, as a wheel, or via a zipapp.
    """
    global _PROVISION_HTML
    if _PROVISION_HTML is None:
        _PROVISION_HTML = (
            resources.files("process_c.templates")
            .joinpath("provision.html")
            .read_text(encoding="utf-8")
        )
    return _PROVISION_HTML


# ──────────────────────────────────────────────────────────────────────────────
# Request / response models
# ──────────────────────────────────────────────────────────────────────────────


class ProvisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ssid: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=8, max_length=63)  # WPA2 PSK bounds
    user_id: str = Field(min_length=1)


# ──────────────────────────────────────────────────────────────────────────────
# Handler factories
# ──────────────────────────────────────────────────────────────────────────────
#
# We build the handlers as closures over (cfg, wifi) rather than reaching
# into request.app for them. Easier to test (just call the returned
# coroutine with a fake Request) and avoids stringly-typed app["wifi"] keys.


def make_index_handler(cfg: Config) -> Any:
    """Captive-portal landing page.

    Always serves the form, even when ``already_provisioned`` is true —
    the form's POST will then 409 and the JS surfaces a friendly error.
    Serving the form unconditionally keeps the URL stable so OS captive-
    portal browsers don't get confused by redirects mid-setup.
    """

    async def index(request: web.Request) -> web.Response:
        return web.Response(
            body=_load_provision_html(),
            content_type="text/html",
            charset="utf-8",
        )

    return index


def make_health_handler(cfg: Config) -> Any:
    async def health(request: web.Request) -> web.Response:
        provisioned = device_state.is_provisioned(cfg.device_file_path)
        return _json_response(
            200,
            {
                "device": "shelfaware-pi",
                "version": __version__,
                "ready_for_provision": True,
                "already_provisioned": provisioned,
            },
        )

    return health


def make_provision_handler(cfg: Config, wifi: WifiBackend) -> Any:
    async def provision(request: web.Request) -> web.Response:
        # Detect whether this came from the HTML form (no JS path) or an
        # API client. Drives whether errors are HTML/redirects or JSON.
        wants_html = _client_wants_html(request)

        if device_state.is_provisioned(cfg.device_file_path):
            return _provision_error(
                wants_html,
                status=409,
                code="already_provisioned",
                detail="Device already has a device.json. POST /reset first.",
            )

        try:
            raw = await _read_provision_payload(request)
        except _PayloadError as exc:
            return _provision_error(
                wants_html, status=400, code="validation", detail=exc.message
            )

        try:
            req = ProvisionRequest.model_validate(raw)
        except ValidationError as exc:
            return _provision_error(
                wants_html,
                status=400,
                code="validation",
                detail=exc.errors(include_url=False, include_input=False),
            )

        # Generate a fresh shelf_id locally so the Pi has a stable identity
        # even if the backend is unreachable. Process B will register this
        # with the backend at its next startup.
        shelf_id = str(uuid.uuid4())
        state = device_state.make_state(user_id=req.user_id, shelf_id=shelf_id)

        try:
            device_state.write(cfg.device_file_path, state)
        except OSError as exc:
            logger.exception("failed to write device.json")
            return _provision_error(
                wants_html,
                status=500,
                code="persistence",
                detail=f"Could not write device file: {exc}",
            )

        # Spawn the WiFi switch in the background; do NOT await it.
        # apply_wifi_credentials blocks for up to ~60s and once it succeeds
        # the AP is gone, so the HTTP response can't reach the phone anyway.
        schedule_provision(wifi, req.ssid, req.password)

        logger.info(
            "provision accepted",
            extra={
                "user_id": req.user_id,
                "shelf_id": shelf_id,
                "ssid": req.ssid,
                "via": "html" if wants_html else "json",
            },
        )

        if wants_html:
            # No-JS browsers land here — render a tiny success page rather
            # than a JSON blob the user wouldn't understand.
            return _html_response(
                _success_html(shelf_id),
                status=202,
            )

        return _json_response(
            202,
            {
                "status": "accepted",
                "shelf_id": shelf_id,
            },
        )

    return provision


def make_reset_handler(cfg: Config) -> Any:
    async def reset(request: web.Request) -> web.Response:
        deleted = device_state.delete(cfg.device_file_path)
        return _json_response(
            200,
            {"status": "reset", "deleted": deleted},
        )

    return reset


# ──────────────────────────────────────────────────────────────────────────────
# Captive-portal probe handlers
# ──────────────────────────────────────────────────────────────────────────────
#
# Each major OS hits a known URL on a known host shortly after joining a
# new WiFi network to detect "is there real internet here?". The expected
# upstream responses are:
#
#   Android: GET http://connectivitycheck.gstatic.com/generate_204
#       → empty body, HTTP 204. Anything else => "captive portal".
#   iOS:     GET http://captive.apple.com/hotspot-detect.html
#       → body must contain the literal string "Success".
#       Anything else => "captive portal".
#   Windows: GET http://www.msftncsi.com/ncsi.txt
#       → body must be exactly "Microsoft NCSI".
#       Anything else => "captive portal".
#
# When AP-side DNS hijacking redirects these hostnames to the Pi (set up
# separately in shelfaware_network_setup.sh), the responses below
# deliberately fail each OS's "I have internet" check, which causes the
# OS to pop its captive-portal browser pointed at our /.
#
# Without DNS hijacking these endpoints are dead code — the OS probes
# never reach the Pi at all. They're cheap to keep around for the day
# DNS hijacking lands.


async def captive_probe_android(request: web.Request) -> web.Response:
    """Android probe: expects 204 empty. We return 200 with a body to fail."""
    return web.Response(text="captive", content_type="text/plain", status=200)


async def captive_probe_apple(request: web.Request) -> web.Response:
    """iOS probe: expects body to contain `Success`. Return something else."""
    return web.Response(
        text="<HTML><HEAD></HEAD><BODY>shelfaware</BODY></HTML>",
        content_type="text/html",
        status=200,
    )


async def captive_probe_windows(request: web.Request) -> web.Response:
    """Windows probe: expects exact `Microsoft NCSI`. Return something else."""
    return web.Response(text="captive", content_type="text/plain", status=200)


# ──────────────────────────────────────────────────────────────────────────────
# CORS
# ──────────────────────────────────────────────────────────────────────────────


@web.middleware
async def cors_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    """Permissive CORS — captive-portal contexts have unpredictable origins.

    Handles OPTIONS preflights inline so they don't reach handlers that
    only support GET/POST.
    """
    if request.method == "OPTIONS":
        return _cors_response(web.Response(status=204))

    response = await handler(request)
    return _cors_response(response)


def _cors_response(response: web.StreamResponse) -> web.StreamResponse:
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _json_response(status: int, payload: dict[str, Any]) -> web.Response:
    return web.json_response(payload, status=status)


def _html_response(body: str, status: int = 200) -> web.Response:
    return web.Response(body=body, status=status, content_type="text/html", charset="utf-8")


class _PayloadError(Exception):
    """Raised when the request body can't be parsed into a dict."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _client_wants_html(request: web.Request) -> bool:
    """Decide whether to render HTML/redirect or return JSON.

    Heuristic, in priority order:
    1. Form-encoded body (Content-Type: application/x-www-form-urlencoded)
       => definitely a no-JS browser submission, return HTML.
    2. Accept header explicitly mentions text/html (and not application/json)
       => browser-driven request without JS, return HTML.
    3. Otherwise => JSON.

    Native apps and the JS in our own form both POST JSON with
    ``Accept: */*`` (or unset), which falls into case 3.
    """
    content_type = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        return True

    accept = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept:
        return False
    if "text/html" in accept:
        return True
    return False


async def _read_provision_payload(request: web.Request) -> dict[str, Any]:
    """Read the body as either JSON or form-encoded into a plain dict.

    Raises :class:`_PayloadError` with a human-readable message if the
    body isn't parseable. Down-stream pydantic validation handles
    field-level errors.
    """
    content_type = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    if content_type == "application/x-www-form-urlencoded":
        # aiohttp's .post() returns a MultiDictProxy; flatten to a regular
        # dict (last value wins for duplicates — same shape pydantic expects).
        form = await request.post()
        return {key: form.get(key) for key in form.keys()}

    # Default to JSON. Some browsers may POST with no Content-Type header
    # at all (rare); JSON parsing will fail clearly in that case.
    body = await request.read()
    if not body:
        raise _PayloadError("Body must be valid JSON.")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _PayloadError(f"Body must be valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise _PayloadError("JSON body must be an object, not a list or scalar.")
    return parsed


def _provision_error(
    wants_html: bool, *, status: int, code: str, detail: Any
) -> web.Response:
    """Render a provisioning error in the format the client expects."""
    if wants_html:
        return _html_response(_error_html(status, code, detail), status=status)
    return _json_response(status, {"error": code, "detail": detail})


# Shared style for the no-JS success / error pages. Mirrors the main
# captive-portal template's light-theme + monospace look so the post-
# submit experience feels continuous with the form.
_PAGE_STYLE = (
    "body{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,"
    "'Liberation Mono','Courier New',monospace;background:#f3f1ee;color:#1f1a17;"
    "margin:0;min-height:100vh;display:flex;align-items:center;"
    "justify-content:center;padding:1.5rem;-webkit-font-smoothing:antialiased}"
    ".box{max-width:440px;width:100%;background:#ffffff;border:1px solid #d9d4ce;"
    "padding:2rem;border-radius:12px}"
    "h1{margin:0 0 0.5rem;font-size:1.5rem;font-weight:700;letter-spacing:-0.01em}"
    "p{margin:0 0 0.85rem;line-height:1.5}"
    ".muted{color:#6b635c;font-size:0.875rem}"
    "code{background:#f3f1ee;border:1px solid #d9d4ce;padding:0.6rem 0.75rem;"
    "border-radius:6px;display:block;margin:0.5rem 0;font-size:0.8rem;"
    "text-align:left;overflow-wrap:break-word;white-space:pre-wrap}"
    "a{color:#1f1a17}"
)


def _success_html(shelf_id: str) -> str:
    """Tiny success page for no-JS browser submissions.

    Inline-styled so it works even with no network. Doesn't try to
    render the shelf_id prominently — the user doesn't need to see it,
    the orchestrator and Process B handle the rest.
    """
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ShelfAware setup</title>"
        f"<style>{_PAGE_STYLE}</style>"
        "<div class='box'>"
        "<h1>Setup accepted</h1>"
        "<p>Your shelf is now connecting to your home WiFi.</p>"
        "<p>Reconnect your phone to your home WiFi to finish setup.</p>"
        f"<p class='muted'>Shelf ID: {shelf_id}</p>"
        "</div>"
    )


def _error_html(status: int, code: str, detail: Any) -> str:
    """Tiny error page for no-JS browser submissions."""
    detail_str = detail if isinstance(detail, str) else json.dumps(detail)
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>ShelfAware setup error</title>"
        f"<style>{_PAGE_STYLE}</style>"
        "<div class='box'>"
        f"<h1>Setup failed ({status})</h1>"
        f"<p class='muted'>{code}</p>"
        f"<code>{detail_str}</code>"
        "<p><a href='/'>Try again</a></p>"
        "</div>"
    )
