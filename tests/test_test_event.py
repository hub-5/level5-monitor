import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import events
import monitor

NOW = datetime(2026, 9, 20, 19, 45, 49, tzinfo=timezone.utc)
NAMES = {"general": "General"}


class TestEventTests(unittest.TestCase):
    def setUp(self):
        for stream in ("sys.stdout", "sys.stderr"):
            p = mock.patch(stream, new_callable=io.StringIO)
            p.start()
            self.addCleanup(p.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.events_path = os.path.join(self.dir.name, "events.json")
        self.pending_path = os.path.join(self.dir.name, "push_pending.json")

    def load(self, path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def test_test_event_shape(self):
        ev = events.build_test_event(NOW, "https://example.invalid/run/1")
        self.assertEqual(ev["type"], "TEST")
        self.assertTrue(ev["id"].startswith("test-"))
        self.assertEqual(ev["franchise"], "general")
        self.assertEqual(list(ev), ["id", "timestamp", "url", "franchise", "type", "title",
                                    "summary", "lines", "rawDetail", "truncated"])

    def test_emit_writes_feed_and_pending_through_production_path(self):
        self.assertTrue(events.emit_test_event(self.events_path, self.pending_path, NAMES, NOW))
        doc = self.load(self.events_path)
        self.assertEqual(doc["schemaVersion"], 1)
        ev = doc["events"][0]
        pending = self.load(self.pending_path)
        self.assertEqual(pending["data"]["ids"], ev["id"])
        self.assertEqual(pending["data"]["count"], "1")
        self.assertEqual(pending["title"], "SrHub (prueba): 1 cambio detectado")
        self.assertEqual(pending["body"], "Novedades en General")

    def test_emit_prepends_to_existing_feed(self):
        events.write_events(self.events_path, [{"id": "old"}])
        events.emit_test_event(self.events_path, self.pending_path, NAMES, NOW)
        ids = [e["id"] for e in self.load(self.events_path)["events"]]
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[1], "old")

    def test_emit_refuses_to_overwrite_invalid_feed(self):
        with open(self.events_path, "w", encoding="utf-8") as f:
            f.write("{no json")
        self.assertFalse(events.emit_test_event(self.events_path, self.pending_path, NAMES, NOW))
        with open(self.events_path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "{no json")
        self.assertFalse(os.path.exists(self.pending_path))

    def test_cli_returns_1_on_failure_and_0_on_success(self):
        cfg = os.path.join(self.dir.name, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"franchise_names": NAMES}, f)
        with mock.patch.object(monitor, "CONFIG_PATH", cfg), \
                mock.patch.object(monitor, "EVENTS_PATH", self.events_path), \
                mock.patch.object(monitor, "PENDING_PATH", self.pending_path), \
                mock.patch.object(monitor.sys, "argv", ["monitor.py", "--emit-test-event"]):
            self.assertEqual(monitor.main(), 0)
            with mock.patch("events.write_events", side_effect=OSError("x")):
                self.assertEqual(monitor.main(), 1)


class WorkflowTests(unittest.TestCase):
    def test_workflows_are_valid_yaml_and_wired_as_planned(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML no instalado")
        base = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows")
        with open(os.path.join(base, "monitor.yml"), encoding="utf-8") as f:
            steps = yaml.safe_load(f)["jobs"]["check"]["steps"]
        names = [s["name"] for s in steps]
        push = steps[names.index("Enviar push resumen (FCM)")]
        self.assertTrue(push["continue-on-error"])
        self.assertNotIn("if", push)  # if: success() implícito -> solo tras el commit
        self.assertLess(names.index("Guardar el nuevo estado en el repositorio"),
                        names.index("Enviar push resumen (FCM)"))
        with open(os.path.join(base, "test-events.yml"), encoding="utf-8") as f:
            wf = yaml.safe_load(f)
        self.assertEqual(wf["concurrency"]["group"], "level5-monitor")


if __name__ == "__main__":
    unittest.main()
