"""main() de extremo a extremo con el rastreo y la red simulados."""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import monitor
from tests.discord_fixture import NEW, make_results

SITE = "Inazuma Eleven RE (IERE)"
CONFIG = {
    "sites": [{"name": SITE, "seed": "https://www.inazuma.jp/re/", "domain": "inazuma.jp",
               "franchise": "inazuma-eleven"}],
    "franchise_names": {"inazuma-eleven": "Inazuma Eleven", "general": "General"},
    "request_delay_seconds": 0,
    "notify_on_new_page": True,
    "max_urls_per_notification": 15,
}
OLD_STATE = {"last_run": "2026-01-01T00:00:00+00:00",
             "pages": {"https://example.invalid/": {"fmt": 2, "removed": False}}}


def fake_crawl(site, state_pages, session, delay, timeout):
    result = make_results()[0]
    for pc in result.changed_pages + result.new_pages + result.removed_pages:
        state_pages[pc.url] = {"text": NEW, "hash_text": "h-" + pc.url, "title": pc.title}
    return result


class MainIntegrationBase(unittest.TestCase):
    argv = ["monitor.py"]
    state = OLD_STATE
    config = CONFIG

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.config_path = os.path.join(d, "config.json")
        self.state_path = os.path.join(d, "state.json")
        self.events_path = os.path.join(d, "events.json")
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f)
        if self.state is not None:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.state, f)

        self.posts = []

        def fake_post(url, json=None, **kw):
            self.posts.append((url, json))
            return mock.Mock(status_code=204, text="")

        patches = [
            mock.patch.object(monitor, "CONFIG_PATH", self.config_path),
            mock.patch.object(monitor, "STATE_PATH", self.state_path),
            mock.patch.object(monitor, "EVENTS_PATH", self.events_path),
            mock.patch.object(monitor, "crawl_site", fake_crawl),
            mock.patch.object(monitor, "now_iso", return_value="2026-09-20T19:45:49+00:00"),
            mock.patch.object(monitor.requests, "post", fake_post),
            mock.patch.object(monitor.sys, "argv", self.argv),
            mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.invalid/hook"}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_main(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = monitor.main()
        return code, out.getvalue(), err.getvalue()

    def read(self, path):
        with open(path, "rb") as f:
            return f.read()


class NormalRunTests(MainIntegrationBase):
    def test_writes_events_and_sends_discord(self):
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        doc = json.loads(self.read(self.events_path))
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(doc["events"][0]["franchise"], "inazuma-eleven")
        self.assertEqual(doc["events"][0]["type"], "PAGE_CHANGED")
        self.assertEqual(len(self.posts), 1)

    def test_discord_payloads_identical_to_monitor_alone(self):
        self.run_main()
        expected = monitor.build_discord_payloads([make_results()[0]], 15)
        self.assertEqual([p for _, p in self.posts], expected)


class FailureIsolationTests(MainIntegrationBase):
    def baseline(self):
        self.run_main()
        return [p for _, p in self.posts], json.loads(self.read(self.state_path))

    def test_events_write_failure_leaves_everything_else_untouched(self):
        payloads, state = self.baseline()
        os.remove(self.events_path)
        self.posts.clear()
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(OLD_STATE, f)

        with mock.patch("events.write_events", side_effect=OSError("disk full")):
            code, _, err = self.run_main()

        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.events_path))
        self.assertEqual(json.loads(self.read(self.state_path)), state)
        self.assertEqual([p for _, p in self.posts], payloads)
        self.assertIn("feed de eventos", err)

    def test_events_module_exploding_is_contained(self):
        payloads, state = self.baseline()
        os.remove(self.events_path)
        self.posts.clear()
        with mock.patch("events.record_events", side_effect=RuntimeError("boom")):
            code, _, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual([p for _, p in self.posts], payloads)
        self.assertIn("RuntimeError", err)

    def test_events_import_failure_is_contained(self):
        with mock.patch.dict("sys.modules", {"events": None}):
            code, _, err = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posts), 1)
        self.assertIn("ModuleNotFoundError", err)
        self.assertIn("last_run", self.read(self.state_path).decode("utf-8"))


class DryRunTests(MainIntegrationBase):
    argv = ["monitor.py", "--dry-run"]

    def test_dry_run_touches_nothing_and_sends_nothing(self):
        before = self.read(self.state_path)
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(self.read(self.state_path), before)
        self.assertFalse(os.path.exists(self.events_path))
        self.assertEqual(self.posts, [])
        self.assertIn("[DRY-RUN] Eventos que se generarían", out)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["config.json", "state.json"])


class FirstRunTests(MainIntegrationBase):
    state = None

    def test_first_run_generates_no_events(self):
        code, _, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertFalse(os.path.exists(self.events_path))
        self.assertEqual(self.posts, [])


class NotifyNewOffTests(MainIntegrationBase):
    config = {**CONFIG, "notify_on_new_page": False}

    def test_new_pages_are_not_in_feed_when_discord_ignores_them(self):
        self.run_main()
        types = {e["type"] for e in json.loads(self.read(self.events_path))["events"]}
        self.assertEqual(types, {"PAGE_CHANGED", "PAGE_REMOVED"})


if __name__ == "__main__":
    unittest.main()
