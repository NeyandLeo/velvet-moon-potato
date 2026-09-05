import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import app as app_module

from app import (
    analyze_schedule_history,
    extract_seat_records,
    filter_showtimes,
    normalize_name,
    normalize_seat_presets,
    resolve_seat_group,
    time_in_range,
    validate_config,
)


def payload():
    return {
        "data": {
            "movies": [
                {
                    "id": 1545360,
                    "nm": "奥德赛",
                    "shows": [
                        {
                            "showDate": "2026-09-05",
                            "plist": [
                                {
                                    "dt": "2026-09-05",
                                    "tm": "19:45",
                                    "th": "4号IMAX激光厅",
                                    "lang": "英语",
                                    "tp": "IMAX2D",
                                    "seqNo": "abc",
                                    "ticketStatus": 0,
                                    "enterShowSeat": 1,
                                    "vipPrice": "108.9",
                                },
                                {
                                    "dt": "2026-09-05",
                                    "tm": "20:00",
                                    "th": "6号杜比影院",
                                    "lang": "英语",
                                    "tp": "2D",
                                    "seqNo": "def",
                                    "ticketStatus": 0,
                                    "enterShowSeat": 1,
                                    "vipPrice": "79.9",
                                },
                                {
                                    "dt": "2026-09-05",
                                    "tm": "21:00",
                                    "th": "情侣厅",
                                    "lang": "英语",
                                    "tp": "2D",
                                    "seqNo": "ghi",
                                    "ticketStatus": 0,
                                    "enterShowSeat": 1,
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    }


class LogicTests(unittest.TestCase):
    def test_normalize_name(self):
        self.assertEqual(
            normalize_name("CGV影城（清河 IMAX店）"),
            normalize_name("CGV影城清河IMAX店"),
        )

    def test_time_range_can_cross_midnight(self):
        self.assertTrue(time_in_range("23:30", "22:00", "01:00"))
        self.assertTrue(time_in_range("00:30", "22:00", "01:00"))
        self.assertFalse(time_in_range("12:00", "22:00", "01:00"))

    def test_filter_uses_priority_and_exclusions(self):
        config = {
            "show_date": "2026-09-05",
            "time_range": {"start": "18:00", "end": "23:00"},
            "hall_keywords": ["杜比", "imax"],
            "exclude_hall_keywords": ["情侣"],
            "format_keywords": [],
            "max_price": "",
        }
        matches = filter_showtimes(payload(), 1545360, config)
        self.assertEqual([item["seq_no"] for item in matches], ["def", "abc"])

    def test_filter_applies_price_and_format(self):
        config = {
            "show_date": "2026-09-05",
            "time_range": {"start": "18:00", "end": "23:00"},
            "hall_keywords": [],
            "exclude_hall_keywords": [],
            "format_keywords": ["imax2d"],
            "max_price": 110,
        }
        matches = filter_showtimes(payload(), 1545360, config)
        self.assertEqual([item["seq_no"] for item in matches], ["abc"])

    def test_server_preserves_fractional_poll_interval(self):
        config = validate_config(
            {
                "movie_name": "奥德赛",
                "cinema_name": "某影院",
                "show_date": "2026-09-05",
                "monitor_at": "2026-09-04T20:00",
                "time_range": {"start": "18:00", "end": "23:00"},
                "poll_interval": 0.2,
            }
        )
        self.assertEqual(config["poll_interval"], 0.2)

    def test_seat_presets_support_ordered_fallback_groups(self):
        presets = normalize_seat_presets(
            {
                "4号IMAX激光厅": {
                    "groups": [
                        {"row": "8排", "seats": "9,10"},
                        {"row": "8", "seats": ["11座", "12座"]},
                        {"row": "7", "seats": ["9", "10"]},
                    ]
                }
            }
        )
        self.assertEqual(presets["4号IMAX激光厅"]["ticket_count"], 2)
        self.assertEqual(presets["4号IMAX激光厅"]["groups"][1]["seats"], ["11", "12"])

    def test_seat_presets_reject_mixed_ticket_counts(self):
        with self.assertRaisesRegex(ValueError, "票数必须一致"):
            normalize_seat_presets(
                {"1厅": {"groups": [{"row": "8", "seats": ["9", "10"]}, {"row": "7", "seats": ["9"]}]}}
            )

    def test_extract_and_resolve_seat_group(self):
        seat_payload = {
            "data": {
                "sections": [
                    {"seats": [{"seatId": "a", "rowId": "8", "columnId": "9", "st": 1},
                               {"seatId": "b", "rowId": "8", "columnId": "10", "st": 1}]}
                ]
            }
        }
        records = extract_seat_records(seat_payload)
        selected = resolve_seat_group(records, {"row": "8", "seats": ["9", "10"]})
        self.assertEqual([item["id"] for item in selected], ["a", "b"])

    def test_analysis_summarizes_live_halls(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            app_module, "HISTORY_FILE", Path(directory) / "history.json"
        ):
            result = analyze_schedule_history(
                5111, 1545360, "2026-09-05", 1, payload()
            )
        self.assertEqual(result["target_date"], "2026-09-05")
        self.assertEqual(result["days"][-1]["show_count"], 3)
        self.assertEqual({hall["name"] for hall in result["halls"]},
                         {"4号IMAX激光厅", "6号杜比影院", "情侣厅"})


if __name__ == "__main__":
    unittest.main()
