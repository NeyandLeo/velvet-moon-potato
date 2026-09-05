import asyncio
import copy
import time
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import app as module
from test_logic import payload


def config(**changes):
    return {
        "movie_name": "奥德赛", "cinema_name": "测试影院",
        "show_date": "2026-09-05", "monitor_at": "2026-09-04T12:00",
        "time_range": {"start": "18:00", "end": "23:00"},
        **changes,
    }


class StatusIsolation:
    def setUp(self):
        self.notification_patch = patch.object(module.order_notifications, "publish")
        self.notification_publish = self.notification_patch.start()
        self.saved = module.status_snapshot()
        module.stop_event.clear()
        module.set_status(running=False, analysis_running=False, pending_order=None,
                          phase="idle", browser_ready=False, logs=[])

    def tearDown(self):
        self.notification_patch.stop()
        module.stop_event.clear()
        module.set_status(**self.saved)


class ApiSafetyTests(StatusIsolation, unittest.TestCase):
    def test_cross_site_requests_blocked(self):
        result = module.app.test_client().post(
            "/api/start", json=config(), headers={"Origin": "https://evil.example"})
        self.assertEqual(result.status_code, 403)

    def test_dns_rebinding_host_blocked(self):
        result = module.app.test_client().get("/api/status", headers={"Host": "evil.example:5000"})
        self.assertEqual(result.status_code, 403)

    def test_invalid_json_shapes_rejected(self):
        for value in ([], [1], "text", 42):
            result = module.app.test_client().post("/api/start", json=value)
            self.assertEqual(result.status_code, 400)

    def test_finite_intervals_only(self):
        for number in (float("nan"), float("inf"), None, "oops", 0, -1):
            with self.assertRaises(ValueError):
                module.validate_config(config(poll_interval=number))

    def test_invalid_ids_prices_and_booleans(self):
        for update in ({"cinema_id": -1}, {"max_price": "NaN"},
                       {"max_price": -2}, {"auto_open_seat": "false"},
                       {"monitor_at": "2026-09-04T12:00+08:00"}):
            with self.assertRaises(ValueError):
                module.validate_config(config(**update))

    def test_strict_hall_filter_and_explicit_fallback(self):
        cfg = config(hall_keywords=["不存在的厅"])
        self.assertEqual(module.filter_showtimes(payload(), 1545360, cfg), [])
        cfg["require_preferred_hall"] = False
        self.assertEqual(len(module.filter_showtimes(payload(), 1545360, cfg)), 3)

    def test_update_interval_while_running(self):
        module.set_status(running=True, current_task=config(poll_interval=5))
        client = module.app.test_client()
        for given, expected in ((0.2, 0.2), (1, 1), (12, 12), (200, 200)):
            result = client.post("/api/settings/poll-interval", json={"poll_interval": given})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(module.status_snapshot()["current_task"]["poll_interval"], expected)

    def test_update_idle_interval_rejected(self):
        result = module.app.test_client().post("/api/settings/poll-interval", json={"poll_interval": 5})
        self.assertEqual(result.status_code, 409)

    def test_stop_invalidates_confirmation(self):
        module.set_status(pending_order={"confirmation_token": "test"}, phase="order_confirmation")
        result = module.app.test_client().post("/api/stop", json={})
        self.assertEqual(result.status_code, 200)
        self.assertIsNone(module.status_snapshot()["pending_order"])
        self.assertTrue(module.stop_event.is_set())

    def test_parallel_starts_launch_only_one_worker(self):
        def start(_):
            return module.app.test_client().post("/api/start", json=config()).status_code
        ran = threading.Event()
        with patch.object(module, "_task_worker", side_effect=lambda _: ran.set()) as worker:
            with patch.object(module.browser_runtime, "configure_preference"):
                with ThreadPoolExecutor(max_workers=6) as pool:
                    results = list(pool.map(start, range(12)))
            self.assertTrue(ran.wait(1))
            self.assertEqual(results.count(202), 1)
            self.assertEqual(results.count(409), 11)
            worker.assert_called_once()

    def test_unresolved_order_prevents_new_task(self):
        module.set_status(phase="order_result_unknown")
        self.assertEqual(module.app.test_client().post("/api/start", json=config()).status_code, 409)

    def test_wrong_name_and_id_cannot_silently_select(self):
        data = payload()
        data["data"]["cinemaName"] = "测试影院"
        module.verify_target_names(data, config(), 1545360)
        for changes in ({"movie_name": "其他影片"}, {"cinema_name": "其他影院"}):
            with self.assertRaises(module.PlatformBlocked):
                module.verify_target_names(data, config(**changes), 1545360)


class AsyncSafetyTests(StatusIsolation, unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_slow_browser_operation(self):
        cancelled = asyncio.Event()
        async def slow():
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.set()
        guarded = asyncio.create_task(module.stop_guard(slow()))
        await asyncio.sleep(0.02)
        module.stop_event.set()
        with self.assertRaises(module.TaskStopped):
            await asyncio.wait_for(guarded, 1)
        self.assertTrue(cancelled.is_set())

    def make_assistant(self):
        page = MagicMock()
        page.url = "https://m.maoyan.com/mtrade/cinema/seat?seqNo=abc"
        page.evaluate = AsyncMock(return_value=["8排9座", "8排10座"])
        page.wait_for_timeout = AsyncMock()
        page.bring_to_front = AsyncMock()
        runtime = SimpleNamespace(command_lock=asyncio.Lock(), get_page=AsyncMock(return_value=page))
        helper = module.MaoyanTicketAssistant(runtime)
        pending = {"confirmation_token": "test-token", "seat_url": page.url,
                   "expires_at": time.time() + 120, "seats": ["8排9座", "8排10座"]}
        module.set_status(pending_order=copy.deepcopy(pending))
        return helper, page, pending

    async def test_changed_show_cannot_submit(self):
        helper, page, _ = self.make_assistant()
        page.url += "changed"
        with self.assertRaisesRegex(RuntimeError, "场次已变化"):
            await helper.confirm_order("test-token")
        page.get_by_role.assert_not_called()

    async def test_changed_seats_cannot_submit(self):
        helper, page, _ = self.make_assistant()
        page.evaluate.return_value = ["1排1座"]
        with self.assertRaisesRegex(RuntimeError, "座位.*不一致"):
            await helper.confirm_order("test-token")
        page.get_by_role.assert_not_called()

    async def test_expired_confirmation_cannot_submit(self):
        helper, page, pending = self.make_assistant()
        pending["expires_at"] = 0
        module.set_status(pending_order=pending)
        with self.assertRaisesRegex(RuntimeError, "超时"):
            await helper.confirm_order("test-token")
        page.get_by_role.assert_not_called()

    async def test_click_timeout_consumes_token_and_never_claims_success(self):
        helper, page, _ = self.make_assistant()
        button = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                                 is_enabled=AsyncMock(return_value=True),
                                 click=AsyncMock(side_effect=TimeoutError("ambiguous")))
        def locator(role, name, exact):
            return SimpleNamespace(count=AsyncMock(return_value=1 if name == "确认选座" else 0),
                                   nth=lambda index: button)
        page.get_by_role.side_effect = locator
        with self.assertRaises(TimeoutError):
            await helper.confirm_order("test-token")
        self.assertIsNone(module.status_snapshot()["pending_order"])
        self.assertEqual(module.status_snapshot()["phase"], "order_result_unknown")
        with self.assertRaisesRegex(RuntimeError, "没有等待确认"):
            await helper.confirm_order("test-token")
        button.click.assert_awaited_once()
        self.notification_publish.assert_called_once()

    async def test_url_change_alone_is_not_login(self):
        helper, page, _ = self.make_assistant()
        page.evaluate.return_value = False
        self.assertFalse(await helper._wait_for_manual_login(page, seconds=0))

    async def fetch_fixture(self, status, body):
        helper, page, _ = self.make_assistant()
        response = SimpleNamespace(status=status, json=AsyncMock(return_value=body))
        future = asyncio.get_running_loop().create_future()
        future.set_result(response)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=SimpleNamespace(value=future))
        context.__aexit__ = AsyncMock(return_value=False)
        page.expect_response.return_value = context
        page.goto = AsyncMock()
        return await helper.fetch_show_payload(
            page, "https://m.maoyan.com/mtrade/cinema/cinema?cinemaId=5111", True)

    async def test_platform_denial_is_not_no_shows(self):
        for status in (401, 403, 429):
            with self.assertRaises(module.PlatformBlocked):
                await self.fetch_fixture(status, {})
        with self.assertRaises(module.PlatformBlocked):
            await self.fetch_fixture(200, {"code": 1001, "data": {"movies": []}})

    async def test_schema_and_cinema_checked(self):
        with self.assertRaisesRegex(RuntimeError, "结构"):
            await self.fetch_fixture(200, {"code": 0, "data": {}})
        with self.assertRaises(module.PlatformBlocked):
            await self.fetch_fixture(200, {"code": 0, "data": {"cinemaId": 123, "movies": []}})
        valid = {"code": 0, "data": {"cinemaId": 5111, "movies": []}}
        self.assertEqual(await self.fetch_fixture(200, valid), valid)

    async def test_unverified_selection_does_not_trigger_fallback_clicks(self):
        helper, page, _ = self.make_assistant()
        page.evaluate.side_effect = [[], ["1排1座"]]
        seat = SimpleNamespace(get_attribute=AsyncMock(return_value="false"), click=AsyncMock())
        helper._exact_seat_locator = AsyncMock(return_value=seat)
        records = [{"id": "a", "row": "8", "column": "9", "seat_no": "9", "status": "1"}]
        preset = {"groups": [{"row": "8", "seats": ["9"]}, {"row": "8", "seats": ["9"]}]}
        result = await helper._select_first_available_group(page, records, preset)
        self.assertIsNone(result)
        seat.click.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
