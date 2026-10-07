import unittest

import requests

import monitor
from events import build_franchise_map

APP = 3384600
NAME = "Steam (PLNWOS)"
CONFIG = {"steam_watch": [{"name": NAME, "app_id": APP, "franchise": "professor-layton"}]}

PRIVATE = {str(APP): {"success": False}}
PUBLIC = {str(APP): {"success": True, "data": {
    "name": "Professor Layton and the New World of Steam",
    "header_image": "https://example.test/header.jpg",
    "release_date": {"coming_soon": True, "date": "Dec 10, 2026"},
    "price_overview": {"final_formatted": "59,99€"},
}}}


class FakeResponse:
    def __init__(self, payload=None, status=200, bad_json=False):
        self.status_code = status
        self._payload = payload
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("no es JSON")
        return self._payload


class FakeSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(state, *responses, config=CONFIG):
    session = FakeSession(*responses)
    return monitor.check_steam_watch(config, state, session, timeout=5), session


class SteamWatchTests(unittest.TestCase):
    def test_first_sighting_only_records_baseline(self):
        state = {}
        results, session = run(state, FakeResponse(PRIVATE))
        self.assertEqual(results, [])
        self.assertFalse(state["steam_watch"][str(APP)]["public"])
        self.assertFalse(state["steam_watch"][str(APP)]["notified"])
        url, kwargs = session.calls[0]
        self.assertEqual(url, monitor.STEAM_APPDETAILS_URL)
        self.assertEqual(kwargs["params"]["appids"], str(APP))

    def test_already_public_on_first_sighting_does_not_notify(self):
        state = {}
        results, _ = run(state, FakeResponse(PUBLIC))
        self.assertEqual(results, [])
        self.assertTrue(state["steam_watch"][str(APP)]["notified"])

    def test_still_private_does_not_notify(self):
        state = {}
        run(state, FakeResponse(PRIVATE))
        results, _ = run(state, FakeResponse(PRIVATE))
        self.assertEqual(results, [])

    def test_going_public_notifies_once(self):
        state = {}
        run(state, FakeResponse(PRIVATE))
        results, _ = run(state, FakeResponse(PUBLIC))
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r.site_name, NAME)
        self.assertEqual(len(r.new_pages), 1)
        pc = r.new_pages[0]
        self.assertEqual(pc.url, f"https://store.steampowered.com/app/{APP}/")
        self.assertEqual(pc.title, "Professor Layton and the New World of Steam")
        self.assertEqual(pc.image_url, "https://example.test/header.jpg")
        self.assertIn("Dec 10, 2026", pc.detail)
        self.assertIn("59,99€", pc.detail)
        self.assertTrue(state["steam_watch"][str(APP)]["notified"])

        # Aunque Steam vuelva a fallar y a recuperarse, no se repite el aviso.
        run(state, FakeResponse(PRIVATE))
        results, _ = run(state, FakeResponse(PUBLIC))
        self.assertEqual(results, [])

    def test_errors_keep_state_and_do_not_notify(self):
        state = {}
        run(state, FakeResponse(PRIVATE))
        before = dict(state["steam_watch"][str(APP)])
        for bad in (
            requests.ConnectionError("sin red"),
            FakeResponse(status=503),
            FakeResponse(bad_json=True),
            FakeResponse([]),            # Steam a veces devuelve [] en vez de objeto
            FakeResponse({"otro": {}}),  # falta la clave del app_id
        ):
            results, _ = run(state, bad)
            self.assertEqual(results, [])
            self.assertEqual(state["steam_watch"][str(APP)], before)

    def test_no_config_is_a_noop(self):
        state = {}
        results, session = run(state, config={})
        self.assertEqual(results, [])
        self.assertEqual(session.calls, [])

    def test_franchise_map_includes_steam_watch(self):
        self.assertEqual(build_franchise_map(CONFIG)[NAME], "professor-layton")


if __name__ == "__main__":
    unittest.main()
