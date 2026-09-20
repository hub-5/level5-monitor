import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

import events
import monitor
from tests.discord_fixture import NEW, OLD, make_results

NOW = datetime(2026, 9, 20, 19, 45, 49, tzinfo=timezone.utc)
FRANCHISES = {"Inazuma Eleven RE (IERE)": "inazuma-eleven", "Professor Layton": "professor-layton"}


def read_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def load_json(path):
    return json.loads(read_text(path))


def state_for(results):
    pages = {}
    for r in results:
        for pc in r.changed_pages + r.new_pages + r.removed_pages:
            pages[pc.url] = {"text": NEW, "hash_text": "h-" + pc.url, "title": pc.title}
    return pages


class BuildEventsTests(unittest.TestCase):
    def build(self, previously_removed=frozenset(), franchises=FRANCHISES, results=None):
        results = results or make_results()
        return events.build_events(results, set(previously_removed), state_for(results),
                                   franchises, NOW)

    def test_one_event_per_type(self):
        evs = self.build()
        self.assertEqual([e["type"] for e in evs[:3]],
                         ["PAGE_CHANGED", "PAGE_NEW", "PAGE_REMOVED"])
        changed, new, removed = evs[:3]
        self.assertEqual(changed["franchise"], "inazuma-eleven")
        self.assertEqual(changed["url"], "https://www.inazuma.jp/re/news/")
        self.assertEqual(changed["title"], "ニュース")
        self.assertEqual(changed["timestamp"], "2026-09-20T19:45:49Z")
        self.assertEqual(changed["lines"][0]["kind"], "CHANGED")
        self.assertEqual(changed["rawDetail"], monitor.make_diff(OLD, NEW))
        self.assertEqual(new["lines"][0], {"kind": "OTHER", "text": "イナズマイレブン 最新情報"})
        self.assertEqual(removed["lines"], [])
        self.assertEqual(removed["rawDetail"], "")
        self.assertIn("Modificada en Inazuma Eleven RE (IERE): ニュース", changed["summary"])
        self.assertNotIn("\n", changed["summary"])

    def test_key_order_and_fields(self):
        ev = self.build()[0]
        self.assertEqual(list(ev), ["id", "timestamp", "url", "franchise", "type", "title",
                                    "summary", "lines", "rawDetail", "truncated"])

    def test_reappeared_detected_from_previously_removed(self):
        evs = self.build(previously_removed={"https://www.inazuma.jp/re/new/"})
        types = {e["url"]: e["type"] for e in evs}
        self.assertEqual(types["https://www.inazuma.jp/re/new/"], "PAGE_REAPPEARED")
        self.assertEqual(types["https://www.layton.jp/x/0"], "PAGE_NEW")

    def test_franchise_falls_back_to_general(self):
        evs = self.build(franchises={})
        self.assertTrue(all(e["franchise"] == "general" for e in evs))

    def test_missing_title_is_null(self):
        results = make_results()
        results[0].removed_pages[0].title = ""
        evs = self.build(results=results)
        removed = [e for e in evs if e["type"] == "PAGE_REMOVED"][0]
        self.assertIsNone(removed["title"])
        self.assertIn(removed["url"], removed["summary"])

    def test_ids_stable_and_unique(self):
        a, b = self.build(), self.build()
        self.assertEqual([e["id"] for e in a], [e["id"] for e in b])
        ids = [e["id"] for e in a]
        self.assertEqual(len(ids), len(set(ids)))
        results = make_results()
        later = events.build_events(results, set(), state_for(results), FRANCHISES,
                                    NOW.replace(minute=55))
        self.assertTrue(set(ids).isdisjoint(e["id"] for e in later))

    def test_same_url_twice_in_a_run_is_deduped(self):
        r = make_results()
        r.append(monitor.CrawlResult(site_name="Otra", new_pages=[r[0].new_pages[0]]))
        evs = self.build(results=r)
        urls = [e["url"] for e in evs if e["type"] == "PAGE_NEW"]
        self.assertEqual(len(urls), len(set(urls)))

    def test_changed_uses_page_text_to_verify_lines(self):
        results = make_results()
        state = state_for(results)
        state["https://www.inazuma.jp/re/news/"]["text"] = "otra cosa"
        evs = events.build_events(results, set(), state, FRANCHISES, NOW)
        self.assertEqual(evs[0]["lines"][0]["kind"], "OTHER")


class MergeTests(unittest.TestCase):
    def ev(self, i):
        return {"id": f"id{i}", "timestamp": "t"}

    def test_cap_300_newest_first(self):
        existing = [self.ev(i) for i in range(250)]
        new = [self.ev(f"n{i}") for i in range(100)]
        merged = events.merge_events(existing, new)
        self.assertEqual(len(merged), 300)
        self.assertEqual(merged[0]["id"], "idn0")
        self.assertEqual(merged[99]["id"], "idn99")
        self.assertEqual(merged[100]["id"], "id0")
        self.assertEqual(merged[-1]["id"], "id199")

    def test_merge_dedupes_by_id(self):
        merged = events.merge_events([self.ev(1), self.ev(2)], [self.ev(2), self.ev(3)])
        self.assertEqual([e["id"] for e in merged], ["id2", "id3", "id1"])

    def test_merge_keeps_events_without_id(self):
        self.assertEqual(len(events.merge_events([{"x": 1}, {"x": 2}], [])), 2)


class FileTests(unittest.TestCase):
    def setUp(self):
        for stream in ("sys.stdout", "sys.stderr"):
            p = mock.patch(stream, new_callable=io.StringIO)
            p.start()
            self.addCleanup(p.stop)
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "events.json")

    def record(self, results=None, now=NOW, **kw):
        results = results or make_results()
        return events.record_events(results, set(), state_for(results), FRANCHISES,
                                    self.path, now=now, **kw)

    def test_written_file_is_valid_utf8_json_with_japanese(self):
        new = self.record()
        text = read_bytes(self.path).decode("utf-8")
        self.assertIn("ニュース", text)
        doc = json.loads(text)
        self.assertEqual(doc["schemaVersion"], 1)
        self.assertEqual(len(doc["events"]), len(new))
        self.assertEqual(list(doc), ["schemaVersion", "events"])
        self.assertEqual(os.listdir(self.dir.name), ["events.json"])

    def test_second_run_prepends(self):
        self.record()
        first = load_json(self.path)["events"]
        self.record(now=NOW.replace(minute=59))
        doc = load_json(self.path)["events"]
        self.assertEqual(len(doc), 2 * len(first))
        self.assertEqual(doc[0]["timestamp"], "2026-09-20T19:59:49Z")

    def test_cap_applies_when_writing(self):
        big = [{"id": f"old{i}"} for i in range(299)]
        events.write_events(self.path, big)
        self.record()
        doc = load_json(self.path)["events"]
        self.assertEqual(len(doc), 300)
        self.assertEqual(doc[0]["type"], "PAGE_CHANGED")

    def test_no_events_does_not_create_file(self):
        self.assertEqual(self.record(results=[monitor.CrawlResult(site_name="x")]), [])
        self.assertFalse(os.path.exists(self.path))

    def test_replace_failure_keeps_original_and_does_not_raise(self):
        self.record()
        before = read_bytes(self.path)
        with mock.patch("events.os.replace", side_effect=OSError("boom")):
            self.assertEqual(self.record(now=NOW.replace(minute=1)), [])
        self.assertEqual(read_bytes(self.path), before)
        self.assertEqual(os.listdir(self.dir.name), ["events.json"])

    def test_unserializable_data_does_not_touch_file(self):
        self.record()
        before = read_bytes(self.path)
        with mock.patch("events.build_events", return_value=[{"id": "x", "bad": object()}]):
            self.assertEqual(self.record(), [])
        self.assertEqual(read_bytes(self.path), before)

    def test_corrupt_or_unknown_schema_is_not_overwritten(self):
        for content in ("{no json", '{"schemaVersion": 99, "events": []}',
                        '{"schemaVersion": 1, "events": "x"}', "[]"):
            with self.subTest(content=content):
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(content)
                self.assertEqual(self.record(), [])
                self.assertEqual(read_text(self.path), content)

    def test_build_failure_is_contained_and_quiet_about_details(self):
        with mock.patch("events.build_events", side_effect=RuntimeError("secret-token")):
            with mock.patch("sys.stderr") as err:
                self.assertEqual(self.record(), [])
        printed = "".join(c.args[0] for c in err.write.call_args_list)
        self.assertIn("RuntimeError", printed)
        self.assertNotIn("secret-token", printed)
        self.assertFalse(os.path.exists(self.path))

    def test_dry_run_writes_nothing(self):
        with mock.patch("builtins.print"):
            new = self.record(dry_run=True)
        self.assertTrue(new)
        self.assertEqual(os.listdir(self.dir.name), [])


if __name__ == "__main__":
    unittest.main()
