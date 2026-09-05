import asyncio
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import app as module
from test_logic import payload
from test_safety import StatusIsolation, config


def multi_date_payload():
    data = payload()
    movie = data["data"]["movies"][0]
    future = copy.deepcopy(movie["shows"][0])
    future["showDate"] = "2026-10-01"
    future["plist"] = [{**future["plist"][0], "dt": "2026-10-01", "th": "未来IMAX厅", "seqNo": "future"}]
    movie["shows"].append(future)
    data["data"]["movies"].append({"id": 42, "nm": "另一部电影", "shows": [
        {"showDate": "2026-09-12", "plist": [{"dt": "2026-09-12", "tm": "12:00", "th": "其他电影杜比厅", "seqNo": "other", "tp": "2D"}]}
    ]})
    return data


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.patches = patch.multiple(module, HISTORY_FILE=directory / "history.json", RUNTIME_DIR=directory)
        self.patches.start()
        self.addCleanup(self.patches.stop)

    def analyze(self, data, target="2026-09-05", lookback=1):
        return module.analyze_schedule_history(5111, 1545360, target, lookback, data)

    def test_includes_all_live_dates_beyond_target_and_lookback(self):
        result = self.analyze(multi_date_payload())
        self.assertEqual(result["live_dates"], ["2026-09-05", "2026-09-12", "2026-10-01"])
        self.assertIn("未来IMAX厅", {h["name"] for h in result["halls"]})
        self.assertEqual(result["live_show_count"], 5)
        self.assertEqual(result["live_movie_count"], 2)

    def test_target_not_on_sale_still_discovers_other_films_halls(self):
        data = multi_date_payload()
        data["data"]["movies"].pop(0)
        result = self.analyze(data)
        self.assertEqual(result["halls"][0]["name"], "其他电影杜比厅")
        self.assertEqual(result["halls"][0]["target_live_shows"], 0)
        self.assertEqual(result["halls"][0]["movies"], ["另一部电影"])

    def test_far_future_target_does_not_exclude_current_live_dates(self):
        result = self.analyze(multi_date_payload(), target="2027-01-01")
        self.assertEqual(result["live_show_count"], 5)
        self.assertEqual(len(result["halls"]), 5)

    def test_history_all_movies_merges_but_not_other_cinemas(self):
        historic = multi_date_payload()
        module.save_schedule_snapshot(5111, 1545360, "测试影院", historic, all_movies=True)
        other_cinema = copy.deepcopy(historic)
        other_cinema["data"]["movies"][0]["shows"][0]["plist"][0]["th"] = "错误影院厅"
        module.save_schedule_snapshot(999, 1545360, "别家影院", other_cinema, all_movies=True)
        result = self.analyze({}, target="2026-09-13", lookback=2)
        self.assertEqual([h["name"] for h in result["halls"]], ["其他电影杜比厅"])
        self.assertEqual(result["halls"][0]["snapshot_shows"], 1)
        self.assertEqual(result["halls"][0]["live_shows"], 0)

    def test_live_empty_date_does_not_restore_old_shows(self):
        module.save_schedule_snapshot(5111, 1545360, "测试影院", payload())
        empty = payload()
        empty["data"]["movies"][0]["shows"][0]["plist"] = []
        result = self.analyze(empty)
        self.assertEqual(result["halls"], [])
        self.assertNotIn("2026-09-05", result["missing_dates"])
        module.save_schedule_snapshot(5111, 1545360, "测试影院", empty, all_movies=True)
        self.assertEqual(self.analyze({})["halls"], [])

    def test_same_hall_merges_sources_without_duplicate_live_history(self):
        module.save_schedule_snapshot(5111, 1545360, "测试影院", payload())
        live = payload()
        live["data"]["movies"][0]["shows"][0]["plist"] = live["data"]["movies"][0]["shows"][0]["plist"][:1]
        live["data"]["movies"][0]["shows"] *= 2
        result = self.analyze(live)
        self.assertEqual(len(result["halls"]), 1)
        self.assertEqual(result["halls"][0]["total_shows"], 1)
        self.assertEqual(result["halls"][0]["target_live_shows"], 1)
        self.assertEqual(result["halls"][0]["snapshot_shows"], 0)

    def test_monitor_still_matches_only_configured_movie_and_date(self):
        matches = module.filter_showtimes(multi_date_payload(), 1545360, config())
        self.assertEqual({s["seq_no"] for s in matches}, {"abc", "def", "ghi"})
        self.assertEqual({s["movie_id"] for s in matches}, {1545360})

    def test_snapshot_all_movies_keeps_existing_version_and_metadata(self):
        days = module.save_schedule_snapshot(5111, 1545360, "测试影院", multi_date_payload(), all_movies=True)
        self.assertEqual(days, 3)
        history = module._load_schedule_history()
        self.assertEqual(history["version"], 1)
        self.assertIn("5111:42:2026-09-12", history["entries"])
        self.assertEqual(history["entries"]["5111:42:2026-09-12"]["shows"][0]["movie_id"], 42)


class AnalysisIntegrationTests(StatusIsolation, unittest.IsolatedAsyncioTestCase):
    async def test_analysis_reads_once_and_saves_all_movies_without_order_actions(self):
        runtime = SimpleNamespace(command_lock=asyncio.Lock(), get_page=AsyncMock(return_value=object()))
        helper = module.MaoyanTicketAssistant(runtime)
        helper.resolve_target = AsyncMock(return_value=(1545360, 5111))
        data = multi_date_payload()
        data["data"]["cinemaName"] = "测试影院"
        helper.fetch_show_payload = AsyncMock(return_value=data)
        helper._open_seat_and_apply_preset = AsyncMock()
        with tempfile.TemporaryDirectory() as temp, patch.multiple(module, HISTORY_FILE=Path(temp) / "history.json", RUNTIME_DIR=Path(temp)):
            result = await helper.analyze(config())
            self.assertEqual(len(module._load_schedule_history()["entries"]), 3)
        self.assertEqual(result["live_show_count"], 5)
        helper.fetch_show_payload.assert_awaited_once()
        helper._open_seat_and_apply_preset.assert_not_called()


if __name__ == "__main__":
    unittest.main()
