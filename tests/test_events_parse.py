import unittest

import events
import monitor
from tests.discord_fixture import NEW, OLD


class ParseDetailTests(unittest.TestCase):
    def parse(self, detail, **kw):
        return events.parse_detail(events.TYPE_CHANGED, detail, **kw)

    # --- formatos reales de make_diff -------------------------------------
    def test_real_make_diff_output_all_three_forms(self):
        detail = monitor.make_diff(OLD, NEW)
        self.assertEqual(
            detail, "✏️ 発売日は~~未定~~**2026年12月**です\n➕ 新キャラクター公開 🎉")
        lines, truncated = self.parse(detail, new_lines=set(NEW.splitlines()))
        self.assertEqual(lines, [
            {"kind": "CHANGED", "oldText": "発売日は未定です", "newText": "発売日は2026年12月です"},
            {"kind": "ADDED", "text": "新キャラクター公開 🎉"},
        ])
        self.assertFalse(truncated)

    def test_removed_line(self):
        detail = monitor.make_diff("a\nb\nc", "a\nc")
        self.assertEqual(detail, "➖ b")
        self.assertEqual(self.parse(detail), ([{"kind": "REMOVED", "text": "b"}], False))

    def test_big_replace_is_removed_plus_added(self):
        detail = monitor.make_diff("aaaaaaaaaa", "zzzzzzzzzz")
        self.assertEqual(detail, "➖ aaaaaaaaaa\n➕ zzzzzzzzzz")
        lines, _ = self.parse(detail)
        self.assertEqual([l["kind"] for l in lines], ["REMOVED", "ADDED"])

    def test_pencil_without_variation_selector(self):
        lines, _ = self.parse("✏ hola ~~mundo~~**tierra**")
        self.assertEqual(lines, [{"kind": "CHANGED", "oldText": "hola mundo",
                                  "newText": "hola tierra"}])

    def test_pure_insertion_and_deletion_inside_line(self):
        lines, _ = self.parse("✏️ abc**X**def\n✏️ abc~~X~~def")
        self.assertEqual(lines[0], {"kind": "CHANGED", "oldText": "abcdef", "newText": "abcXdef"})
        self.assertEqual(lines[1], {"kind": "CHANGED", "oldText": "abcXdef", "newText": "abcdef"})

    # --- emojis y marcas dentro del texto real ----------------------------
    def test_emojis_inside_text_do_not_count_as_prefix(self):
        lines, _ = self.parse("➕ ➖ menos ✏️ lápiz 🎉\n➖ ➕ más")
        self.assertEqual(lines, [
            {"kind": "ADDED", "text": "➖ menos ✏️ lápiz 🎉"},
            {"kind": "REMOVED", "text": "➕ más"},
        ])

    def test_lone_markers_in_real_text_fall_back_to_other(self):
        for raw in ("✏️ 5**3 es ~~8~~**9**", "✏️ a~~b~~**c** ~~ fin", "✏️ x ~~~y~~~ z"):
            with self.subTest(raw=raw):
                self.assertEqual(self.parse(raw)[0], [{"kind": "OTHER", "text": raw}])

    def test_balanced_literal_markers_caught_by_new_text_check(self):
        # El texto real de la página contiene "**b**"; la línea de Discord
        # es "a **b** ~~c~~**d**". Estructuralmente parece válida, pero el
        # newText reconstruido ("a b d") no existe en la página.
        raw = "✏️ a **b** ~~c~~**d**"
        page = {"a **b** d"}
        self.assertEqual(self.parse(raw, new_lines=page)[0], [{"kind": "OTHER", "text": raw}])

    def test_marker_cut_by_char_limit_is_other(self):
        raw = "✏️ hola ~~mun"
        self.assertEqual(self.parse(raw)[0], [{"kind": "OTHER", "text": raw}])

    def test_line_cut_after_valid_pair_is_other_with_new_text_check(self):
        raw = "✏️ hola ~~x~~**y** mu…"
        self.assertEqual(self.parse(raw, new_lines={"hola y mundo"})[0],
                         [{"kind": "OTHER", "text": raw}])

    def test_visible_space_marker_is_other(self):
        raw = "✏️ a~~␣~~b"
        self.assertEqual(self.parse(raw)[0], [{"kind": "OTHER", "text": raw}])

    # --- otros casos ------------------------------------------------------
    def test_empty_detail(self):
        for t in (events.TYPE_CHANGED, events.TYPE_NEW, events.TYPE_REMOVED):
            self.assertEqual(events.parse_detail(t, ""), ([], False))

    def test_trailer_marks_truncated_and_is_not_a_line(self):
        detail = monitor.make_diff("", "\n".join(f"línea {i}" for i in range(12)))
        self.assertIn("…y 4 líneas más de diferencia.", detail)
        lines, truncated = self.parse(detail)
        self.assertTrue(truncated)
        self.assertEqual(len(lines), 8)
        self.assertTrue(all(l["kind"] == "ADDED" for l in lines))

    def test_char_cut_marks_truncated(self):
        detail = monitor.make_diff("", "x" * 100 + "\n" + "y" * 600)
        self.assertTrue(detail.endswith("…"))
        self.assertTrue(self.parse(detail)[1])

    def test_no_visible_difference_sentence_is_other(self):
        detail = monitor.make_diff("igual", "igual")
        lines, truncated = self.parse(detail)
        self.assertEqual(lines, [{"kind": "OTHER", "text": detail}])
        self.assertFalse(truncated)

    def test_unknown_line_is_other(self):
        self.assertEqual(self.parse("algo raro")[0], [{"kind": "OTHER", "text": "algo raro"}])

    def test_non_changed_types_are_verbatim_other(self):
        detail = "➕ parece diff\nlínea normal"
        for t in (events.TYPE_NEW, events.TYPE_REAPPEARED):
            lines, _ = events.parse_detail(t, detail)
            self.assertEqual(lines, [{"kind": "OTHER", "text": "➕ parece diff"},
                                     {"kind": "OTHER", "text": "línea normal"}])

    def test_new_page_snippet_cut_is_truncated(self):
        detail = monitor._truncate("a" * 300)
        self.assertTrue(events.parse_detail(events.TYPE_NEW, detail)[1])

    def test_raw_detail_cap(self):
        f = events.detail_fields(events.TYPE_NEW, "z" * 5000)
        self.assertLessEqual(len(f["rawDetail"]), events.MAX_RAW_DETAIL + 1)
        self.assertTrue(f["truncated"])
        f = events.detail_fields(events.TYPE_CHANGED, "➕ hola")
        self.assertEqual(f, {"lines": [{"kind": "ADDED", "text": "hola"}],
                             "rawDetail": "➕ hola", "truncated": False})


if __name__ == "__main__":
    unittest.main()
