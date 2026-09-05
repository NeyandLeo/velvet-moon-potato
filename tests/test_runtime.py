import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app as module
from test_safety import StatusIsolation, config
from test_seat_adapter import fixture


class RuntimeTests(StatusIsolation, unittest.IsolatedAsyncioTestCase):
    async def test_context_close_invalidates_handles_and_pending_order(self):
        runtime = module.BrowserRuntime()
        context = object()
        runtime.context = context
        runtime.page = object()
        module.set_status(browser_ready=True, pending_order={"test": True})
        runtime._context_closed(context)
        self.assertIsNone(runtime.context)
        self.assertIsNone(runtime.page)
        self.assertFalse(module.status_snapshot()["browser_ready"])
        self.assertIsNone(module.status_snapshot()["pending_order"])

    async def test_stale_close_event_does_not_clear_new_context(self):
        runtime = module.BrowserRuntime()
        runtime.context = object()
        expected = runtime.context
        runtime._context_closed(object())
        self.assertIs(runtime.context, expected)

    async def test_explicit_get_page_recreates_closed_context(self):
        runtime = module.BrowserRuntime()
        page = SimpleNamespace(is_closed=lambda: False)
        async def reopen():
            runtime.context = object()
            runtime.page = page
        runtime._bootstrap = AsyncMock(side_effect=reopen)
        self.assertIs(await runtime.get_page(), page)
        runtime._bootstrap.assert_awaited_once()

    async def test_status_poll_does_not_reopen_browser(self):
        runtime = module.BrowserRuntime()
        runtime.command_lock = asyncio.Lock()
        runtime._bootstrap = AsyncMock()
        self.assertFalse((await runtime.login_state())["logged_in"])
        runtime._bootstrap.assert_not_awaited()

    async def test_browser_close_preserves_unknown_order_result(self):
        runtime = module.BrowserRuntime()
        runtime.context = object()
        module.set_status(phase="order_result_unknown")
        runtime._context_closed(runtime.context)
        self.assertEqual(module.status_snapshot()["phase"], "order_result_unknown")

    async def test_auto_submit_configuration_defaults_and_opt_out(self):
        self.assertTrue(module.validate_config(config())["auto_submit_order"])
        self.assertFalse(module.validate_config(config(auto_submit_order=False))["auto_submit_order"])
        with self.assertRaises(ValueError):
            module.validate_config(config(auto_submit_order="true"))

    async def test_auto_dispatch_uses_existing_lock_and_only_submits_once(self):
        runtime = SimpleNamespace(command_lock=asyncio.Lock())
        helper = module.MaoyanTicketAssistant(runtime)
        page = MagicMock()
        page.url = "https://m.maoyan.com/mtrade/cinema/seat?seqNo=test"
        callbacks = {}
        page.on.side_effect = lambda event, cb: callbacks.update({event: cb})
        response = SimpleNamespace(request=SimpleNamespace(resource_type="xhr"),
                                   headers={"content-type": "application/json"}, status=200,
                                   url="https://m.maoyan.com/api/mtrade/seat/v8/show/seats.json",
                                   json=AsyncMock(return_value=fixture()))
        async def navigate(*args, **kwargs):
            await callbacks["response"](response)
        page.goto = AsyncMock(side_effect=navigate)
        page.bring_to_front = AsyncMock()
        page.locator.return_value.wait_for = AsyncMock()
        helper._wait_for_manual_login = AsyncMock(return_value=True)
        helper._verification_present = AsyncMock(return_value=False)
        helper._select_maoyan_group = AsyncMock(return_value=(0, ["8排9座"]))
        helper._maoyan_checkout = AsyncMock(return_value=(object(), "100.00"))
        helper.confirm_order = AsyncMock()
        module.set_status(current_task={"cinema_name": "测试影院", "auto_submit_order": True})
        async with runtime.command_lock:
            await asyncio.wait_for(helper._open_seat_and_apply_preset(
                page, page.url, {"date": "2026-09-04", "time": "19:45", "hall": "测试厅"},
                {"groups": [{"row": "8", "seats": ["9"]}]}, 5111, 1545360,
            ), timeout=1)
        helper.confirm_order.assert_awaited_once()
        self.assertTrue(helper.confirm_order.call_args.kwargs["already_locked"])

    async def test_submit_under_existing_lock_does_not_deadlock(self):
        button = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                                 is_enabled=AsyncMock(return_value=True), click=AsyncMock())
        page = SimpleNamespace(url="https://m.maoyan.com/mtrade/cinema/seat?seqNo=test",
                               wait_for_timeout=AsyncMock(), bring_to_front=AsyncMock())
        page.get_by_role = lambda role, name, exact: SimpleNamespace(
            count=AsyncMock(return_value=1 if name == "确认选座" else 0), nth=lambda i: button)
        runtime = SimpleNamespace(command_lock=asyncio.Lock(), get_page=AsyncMock(return_value=page))
        helper = module.MaoyanTicketAssistant(runtime)
        helper._selected_seat_labels = AsyncMock(return_value=["8排9座"])
        module.set_status(pending_order={"confirmation_token": "test", "seat_url": page.url,
                                        "expires_at": time.time()+60, "seats": ["8排9座"]})
        async with runtime.command_lock:
            await asyncio.wait_for(helper.confirm_order("test", already_locked=True), timeout=1)
        button.click.assert_awaited_once()
        self.assertEqual(module.status_snapshot()["phase"], "order_result_unknown")
        self.assertIsNone(module.status_snapshot()["pending_order"])


if __name__ == "__main__":
    unittest.main()
