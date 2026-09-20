import json
import os
import re
import unittest

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config.json")
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _norm(slug: str) -> str:
    return re.sub(r"[^a-z0-9]", "", slug.lower())


class ConfigFranchiseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cls.config = json.load(f)
        cls.sites = cls.config["sites"]
        cls.names = cls.config.get("franchise_names", {})

    def test_every_site_has_franchise(self):
        for site in self.sites:
            self.assertTrue(site.get("franchise"), f"sin franchise: {site['name']}")

    def test_slugs_are_kebab_case(self):
        for site in self.sites:
            self.assertRegex(site["franchise"], SLUG_RE, site["name"])

    def test_no_slug_variants_with_different_spelling(self):
        by_norm = {}
        for slug in {s["franchise"] for s in self.sites} | set(self.names):
            by_norm.setdefault(_norm(slug), set()).add(slug)
        for norm, slugs in by_norm.items():
            self.assertEqual(len(slugs), 1, f"grafías distintas del mismo slug: {slugs}")

    def test_every_slug_has_visible_name(self):
        for site in self.sites:
            self.assertIn(site["franchise"], self.names)
        self.assertIn("general", self.names)

    def test_site_names_unique(self):
        names = [s["name"] for s in self.sites]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
