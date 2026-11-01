import unittest

from app.main import format_record, parse_champions, parse_record, templates


CHAMPIONS_HTML = """
<h2>Super middleweight (168 lb)</h2>
<table class="wikitable"><tbody>
<tr><td>WBA</td><td>WBC</td><td>IBF</td><td>WBO</td><td>The Ring</td></tr>
<tr>
<td rowspan="2"><a href="/wiki/Jaime_Munguia">Jaime Munguia</a><br>47–2 (35 KO)<br>May 2, 2026</td>
<td><a href="/wiki/Canelo_Álvarez">Canelo Álvarez</a><br>64–3–2 (39 KO)<br>October 31, 2026</td>
<td rowspan="2"><a href="/wiki/Osleys_Iglesias">Osleys Iglesias</a><br>16–0 (15 KO)<br>April 9, 2026</td>
<td rowspan="2">vacant</td><td rowspan="2">vacant</td>
</tr>
<tr><td><a href="/wiki/Lester_Martínez">Lester Martínez</a><br><span style="font-size: 85%;">Interim champion</span><br>21–0–1 (17 KO)<br>March 21, 2026</td></tr>
</tbody></table>
"""


class RecordTests(unittest.TestCase):
    def test_supported_formats(self):
        cases = [
            ("64–3–2 (39 KO)", ("64", "3", "2", "39")),
            ("21–0–1 (17 KO)", ("21", "0", "1", "17")),
            ("34–2 (1) (27 KO)", ("34", "2", "1", "27")),
            ("27-0 (19 KO)", ("27", "0", None, "19")),
            (" 21 — 0 — 0 (17 KOs) ", ("21", "0", "0", "17")),
            ("21-0-1 (17 KO)", ("21", "0", "1", "17")),
        ]
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertEqual(parse_record(record), dict(zip(
                    ("wins", "losses", "draws", "kos"), expected
                )))

    def test_unsupported_records_preserve_raw_text(self):
        for record in ("64–3–2–1 (39 KO)", "unknown", "prefix 21–0 (17 KO)"):
            with self.subTest(record=record):
                self.assertIsNone(parse_record(record))
                self.assertEqual(format_record(record), record)
        self.assertIsNone(parse_record(None))
        self.assertIsNone(format_record(None))

    def test_scraped_records_and_rendered_page(self):
        divisions = parse_champions(CHAMPIONS_HTML)
        canelo, lester = divisions[0]["wbc"]
        self.assertEqual(canelo["recordParts"], {
            "wins": "64", "losses": "3", "draws": "2", "kos": "39"
        })
        self.assertEqual(lester["recordParts"], {
            "wins": "21", "losses": "0", "draws": "1", "kos": "17"
        })
        self.assertEqual(canelo["recordDisplay"], "W:64 L:3 D:2 KO:39")
        self.assertEqual(lester["recordDisplay"], "W:21 L:0 D:1 KO:17")
        self.assertEqual(lester["type"], "Interim")
        self.assertEqual(lester["date"], "March 21, 2026")
        self.assertEqual(len(divisions[0]["wba"]), 1)
        page = templates.get_template("index.html").render(divisions=divisions)
        self.assertIn('<span class="record-dash">64-3-2</span>', page)
        self.assertIn('<span class="record-dash">21-0-1</span>', page)


if __name__ == "__main__":
    unittest.main()
