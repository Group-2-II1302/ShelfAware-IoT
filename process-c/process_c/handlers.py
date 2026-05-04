"""HTTP handlers for the three Process C endpoints.

The handlers are intentionally thin: validation lives in pydantic models,
state mutation lives in :mod:`process_c.device_state`, and the actual
WiFi work lives in :mod:`process_c.provisioner`. This keeps each handler
under ~25 lines and makes them trivially unit-testable with a stub
WifiBackend.

Endpoints
---------
GET  /health
    Used by the frontend to detect "I'm talking to a Pi in setup mode."
    Always 200 OK; no auth.

POST /provision
    Body: ``{"ssid", "password", "user_id"}``. On success persists
    device.json (with a freshly generated shelf_id) and schedules the
    background WiFi switch. Returns 202 immediately.

POST /reset
    Wipes device.json. Disabled by default — only registered when
    ``cfg.allow_reset_endpoint`` is true. Useful for dev / re-provisioning.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from process_c import __version__, device_state
from process_c.config import Config
from process_c.provisioner import schedule_provision
from process_c.wifi_backend import WifiBackend

logger = logging.getLogger(__name__)


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
        # Refuse if already provisioned — caller must hit /reset first.
        # Frontend should check /health first, but defend anyway.
        if device_state.is_provisioned(cfg.device_file_path):
            return _json_response(
                409,
                {
                    "error": "already_provisioned",
                    "detail": "Device already has a device.json. POST /reset first.",
                },
            )

        try:
            raw = await request.json()
        except ValueError:
            return _json_response(
                400, {"error": "validation", "detail": "Body must be valid JSON."}
            )

        try:
            req = ProvisionRequest.model_validate(raw)
        except ValidationError as exc:
            return _json_response(
                400,
                {
                    "error": "validation",
                    "detail": exc.errors(include_url=False, include_input=False),
                },
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
            return _json_response(
                500,
                {
                    "error": "persistence",
                    "detail": f"Could not write device file: {exc}",
                },
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
            },
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
