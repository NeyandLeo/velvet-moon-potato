"""Public cinema metadata only; no cookies, login, ticket or order endpoints.

Source: i.maoyan.com/dianying/cities.json, /ajax/filterCinemas?ci=...,
/ajax/cinemaList?cityId=...&offset=...&limit=20. A completed listing is a
snapshot of that endpoint, not a guarantee that every operating cinema is listed.
"""
from __future__ import annotations

import argparse
import copy
import json
import math
import os
import subprocess
from pathlib import Path
import threading
import time
from datetime import datetime
from urllib.parse import urlencode

CITIES = {1: "北京市", 10: "上海市"}
BASE = "https://i.maoyan.com"


class CatalogError(ValueError):
    pass


def interval_value(value):
    if isinstance(value, bool):
        raise CatalogError("更新间隔必须为正数")
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise CatalogError("更新间隔必须为正数") from exc
    if not math.isfinite(result) or result <= 0:
        raise CatalogError("更新间隔必须为有限正数")
    return result


def fetch_public(path, params):
    # macOS system curl is the verified public-data transport. Do not load the
    # user's curlrc, add authentication, follow redirects, spoof a UA or retry.
    try:
        response = subprocess.run(
            ["curl", "-q", "--silent", "--show-error", "--fail", "--max-time", "15",
             "--max-filesize", "4000000", BASE + path + "?" + urlencode(params)],
            capture_output=True, timeout=20, check=True,
        )
        raw = response.stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise CatalogError("公开目录下载失败（需要系统 curl）；遇到拒绝或超时不重试，旧数据保留") from exc
    if len(raw) > 4_000_000:
        raise CatalogError("目录响应超出大小限制")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise CatalogError("猫眼未返回目录数据，可能需要验证；已停止更新") from exc
    if not isinstance(data, dict):
        raise CatalogError("目录响应格式发生变化")
    return data


def normalize_cinema(raw, city_id, districts):
    cinema_id = raw.get("id")
    name, address = raw.get("nm"), raw.get("addr")
    if type(cinema_id) is not int or cinema_id <= 0 or not isinstance(name, str) or not name.strip() or not isinstance(address, str):
        raise CatalogError("影院记录缺少有效 ID、名称或地址")
    address = address.strip()
    local_address = address.removeprefix(CITIES[city_id]).removeprefix(CITIES[city_id][:-1]).strip()
    matches = [d for d in districts if local_address.startswith(d["name"])]
    district = matches[0] if len(matches) == 1 else {"id": None, "name": "待核实区划"}
    return {
        "id": cinema_id, "city_id": city_id, "name": name.strip(), "address": address,
        "district_id": district["id"], "district": district["name"],
        "source_url": f"https://www.maoyan.com/cinema/{cinema_id}",
    }


def collect_city(city_id, interval=3, cancel=None, progress=lambda message: None, fetch=fetch_public):
    if city_id not in CITIES:
        raise CatalogError("仅支持北京和上海")
    interval = interval_value(interval)
    cancel = cancel or threading.Event()
    last_request = None

    def get(path, params):
        nonlocal last_request
        if last_request is not None:
            cancel.wait(max(0, interval - (time.monotonic() - last_request)))
        if cancel.is_set():
            raise CatalogError("更新已停止，保留原有本地目录")
        last_request = time.monotonic()
        return fetch(path, params)

    filters = get("/ajax/filterCinemas", {"ci": city_id})
    try:
        districts = [{"id": item["id"], "name": item["name"]}
                     for item in filters["district"]["subItems"] if item["id"] > 0]
        if not districts or any(type(d["id"]) is not int or not isinstance(d["name"], str) or not d["name"] for d in districts):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise CatalogError("猫眼行政区数据格式变化，未覆盖本地目录") from exc
    offset, records, total = 0, {}, None
    for _ in range(100):
        data = get("/ajax/cinemaList", {"cityId": city_id, "offset": offset, "limit": 20})
        batch, paging = data.get("cinemas"), data.get("paging", {})
        if not isinstance(batch, list) or not isinstance(paging, dict):
            raise CatalogError("猫眼影院列表不可用，已停止更新")
        current_total = paging.get("total")
        if type(current_total) is not int or current_total <= 0 or type(paging.get("hasMore")) is not bool or paging.get("offset") != offset:
            raise CatalogError("猫眼分页信息无效，未覆盖本地目录")
        if total is not None and total != current_total:
            raise CatalogError("更新期间目录总数发生变化，请稍后重新更新")
        total = current_total
        before = len(records)
        for raw in batch:
            if not isinstance(raw, dict):
                raise CatalogError("影院记录格式变化")
            cinema = normalize_cinema(raw, city_id, districts)
            records[cinema["id"]] = cinema
        progress(f"{CITIES[city_id]}：已读取 {len(records)} / {total} 家影院")
        if not paging["hasMore"]:
            if len(records) != total:
                raise CatalogError(f"目录不完整（{len(records)}/{total}），保留旧数据；请稍后更新")
            if cancel.is_set():
                raise CatalogError("更新已停止，保留原有本地目录")
            return {"id": city_id, "name": CITIES[city_id], "updated_at": datetime.now().isoformat(timespec="seconds"),
                    "source": BASE + "/ajax/cinemaList?cityId=" + str(city_id),
                    "listing_complete": True, "source_total": total, "districts": districts,
                    "cinemas": sorted(records.values(), key=lambda row: (row["district"], row["name"], row["id"]))}
        if not batch or before == len(records):
            raise CatalogError("分页没有新增影院，已停止，避免反复请求")
        offset += len(batch)
    raise CatalogError("目录超过 100 页更新上限，保留旧数据")


class CatalogStore:
    def __init__(self, path, seed=None):
        self.path = Path(path)
        self.seed = Path(seed) if seed else None
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            source = self.path if self.path.exists() else self.seed
            if not source or not source.exists():
                return {"schema_version": 1, "cities": []}
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                if data.get("schema_version") != 1 or not isinstance(data.get("cities"), list):
                    raise ValueError()
                return data
            except (ValueError, AttributeError) as exc:
                raise CatalogError("本地影院库损坏，请保留文件并检查，不会静默覆盖") from exc

    def save_city(self, city):
        with self.lock:
            data = self.read()
            data["cities"] = [row for row in data["cities"] if row["id"] != city["id"]] + [city]
            data["cities"].sort(key=lambda row: row["id"])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, self.path)


class CatalogSync:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.cancel = threading.Event()
        self.state = {"running": False, "message": "本地影院库就绪", "revision": 0}

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def start(self, city_id, interval):
        if type(city_id) is not int or city_id not in CITIES:
            raise CatalogError("仅支持北京（1）和上海（10）")
        interval = interval_value(interval)
        with self.lock:
            if self.state["running"]:
                raise CatalogError("已有影院目录正在更新")
            self.cancel.clear()
            self.state.update(running=True, city_id=city_id, message="正在更新 " + CITIES[city_id])
            threading.Thread(target=self._worker, args=(city_id, interval), daemon=True, name="cinema-catalog").start()

    def _worker(self, city_id, interval):
        def progress(message):
            with self.lock:
                self.state["message"] = message
        try:
            city = collect_city(city_id, interval, self.cancel, progress)
            with self.lock:
                if self.cancel.is_set():
                    raise CatalogError("更新已停止，保留原有本地目录")
                self.store.save_city(city)
                self.state["revision"] += 1
                self.state["message"] = f"{city['name']} 更新完成：{len(city['cinemas'])} 家影院"
        except Exception as exc:
            progress("更新未完成：" + str(exc))
        finally:
            with self.lock:
                self.state["running"] = False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="低频下载北京、上海公开影院目录；不访问账户及订单")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "data" / "cinemas.json")
    parser.add_argument("--interval", type=float, default=3)
    args = parser.parse_args()
    store = CatalogStore(args.output)
    for city_id in CITIES:
        store.save_city(collect_city(city_id, args.interval, progress=lambda message: print(message, flush=True)))
        if city_id != list(CITIES)[-1]:
            time.sleep(interval_value(args.interval))
