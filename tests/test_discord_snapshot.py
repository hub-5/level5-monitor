"""Garantiza que la salida de Discord/Telegram no cambia al añadir el feed.
El snapshot se generó con el código anterior a events.py."""
import json
import os
import unittest
from unittest import mock

import monitor
from tests.discord_fixture import make_results

SNAPSHOT = os.path.join(os.path.dirname(__file__), "fixtures", "discord_snapshot.json")


def current_output():
    with mock.patch.object(monitor, "now_iso", return_value="2026-01-01T00:00:00+00:00"):
        payloads = monitor.build_discord_payloads(make_results(), 15)
    return {"payloads": payloads, "text": monitor.build_text_lines(make_results(), 15)}


class DiscordSnapshotTests(unittest.TestCase):
    def test_output_unchanged(self):
        with open(SNAPSHOT, encoding="utf-8") as f:
            expected = json.load(f)
        self.assertEqual(json.loads(json.dumps(current_output())), expected)


if __name__ == "__main__":
    unittest.main()
