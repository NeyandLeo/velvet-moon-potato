"""猫眼 H5 v8 座位布局适配（只解析页面返回数据，不请求下单接口）。

依据 2026-09-04 猫眼公开前端 commons.febb5800.js 中 seat.jsx / lib.js：
按 regions[].rows[].seats[] 渲染；状态 1 可售；可售/已售情侣右半不渲染。
排号显示 rowId，不是用于布局的 rowNum。适配器只自动点击普通单区域座位。
"""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re


class SeatLayoutError(ValueError):
    pass


REGION_SELECTOR = '[class*="seatRegion-"]'
ROW_SELECTOR = '[class*="seatRow-"]'
NAV_SELECTOR = '.nav-region .nav-row'
SUMMARY_SELECTOR = '[class*="selectedList-"] [class*="seatDesc-"]'
CHECKOUT_SELECTOR = 'div[data-bid="b_212zq"]'


@dataclass
class SeatLayout:
    rows: list[dict]
    records: list[dict]
    region_name: str


def parse_v8_layout(payload: dict) -> SeatLayout:
    data = payload.get("data") or {}
    regions = (data.get("seat") or {}).get("regions")
    if not isinstance(regions, list) or len(regions) != 1:
        raise SeatLayoutError("当前仅支持单区域 v8 座位图；多区域或其他格式请人工选座")
    region = regions[0]
    if region.get("canSell") is False:
        raise SeatLayoutError("当前区域不可售")
    source_rows = region.get("rows")
    if not isinstance(source_rows, list) or not source_rows:
        raise SeatLayoutError("座位图没有行数据")
    rows, records, row_ids = [], [], set()
    for row_index, row in enumerate(source_rows):
        seats = row.get("seats")
        if not isinstance(seats, list):
            raise SeatLayoutError("座位行结构不兼容")
        row_id = "" if row.get("rowId") is None else str(row["rowId"])
        blank_row = all(seat.get("seatStatus", seat.get("status")) == 0 for seat in seats)
        # 横向过道也会返回一整行占位，排号可为空且重复；仍保留该行 DOM 索引。
        if not blank_row:
            if not row_id or row_id in row_ids:
                raise SeatLayoutError(f"第 {row_index + 1} 个实体座位行排号缺失或重复")
            row_ids.add(row_id)
        rendered = 0
        for seat in seats:
            seat_type = seat.get("seatType")
            if isinstance(seat_type, int) and not isinstance(seat_type, bool):
                seat_type = {0: "N", 1: "L", 2: "R"}.get(seat_type)
            status = seat.get("seatStatus", seat.get("status"))
            if isinstance(status, bool) or status not in (0, 1, 2, 3, 4):
                raise SeatLayoutError("出现未知座位状态，停止以免错位")
            if status in (1, 2, 3, 4) and seat_type == "R":
                continue
            if status != 0 and seat_type not in {"N", "L"}:
                raise SeatLayoutError("出现未知座位类型，不能确定图片顺序")
            column_id = str(seat.get("columnId", ""))
            if status != 0:
                if not column_id or not seat.get("seatNo"):
                    raise SeatLayoutError("座号或座位标识缺失")
                records.append({
                    "row": row_id, "column": column_id, "seat_no": str(seat["seatNo"]),
                    "status": status, "type": seat_type,
                    "dom_row": row_index, "dom_seat": rendered,
                })
            rendered += 1  # 过道 no-seat 也占一个 DOM 位置。
        rows.append({"label": row_id, "count": rendered})
    identities = [(r["row"], r["column"]) for r in records]
    if len(identities) != len(set(identities)):
        raise SeatLayoutError("存在重复排座号，不能唯一定位")
    return SeatLayout(rows, records, str(region.get("regionName") or ""))


def verify_dom_layout(layout: SeatLayout, observed: dict) -> None:
    if observed.get("regions") != 1:
        raise SeatLayoutError("页面区域数与座位响应不一致")
    if observed.get("labels") != [r["label"] for r in layout.rows]:
        raise SeatLayoutError("页面排号顺序与座位响应不一致")
    if observed.get("counts") != [r["count"] for r in layout.rows]:
        raise SeatLayoutError("页面座位图片数与响应不一致（可能正在加载或页面已改版）")


def resolve_group(layout: SeatLayout, group: dict) -> list[dict]:
    selected = []
    for seat in group["seats"]:
        matches = [r for r in layout.records if r["row"] == str(group["row"]) and r["column"] == str(seat)]
        if len(matches) != 1:
            raise SeatLayoutError(f"找不到唯一的 {group['row']}排{seat}座")
        record = matches[0]
        if record["status"] != 1:
            raise SeatLayoutError(f"{group['row']}排{seat}座不可售（状态 {record['status']}）")
        if record["type"] != "N":
            raise SeatLayoutError("情侣座会联动选择，当前请人工处理")
        selected.append(record)
    return selected


def normalize_summary(texts: list[str], region_name: str) -> list[str]:
    labels = []
    for text in texts:
        match = re.fullmatch(r"(.*?)(\d+)排(\d+)座", text.strip())
        if not match or match[1] not in ("", region_name):
            raise SeatLayoutError("已选座位描述格式或区域名称不一致")
        labels.append(f"{match[2]}排{match[3]}座")
    if len(labels) != len(set(labels)):
        raise SeatLayoutError("已选座位列表有重复项")
    return labels


def checkout_total(text: str) -> str:
    match = re.fullmatch(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)\s*确认选座", text.strip())
    if not match:
        raise SeatLayoutError("无法读出确认选座按钮上的实际总价")
    try:
        total = Decimal(match[1])
        if total <= 0:
            raise SeatLayoutError("总价异常，请人工核对")
        return str(total.quantize(Decimal("0.01")))
    except InvalidOperation as exc:
        raise SeatLayoutError("总价格式异常") from exc
