import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

import app as module
from cinema_catalog import CatalogError, CatalogStore, CatalogSync, collect_city, normalize_cinema, interval_value, fetch_public
from test_safety import StatusIsolation, config


DISTRICTS = [{"id": 17, "name": "海淀区"}, {"id": 14, "name": "朝阳区"}]
FILTER = {"district": {"subItems": [{"id": -1, "name": "全部"}] + DISTRICTS}}


def raw(cinema_id=5111, address="海淀区清河中街68号"):
    return {"id": cinema_id, "nm": "CGV影城（清河IMAX店）", "addr": address}


def page(rows, total, offset=0, more=False):
    return {"cinemas": rows, "paging": {"offset": offset, "total": total, "hasMore": more}}


def collect(*pages, cancel=None):
    fetch = Mock(side_effect=[FILTER, *pages])
    return collect_city(1, .000001, cancel, fetch=fetch)


class CatalogTests(unittest.TestCase):
    def test_district_uses_address_prefix_not_branch_name(self):
        for address in ("海淀区清河", "北京市海淀区清河", "北京海淀区清河"):
            self.assertEqual(normalize_cinema(raw(address=address), 1, DISTRICTS)["district_id"], 17)
        self.assertIsNone(normalize_cinema(raw(address="某路，靠近海淀区"), 1, DISTRICTS)["district_id"])

    def test_never_guess_missing_id(self):
        for change in ({"id": None}, {"id": True}, {"id": -1}, {"nm": ""}, {"addr": None}):
            with self.assertRaises(CatalogError):
                normalize_cinema({**raw(), **change}, 1, DISTRICTS)

    def test_positive_custom_interval(self):
        for number in (.1, .5, 1, 3, 200):
            self.assertEqual(interval_value(number), number)
        for number in (0, -1, float("inf"), float("nan"), None, True):
            with self.assertRaises(CatalogError):
                interval_value(number)

    def test_complete_paginated_listing(self):
        city = collect(page([raw()], 2, more=True), page([raw(2)], 2, offset=1))
        self.assertEqual(len(city["cinemas"]), 2)
        self.assertTrue(city["listing_complete"])
        self.assertEqual(city["source_total"], 2)

    def test_duplicates_cannot_be_marked_complete(self):
        with self.assertRaises(CatalogError):
            collect(page([raw()], 2, more=True), page([raw()], 2, offset=1))

    def test_stalled_pagination_stops(self):
        with self.assertRaisesRegex(CatalogError, "没有新增"):
            collect(page([raw()], 3, more=True), page([raw()], 3, offset=1, more=True))

    def test_changed_total_invalid_response_or_offset_stops(self):
        for response in ({"error": "verify"}, page([], 1), page([raw()], 1, offset=20)):
            with self.assertRaises(CatalogError):
                collect(response)
        with self.assertRaisesRegex(CatalogError, "总数发生变化"):
            collect(page([raw()], 2, more=True), page([raw(2)], 3, offset=1))

    def test_cancel_before_request(self):
        cancel = threading.Event()
        cancel.set()
        fetch = Mock()
        with self.assertRaises(CatalogError):
            collect_city(1, cancel=cancel, fetch=fetch)
        fetch.assert_not_called()

    def test_cancel_interrupts_pagination_wait_without_another_request(self):
        cancel, first_request, finished = threading.Event(), threading.Event(), threading.Event()
        calls, errors = [], []
        def fetch(path, params):
            calls.append(path)
            first_request.set()
            return FILTER
        def worker():
            try:
                collect_city(1, 60, cancel=cancel, fetch=fetch)
            except CatalogError as exc:
                errors.append(str(exc))
            finally:
                finished.set()
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        self.assertTrue(first_request.wait(1))
        cancel.set()
        self.assertTrue(finished.wait(1))
        self.assertEqual(calls, ["/ajax/filterCinemas"])
        self.assertIn("更新已停止", errors[0])

    def test_successful_city_save_preserves_other_city_and_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed = Path(tmp) / "seed.json"
            target = Path(tmp) / "runtime" / "cinemas.json"
            beijing = collect(page([raw()], 1))
            CatalogStore(seed).save_city(beijing)
            saved_seed = seed.read_bytes()
            store = CatalogStore(target, seed)
            self.assertEqual(store.read()["cities"][0]["id"], 1)
            shanghai = {**copy.deepcopy(beijing), "id": 10, "name": "上海市"}
            store.save_city(shanghai)
            self.assertEqual([c["id"] for c in store.read()["cities"]], [1, 10])
            self.assertEqual(seed.read_bytes(), saved_seed)
            self.assertFalse(target.with_suffix(".tmp").exists())

    def test_failed_sync_keeps_old_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CatalogStore(Path(tmp) / "cinemas.json")
            store.save_city(collect(page([raw()], 1)))
            before = store.path.read_bytes()
            sync = CatalogSync(store)
            with patch("cinema_catalog.collect_city", side_effect=CatalogError("拒绝访问")):
                sync._worker(1, 3)
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(sync.snapshot()["revision"], 0)
            self.assertIn("拒绝访问", sync.snapshot()["message"])

    def test_corrupt_store_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CatalogStore(Path(tmp) / "cinemas.json")
            store.path.write_text("not json")
            with self.assertRaises(CatalogError):
                store.save_city(collect(page([raw()], 1)))
            self.assertEqual(store.path.read_text(), "not json")

    def test_public_transport_does_not_retry_or_use_credentials(self):
        with patch("cinema_catalog.subprocess.run", return_value=Mock(stdout=b'<html>verification</html>')) as run:
            with self.assertRaises(CatalogError):
                fetch_public("/ajax/cinemaList", {"cityId": 1})
            run.assert_called_once()
            args = run.call_args.args[0]
            self.assertEqual(args[:2], ["curl", "-q"])
            self.assertNotIn("--retry", args)
            self.assertNotIn("--location", args)
            self.assertNotIn("--cookie", args)

    def test_bundled_snapshot_has_both_cities_and_consistent_districts(self):
        data = CatalogStore(module.BASE_DIR / "data" / "cinemas.json").read()
        self.assertEqual([c["id"] for c in data["cities"]], [1, 10])
        for city in data["cities"]:
            self.assertEqual(len(city["cinemas"]), city["source_total"])
            self.assertEqual(len({c["id"] for c in city["cinemas"]}), len(city["cinemas"]))
            district_ids = {d["id"] for d in city["districts"]}
            for cinema in city["cinemas"]:
                self.assertEqual(cinema["city_id"], city["id"])
                self.assertTrue(cinema["district_id"] is None or cinema["district_id"] in district_ids)
                self.assertEqual(set(cinema), {"id", "city_id", "name", "address", "district_id", "district", "source_url"})


class CatalogApiTests(StatusIsolation, unittest.TestCase):
    def test_read_is_offline(self):
        with patch("cinema_catalog.subprocess.run", side_effect=AssertionError("must stay offline")):
            response = module.app.test_client().get("/api/cinemas")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["cities"]), 2)

    def test_sync_blocked_during_ticket_task(self):
        module.set_status(running=True)
        with patch.object(module.catalog_sync, "start") as start:
            self.assertEqual(module.app.test_client().post("/api/cinemas/sync", json={"city_id": 1}).status_code, 409)
            start.assert_not_called()

    def test_task_cannot_start_during_sync(self):
        with patch.object(module.catalog_sync, "snapshot", return_value={"running": True}):
            client = module.app.test_client()
            for endpoint in ("/api/start", "/api/analyze", "/api/browser/login", "/api/cinemas/sync"):
                self.assertEqual(client.post(endpoint, json=config()).status_code, 409)

    def test_bad_sync_input_and_cross_site_request(self):
        client = module.app.test_client()
        for payload in ({"city_id": 20}, {"city_id": True}, {"city_id": 1, "interval": 0}, {"city_id": 1, "interval": "NaN"}):
            self.assertEqual(client.post("/api/cinemas/sync", json=payload).status_code, 400)
        self.assertEqual(client.post("/api/cinemas/sync", json={"city_id": 1}, headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_sync_endpoint_starts_once_and_returns_202(self):
        with patch.object(module.catalog_sync, "start") as start:
            response = module.app.test_client().post("/api/cinemas/sync", json={"city_id": 10, "interval": .5})
        self.assertEqual(response.status_code, 202)
        start.assert_called_once_with(10, .5)

    def test_stop_only_cancels_directory(self):
        sync = CatalogSync(Mock())
        with patch.object(module, "catalog_sync", sync):
            self.assertEqual(module.app.test_client().post("/api/cinemas/stop", json={}).status_code, 200)
        self.assertTrue(sync.cancel.is_set())
        self.assertFalse(module.stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
