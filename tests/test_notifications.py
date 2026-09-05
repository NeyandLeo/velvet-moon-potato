import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as module
from order_notifications import OrderNotifications


class NotificationTests(unittest.TestCase):
    def test_reminder_survives_restart_and_stale_ack_cannot_clear_new_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "notification.json"
            store = OrderNotifications(path)
            store.publish({"movie": "奥德赛", "confirmation_token": "secret", "seat_url": "private"}, 100)
            first = store.snapshot()
            self.assertEqual(first["reference_deadline"], 1000)
            self.assertNotIn("secret", path.read_text())
            self.assertNotIn("private", path.read_text())
            restored = OrderNotifications(path)
            self.assertEqual(restored.snapshot(), first)
            restored.publish({"movie": "奥德赛"}, 200)
            self.assertFalse(restored.acknowledge(first["id"]))
            self.assertFalse(restored.snapshot()["acknowledged"])
            self.assertTrue(restored.acknowledge(restored.snapshot()["id"]))
            self.assertTrue(OrderNotifications(path).snapshot()["acknowledged"])

    def test_ack_endpoint_does_not_change_order_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = OrderNotifications(Path(directory) / "notification.json")
            store.publish({}, 100)
            before = module.status_snapshot()
            with patch.object(module, "order_notifications", store):
                client = module.app.test_client()
                self.assertEqual(client.get("/api/notifications").json["event"]["kind"], "checkout_attempted")
                self.assertEqual(client.post("/api/notifications/ack", json={"event_id": "stale"}).status_code, 409)
                self.assertEqual(client.post("/api/notifications/ack", json={"event_id": store.snapshot()["id"]}).status_code, 200)
            self.assertEqual(module.status_snapshot(), before)
