"""Tests for :mod:`process_c.provisioner`."""

from __future__ import annotations

import asyncio

import pytest

from process_c.provisioner import perform_wifi_switch, schedule_provision
from process_c.wifi_backend import ConnectResult, FakeWifiBackend


class _RaisingBackend(FakeWifiBackend):
    def apply_wifi_credentials(
        self, ssid: str, password: str, timeout: int = 60
    ) -> ConnectResult:
        raise RuntimeError("nmcli exploded")


class TestPerformWifiSwitch:
    async def test_success_passes_args_and_returns_result(self) -> None:
        wifi = FakeWifiBackend(next_result=ConnectResult.SUCCESS)
        result = await perform_wifi_switch(wifi, "MyWifi", "hunter22", timeout=30)
        assert result == ConnectResult.SUCCESS
        assert wifi.calls == [("MyWifi", "hunter22", 30)]

    async def test_timeout_result_is_returned(self) -> None:
        wifi = FakeWifiBackend(next_result=ConnectResult.TIMEOUT)
        result = await perform_wifi_switch(wifi, "ssid", "password")
        assert result == ConnectResult.TIMEOUT

    async def test_exception_triggers_rollback_and_returns_error(self) -> None:
        wifi = _RaisingBackend()
        result = await perform_wifi_switch(wifi, "ssid", "password")
        assert result == ConnectResult.ERROR
        assert wifi.rollback_calls == 1

    async def test_on_success_hook_called_only_when_success(self) -> None:
        called = asyncio.Event()

        async def hook() -> None:
            called.set()

        wifi = FakeWifiBackend(next_result=ConnectResult.SUCCESS)
        await perform_wifi_switch(wifi, "s", "password", on_success=hook)
        assert called.is_set()

        called.clear()
        wifi = FakeWifiBackend(next_result=ConnectResult.TIMEOUT)
        await perform_wifi_switch(wifi, "s", "password", on_success=hook)
        assert not called.is_set()

    async def test_on_success_hook_exception_is_swallowed(self) -> None:
        async def bad_hook() -> None:
            raise RuntimeError("boom")

        wifi = FakeWifiBackend(next_result=ConnectResult.SUCCESS)
        # Should not raise:
        result = await perform_wifi_switch(wifi, "s", "password", on_success=bad_hook)
        assert result == ConnectResult.SUCCESS


class TestScheduleProvision:
    async def test_returns_awaitable_task(self) -> None:
        wifi = FakeWifiBackend()
        task = schedule_provision(wifi, "ssid", "password")
        assert isinstance(task, asyncio.Task)
        result = await task
        assert result == ConnectResult.SUCCESS

    async def test_task_name_includes_ssid_prefix(self) -> None:
        wifi = FakeWifiBackend()
        task = schedule_provision(wifi, "MyHomeWiFi-2026", "password")
        try:
            assert "MyHomeWiFi" in task.get_name()
        finally:
            await task

    async def test_does_not_block_caller(self) -> None:
        # If schedule_provision were synchronous, this whole thing would
        # take >0.1s; instead it must return immediately.
        wifi = FakeWifiBackend()

        # Inject artificial delay into the backend so we can observe it:
        original = wifi.apply_wifi_credentials

        def slow(ssid: str, password: str, timeout: int = 60) -> ConnectResult:
            import time as _t
            _t.sleep(0.1)
            return original(ssid, password, timeout)

        wifi.apply_wifi_credentials = slow  # type: ignore[method-assign]

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        task = schedule_provision(wifi, "s", "password")
        elapsed_to_schedule = loop.time() - t0

        assert elapsed_to_schedule < 0.05, (
            f"schedule_provision blocked for {elapsed_to_schedule:.3f}s"
        )

        await task  # let it finish so pytest doesn't warn
