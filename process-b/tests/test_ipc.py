"""Tests for :mod:`process_b.ipc`.

We bind a real loopback UDP socket and assert the bytes on the wire. UDP is
cheap and synchronous; mocking ``socket.socket`` would be more code than the
real thing.
"""

from __future__ import annotations

import json
import socket

import pytest

from process_b import ipc


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class TestSendWake:
    def test_sends_expected_json_payload(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(1.0)
            _, port = receiver.getsockname()

            ipc.send_wake("127.0.0.1", port)

            data, _addr = receiver.recvfrom(4096)

        decoded = json.loads(data.decode("utf-8"))
        assert decoded == {"type": "wake"}

    def test_unreachable_destination_does_not_raise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Sending to an unbound port on loopback typically does NOT raise
        # on UDP (the OS reports it asynchronously via ICMP). To force an
        # actual OSError, we send to an address with an invalid host.
        # Either outcome — silent success or logged warning — is acceptable;
        # the contract is "never raises."
        with caplog.at_level("WARNING", logger="process_b.ipc"):
            ipc.send_wake("invalid_host_should_fail.local", 65535)
        # No assertion on log content — the OS may resolve it differently.
        # The point is: we returned without raising.

    def test_send_wake_uses_ipv4_udp(self) -> None:
        # Receive on a v4 socket; if send_wake creates a v6 socket it won't
        # arrive, and the recv timeout will trip.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
            receiver.bind(("127.0.0.1", 0))
            receiver.settimeout(1.0)
            _, port = receiver.getsockname()

            ipc.send_wake("127.0.0.1", port)

            data, _ = receiver.recvfrom(4096)
            assert data
