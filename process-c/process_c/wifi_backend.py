"""Pluggable WiFi-switching backend.

Process C delegates the actual ``nmcli`` work to Anjinsan's
``orchestration/wifi_connector.py``. That file is a script, not a package,
which makes a direct import awkward. We hide the import behind a small
:class:`WifiBackend` protocol so:

- Tests can substitute :class:`FakeWifiBackend` with no monkeypatching of
  module globals or sys.path.
- The awkward ``sys.path`` insert lives in exactly one place
  (:class:`RealWifiBackend`) and is only triggered when the real backend
  is constructed.

Why a Protocol rather than abstract base class
-----------------------------------------------
Structural typing (``Protocol``) means tests can pass a plain stub object
without inheriting from anything. Lower friction.
"""

from __future__ import annotations

import logging
import os
import sys
from enum import Enum, auto
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class ConnectResult(Enum):
    """Mirrors ``orchestration.wifi_connector.ConnectResult``.

    Defined locally so callers don't need to know whether the underlying
    enum comes from the real module or a fake.
    """

    SUCCESS = auto()
    TIMEOUT = auto()
    ERROR = auto()


class WifiBackend(Protocol):
    """Anything that can attempt a WiFi switch and (best-effort) roll back."""

    def apply_wifi_credentials(
        self, ssid: str, password: str, timeout: int = 60
    ) -> ConnectResult:
        ...

    def rollback_to_ap(self) -> bool:
        ...


# ──────────────────────────────────────────────────────────────────────────────
# Real backend: imports orchestration/wifi_connector.py at construction time
# ──────────────────────────────────────────────────────────────────────────────


class RealWifiBackend:
    """Production backend that delegates to ``orchestration/wifi_connector``.

    The import is deferred to ``__init__`` (rather than module load) so that
    importing :mod:`process_c.wifi_backend` is always cheap and side-effect
    free, even on machines without ``nmcli`` (developer laptops, CI).
    """

    def __init__(self, orchestration_dir: str | os.PathLike[str] | None = None) -> None:
        self._wc = self._import_wifi_connector(orchestration_dir)

    @staticmethod
    def _import_wifi_connector(
        orchestration_dir: str | os.PathLike[str] | None,
    ) -> object:
        # Default candidates, in order of plausibility:
        # 1. /opt/shelfaware/orchestration  (production install path)
        # 2. <repo>/orchestration           (dev mode: ../orchestration)
        candidates: list[Path] = []
        if orchestration_dir is not None:
            candidates.append(Path(orchestration_dir))
        candidates.append(Path("/opt/shelfaware/orchestration"))
        # Repo layout: process-c/process_c/wifi_backend.py → ../../orchestration
        candidates.append(Path(__file__).resolve().parent.parent.parent / "orchestration")

        for d in candidates:
            if (d / "wifi_connector.py").exists():
                if str(d) not in sys.path:
                    sys.path.insert(0, str(d))
                import wifi_connector  # noqa: PLC0415  (intentional deferred import)

                logger.info(
                    "loaded real wifi_connector",
                    extra={"orchestration_dir": str(d)},
                )
                return wifi_connector

        raise RuntimeError(
            "wifi_connector.py not found in any of: "
            + ", ".join(str(c) for c in candidates)
            + ". Set orchestration_dir explicitly, or use FakeWifiBackend."
        )

    def apply_wifi_credentials(
        self, ssid: str, password: str, timeout: int = 60
    ) -> ConnectResult:
        # The wifi_connector module exposes its own ConnectResult enum;
        # translate by name so we stay decoupled.
        result = self._wc.apply_wifi_credentials(ssid, password, timeout=timeout)  # type: ignore[attr-defined]
        return ConnectResult[result.name]

    def rollback_to_ap(self) -> bool:
        ok: bool = self._wc.rollback_to_ap()  # type: ignore[attr-defined]
        return ok


# ──────────────────────────────────────────────────────────────────────────────
# Fake backend: for tests and SHELFAWARE_DEV=1 mode
# ──────────────────────────────────────────────────────────────────────────────


class FakeWifiBackend:
    """In-memory fake — never touches the network.

    Default behaviour: every ``apply_wifi_credentials`` call returns
    :attr:`ConnectResult.SUCCESS`. Tests can override
    :attr:`next_result` to drive failure paths.
    """

    def __init__(self, *, next_result: ConnectResult = ConnectResult.SUCCESS) -> None:
        self.next_result = next_result
        self.calls: list[tuple[str, str, int]] = []
        self.rollback_calls: int = 0

    def apply_wifi_credentials(
        self, ssid: str, password: str, timeout: int = 60
    ) -> ConnectResult:
        self.calls.append((ssid, password, timeout))
        logger.info(
            "[fake wifi] apply_wifi_credentials called",
            extra={"ssid": ssid, "timeout": timeout, "result": self.next_result.name},
        )
        return self.next_result

    def rollback_to_ap(self) -> bool:
        self.rollback_calls += 1
        logger.info("[fake wifi] rollback_to_ap called")
        return True
