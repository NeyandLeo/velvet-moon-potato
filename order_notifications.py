"""Durable local reminders; an attempted checkout is not verified success."""
import json
import threading
import time
import uuid
from pathlib import Path


class OrderNotifications:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.event = None
        if self.path.exists():
            self.event = json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.event, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def publish(self, pending, attempted_at):
        with self.lock:
            self.event = {
                "id": uuid.uuid4().hex, "kind": "checkout_attempted",
                "title": "选座已提交，请立即核对并付款",
                "message": "请马上打开猫眼核对订单；若已生成待付款订单，请按猫眼倒计时完成付款。提交结果尚未确认，请勿重复下单。",
                "created_at": time.time(), "attempted_at": attempted_at,
                "reference_deadline": attempted_at + 15 * 60,
                "acknowledged": False,
                "details": {key: pending.get(key) for key in ("movie", "cinema", "date", "time", "hall", "seats", "total_price")},
            }
            self._save()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.event))

    def acknowledge(self, event_id):
        with self.lock:
            if not self.event or self.event["id"] != event_id:
                return False
            self.event["acknowledged"] = True
            self._save()
            return True
