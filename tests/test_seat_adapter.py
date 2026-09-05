import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app as module
from seat_adapter import (
    SeatLayoutError, parse_v8_layout, verify_dom_layout, resolve_group,
    normalize_summary, checkout_total,
)
from test_safety import StatusIsolation


def fixture():
    # rowNum 是布局索引，不是用户看到的排号；开头空位也必须计入 DOM。
    return {"data": {"seat": {"regions": [{"regionId": "a", "regionName": "A区", "rows": [
        {"rowId": "8", "rowNum": 1, "seats": [
            {"seatStatus": 0},
            {"columnId": "9", "seatNo": "s9", "seatStatus": 1, "seatType": "N"},
            {"columnId": "10", "seatNo": "s10", "seatStatus": 1, "seatType": "N"},
            {"columnId": "11", "seatNo": "s11", "seatStatus": 3, "seatType": "N"},
            {"columnId": "12", "seatNo": "s12", "seatStatus": 4, "seatType": "N"},
        ]},
        {"rowId": "9", "rowNum": 2, "seats": [
            {"columnId": "1", "seatNo": "c1", "seatStatus": 1, "seatType": "L"},
            {"columnId": "2", "seatNo": "c2", "seatStatus": 1, "seatType": "R"},
            {"columnId": "3", "seatNo": "s3", "seatStatus": 1, "seatType": "N"},
        ]},
    ]}]}}}


class LayoutTests(unittest.TestCase):
    def test_blank_aisle_rows_keep_dom_indices(self):
        data = fixture()
        rows = data["data"]["seat"]["regions"][0]["rows"]
        rows.insert(0, {"rowId": "", "seats": [{"seatStatus": 0}, {"seatStatus": 0}]})
        rows.append({"rowId": "", "seats": [{"seatStatus": 0}]})
        layout = parse_v8_layout(data)
        self.assertEqual(layout.rows[0], {"label": "", "count": 2})
        self.assertEqual(resolve_group(layout, {"row": "8", "seats": ["9"]})[0]["dom_row"], 1)

    def test_missing_actual_seat_row_still_rejected(self):
        data = fixture()
        data["data"]["seat"]["regions"][0]["rows"][0]["rowId"] = ""
        with self.assertRaisesRegex(SeatLayoutError, "实体座位行"):
            parse_v8_layout(data)

    def test_row_id_not_row_num_and_aisle_preserved(self):
        layout = parse_v8_layout(fixture())
        selected = resolve_group(layout, {"row": "8", "seats": ["9", "10"]})
        self.assertEqual([r["dom_seat"] for r in selected], [1, 2])
        self.assertEqual(selected[0]["dom_row"], 0)

    def test_couple_right_half_not_rendered(self):
        layout = parse_v8_layout(fixture())
        self.assertEqual(layout.rows[1]["count"], 2)
        self.assertEqual(resolve_group(layout, {"row": "9", "seats": ["3"]})[0]["dom_seat"], 1)

    def test_couple_automatic_selection_rejected(self):
        with self.assertRaisesRegex(SeatLayoutError, "情侣"):
            resolve_group(parse_v8_layout(fixture()), {"row": "9", "seats": ["1"]})

    def test_sold_and_forbidden_never_selectable(self):
        for seat in ("11", "12"):
            with self.assertRaisesRegex(SeatLayoutError, "不可售"):
                resolve_group(parse_v8_layout(fixture()), {"row": "8", "seats": [seat]})

    def test_numeric_type_and_status_field(self):
        data = fixture()
        seat = data["data"]["seat"]["regions"][0]["rows"][0]["seats"][1]
        seat.update(seatType=0, status=1)
        del seat["seatStatus"]
        self.assertEqual(parse_v8_layout(data).records[0]["type"], "N")

    def test_multiple_regions_are_not_guessed(self):
        data = fixture()
        data["data"]["seat"]["regions"] *= 2
        with self.assertRaises(SeatLayoutError):
            parse_v8_layout(data)

    def test_unknown_status_fails_closed(self):
        data = fixture()
        data["data"]["seat"]["regions"][0]["rows"][0]["seats"][1]["seatStatus"] = 99
        with self.assertRaises(SeatLayoutError):
            parse_v8_layout(data)

    def test_dom_shape_must_match_including_aisles(self):
        layout = parse_v8_layout(fixture())
        valid = {"regions": 1, "labels": ["8", "9"], "counts": [5, 2]}
        verify_dom_layout(layout, valid)
        for change in ({"regions": 2}, {"labels": ["9", "8"]}, {"counts": [4, 2]}):
            with self.assertRaises(SeatLayoutError):
                verify_dom_layout(layout, {**valid, **change})

    def test_summary_uses_page_display_labels(self):
        self.assertEqual(normalize_summary(["A区8排9座", "A区8排10座"], "A区"), ["8排9座", "8排10座"])
        self.assertEqual(normalize_summary(["8排9座"], ""), ["8排9座"])

    def test_unrecognized_summary_never_ignored(self):
        for texts in (["B区8排9座"], ["unknown"], ["8排9座", "8排9座"]):
            with self.assertRaises(SeatLayoutError):
                normalize_summary(texts, "A区")

    def test_actual_checkout_total(self):
        self.assertEqual(checkout_total("¥239.8 确认选座"), "239.80")
        for text in ("请先选座", "¥99起 确认选座", "¥0 确认选座", "¥99 支付"):
            with self.assertRaises(SeatLayoutError):
                checkout_total(text)


class AdapterFlowTests(StatusIsolation, unittest.IsolatedAsyncioTestCase):
    def helper(self):
        helper = module.MaoyanTicketAssistant(SimpleNamespace(command_lock=asyncio.Lock()))
        helper.seat_layout = parse_v8_layout(fixture())
        helper._verify_maoyan_dom = AsyncMock()
        helper._selected_seat_labels = AsyncMock(side_effect=[[], [], ["8排9座"], ["8排9座"], ["8排9座", "8排10座"]])
        locator = MagicMock()
        locator.locator.return_value = locator
        locator.nth.return_value = locator
        locator.evaluate = AsyncMock(return_value="IMG")
        locator.click = AsyncMock()
        page = MagicMock()
        page.locator.return_value = locator
        return helper, page, locator

    async def test_selects_images_and_confirms_visible_summary(self):
        helper, page, locator = self.helper()
        result = await helper._select_maoyan_group(page, {"groups": [{"row": "8", "seats": ["9", "10"]}]})
        self.assertEqual(result, (0, ["8排9座", "8排10座"]))
        self.assertEqual(locator.click.await_count, 2)

    async def test_sold_first_group_falls_back_before_click(self):
        helper, page, locator = self.helper()
        result = await helper._select_maoyan_group(page, {"groups": [
            {"row": "8", "seats": ["11", "12"]},
            {"row": "8", "seats": ["9", "10"]},
        ]})
        self.assertEqual(result[0], 1)
        self.assertEqual(locator.click.await_count, 2)

    async def test_existing_user_selection_not_changed(self):
        helper, page, locator = self.helper()
        helper._selected_seat_labels.side_effect = None
        helper._selected_seat_labels.return_value = ["8排9座"]
        with self.assertRaisesRegex(SeatLayoutError, "已有选中"):
            await helper._select_maoyan_group(page, {"groups": [{"row": "8", "seats": ["9"]}]})
        locator.click.assert_not_awaited()

    async def test_wrong_selection_aborts_without_retry_or_undo(self):
        helper, page, locator = self.helper()
        helper._selected_seat_labels.side_effect = [[], [], ["1排1座"]]
        with self.assertRaisesRegex(SeatLayoutError, "非预设"):
            await helper._select_maoyan_group(page, {"groups": [
                {"row": "8", "seats": ["9"]}, {"row": "8", "seats": ["10"]},
            ]})
        locator.click.assert_awaited_once()

    async def test_total_change_blocks_submission(self):
        helper, page, locator = self.helper()
        helper.runtime.get_page = AsyncMock(return_value=page)
        page.url = "https://m.maoyan.com/mtrade/cinema/seat?seqNo=a"
        helper._selected_seat_labels.side_effect = None
        helper._selected_seat_labels.return_value = ["8排9座"]
        helper._maoyan_checkout = AsyncMock(return_value=(locator, "120.00"))
        import time
        module.set_status(pending_order={"confirmation_token": "test", "expires_at": time.time()+60,
                          "seat_url": page.url, "seats": ["8排9座"], "adapter": "maoyan_v8", "total_price": "110.00"})
        with self.assertRaisesRegex(RuntimeError, "总价已变化"):
            await helper.confirm_order("test")
        locator.click.assert_not_awaited()

    async def test_open_v8_page_prepares_confirmation_without_submitting(self):
        helper, page, locator = self.helper()
        callbacks = {}
        page.on.side_effect = lambda event, callback: callbacks.update({event: callback})
        page.url = "https://m.maoyan.com/mtrade/cinema/seat?seqNo=abc"
        response = SimpleNamespace(
            request=SimpleNamespace(resource_type="xhr"),
            headers={"content-type": "application/json"}, status=200,
            url="https://m.maoyan.com/api/mtrade/seat/v8/show/seats.json",
            json=AsyncMock(return_value=fixture()),
        )
        async def navigate(*args, **kwargs):
            await callbacks["response"](response)
        page.goto = AsyncMock(side_effect=navigate)
        page.bring_to_front = AsyncMock()
        locator.wait_for = AsyncMock()
        helper._wait_for_manual_login = AsyncMock(return_value=True)
        helper._verification_present = AsyncMock(return_value=False)
        helper._select_maoyan_group = AsyncMock(return_value=(0, ["8排9座", "8排10座"]))
        helper._maoyan_checkout = AsyncMock(return_value=(locator, "239.80"))
        module.set_status(current_task={"cinema_name": "测试影院"})
        await helper._open_seat_and_apply_preset(
            page, page.url, {"date": "2026-09-04", "time": "19:45", "hall": "IMAX厅", "movie": "奥德赛"},
            {"groups": [{"row": "8", "seats": ["9", "10"]}]}, 5111, 1545360,
        )
        state = module.status_snapshot()
        self.assertEqual(state["phase"], "order_confirmation")
        self.assertEqual(state["pending_order"]["total_price"], "239.80")
        self.assertEqual(state["pending_order"]["adapter"], "maoyan_v8")
        locator.click.assert_not_awaited()
        page.remove_listener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
