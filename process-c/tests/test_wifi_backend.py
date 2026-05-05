"""Tests for :mod:`process_c.wifi_backend`.

Only :class:`FakeWifiBackend` is exercised here. :class:`RealWifiBackend`
is integration-tested implicitly via end-to-end runs on the Pi — unit
testing it would require either a real ``nmcli`` or extensive mocking of
the dynamic import dance, neither of which is worth it.
"""

from __future__ import annotations

from process_c.wifi_backend import ConnectResult, FakeWifiBackend


class TestFakeWifiBackend:
    def test_default_returns_success(self) -> None:
        wifi = FakeWifiBackend()
        result = wifi.apply_wifi_credentials("ssid", "password")
        assert result == ConnectResult.SUCCESS

    def test_records_calls(self) -> None:
        wifi = FakeWifiBackend()
        wifi.apply_wifi_credentials("ssid1", "password1", timeout=30)
        wifi.apply_wifi_credentials("ssid2", "password2")
        assert wifi.calls == [
            ("ssid1", "password1", 30),
            ("ssid2", "password2", 60),
        ]

    def test_can_be_configured_to_return_other_results(self) -> None:
        wifi = FakeWifiBackend(next_result=ConnectResult.TIMEOUT)
        assert wifi.apply_wifi_credentials("s", "p") == ConnectResult.TIMEOUT

        wifi.next_result = ConnectResult.ERROR
        assert wifi.apply_wifi_credentials("s", "p") == ConnectResult.ERROR

    def test_rollback_returns_true_and_counts(self) -> None:
        wifi = FakeWifiBackend()
        assert wifi.rollback_to_ap() is True
        assert wifi.rollback_to_ap() is True
        assert wifi.rollback_calls == 2
