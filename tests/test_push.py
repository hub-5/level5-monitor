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
from tests.discord_fixture import make_results
from tests.test_events_build import FRANCHISES, NOW, state_for
from tests.test_main_integration import MainIntegrationBase

NAMES = {"inazuma-eleven": "Inazuma Eleven", "professor-layton": "Professor Layton",
         "level5": "LEVEL-5", "fantasy-life": "Fantasy Life", "decapolice": "Decapolice",
         "general": "General"}


def ev(i, franchise="level5"):
    return {"id": f"{i:016x}", "franchise": franchise}


class BuildPushPendingTests(unittest.TestCase):
    def test_title_singular_and_plural(self):
        self.assertEqual(events.build_push_pending([ev(1)], NAMES)["title"],
                         "SrHub: 1 cambio detectado")
        self.assertEqual(events.build_push_pending([ev(1), ev(2)], NAMES)["title"],
                         "SrHub: 2 cambios detectados")

    def test_body_lists_affected_franchises_once_in_order(self):
        evs = [ev(1, "level5"), ev(2, "inazuma-eleven"), ev(3, "level5"),
               ev(4, "professor-layton")]
        p = events.build_push_pending(evs, NAMES)
        self.assertEqual(p["body"], "Novedades en LEVEL-5, Inazuma Eleven y Professor Layton")
        self.assertEqual(p["data"]["franchises"], "level5,inazuma-eleven,professor-layton")

    def test_body_variants(self):
        one = events.build_push_pending([ev(1, "decapolice")], NAMES)["body"]
        two = events.build_push_pending([ev(1, "decapolice"), ev(2, "level5")], NAMES)["body"]
        self.assertEqual(one, "Novedades en Decapolice")
        self.assertEqual(two, "Novedades en Decapolice y LEVEL-5")
        many = [ev(i, f"f{i}") for i in range(6)]
        body = events.build_push_pending(many, {f"f{i}": f"F{i}" for i in range(6)})["body"]
        self.assertEqual(body, "Novedades en F0, F1, F2, F3 y 2 más")

    def test_unknown_franchise_uses_slug(self):
        self.assertEqual(events.build_push_pending([ev(1, "nuevo")], NAMES)["body"],
                         "Novedades en nuevo")

    def test_data_ids_capped_but_count_is_total(self):
        evs = [ev(i) for i in range(300)]
        data = events.build_push_pending(evs, NAMES)["data"]
        self.assertEqual(data["count"], "300")
        ids = data["ids"].split(",")
        self.assertEqual(len(ids), events.MAX_PUSH_IDS)
        self.assertEqual(ids[0], evs[0]["id"])
        self.assertTrue(all(isinstance(v, str) for v in data.values()))
        self.assertLess(len(json.dumps(data).encode("utf-8")), 4096)


class RecordPendingTests(unittest.TestCase):
    def setUp(self):
        for stream in ("sys.stdout", "sys.stderr"):
            p = mock.patch(stream, new_callable=io.StringIO)
            p.start()
            self.addCleanup(p.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.events_path = os.path.join(self.dir.name, "events.json")
        self.pending_path = os.path.join(self.dir.name, "push_pending.json")

    def record(self, **kw):
        results = kw.pop("results", None) or make_results()
        return events.record_events(
            results, set(), state_for(results), FRANCHISES, self.events_path, now=NOW,
            pending_path=self.pending_path, franchise_names=NAMES, **kw)

    def test_pending_written_once_with_all_ids_after_events(self):
        new = self.record()
        with open(self.pending_path, encoding="utf-8") as f:
            pending = json.load(f)
        self.assertEqual(pending["title"], f"SrHub: {len(new)} cambios detectados")
        self.assertEqual(pending["data"]["ids"].split(","), [e["id"] for e in new][:50])
        self.assertIn("Inazuma Eleven", pending["body"])

    def test_no_pending_if_events_write_fails(self):
        with mock.patch("events.write_events", side_effect=OSError("x")):
            self.assertEqual(self.record(), [])
        self.assertFalse(os.path.exists(self.pending_path))

    def test_no_pending_without_events_or_in_dry_run(self):
        self.record(results=[monitor.CrawlResult(site_name="x")])
        self.record(dry_run=True)
        self.assertFalse(os.path.exists(self.pending_path))

    def test_pending_failure_keeps_feed(self):
        real = events.atomic_write_json

        def flaky(path, data):
            if path == self.pending_path:
                raise OSError("no space")
            return real(path, data)

        with mock.patch("events.atomic_write_json", side_effect=flaky):
            new = self.record()
        self.assertTrue(new)
        self.assertTrue(os.path.exists(self.events_path))
        self.assertFalse(os.path.exists(self.pending_path))


class SendPushDataTests(unittest.TestCase):
    SECRET = "SECRET-PRIVATE-KEY-123"

    def setUp(self):
        info = {"project_id": "demo-project", "private_key": self.SECRET,
                "client_email": "x@demo.iam.gserviceaccount.com"}
        self.info_json = json.dumps(info)
        env = mock.patch.dict(os.environ, {"FIREBASE_SERVICE_ACCOUNT": self.info_json})
        env.start()
        self.addCleanup(env.stop)
        self.posts = []

    def fake_post(self, status=200):
        def post(url, json=None, headers=None, timeout=None):
            self.posts.append({"url": url, "json": json, "headers": headers})
            return mock.Mock(status_code=status, text="")
        return post

    def creds(self):
        creds = mock.Mock()
        creds.token = "ACCESS-TOKEN-456"
        return mock.patch("google.oauth2.service_account.Credentials.from_service_account_info",
                          return_value=creds)

    def test_data_is_sent_as_strings(self):
        with self.creds(), mock.patch.object(monitor.requests, "post", self.fake_post()), \
                redirect_stdout(io.StringIO()):
            ok = monitor.send_push("t", "b", data={"count": 3, "ids": "a,b"})
        self.assertTrue(ok)
        msg = self.posts[0]["json"]["message"]
        self.assertEqual(msg["data"], {"count": "3", "ids": "a,b"})
        self.assertEqual(msg["topic"], "radar")
        self.assertEqual(msg["notification"], {"title": "t", "body": "b"})
        self.assertIn("demo-project", self.posts[0]["url"])

    def test_without_data_payload_is_unchanged(self):
        with self.creds(), mock.patch.object(monitor.requests, "post", self.fake_post()), \
                redirect_stdout(io.StringIO()):
            monitor.send_push("t", "b")
        self.assertEqual(self.posts[0]["json"],
                         {"message": {"topic": "radar", "notification": {"title": "t", "body": "b"}}})

    def run_send_pending(self, pending, status=200, auth_error=None):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, "push_pending.json")
        if pending is not None:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(pending, f)
        out, err = io.StringIO(), io.StringIO()
        creds = (mock.patch("google.oauth2.service_account.Credentials.from_service_account_info",
                            side_effect=auth_error) if auth_error else self.creds())
        with mock.patch.object(monitor, "PENDING_PATH", path), creds, \
                mock.patch.object(monitor.requests, "post", self.fake_post(status)), \
                redirect_stdout(out), redirect_stderr(err):
            code = monitor.send_pending()
        return code, out.getvalue() + err.getvalue(), path

    PENDING = {"title": "SrHub: 2 cambios detectados", "body": "Novedades en LEVEL-5",
               "data": {"count": "2", "ids": "a,b", "franchises": "level5"}}

    def test_send_pending_sends_exactly_one_push_and_removes_file(self):
        code, _, path = self.run_send_pending(self.PENDING)
        self.assertEqual(code, 0)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(self.posts[0]["json"]["message"]["data"], self.PENDING["data"])
        self.assertFalse(os.path.exists(path))

    def test_send_pending_without_file_is_a_noop(self):
        code, out, _ = self.run_send_pending(None)
        self.assertEqual(code, 0)
        self.assertEqual(self.posts, [])
        self.assertIn("No hay push pendiente", out)

    def test_send_pending_fcm_error_returns_1_and_keeps_secrets_out(self):
        code, out, path = self.run_send_pending(self.PENDING, status=500)
        self.assertEqual(code, 1)
        self.assertTrue(os.path.exists(path))
        for secret in (self.SECRET, "ACCESS-TOKEN-456", self.info_json):
            self.assertNotIn(secret, out)

    def test_auth_failure_does_not_leak_secret(self):
        code, out, _ = self.run_send_pending(
            self.PENDING, auth_error=ValueError(f"bad key {self.SECRET}"))
        self.assertEqual(code, 1)
        self.assertEqual(self.posts, [])
        self.assertNotIn(self.SECRET, out)
        self.assertIn("ValueError", out)

    def test_malformed_pending_returns_1(self):
        code, _, _ = self.run_send_pending({"title": 5})
        self.assertEqual(code, 1)
        self.assertEqual(self.posts, [])


class MainPendingTests(MainIntegrationBase):
    def setUp(self):
        super().setUp()
        self.pending_path = os.path.join(self.tmp.name, "push_pending.json")
        p = mock.patch.object(monitor, "PENDING_PATH", self.pending_path)
        p.start()
        self.addCleanup(p.stop)

    def test_main_leaves_one_pending_and_sends_no_push_itself(self):
        self.run_main()
        with open(self.pending_path, encoding="utf-8") as f:
            pending = json.load(f)
        self.assertEqual(pending["title"], "SrHub: 3 cambios detectados")
        self.assertEqual(pending["body"], "Novedades en Inazuma Eleven")
        self.assertEqual(len(pending["data"]["ids"].split(",")), 3)
        self.assertTrue(all("fcm.googleapis.com" not in url for url, _ in self.posts))

    def test_stale_pending_is_removed_at_start(self):
        with open(self.pending_path, "w", encoding="utf-8") as f:
            f.write("{}")
        with mock.patch("events.write_events", side_effect=OSError("x")):
            self.run_main()
        self.assertFalse(os.path.exists(self.pending_path))

    def test_argv_send_pending_routes_to_send_pending(self):
        with mock.patch.object(monitor.sys, "argv", ["monitor.py", "--send-pending"]), \
                mock.patch.object(monitor, "send_pending", return_value=0) as sp:
            self.assertEqual(self.run_main()[0], 0)
        sp.assert_called_once()


if __name__ == "__main__":
    unittest.main()
