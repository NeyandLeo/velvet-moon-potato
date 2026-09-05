#!/usr/bin/env python3
"""猫眼电影票监控助手。

使用猫眼自己的网页和持久化可控浏览器会话：用户手动登录；程序低频刷新
影院排片页并读取页面已经取得的排片响应；匹配到场次后可按影厅预设选座；
按用户配置自动提交待支付订单，或在 WebUI 二次确认；支付始终由用户完成。

程序不会伪造接口签名、绕过验证码或自动提交支付。
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import math
import os
import re
import secrets
import socket
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from contextlib import nullcontext
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs, urlencode, urlsplit

from flask import Flask, jsonify, render_template, request
from playwright.async_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    async_playwright,
)
from seat_adapter import (
    SeatLayout, SeatLayoutError, parse_v8_layout, verify_dom_layout, resolve_group,
    normalize_summary, checkout_total, REGION_SELECTOR, ROW_SELECTOR, NAV_SELECTOR,
    SUMMARY_SELECTOR, CHECKOUT_SELECTOR,
)
from cinema_catalog import CatalogStore, CatalogSync, CatalogError
from order_notifications import OrderNotifications


BASE_DIR = Path(__file__).resolve().parent
APP_VERSION = "2026.09.05.1"
STARTED_AT = datetime.now().isoformat(timespec="seconds")


def source_revision() -> str:
    digest = hashlib.sha256()
    for filename in ("app.py", "seat_adapter.py", "cinema_catalog.py", "order_notifications.py", "templates/index.html"):
        digest.update((BASE_DIR / filename).read_bytes())
    return digest.hexdigest()[:12]


LOADED_REVISION = source_revision()
RUNTIME_DIR = BASE_DIR / ".runtime"
HISTORY_FILE = RUNTIME_DIR / "schedule_history.json"
catalog_store = CatalogStore(RUNTIME_DIR / "cinemas.json", BASE_DIR / "data" / "cinemas.json")
catalog_sync = CatalogSync(catalog_store)
order_notifications = OrderNotifications(RUNTIME_DIR / "order-notification.json")
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
    "Mobile/15E148 Safari/604.1"
)
MAX_POLL_INTERVAL = 120.0
MAX_LOGS = 300

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("maoyan-helper")

app = Flask(__name__)
status_lock = threading.RLock()
history_lock = threading.RLock()
stop_event = threading.Event()
control_lock = threading.RLock()


class TaskStopped(Exception):
    pass


class PlatformBlocked(Exception):
    """平台拒绝访问时终止自动请求，不把它当作暂无场次。"""


def serialized_control(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with control_lock:
            return function(*args, **kwargs)
    return wrapped


@app.before_request
def require_local_request():
    # 仅绑定 loopback 仍不能防止恶意网页通过用户浏览器发起本地操作。
    if request.host not in {"127.0.0.1:5000", "localhost:5000", "localhost"}:
        return jsonify({"error": "仅允许本机访问"}), 403
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify({"error": "不允许跨站操作"}), 403
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return jsonify({"error": "不允许跨站操作"}), 403
        if not request.is_json or not isinstance(request.get_json(silent=True), dict):
            return jsonify({"error": "请求必须是 JSON 对象"}), 400


async def stop_guard(coroutine):
    task = asyncio.create_task(coroutine)
    try:
        while not task.done():
            if stop_event.is_set():
                raise TaskStopped("任务已停止")
            await asyncio.wait({task}, timeout=0.2)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def bounded_interval(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("刷新间隔必须是有限数字") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError("刷新间隔必须是大于 0 的有限数字，支持小数")
    return number


def verify_target_names(payload: Dict[str, Any], config: Dict[str, Any], movie_id: int) -> None:
    data = payload["data"]
    actual_cinema = data.get("cinemaName", "")
    if config.get("cinema_name") and normalize_name(actual_cinema) != normalize_name(config["cinema_name"]):
        raise PlatformBlocked(f"影院名称与排片响应不一致（实际：{actual_cinema or '未知'}），请核对 cinemaId 和名称")
    for movie in data["movies"]:
        if str(movie.get("id")) == str(movie_id):
            if config.get("movie_name") and normalize_name(movie.get("nm", "")) != normalize_name(config["movie_name"]):
                raise PlatformBlocked(f"电影名称与 movieId 不一致（实际：{movie.get('nm', '未知')}），请核对")

grabber_status: Dict[str, Any] = {
    "version": APP_VERSION,
    "started_at": STARTED_AT,
    "pid": os.getpid(),
    "running": False,
    "phase": "idle",
    "message": "待机中",
    "logs": [],
    "current_task": None,
    "matched_show": None,
    "browser_ready": False,
    "browser_name": None,
    "analysis_running": False,
    "schedule_analysis": None,
    "pending_order": None,
}


@app.after_request
def disable_response_cache(response):
    response.headers["Cache-Control"] = "no-store"
    return response


def set_status(**changes: Any) -> None:
    with status_lock:
        grabber_status.update(changes)


def status_snapshot() -> Dict[str, Any]:
    with status_lock:
        return copy.deepcopy(grabber_status)


def log_message(message: str, level: str = "info") -> None:
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message}
    with status_lock:
        grabber_status["logs"].append(entry)
        grabber_status["logs"] = grabber_status["logs"][-MAX_LOGS:]
    logger.info("[%s] %s", level.upper(), message)


def normalize_name(value: str) -> str:
    """用于名称比较，保留中英文和数字，忽略空格及常见标点。"""
    return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", value or "").lower()


def parse_minutes(value: str) -> int:
    hour, minute = (int(part) for part in value.strip().split(":"))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("时间必须为 HH:MM")
    return hour * 60 + minute


def time_in_range(value: str, start: str, end: str) -> bool:
    current = parse_minutes(value)
    first = parse_minutes(start)
    last = parse_minutes(end)
    if first <= last:
        return first <= current <= last
    return current >= first or current <= last


def _keywords(value: Any) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[,，\n]", value)
    return [str(item).strip().lower() for item in (value or []) if str(item).strip()]


def iter_all_showtimes(payload: Dict[str, Any], movie_id: Optional[int] = None) -> Iterable[Dict[str, Any]]:
    data = payload.get("data") or {}
    for movie in data.get("movies") or []:
        if movie_id is not None and int(movie.get("id") or 0) != int(movie_id):
            continue
        for show_group in movie.get("shows") or []:
            group_date = show_group.get("showDate", "")
            for show in show_group.get("plist") or []:
                yield {
                    "movie_id": int(movie.get("id") or 0),
                    "movie": movie.get("nm", ""),
                    "date": show.get("dt") or group_date,
                    "time": show.get("tm", ""),
                    "hall": show.get("th", ""),
                    "language": show.get("lang", ""),
                    "format": show.get("tp", ""),
                    "seq_no": str(show.get("seqNo", "")),
                    "ticket_status": show.get("ticketStatus"),
                    "enter_seat": show.get("enterShowSeat"),
                    "sale_text": show.get("saleTimeText", ""),
                    "price": show.get("vipPrice") or show.get("vipDisPrice"),
                }


def iter_showtimes(payload: Dict[str, Any], movie_id: int, show_date: str) -> Iterable[Dict[str, Any]]:
    for show in iter_all_showtimes(payload, movie_id):
        if show["date"] == show_date:
            yield show


def _history_key(cinema_id: int, movie_id: int, show_date: str) -> str:
    return f"{int(cinema_id)}:{int(movie_id)}:{show_date}"


def _load_schedule_history() -> Dict[str, Any]:
    with history_lock:
        if not HISTORY_FILE.exists():
            return {"version": 1, "entries": {}}
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if not isinstance(data.get("entries"), dict):
                raise ValueError("entries 无效")
            return data
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            log_message(f"排片历史文件无法读取，将从新快照继续：{exc}", "warning")
            return {"version": 1, "entries": {}}


def save_schedule_snapshot(
    cinema_id: int,
    movie_id: int,
    cinema_name: str,
    payload: Dict[str, Any],
    all_movies: bool = False,
) -> int:
    """保存响应中的日期（含明确空排片），可覆盖同影院所有电影。"""
    grouped = schedule_groups(payload, None if all_movies else movie_id)
    if not grouped:
        return 0

    with history_lock:
        history = _load_schedule_history()
        captured_at = datetime.now().isoformat(timespec="seconds")
        for (entry_movie_id, show_date), shows in grouped.items():
            history["entries"][_history_key(cinema_id, entry_movie_id, show_date)] = {
                "cinema_id": int(cinema_id),
                "cinema_name": cinema_name,
                "movie_id": entry_movie_id,
                "date": show_date,
                "captured_at": captured_at,
                "shows": shows,
            }
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        temporary = HISTORY_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(HISTORY_FILE)
    return len({day for _, day in grouped})


def schedule_groups(payload: Dict[str, Any], movie_id: Optional[int] = None) -> Dict[Any, Any]:
    """按电影和日期整理当前响应；保留空 plist，避免恢复已撤掉的旧场次。"""
    grouped: Dict[Any, Any] = {}
    for movie in (payload.get("data") or {}).get("movies") or []:
        entry_id = int(movie.get("id") or 0)
        if entry_id <= 0 or (movie_id is not None and entry_id != movie_id):
            continue
        for group in movie.get("shows") or []:
            if group.get("showDate"):
                day = date.fromisoformat(group["showDate"]).isoformat()
                grouped.setdefault((entry_id, day), [])
    for show in iter_all_showtimes(payload, movie_id):
        if show["movie_id"] > 0 and show["date"]:
            day = date.fromisoformat(show["date"]).isoformat()
            show["date"] = day
            grouped.setdefault((show["movie_id"], day), []).append(show)
    for key, shows in grouped.items():
        # A repeated group/record must not inflate hall counts.
        unique = {}
        for show in shows:
            identity = ("seq", show["seq_no"]) if show["seq_no"] else (
                "slot", show["hall"], show["time"], show["language"], show["format"])
            unique[identity] = show
        grouped[key] = list(unique.values())
    return grouped


def analyze_schedule_history(
    cinema_id: int,
    movie_id: int,
    target_date: str,
    lookback_days: int,
    live_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """当前响应的所有电影/日期 + 目标日前 N 天至目标日的本地历史。

    实时日期不受回看窗口或目标日期截断；历史和实时按电影/日期合并，
    实时优先。候选厅发现不会改变 filter_showtimes 的购票条件。
    """
    history = _load_schedule_history()
    live_groups = schedule_groups(live_payload or {})
    target = date.fromisoformat(target_date)
    history_dates = {(target - timedelta(days=offset)).isoformat() for offset in range(lookback_days, -1, -1)}
    combined = {}
    for entry in history["entries"].values():
        if str(entry.get("cinema_id")) != str(cinema_id) or entry.get("date") not in history_dates:
            continue
        entry_id = int(entry.get("movie_id") or 0)
        if entry_id > 0:
            combined[(entry_id, entry["date"])] = ("snapshot", entry.get("shows") or [])
    combined.update({key: ("live", shows) for key, shows in live_groups.items()})
    live_dates = sorted({day for _, day in live_groups})
    dates = sorted(history_dates | set(live_dates))
    day_summaries: List[Dict[str, Any]] = []
    halls: Dict[str, Dict[str, Any]] = {}
    missing_dates: List[str] = []

    for day_text in dates:
        entries = [(entry_id, source, shows) for (entry_id, day), (source, shows) in combined.items() if day == day_text]
        sources = {source for _, source, _ in entries}
        source = "mixed" if len(sources) > 1 else next(iter(sources), "missing")
        shows = [{**show, "movie_id": entry_id, "source": entry_source}
                 for entry_id, entry_source, rows in entries for show in rows]
        if not entries:
            missing_dates.append(day_text)
        day_halls: Dict[str, List[str]] = {}
        for show in shows:
            hall = str(show.get("hall") or "").strip()
            if not hall:
                continue
            day_halls.setdefault(hall, []).append(str(show.get("time") or ""))
            summary = halls.setdefault(
                hall,
                {"name": hall, "total_shows": 0, "dates": [], "times": [], "formats": [],
                 "movies": [], "live_shows": 0, "snapshot_shows": 0,
                 "target_live_shows": 0, "target_shows": 0},
            )
            summary["total_shows"] += 1
            summary["live_shows" if show["source"] == "live" else "snapshot_shows"] += 1
            if show["movie_id"] == movie_id:
                summary["target_shows"] += 1
                if show["source"] == "live":
                    summary["target_live_shows"] += 1
            film_name = str(show.get("movie") or f"电影 ID {show['movie_id']}")
            if film_name not in summary["movies"]:
                summary["movies"].append(film_name)
            if day_text not in summary["dates"]:
                summary["dates"].append(day_text)
            show_time = str(show.get("time") or "")
            if show_time and show_time not in summary["times"]:
                summary["times"].append(show_time)
            show_format = " ".join(
                part for part in [str(show.get("language") or ""), str(show.get("format") or "")] if part
            )
            if show_format and show_format not in summary["formats"]:
                summary["formats"].append(show_format)
        day_summaries.append(
            {
                "date": day_text,
                "is_target": day_text == target_date,
                "source": source,
                "show_count": len(shows),
                "live_show_count": sum(show["source"] == "live" for show in shows),
                "snapshot_show_count": sum(show["source"] == "snapshot" for show in shows),
                "halls": [
                    {"name": hall, "times": sorted(times)} for hall, times in sorted(day_halls.items())
                ],
            }
        )

    ordered_halls = sorted(halls.values(), key=lambda item: (-item["target_live_shows"], -item["live_shows"], -item["total_shows"], item["name"]))
    for hall in ordered_halls:
        hall["times"].sort()
        hall["dates"].sort()
        hall["formats"].sort()
        hall["movies"].sort()
    return {
        "cinema_id": int(cinema_id),
        "movie_id": int(movie_id),
        "target_date": target_date,
        "lookback_days": lookback_days,
        "days": day_summaries,
        "halls": ordered_halls,
        "missing_dates": missing_dates,
        "scope": "all_movies_all_returned_dates_with_history",
        "live_dates": live_dates,
        "live_movie_count": len({entry_id for entry_id, _ in live_groups}),
        "live_show_count": sum(len(shows) for shows in live_groups.values()),
        "coverage_note": "已汇总猫眼当前响应中所有电影、所有返回日期（含未来），并补充回看窗口内的本地历史。仅代表本次响应范围，无法确认网站未返回或尚未公布的排片；候选影厅不保证会放映目标电影。",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def filter_showtimes(payload: Dict[str, Any], movie_id: int, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    preferred = _keywords(config.get("hall_keywords"))
    excluded = _keywords(config.get("exclude_hall_keywords"))
    formats = _keywords(config.get("format_keywords"))
    start = config["time_range"]["start"]
    end = config["time_range"]["end"]
    max_price = config.get("max_price")
    start_minutes = parse_minutes(start)
    end_minutes = parse_minutes(end)
    if end_minutes < start_minutes:
        end_minutes += 24 * 60
    target_midpoint = (start_minutes + end_minutes) / 2

    candidates: List[Dict[str, Any]] = []
    for show in iter_showtimes(payload, movie_id, config["show_date"]):
        searchable = f"{show['hall']} {show['language']} {show['format']}".lower()
        if not show["seq_no"] or show["ticket_status"] != 0 or show["enter_seat"] != 1:
            continue
        if not show["time"] or not time_in_range(show["time"], start, end):
            continue
        if excluded and any(word in searchable for word in excluded):
            continue
        if formats and not any(word in searchable for word in formats):
            continue

        price_value: Optional[float] = None
        try:
            if show["price"] not in (None, ""):
                price_value = float(show["price"])
        except (TypeError, ValueError):
            pass
        if max_price not in (None, "") and price_value is not None and price_value > float(max_price):
            continue

        keyword_rank = next(
            (index for index, word in enumerate(preferred) if word in searchable), len(preferred)
        )
        if preferred and config.get("require_preferred_hall", True) and keyword_rank == len(preferred):
            continue
        show["keyword_rank"] = keyword_rank
        show["matched_keyword"] = preferred[keyword_rank] if keyword_rank < len(preferred) else ""
        show_minutes = parse_minutes(show["time"])
        if show_minutes < start_minutes:
            show_minutes += 24 * 60
        show["time_distance"] = abs(show_minutes - target_midpoint)
        candidates.append(show)

    candidates.sort(key=lambda item: (item["keyword_rank"], item["time_distance"], item["time"]))
    return candidates


def normalize_seat_presets(value: Any) -> Dict[str, Dict[str, Any]]:
    if not value:
        return {}
    if not isinstance(value, dict):
        raise ValueError("座位预设格式无效")
    result: Dict[str, Dict[str, Any]] = {}
    for hall, raw in value.items():
        hall_name = str(hall or "").strip()
        if not hall_name or not isinstance(raw, dict):
            continue
        groups_value = raw.get("groups")
        if not isinstance(groups_value, list):
            groups_value = [{"row": raw.get("row"), "seats": raw.get("seats")}]
        groups: List[Dict[str, Any]] = []
        ticket_count: Optional[int] = None
        for group in groups_value[:5]:
            if not isinstance(group, dict):
                continue
            row = str(group.get("row") or "").strip().replace("排", "")
            seats_value = group.get("seats") or []
            if isinstance(seats_value, str):
                seats_value = re.split(r"[,，\s]+", seats_value)
            seats = []
            for seat in seats_value:
                seat_text = str(seat).strip().replace("座", "")
                if seat_text and seat_text not in seats:
                    seats.append(seat_text)
            if not row or not seats:
                continue
            if len(seats) > 6:
                raise ValueError(f"{hall_name} 的每组座位最多 6 个")
            if ticket_count is None:
                ticket_count = len(seats)
            elif len(seats) != ticket_count:
                raise ValueError(f"{hall_name} 的候选座位组票数必须一致")
            groups.append({"row": row, "seats": seats})
        if not groups:
            continue
        result[hall_name] = {"groups": groups, "ticket_count": ticket_count}
    return result


def preset_for_hall(presets: Dict[str, Dict[str, Any]], hall: str) -> Optional[Dict[str, Any]]:
    wanted = normalize_name(hall)
    exact = [preset for name, preset in presets.items() if normalize_name(name) == wanted]
    return exact[0] if len(exact) == 1 else None


def extract_seat_records(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    """兼容地提取座位响应中的行、座号与状态，仅用于核对预设。"""
    records: List[Dict[str, str]] = []
    seen = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            has_seat_identity = any(
                key in node for key in ("seatNo", "seatId", "columnId", "columnNum")
            )
            has_row_identity = any(key in node for key in ("rowId", "rowNum", "rowName"))
            if has_seat_identity and has_row_identity:
                row = next(
                    (str(node[key]) for key in ("rowNum", "rowId", "rowName") if node.get(key) not in (None, "")),
                    "",
                ).replace("排", "")
                column = next(
                    (
                        str(node[key])
                        for key in ("columnNum", "columnId", "seatNo")
                        if node.get(key) not in (None, "")
                    ),
                    "",
                ).replace("座", "")
                seat_no = str(node.get("seatNo") or "")
                identity = str(node.get("seatId") or node.get("id") or f"{row}:{column}")
                key = (identity, row, column)
                if row and column and key not in seen:
                    seen.add(key)
                    records.append(
                        {
                            "id": identity,
                            "row": row,
                            "column": column,
                            "seat_no": seat_no,
                            "status": str(
                                node.get("seatStatus")
                                if node.get("seatStatus") is not None
                                else node.get("status", node.get("st", ""))
                            ),
                        }
                    )
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(payload)
    return records


def resolve_seat_group(
    seat_records: List[Dict[str, str]], group: Dict[str, Any]
) -> List[Dict[str, str]]:
    row = str(group["row"])
    wanted = [str(item) for item in group["seats"]]
    selected: List[Dict[str, str]] = []
    for seat in wanted:
        matches = [
            record
            for record in seat_records
            if record["row"] == row
            and (
                record["column"] == seat
                or record["seat_no"] == seat
                or record["seat_no"] == f"{row}排{seat}座"
            )
        ]
        if len(matches) != 1:
            return []
        selected.append(matches[0])
    return selected


def seat_record_is_known_unavailable(record: Dict[str, str]) -> bool:
    return record.get("status", "").strip().lower() in {
        "2", "3", "sold", "locked", "unavailable", "disabled", "lk", "s",
    }


class BrowserRuntime:
    """在独立 asyncio 线程中维护一个可见、可复用的 Chromium 会话。"""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._startup_lock = threading.Lock()
        self._playwright: Optional[Playwright] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.command_lock: Optional[asyncio.Lock] = None
        self.preference = "auto"
        self.browser_name: Optional[str] = None

    def configure_preference(self, preference: Any) -> None:
        choice = str(preference or "auto").strip().lower()
        if choice not in {"auto", "chrome", "msedge", "chromium"}:
            raise ValueError("浏览器类型必须是 auto、chrome、msedge 或 chromium")
        if self.context and self._thread and self._thread.is_alive() and choice != self.preference:
            current_choice = {
                "Google Chrome": "chrome",
                "Microsoft Edge": "msedge",
                "Playwright Chromium": "chromium",
            }.get(self.browser_name)
            if choice in {"auto", current_choice}:
                self.preference = choice
                return
            raise RuntimeError(
                f"自动化浏览器已经以 {self.browser_name or self.preference} 启动；"
                "如需切换，请关闭本工具后重新启动"
            )
        self.preference = choice

    def ensure_started(self, timeout: float = 25) -> None:
        with self._startup_lock:
            if not self._thread or not self._thread.is_alive():
                self._ready.clear()
                self._startup_error = None
                self._thread = threading.Thread(target=self._thread_main, daemon=True, name="maoyan-browser")
                self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("自动化浏览器启动超时")
        if self._startup_error:
            raise RuntimeError(f"自动化浏览器启动失败：{self._startup_error}")

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._bootstrap())
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        self._loop.run_forever()

    async def _bootstrap(self) -> None:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        choices = (
            [self.preference]
            if self.preference != "auto"
            else ["chrome", "msedge", "chromium"]
        )
        failures: List[str] = []
        for choice in choices:
            legacy_chrome_profile = RUNTIME_DIR / "maoyan-chrome-profile"
            profile_dir = (
                legacy_chrome_profile
                if choice == "chrome" and legacy_chrome_profile.exists()
                else RUNTIME_DIR / f"browser-profile-{choice}"
            )
            profile_dir.mkdir(parents=True, exist_ok=True)
            launch_options = {
                "user_data_dir": str(profile_dir),
                "headless": False,
                # H5 路由不等于手机模拟：允许桌面窗口自由缩放，移除固定 430px 视口。
                "no_viewport": True,
                "args": ["--window-size=1280,960"],
                "user_agent": MOBILE_UA,
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "is_mobile": False,
                "has_touch": False,
            }
            try:
                if choice == "chromium":
                    self.context = await self._playwright.chromium.launch_persistent_context(
                        **launch_options
                    )
                else:
                    self.context = await self._playwright.chromium.launch_persistent_context(
                        channel=choice, **launch_options
                    )
                self.browser_name = {
                    "chrome": "Google Chrome",
                    "msedge": "Microsoft Edge",
                    "chromium": "Playwright Chromium",
                }[choice]
                break
            except Exception as exc:
                failures.append(f"{choice}: {exc}")
        if not self.context:
            raise RuntimeError("；".join(failures))
        current_context = self.context
        current_context.on("close", lambda *_: self._context_closed(current_context))
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        if self.command_lock is None:
            self.command_lock = asyncio.Lock()
        set_status(browser_ready=True, browser_name=self.browser_name)
        log_message(f"{self.browser_name} 已就绪；登录状态保存在项目的 .runtime 目录")

    def _context_closed(self, closed_context) -> None:
        if self.context is not closed_context:
            return  # 旧 context 的延迟事件不得清除刚重建的会话。
        self.context = None
        self.page = None
        snapshot = status_snapshot()
        set_status(browser_ready=False, pending_order=None)
        if snapshot["running"] or snapshot["analysis_running"]:
            stop_event.set()
        if snapshot["phase"] not in {"creating_order", "order_result_unknown"}:
            set_status(phase="browser_closed", message="猫眼浏览器已关闭；点击“打开登录浏览器”可重新打开")
        log_message("浏览器会话已关闭，不恢复监控或重试下单；下次打开登录时重建会话", "warning")

    def submit(self, coroutine: Any):
        try:
            self.ensure_started()
        except Exception:
            coroutine.close()
            raise
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def get_page(self) -> Page:
        # 调用者均持有 command_lock；重建同一浏览器会话不会并发启动。
        if not self.context:
            await self._bootstrap()
        if self.page and not self.page.is_closed():
            return self.page
        try:
            self.page = await self.context.new_page()
        except PlaywrightError as exc:
            if "closed" not in str(exc).lower():
                raise
            self._context_closed(self.context)
            await self._bootstrap()
        return self.page

    async def open_login(self) -> Dict[str, Any]:
        assert self.command_lock is not None
        async with self.command_lock:
            page = await self.get_page()
            redirect = "https://m.maoyan.com/mtrade/order/list"
            url = "https://passport.maoyan.com/mtrade/login?" + urlencode({"redirectURL": redirect})
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            await page.bring_to_front()
            set_status(phase="waiting_login", message=f"请在 {self.browser_name} 中手动登录")
            log_message("已打开猫眼登录页。请手动输入手机号、验证码并登录；程序不会读取这些信息。")
            return {"url": page.url, "browser_name": self.browser_name}

    async def login_state(self) -> Dict[str, Any]:
        assert self.command_lock is not None
        async with self.command_lock:
            if not self.context or not self.page or self.page.is_closed():
                return {"logged_in": False, "url": "", "browser_ready": bool(self.context)}
            page = self.page  # 只读状态轮询不得偷偷重开用户关闭的窗口。
            url = page.url
            if "passport.maoyan.com" in url:
                return {"logged_in": False, "url": url}
            logged_in = False
            if urlsplit(url).hostname in {"m.maoyan.com", "i.maoyan.com", "www.maoyan.com"}:
                try:
                    logged_in = await page.evaluate(
                        "() => Boolean(window.AppData?.user?.id || window.AppData?.user?.token)"
                    )
                except Exception:
                    pass
            if logged_in and status_snapshot()["phase"] in {"idle", "ready", "waiting_login"}:
                set_status(phase="ready", message="猫眼已登录")
            return {"logged_in": logged_in, "url": url}


browser_runtime = BrowserRuntime()


class MaoyanTicketAssistant:
    def __init__(self, runtime: BrowserRuntime) -> None:
        self.runtime = runtime
        self.seat_layout: Optional[SeatLayout] = None

    async def _verification_present(self, page: Page) -> bool:
        url = page.url.lower()
        if "verify.meituan.com" in url or "yoda" in url:
            return True
        try:
            return "验证中心" in await page.title()
        except Exception:
            return False

    async def _wait_for_manual_login(self, page: Page, seconds: int = 300) -> bool:
        notified = False
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not stop_event.is_set():
            if urlsplit(page.url).hostname in {"m.maoyan.com", "i.maoyan.com"}:
                try:
                    verified = await page.evaluate(
                        "() => Boolean(window.AppData?.user?.id || window.AppData?.user?.token)"
                    )
                    if verified:
                        if notified:
                            log_message("检测到页面已登录", "success")
                        return True
                except Exception:
                    pass
            if not notified:
                set_status(phase="waiting_login", message="正在确认登录；若出现登录页请手动完成")
                log_message("当前页面尚未确认登录；请检查浏览器中的登录/验证提示", "warning")
                notified = True
            await asyncio.sleep(1)
        return False

    async def ensure_login(self, page: Page) -> bool:
        await page.goto(
            "https://m.maoyan.com/mtrade/order/list",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.bring_to_front()
        return await self._wait_for_manual_login(page)

    async def discover_movie_id(self, page: Page, movie_name: str) -> int:
        await page.goto("https://i.maoyan.com/cinemas#movie", wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(2_000)
        items = page.locator(".page.n-hot .list-wrap .item")
        count = await items.count()
        wanted = normalize_name(movie_name)
        partial: List[tuple[str, str]] = []
        for index in range(count):
            item = items.nth(index)
            title = (await item.locator(".movie-title .title").inner_text()).strip()
            movie_id = await item.get_attribute("data-id")
            if normalize_name(title) == wanted and movie_id:
                log_message(f"识别影片：{title}（movieId={movie_id}）", "success")
                return int(movie_id)
            if wanted in normalize_name(title) and movie_id:
                partial.append((title, movie_id))
        if len(partial) == 1:
            title, movie_id = partial[0]
            log_message(f"模糊匹配影片：{title}（movieId={movie_id}）", "warning")
            return int(movie_id)
        if partial:
            raise RuntimeError(f"影片名不唯一：{'、'.join(name for name, _ in partial[:6])}；请填写 movieId")
        raise RuntimeError("未在猫眼热映列表找到影片；请在高级设置中填写 movieId")

    async def discover_cinema_id(
        self, page: Page, movie_id: int, cinema_name: str, config: Dict[str, Any]
    ) -> int:
        query: Dict[str, Any] = {"movieId": movie_id, "ci": config["city_id"]}
        if config.get("latitude") not in (None, ""):
            query["lat"] = config["latitude"]
        if config.get("longitude") not in (None, ""):
            query["lng"] = config["longitude"]
        await page.goto(
            "https://m.maoyan.com/mtrade/cinema/movie?" + urlencode(query),
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        await page.wait_for_timeout(2_500)

        wanted = normalize_name(cinema_name)
        seen: Dict[str, str] = {}
        stable_rounds = 0
        for _ in range(35):
            items = page.locator(".cinema-wrap .list-wrap .item")
            count = await items.count()
            before = len(seen)
            for index in range(count):
                item = items.nth(index)
                try:
                    title = (await item.locator(".title-block .title").inner_text()).strip()
                except Exception:
                    continue
                seen[title] = title
            exact = [name for name in seen if normalize_name(name) == wanted]
            if len(exact) == 1:
                chosen = exact[0]
                locator = page.locator(".cinema-wrap .list-wrap .item").filter(has_text=chosen).first
                await locator.click()
                await page.wait_for_timeout(1_500)
                found = re.search(r"cinemaId=(\d+)", page.url)
                if not found:
                    raise RuntimeError("已找到影院，但未能识别 cinemaId")
                log_message(f"识别影院：{chosen}（cinemaId={found.group(1)}）", "success")
                return int(found.group(1))
            if len(exact) > 1:
                raise RuntimeError("出现多个同名影院，请填写 cinemaId")

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(bounded_interval(config.get("poll_interval", 5)) * 1000)
            stable_rounds = stable_rounds + 1 if len(seen) == before else 0
            if stable_rounds >= 3:
                break

        partial = [name for name in seen if wanted in normalize_name(name)]
        if len(partial) == 1:
            chosen = partial[0]
            locator = page.locator(".cinema-wrap .list-wrap .item").filter(has_text=chosen).first
            await locator.click()
            await page.wait_for_timeout(1_500)
            found = re.search(r"cinemaId=(\d+)", page.url)
            if not found:
                raise RuntimeError("已找到影院，但未能识别 cinemaId")
            log_message(f"模糊匹配影院：{chosen}（cinemaId={found.group(1)}）", "warning")
            return int(found.group(1))
        if len(partial) > 1:
            raise RuntimeError(f"影院名不唯一：{'、'.join(partial[:8])}；请填写 cinemaId")
        raise RuntimeError(f"未找到影院“{cinema_name}”；请核对城市、名称，或填写 cinemaId")

    async def fetch_show_payload(self, page: Page, show_url: str, first: bool) -> Dict[str, Any]:
        predicate = lambda response: "/cinema/cinema/shows.json" in response.url
        async with page.expect_response(predicate, timeout=30_000) as response_info:
            if first or "/mtrade/cinema/cinema" not in page.url:
                await page.goto(show_url, wait_until="domcontentloaded", timeout=30_000)
            else:
                await page.reload(wait_until="domcontentloaded", timeout=30_000)
        response = await response_info.value
        if response.status in {401, 403, 429}:
            raise PlatformBlocked(f"猫眼返回 HTTP {response.status}，已停止自动请求，请人工检查登录/验证或稍后重试")
        if response.status != 200:
            raise RuntimeError(f"排片读取失败：HTTP {response.status}")
        payload = await response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise PlatformBlocked("猫眼排片响应不是成功状态，已停止；请在浏览器检查提示")
        if not isinstance(payload.get("data"), dict) or not isinstance(payload["data"].get("movies"), list):
            raise RuntimeError("排片响应结构不兼容，不能当作暂无场次")
        expected_cinema = parse_qs(urlsplit(show_url).query).get("cinemaId", [None])[0]
        if str(payload["data"].get("cinemaId")) != expected_cinema:
            raise PlatformBlocked("排片响应的影院 ID 与目标不一致，已停止以避免操作错误影院")
        return payload

    async def wait_for_verification(self, page: Page, timeout: int = 180) -> bool:
        set_status(phase="verification_required", message="请在自动化浏览器中完成人机验证")
        log_message("猫眼要求人机验证。程序已暂停，请手动完成；不会尝试绕过。", "warning")
        await page.bring_to_front()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not stop_event.is_set():
            if not await self._verification_present(page):
                log_message("验证页面已离开，恢复监控", "success")
                return True
            await asyncio.sleep(2)
        return False

    async def resolve_target(
        self, page: Page, config: Dict[str, Any]
    ) -> tuple[int, int]:
        movie_id = int(config.get("movie_id") or 0)
        if not movie_id:
            movie_id = await self.discover_movie_id(page, config["movie_name"])
        cinema_id = int(config.get("cinema_id") or 0)
        if not cinema_id:
            cinema_id = await self.discover_cinema_id(page, movie_id, config["cinema_name"], config)
        return movie_id, cinema_id

    async def analyze(self, raw_config: Dict[str, Any]) -> Dict[str, Any]:
        assert self.runtime.command_lock is not None
        async with self.runtime.command_lock:
            config = validate_analysis_config(raw_config)
            page = await self.runtime.get_page()
            set_status(phase="analyzing", message="正在读取影院当前全部排片日期和电影")
            movie_id, cinema_id = await self.resolve_target(page, config)
            show_url = "https://m.maoyan.com/mtrade/cinema/cinema?" + urlencode(
                {"cinemaId": cinema_id, "movieId": movie_id, "date": config["show_date"]}
            )
            payload = await self.fetch_show_payload(page, show_url, True)
            verify_target_names(payload, config, movie_id)
            saved_days = save_schedule_snapshot(
                cinema_id, movie_id, config.get("cinema_name", ""), payload, all_movies=True
            )
            analysis = analyze_schedule_history(
                cinema_id,
                movie_id,
                config["show_date"],
                config["lookback_days"],
                payload,
            )
            analysis["cinema_name"] = config.get("cinema_name", "")
            analysis["movie_name"] = config.get("movie_name", "")
            set_status(
                phase="analysis_ready",
                message=f"识别到 {len(analysis['halls'])} 个可能影厅",
                schedule_analysis=analysis,
            )
            log_message(
                f"排片侦察完成：保存 {saved_days} 个当前日期、"
                f"{analysis['live_movie_count']} 部电影、{analysis['live_show_count']} 场排片，合并历史后识别到 "
                f"{len(analysis['halls'])} 个影厅",
                "success",
            )
            log_message(analysis["coverage_note"])
            if analysis["missing_dates"]:
                log_message(
                    "以下日期猫眼当前响应和本地历史均无数据："
                    + "、".join(analysis["missing_dates"]),
                    "warning",
                )
            return analysis

    async def _open_seat_and_apply_preset(
        self,
        page: Page,
        seat_url: str,
        chosen: Dict[str, Any],
        preset: Optional[Dict[str, Any]],
        cinema_id: int,
        movie_id: int,
    ) -> None:
        self.seat_layout = None
        payload_candidates: List[tuple[str, Dict[str, Any], int]] = []
        payload_ready = asyncio.Event()
        set_status(phase="loading_seats", message="正在等待选座页和座位图响应", seat_diagnostics=[])

        async def collect_response(response: Any) -> None:
            try:
                resource_type = response.request.resource_type
                content_type = (response.headers.get("content-type") or "").lower()
                if resource_type not in {"xhr", "fetch"} or "json" not in content_type:
                    return
                if not urlsplit(response.url).path.endswith("/seat/v8/show/seats.json"):
                    return
                if response.status != 200:
                    return
                payload = await response.json()
                if isinstance(payload, dict) and isinstance((payload.get("data") or {}).get("seat"), dict):
                    count = sum(len(row.get("seats", [])) for region in payload["data"]["seat"].get("regions", []) for row in region.get("rows", []))
                    payload_candidates.append((response.url, payload, count))
                    payload_ready.set()
            except Exception:
                return

        page.on("response", collect_response)
        try:
            await page.goto(seat_url, wait_until="domcontentloaded", timeout=30_000)
            # 登录重定向期间继续监听；旧版固定六秒后移除监听会漏掉登录后的座位响应。
            if not await self._wait_for_manual_login(page, seconds=180):
                raise RuntimeError("进入选座页时登录未确认，请检查浏览器")
            set_status(phase="loading_seats", message="登录已确认，正在等待座位图")
            if await self._verification_present(page):
                raise PlatformBlocked("选座页要求验证，请手动完成后重试")
            try:
                await asyncio.wait_for(payload_ready.wait(), timeout=20)
            except asyncio.TimeoutError:
                log_message("20 秒内未收到 v8 座位图响应，不继续自动点击", "warning")
        finally:
            page.remove_listener("response", collect_response)
        seat_payload = max(payload_candidates, key=lambda item: item[2])[1] if payload_candidates else None
        if payload_candidates:
            source, _, count = max(payload_candidates, key=lambda item: item[2])
            log_message(f"已从 {urlsplit(source).path or '页面内嵌数据'} 识别 {count} 个座位")
        else:
            title = ""
            try:
                title = await page.title()
            except Exception:
                pass
            log_message(
                f"未捕获到结构化座位数据（页面：{title or page.url}），将保留页面供人工选座",
                "warning",
            )

        if await self._verification_present(page):
            raise PlatformBlocked("选座页要求验证，已停止，请手动完成")
        if urlsplit(page.url).hostname != "m.maoyan.com" or urlsplit(page.url).path != "/mtrade/cinema/seat":
            raise RuntimeError("尚未进入猫眼选座页，请检查浏览器提示")
        await page.bring_to_front()

        if not preset:
            set_status(
                phase="seat_selection",
                message="已进入选座页；该影厅没有座位预设，请人工选座",
            )
            log_message(f"{chosen['hall']} 没有座位预设，请在浏览器中人工选座。", "warning")
            return
        if not seat_payload:
            set_status(
                phase="seat_selection",
                message="已进入选座页；无法读取座位图，请人工选座",
            )
            return

        try:
            self.seat_layout = parse_v8_layout(seat_payload)
            log_message(f"v8 适配器：{len(self.seat_layout.rows)} 排，{len(self.seat_layout.records)} 个实体座位")
            await page.locator(REGION_SELECTOR).wait_for(state="visible", timeout=15_000)
            selected_group = await self._select_maoyan_group(page, preset)
        except TaskStopped:
            raise
        except Exception as exc:
            # 不将选座失败重新抛给排片刷新循环，避免刷新/重复点击已选座位。
            await self._save_seat_diagnostics(page, seat_payload, str(exc))
            set_status(phase="seat_selection", message=f"选座适配未完成：{exc}；请在猫眼核对", seat_diagnostics=[str(exc)])
            log_message(f"选座停止：{exc}", "warning")
            return
        if not selected_group:
            set_status(
                phase="seat_selection",
                message="无法安全确认预设座位，请在猫眼核对已选状态并人工处理",
            )
            log_message(
                "没有候选组合能同时满足：座位存在、状态未明确不可售、页面元素可精确定位。",
                "warning",
            )
            return
        group_index, labels = selected_group

        try:
            _, total_price = await self._maoyan_checkout(page)
        except Exception as exc:
            set_status(phase="seat_selection", message=f"座位已选，但无法核对确认按钮和总价：{exc}")
            log_message("座位已选，请在猫眼人工核对总价和提交；未尝试锁座", "warning")
            return

        pending = {
            "movie": chosen.get("movie", ""),
            "cinema": status_snapshot().get("current_task", {}).get("cinema_name", ""),
            "cinema_id": cinema_id,
            "movie_id": movie_id,
            "date": chosen["date"],
            "time": chosen["time"],
            "hall": chosen["hall"],
            "seats": labels,
            "preset_priority": group_index + 1,
            "reference_price": chosen.get("price"),
            "total_price": total_price,
            "adapter": "maoyan_v8",
            "seat_url": page.url,
            "expires_at": time.time() + 120,
            "confirm_text": "确认锁座并进入订单",
            "confirmation_token": secrets.token_urlsafe(24),
        }
        auto_submit = bool((status_snapshot().get("current_task") or {}).get("auto_submit_order", False))
        set_status(
            phase="order_confirmation",
            message="座位已核对，准备自动提交待支付订单" if auto_submit else "预设座位已选择，请在 WebUI 核对后确认锁座",
            pending_order=pending,
        )
        log_message(
            f"已选择第 {group_index + 1} 组预设：" + "、".join(labels)
            + ("；按任务设置自动提交待支付订单。" if auto_submit else "；等待 WebUI 二次确认。"),
            "success",
        )
        if auto_submit:
            # run 已持有浏览器锁，不能再次申请同一 asyncio.Lock。
            try:
                await self.confirm_order(pending["confirmation_token"], already_locked=True)
            except Exception as exc:
                if status_snapshot()["phase"] != "order_result_unknown":
                    set_status(phase="seat_selection", pending_order=None, message=f"自动提交前校验失败：{exc}")
                log_message(f"自动提交流程未确认完成：{exc}；请在猫眼核对，不自动重试", "warning")

    async def _save_seat_diagnostics(self, page: Page, payload: Dict[str, Any], error: str) -> None:
        """只保存用于布局回归的白名单字段，不保存整个响应/账号/请求签名。"""
        regions = []
        for region in ((payload.get("data") or {}).get("seat") or {}).get("regions", []):
            safe = {key: region[key] for key in ("regionId", "regionName", "canSell") if key in region}
            safe["rows"] = []
            for row in region.get("rows", []):
                safe_row = {key: row[key] for key in ("rowId", "rowNum") if key in row}
                safe_row["seats"] = [{key: seat[key] for key in ("rowId", "columnId", "seatNo", "seatStatus", "status", "seatType") if key in seat} for seat in row.get("seats", [])]
                safe["rows"].append(safe_row)
            regions.append(safe)
        try:
            observed = await self._observe_maoyan_dom(page)
            snapshot = {"error": error, "observed": observed, "data": {"seat": {"regions": regions}}}
            RUNTIME_DIR.mkdir(exist_ok=True)
            (RUNTIME_DIR / "last-seat-diagnostics.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
            log_message("已保存脱敏座位布局诊断到 .runtime/last-seat-diagnostics.json")
        except Exception:
            log_message("未能保存座位布局诊断", "warning")

    async def _observe_maoyan_dom(self, page: Page):
        observed = await page.evaluate("""({region, row, nav}) => ({
            regions: document.querySelectorAll(region).length,
            labels: [...document.querySelectorAll(nav)].map(el => el.textContent.trim()),
            counts: [...document.querySelectorAll(region + ' ' + row)]
                .map(el => el.querySelectorAll('.my-seat').length)
        })""", {"region": REGION_SELECTOR, "row": ROW_SELECTOR, "nav": NAV_SELECTOR})
        return observed

    async def _verify_maoyan_dom(self, page: Page) -> None:
        observed = await self._observe_maoyan_dom(page)
        assert self.seat_layout is not None
        verify_dom_layout(self.seat_layout, observed)

    async def _select_maoyan_group(self, page: Page, preset: Dict[str, Any]):
        assert self.seat_layout is not None
        await self._verify_maoyan_dom(page)
        if await self._selected_seat_labels(page):
            raise SeatLayoutError("页面已有选中座位，程序不会覆盖；请人工处理或清空后重新监控")
        reasons = []
        for group_index, group in enumerate(preset["groups"]):
            try:
                records = resolve_group(self.seat_layout, group)
            except SeatLayoutError as exc:
                reason = f"候选 {group_index + 1}：{exc}"
                reasons.append(reason)
                log_message(reason, "warning")
                continue
            set_status(phase="selecting_seats", message=f"正在选择第 {group_index + 1} 组预设")
            expected = []
            for record in records:
                if stop_event.is_set():
                    raise TaskStopped("选座已取消")
                await self._verify_maoyan_dom(page)
                if sorted(await self._selected_seat_labels(page)) != sorted(expected):
                    raise SeatLayoutError("点击前已选座位发生变化，停止以免覆盖人工操作")
                locator = page.locator(REGION_SELECTOR).locator(ROW_SELECTOR).nth(record["dom_row"]).locator(".my-seat").nth(record["dom_seat"])
                if await locator.evaluate("el => el.tagName") != "IMG":
                    raise SeatLayoutError("目标不是座位图片，停止点击")
                await locator.click(timeout=10_000)
                expected.append(f"{record['row']}排{record['column']}座")
                # 等待 React 的已选列表更新，不以 click 返回作为选座成功。
                for _ in range(20):
                    actual = await self._selected_seat_labels(page)
                    if sorted(actual) == sorted(expected):
                        break
                    if any(label not in expected for label in actual):
                        raise SeatLayoutError("页面选中了非预设座位，请人工核对，不自动撤销或重试")
                    await asyncio.sleep(0.1)
                else:
                    raise SeatLayoutError(f"未确认选中 {expected[-1]}，可能已售或页面提示限制，请人工核对")
            set_status(seat_diagnostics=reasons)
            return group_index, expected
        set_status(seat_diagnostics=reasons)
        return None

    async def _maoyan_checkout(self, page: Page):
        locator = page.locator(CHECKOUT_SELECTOR)
        if await locator.count() != 1 or not await locator.is_visible():
            raise SeatLayoutError("未找到唯一的猫眼确认选座控件")
        return locator, checkout_total(await locator.inner_text())

    async def _exact_seat_locator(
        self, page: Page, record: Dict[str, str], row: str, wanted_seat: str
    ) -> Any:
        """只定位带明确座位属性的元素，不做模糊文字匹配。"""
        selectors = [
            f'[data-row-id="{record["row"]}"][data-column-id="{record["column"]}"]',
            f'[data-row="{record["row"]}"][data-column="{record["column"]}"]',
            f'[data-seat-id="{record["id"]}"]',
            f'[data-seat-no="{record["seat_no"]}"]' if record["seat_no"] else "",
            f'[aria-label="{row}排{wanted_seat}座"]',
        ]
        for selector in selectors:
            if not selector:
                continue
            locator = page.locator(selector)
            if (
                await locator.count() == 1
                and await locator.first.is_visible()
                and await locator.first.is_enabled()
            ):
                return locator.first
        return None

    async def _select_first_available_group(
        self, page: Page, records: List[Dict[str, str]], preset: Dict[str, Any]
    ) -> Optional[tuple[int, List[str]]]:
        if await self._selected_seat_labels(page):
            return None  # 不改变用户已经选择的座位。
        for group_index, group in enumerate(preset["groups"]):
            selected = resolve_seat_group(records, group)
            if len(selected) != len(group["seats"]):
                continue
            if any(seat_record_is_known_unavailable(record) for record in selected):
                continue
            locators = []
            for record, wanted_seat in zip(selected, group["seats"]):
                locator = await self._exact_seat_locator(
                    page, record, str(group["row"]), str(wanted_seat)
                )
                if locator is None:
                    locators = []
                    break
                state = await locator.get_attribute("aria-selected")
                if state is None:
                    state = await locator.get_attribute("aria-pressed")
                if state != "false":
                    locators = []
                    break  # 未验证的页面结构不猜测“可选/已选”。
                locators.append(locator)
            if len(locators) != len(selected):
                continue

            clicked = []
            try:
                for locator in locators:
                    await locator.click()
                    clicked.append(locator)
                    await page.wait_for_timeout(150)
            except Exception:
                log_message("部分选座操作结果不明确，请在猫眼手动核对；不会反向点击或切换下一组", "warning")
                return None
            labels = [f"{group['row']}排{seat}座" for seat in group["seats"]]
            if sorted(await self._selected_seat_labels(page)) != sorted(labels):
                log_message("无法核实页面完整的已选座位列表，停止自动操作，请人工核对", "warning")
                return None
            return group_index, labels
        return None

    async def _selected_seat_labels(self, page: Page) -> List[str]:
        if self.seat_layout is not None:
            texts = await page.locator(SUMMARY_SELECTOR).all_text_contents()
            return normalize_summary(texts, self.seat_layout.region_name)
        # 只接受明确的可访问性状态；Canvas、CSS 私有状态须另行实测适配。
        return await page.evaluate("""() => [...document.querySelectorAll(
            '[aria-selected="true"][aria-label], [aria-pressed="true"][aria-label]'
        )].map(el => el.getAttribute('aria-label'))
          .filter(label => /^\\d+排\\d+座$/.test(label))""")

    async def confirm_order(self, confirmation_token: str, *, already_locked: bool = False) -> Dict[str, Any]:
        assert self.runtime.command_lock is not None
        async with (nullcontext() if already_locked else self.runtime.command_lock):
            pending = status_snapshot().get("pending_order")
            if not pending:
                raise RuntimeError("当前没有等待确认的预设座位")
            expected_token = str(pending.get("confirmation_token") or "")
            if not expected_token or not secrets.compare_digest(expected_token, confirmation_token):
                raise RuntimeError("确认令牌无效或已过期")
            if stop_event.is_set() or time.time() > pending.get("expires_at", 0):
                set_status(pending_order=None)
                raise RuntimeError("确认已取消或超时，请在猫眼手动核对")
            page = await self.runtime.get_page()
            if page.url != pending.get("seat_url"):
                set_status(pending_order=None)
                raise RuntimeError("浏览器场次已变化，未执行锁座")
            if sorted(await self._selected_seat_labels(page)) != sorted(pending["seats"]):
                set_status(pending_order=None)
                raise RuntimeError("页面座位与待确认记录不一致，未执行锁座")

            if pending.get("adapter") == "maoyan_v8":
                if self.seat_layout is None:
                    raise RuntimeError("选座适配会话已丢失，请人工处理")
                await self._verify_maoyan_dom(page)
                button, actual_total = await self._maoyan_checkout(page)
                if actual_total != pending.get("total_price"):
                    set_status(pending_order=None, phase="seat_selection", message="总价发生变化，请在猫眼重新核对")
                    raise RuntimeError("猫眼总价已变化，未执行锁座")
                button_name = "确认选座"
            else:
                buttons = []
                for name in ("确认选座", "去结算", "确认座位"):
                    locator = page.get_by_role("button", name=name, exact=True)
                    for index in range(await locator.count()):
                        item = locator.nth(index)
                        if await item.is_visible() and await item.is_enabled():
                            buttons.append((name, item))
                if len(buttons) != 1:
                    raise RuntimeError("未能唯一识别猫眼的“确认选座/去结算”按钮，未执行锁座")
                button_name, button = buttons[0]
            with status_lock:
                if stop_event.is_set() or not grabber_status.get("pending_order"):
                    raise RuntimeError("确认已取消，未执行锁座")
                # 点击前消耗令牌。超时/网络不确定时也不能重复点击。
                grabber_status.update(pending_order=None, phase="creating_order", message=f"正在点击“{button_name}”")
            attempted_at = time.time()
            try:
                await button.click(timeout=10_000)
                await page.wait_for_timeout(2_000)
                await page.bring_to_front()
            finally:
                set_status(
                    phase="order_result_unknown",
                    message="已尝试提交选座，结果须在猫眼核对；不会自动重试，请勿重复下单",
                )
                try:
                    order_notifications.publish(pending, attempted_at)
                except Exception as exc:
                    log_message(f"付款提醒保存失败，请立即在猫眼核对订单：{exc}", "error")
            result = {
                key: value for key, value in {**pending, "url": page.url}.items()
                if key != "confirmation_token"
            }
            log_message("已尝试点击确认选座，尚未验证订单是否创建；请在猫眼核对并人工支付。", "warning")
            return result

    async def run(self, raw_config: Dict[str, Any]) -> None:
        assert self.runtime.command_lock is not None
        async with self.runtime.command_lock:
            config = validate_config(raw_config)
            page = await self.runtime.get_page()
            set_status(phase="preparing", message="检查登录和目标信息")

            if not await self.ensure_login(page):
                if stop_event.is_set():
                    set_status(phase="stopped", message="任务已停止")
                    log_message("任务已由用户停止", "warning")
                    return
                raise RuntimeError("等待登录超时")

            movie_id, cinema_id = await self.resolve_target(page, config)
            config["movie_id"] = movie_id
            config["cinema_id"] = cinema_id
            set_status(current_task=config)

            show_url = "https://m.maoyan.com/mtrade/cinema/cinema?" + urlencode(
                {"cinemaId": cinema_id, "movieId": movie_id, "date": config["show_date"]}
            )
            monitor_at = datetime.fromisoformat(config["monitor_at"])
            while datetime.now() < monitor_at and not stop_event.is_set():
                seconds = int((monitor_at - datetime.now()).total_seconds())
                set_status(phase="waiting", message=f"距离监控开始还有 {seconds} 秒")
                await asyncio.sleep(min(max(seconds, 1), 10))
            if stop_event.is_set():
                set_status(phase="stopped", message="任务已停止")
                log_message("任务已由用户停止", "warning")
                return

            interval = config["poll_interval"]
            deadline = time.monotonic() + config["monitor_minutes"] * 60
            first = True
            attempts = 0
            consecutive_errors = 0
            last_halls = ""
            set_status(phase="monitoring", message=f"每 {interval:g} 秒刷新一次")
            log_message(
                f"开始监控 {config['show_date']} {config['time_range']['start']}-"
                f"{config['time_range']['end']}，刷新间隔 {interval:g} 秒"
            )

            while time.monotonic() < deadline and not stop_event.is_set():
                attempts += 1
                try:
                    payload = await self.fetch_show_payload(page, show_url, first)
                    verify_target_names(payload, config, movie_id)
                    first = False
                    consecutive_errors = 0
                    save_schedule_snapshot(
                        cinema_id, movie_id, config.get("cinema_name", ""), payload, all_movies=True
                    )
                    all_shows = list(iter_showtimes(payload, movie_id, config["show_date"]))
                    halls = "、".join(sorted({show["hall"] for show in all_shows if show["hall"]}))
                    if halls and halls != last_halls:
                        log_message(f"已识别影厅：{halls}")
                        last_halls = halls

                    matches = filter_showtimes(payload, movie_id, config)
                    if matches:
                        chosen = matches[0]
                        public_show = {
                            key: value
                            for key, value in chosen.items()
                            if key not in {"keyword_rank", "time_distance"}
                        }
                        set_status(
                            phase="matched",
                            message=f"已匹配 {chosen['time']} {chosen['hall']}",
                            matched_show=public_show,
                        )
                        log_message(
                            f"匹配成功：{chosen['date']} {chosen['time']}｜{chosen['hall']}｜"
                            f"{chosen['language']} {chosen['format']}",
                            "success",
                        )
                        if config["auto_open_seat"]:
                            seat_url = "https://m.maoyan.com/mtrade/cinema/seat?" + urlencode(
                                {
                                    "seqNo": chosen["seq_no"],
                                    "cinemaId": cinema_id,
                                    "movieId": movie_id,
                                    "date": chosen["date"],
                                }
                            )
                            preset = preset_for_hall(config["seat_presets"], chosen["hall"])
                            try:
                                await self._open_seat_and_apply_preset(
                                    page, seat_url, chosen, preset, cinema_id, movie_id,
                                )
                            except (PlatformBlocked, TaskStopped):
                                raise
                            except Exception as exc:
                                set_status(phase="seat_selection", message=f"选座页操作中断，请人工核对：{exc}")
                                log_message("已停止本次场次处理，不自动刷新或重复点击座位", "warning")
                        return

                    set_status(
                        phase="monitoring",
                        message=f"第 {attempts} 次刷新：暂无符合条件的可售场次",
                    )
                    if attempts == 1 or attempts % 10 == 0:
                        log_message(f"第 {attempts} 次刷新：发现 {len(all_shows)} 个目标日期场次，暂无匹配")
                except PlatformBlocked:
                    raise
                except Exception as exc:
                    consecutive_errors += 1
                    log_message(f"第 {attempts} 次刷新失败：{exc}", "warning")
                    if await self._verification_present(page):
                        raise PlatformBlocked("猫眼要求人工验证，已停止自动请求；完成后请重新启动")
                    if consecutive_errors >= 5:
                        raise RuntimeError("连续 5 次读取排片失败，已停止以避免持续请求")

                # 以本次读取完成为起点；配置变更不会触发额外请求。
                finished_at = time.monotonic()
                while not stop_event.is_set() and time.monotonic() < deadline:
                    interval = status_snapshot()["current_task"]["poll_interval"]
                    delay = interval if not consecutive_errors else min(max(interval, 5) * (2 ** min(consecutive_errors, 5)), max(MAX_POLL_INTERVAL, interval))
                    remaining = finished_at + delay - time.monotonic()
                    set_status(effective_poll_interval=delay, next_refresh_in=max(0, round(remaining, 1)))
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(remaining, 0.2))

            if stop_event.is_set():
                set_status(phase="stopped", message="任务已停止")
                log_message("任务已由用户停止", "warning")
            else:
                set_status(phase="timeout", message="监控时间已结束")
                log_message("监控时间已结束，未找到符合条件的场次", "warning")


assistant = MaoyanTicketAssistant(browser_runtime)


def validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("配置必须是 JSON 对象")
    config = dict(raw or {})
    if not config.get("movie_name") and not config.get("movie_id"):
        raise ValueError("请填写电影名称或 movieId")
    if not config.get("cinema_name") and not config.get("cinema_id"):
        raise ValueError("请填写影院名称或 cinemaId")
    try:
        datetime.strptime(config["show_date"], "%Y-%m-%d")
        if datetime.fromisoformat(config["monitor_at"]).tzinfo is not None:
            raise ValueError("开始监控时间请使用本机时间，不附带时区偏移")
        parse_minutes(config["time_range"]["start"])
        parse_minutes(config["time_range"]["end"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"日期或时间配置无效：{exc}") from exc

    config["city_id"] = int(config.get("city_id") or 1)
    config["poll_interval"] = bounded_interval(config.get("poll_interval", 5))
    config["monitor_minutes"] = max(1, min(180, int(config.get("monitor_minutes") or 30)))
    for key in ("auto_open_seat", "require_preferred_hall", "auto_submit_order"):
        config.setdefault(key, True)
        if not isinstance(config[key], bool):
            raise ValueError(f"{key} 必须是布尔值")
    config["seat_presets"] = normalize_seat_presets(config.get("seat_presets"))
    config["browser_preference"] = str(config.get("browser_preference") or "auto").lower()
    if config["browser_preference"] not in {"auto", "chrome", "msedge", "chromium"}:
        raise ValueError("浏览器类型无效")
    if config.get("movie_id") not in (None, ""):
        config["movie_id"] = int(config["movie_id"])
    if config.get("cinema_id") not in (None, ""):
        config["cinema_id"] = int(config["cinema_id"])
    if config.get("max_price") not in (None, ""):
        config["max_price"] = float(config["max_price"])
        if not math.isfinite(config["max_price"]) or config["max_price"] < 0:
            raise ValueError("参考价必须为非负有限数字")
    for key in ("city_id", "cinema_id", "movie_id"):
        if config.get(key) not in (None, "") and config[key] <= 0:
            raise ValueError(f"{key} 必须为正整数")
    return config


def validate_analysis_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    config = dict(raw or {})
    if not config.get("movie_name") and not config.get("movie_id"):
        raise ValueError("请填写电影名称或 movieId")
    if not config.get("cinema_name") and not config.get("cinema_id"):
        raise ValueError("请填写影院名称或 cinemaId")
    try:
        date.fromisoformat(config["show_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"观影日期无效：{exc}") from exc
    config["city_id"] = int(config.get("city_id") or 1)
    config["lookback_days"] = max(1, min(14, int(config.get("lookback_days") or 7)))
    config["browser_preference"] = str(config.get("browser_preference") or "auto").lower()
    if config["browser_preference"] not in {"auto", "chrome", "msedge", "chromium"}:
        raise ValueError("浏览器类型无效")
    if config.get("movie_id") not in (None, ""):
        config["movie_id"] = int(config["movie_id"])
    if config.get("cinema_id") not in (None, ""):
        config["cinema_id"] = int(config["cinema_id"])
    return config


def _task_worker(config: Dict[str, Any]) -> None:
    try:
        future = browser_runtime.submit(stop_guard(assistant.run(config)))
        future.result()
    except TaskStopped:
        if status_snapshot()["phase"] != "order_result_unknown":
            set_status(phase="stopped", message="任务已停止", pending_order=None)
        log_message("已停止自动操作；已经发出的页面请求或订单不能撤回，请在猫眼核对")
    except PlatformBlocked as exc:
        set_status(phase="verification_required", message=str(exc))
        log_message(str(exc), "warning")
    except Exception as exc:
        set_status(phase="error", message=str(exc))
        log_message(f"任务失败：{exc}", "error")
    finally:
        set_status(running=False, next_refresh_in=None)


def _analysis_worker(config: Dict[str, Any]) -> None:
    try:
        future = browser_runtime.submit(stop_guard(assistant.analyze(config)))
        future.result()
    except TaskStopped:
        set_status(phase="stopped", message="侦察已停止")
    except Exception as exc:
        set_status(phase="error", message=str(exc))
        log_message(f"排片侦察失败：{exc}", "error")
    finally:
        set_status(analysis_running=False)


@app.route("/")
def index():
    return render_template("index.html")


@app.get("/api/cinemas")
def get_cinema_catalog():
    try:
        return jsonify(catalog_store.read())
    except (CatalogError, OSError) as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/notifications")
def get_order_notification():
    return jsonify({"event": order_notifications.snapshot()})


@app.post("/api/notifications/ack")
def acknowledge_order_notification():
    if not order_notifications.acknowledge(request.get_json().get("event_id")):
        return jsonify({"error": "提醒已更新，请核对最新提醒"}), 409
    return jsonify({"success": True})


@app.get("/api/cinemas/sync")
def get_catalog_sync():
    return jsonify(catalog_sync.snapshot())


@app.post("/api/cinemas/sync")
@serialized_control
def sync_cinema_catalog():
    snapshot = status_snapshot()
    if snapshot["running"] or snapshot["analysis_running"] or snapshot["pending_order"] or snapshot["phase"] in {"creating_order", "order_result_unknown"}:
        return jsonify({"error": "请先结束当前购票或侦察任务，再更新影院目录"}), 409
    if catalog_sync.snapshot()["running"]:
        return jsonify({"error": "影院目录正在更新"}), 409
    try:
        payload = request.get_json()
        catalog_sync.start(payload.get("city_id"), payload.get("interval", 3))
        return jsonify({"success": True}), 202
    except (CatalogError, TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/cinemas/stop")
@serialized_control
def stop_catalog_sync():
    with catalog_sync.lock:
        catalog_sync.cancel.set()
    return jsonify({"success": True})


@app.post("/api/browser/login")
@serialized_control
def open_login():
    if catalog_sync.snapshot()["running"]:
        return jsonify({"error": "请先停止影院目录更新"}), 409
    snapshot = status_snapshot()
    if snapshot["running"] or snapshot["analysis_running"] or snapshot["pending_order"] or snapshot["phase"] == "order_result_unknown":
        return jsonify({"error": "任务运行中，不能切换登录页面"}), 409
    try:
        payload = request.get_json(silent=True) or {}
        browser_runtime.configure_preference(payload.get("browser_preference", "auto"))
        result = browser_runtime.submit(browser_runtime.open_login()).result(timeout=40)
        return jsonify({"success": True, **result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/browser/login-state")
def login_state():
    try:
        snapshot = status_snapshot()
        if not snapshot["browser_ready"]:
            return jsonify({"logged_in": False, "browser_ready": False})
        if snapshot["running"] or snapshot["analysis_running"] or snapshot["phase"] == "creating_order":
            return jsonify({"logged_in": None, "browser_ready": True, "busy": True})
        result = browser_runtime.submit(browser_runtime.login_state()).result(timeout=10)
        return jsonify({"browser_ready": True, **result})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/start")
@serialized_control
def start_task():
    if catalog_sync.snapshot()["running"]:
        return jsonify({"error": "请先停止影院目录更新"}), 409
    with status_lock:
        if grabber_status["running"] or grabber_status["analysis_running"] or grabber_status["pending_order"] or grabber_status["phase"] == "order_result_unknown":
            return jsonify({"error": "已有任务正在运行"}), 409
    try:
        config = validate_config(request.get_json(silent=True) or {})
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        browser_runtime.configure_preference(config["browser_preference"])
    except (TypeError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 409

    stop_event.clear()
    set_status(
        running=True,
        phase="starting",
        message="正在启动",
        current_task=config,
        matched_show=None,
        pending_order=None,
        logs=[],
    )
    threading.Thread(target=_task_worker, args=(config,), daemon=True, name="maoyan-task").start()
    return jsonify({"success": True, "poll_interval": config["poll_interval"]}), 202


@app.post("/api/analyze")
@serialized_control
def analyze_schedule():
    if catalog_sync.snapshot()["running"]:
        return jsonify({"error": "请先停止影院目录更新"}), 409
    with status_lock:
        if grabber_status["running"] or grabber_status["analysis_running"] or grabber_status["pending_order"] or grabber_status["phase"] == "order_result_unknown":
            return jsonify({"error": "已有任务正在运行"}), 409
    try:
        config = validate_analysis_config(request.get_json(silent=True) or {})
        browser_runtime.configure_preference(config["browser_preference"])
    except (TypeError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    stop_event.clear()
    set_status(
        analysis_running=True,
        phase="analyzing",
        message="正在启动排片侦察",
        schedule_analysis=None,
        logs=[],
    )
    threading.Thread(
        target=_analysis_worker, args=(config,), daemon=True, name="maoyan-analysis"
    ).start()
    return jsonify({"success": True}), 202


@app.post("/api/order/confirm")
@serialized_control
def confirm_order():
    pending = status_snapshot().get("pending_order")
    if not pending:
        return jsonify({"error": "当前没有等待确认的订单"}), 409
    try:
        payload = request.get_json(silent=True) or {}
        token = str(payload.get("confirmation_token") or "")
        if not token or not secrets.compare_digest(
            str(pending.get("confirmation_token") or ""), token
        ):
            return jsonify({"error": "确认令牌无效或已过期"}), 403
        future = browser_runtime.submit(assistant.confirm_order(token))
        try:
            result = future.result(timeout=20)
        except TimeoutError:
            future.cancel()
            set_status(pending_order=None, phase="order_result_unknown", message="操作超时，结果不明确，请在猫眼核对，不要重复提交")
            raise
        return jsonify({"success": True, "order_verified": False, "attempt": result})
    except Exception as exc:
        log_message(f"锁座确认未完成，结果以猫眼页面为准：{exc}", "error")
        return jsonify({"error": str(exc)}), 409


@app.post("/api/settings/poll-interval")
def update_poll_interval():
    try:
        interval = bounded_interval(request.get_json().get("poll_interval"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    with status_lock:
        if not grabber_status["running"]:
            return jsonify({"error": "没有正在运行的监控任务"}), 409
        grabber_status["current_task"]["poll_interval"] = interval
    log_message(f"刷新间隔已调整为 {interval:g} 秒（错误退避仍然生效）")
    return jsonify({"success": True, "poll_interval": interval})


@app.post("/api/stop")
def stop_task():
    stop_event.set()
    snapshot = status_snapshot()
    set_status(pending_order=None, message="正在停止……" if snapshot["running"] or snapshot["analysis_running"] else "已取消待确认操作；如有订单请在猫眼核对")
    if not snapshot["running"] and not snapshot["analysis_running"] and snapshot["phase"] != "creating_order":
        set_status(phase="stopped")
    return jsonify({"success": True})


@app.get("/api/status")
def get_status():
    return jsonify(status_snapshot())


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "time": datetime.now().isoformat(timespec="seconds"),
                    "version": APP_VERSION, "revision": LOADED_REVISION,
                    "started_at": STARTED_AT, "pid": os.getpid(),
                    "restart_required": source_revision() != LOADED_REVISION})


if __name__ == "__main__":
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 5000))
        except OSError:
            raise SystemExit("5000 端口已被占用。可能旧后台仍在运行：关闭浏览器不等于停止后台。请在旧终端 Ctrl+C 停止服务后重新运行；不要重复启动。")
    print(f"猫眼助手 {APP_VERSION} | PID {os.getpid()} | 启动于 {STARTED_AT}", flush=True)
    if os.environ.get("MAOYAN_NO_OPEN") != "1":
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(host="127.0.0.1", port=5000, debug=False, threaded=True, use_reloader=False)
