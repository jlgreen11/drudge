#!/usr/bin/env python3
"""Test suite for THE GRUDGE REPORT (build.py + template.html app JS).

Stdlib unittest only — zero network, zero pip deps. Every fixture pubDate is
generated relative to datetime.now(timezone.utc) at test runtime so the suite
never rots across the 48h ingest window. End-to-end tests run inside a
TemporaryDirectory with a monkeypatched build.fetch, so they can never touch
the repo's real state.json / index.html / feed.xml.

Node-based tests (app-JS execution under a DOM stub, partition parity,
node --check) are skipped when node is not on PATH.

Run:  python3 -m unittest -v
"""

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from unittest import mock

import build

NODE = shutil.which("node")


# ── Fixture helpers (all dates relative to runtime — never hardcoded) ───────

def utcnow():
    return datetime.now(timezone.utc)


def hours_ago(h):
    return utcnow() - timedelta(hours=h)


def rfc(dt):
    """RFC 2822 date string for RSS pubDate fixtures."""
    return format_datetime(dt)


def rss2_bytes(items):
    """RSS 2.0 fixture. items = [(title_xml, link_or_None, pubdate_or_None)]."""
    rows = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<rss version="2.0"><channel>',
            '<title>Wire</title><link>http://example.com/</link>',
            '<description>fixture</description>']
    for title, link, pub in items:
        rows.append('<item>')
        rows.append(f'<title>{title}</title>')
        if link is not None:
            rows.append(f'<link>{link}</link>')
        if pub:
            rows.append(f'<pubDate>{pub}</pubDate>')
        rows.append('</item>')
    rows.append('</channel></rss>')
    return ''.join(rows).encode('utf-8')


def rdf_bytes(items):
    """RSS 1.0 / RDF fixture: rdf:RDF root with a DEFAULT namespace, dc:date."""
    rows = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
            'xmlns="http://purl.org/rss/1.0/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">',
            '<channel rdf:about="http://example.com/">'
            '<title>Wire</title><link>http://example.com/</link></channel>']
    for title, link, date in items:
        rows.append(f'<item rdf:about="{link}">')
        rows.append(f'<title>{title}</title><link>{link}</link>')
        if date:
            rows.append(f'<dc:date>{date}</dc:date>')
        rows.append('</item>')
    rows.append('</rdf:RDF>')
    return ''.join(rows).encode('utf-8')


def atom_bytes(entries):
    """Atom fixture. entries = [(title_xml, href_or_None, iso_date_or_None)]."""
    rows = ['<?xml version="1.0" encoding="UTF-8"?>',
            '<feed xmlns="http://www.w3.org/2005/Atom"><title>Wire</title>']
    for title, href, date in entries:
        rows.append('<entry>')
        rows.append(f'<title>{title}</title>')
        rows.append(f'<link href="{href}"/>' if href is not None else '<link/>')
        if date:
            rows.append(f'<updated>{date}</updated>')
        rows.append('</entry>')
    rows.append('</feed>')
    return ''.join(rows).encode('utf-8')


def wire_item(title, link=None, source="BBC", age_hours=2.0):
    """A parse_feed-shaped item dict, ready for dedupe_and_rank."""
    slug = re.sub(r"\W+", "-", title.lower()).strip("-")
    return {"source": source, "title": title,
            "link": link or f"http://example.com/{slug}",
            "age_hours": age_hours}


def mk_item(title, *, score=10.0, cluster=1, age_hours=5.0, **extra):
    """A ranked-story dict with every field apply_state/choose_lead/render use."""
    it = {
        "title": title,
        "link": "http://example.com/" + re.sub(r"\W+", "-", title.lower()),
        "source": "BBC",
        "age_hours": age_hours,
        "toks": build.tokens(title),
        "score": float(score),
        "cluster": cluster,
        "tone": 0,
        "topic": build.TOPIC_CATCHALL,
        "trump": False,
        "k": "deadbeef00",
    }
    it.update(extra)
    return it


def mk_state_entry(title, first_seen, peak_outlets=1, last_seen=None):
    return {"toks": sorted(build.tokens(title)),
            "first_seen": first_seen.isoformat(),
            "last_seen": (last_seen or first_seen).isoformat(),
            "peak_outlets": peak_outlets}


def hist_entry(days_back, rosy=55, trump=30, fw=25):
    d = (utcnow() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    return {"d": d, "rosy": rosy, "trump": trump, "fw": fw}


def gate_feeds(source_counts):
    """url -> RSS bytes for the first len(source_counts) FEEDS entries; every
    title is globally unique so dedupe keeps item counts predictable."""
    url_map, n = {}, 0
    for (source, url), count in zip(build.FEEDS, source_counts):
        items = []
        for _ in range(count):
            items.append((f"tale{n}alpha tale{n}bravo tale{n}charlie",
                          f"http://example.com/tale{n}", rfc(hours_ago(2))))
            n += 1
        url_map[url] = rss2_bytes(items)
    return url_map


class TempDirCase(unittest.TestCase):
    """Runs each test chdir'd into a fresh temp dir; always restores cwd."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._restore)
        os.chdir(self._td.name)

    def _restore(self):
        os.chdir(self._cwd)
        self._td.cleanup()


class MainRunnerMixin:
    """Run build.main() with fetch stubbed to serve url_map (thread-safe)."""

    def run_main(self, url_map):
        def stub_fetch(url, timeout=20):
            try:
                return url_map[url]
            except KeyError:
                raise ValueError("stub: feed down")

        with mock.patch.object(build, "fetch", stub_fetch), \
                mock.patch.dict(os.environ), \
                contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop("GITHUB_STEP_SUMMARY", None)
            return build.main()


# ── 1. parse_feed ───────────────────────────────────────────────────────────

class TestParseFeed(unittest.TestCase):

    def test_rss2_title_unescaped_and_whitespace_collapsed(self):
        # Feed publishers double-escape: XML-unescape happens in the parser,
        # then parse_feed html.unescape()s what's left.
        raw = rss2_bytes([("Rally &amp;#8212; markets &amp;amp; bonds\n   surge",
                           "http://example.com/a", rfc(hours_ago(2)))])
        items = build.parse_feed("BBC", raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Rally — markets & bonds surge")
        self.assertEqual(items[0]["link"], "http://example.com/a")
        self.assertAlmostEqual(items[0]["age_hours"], 2.0, delta=0.05)

    def test_rss2_drops_non_http_links(self):
        raw = rss2_bytes([
            ("Keeper story alpha", "https://example.com/keep", rfc(hours_ago(1))),
            ("Ftp story bravo", "ftp://example.com/nope", rfc(hours_ago(1))),
            ("Relative story charlie", "/relative/nope", rfc(hours_ago(1))),
            ("Linkless story delta", None, rfc(hours_ago(1))),
        ])
        items = build.parse_feed("BBC", raw)
        self.assertEqual([i["title"] for i in items], ["Keeper story alpha"])

    def test_rss2_drops_items_older_than_48h(self):
        raw = rss2_bytes([
            ("Fresh story alpha", "http://example.com/a", rfc(hours_ago(2))),
            ("Stale story bravo", "http://example.com/b", rfc(hours_ago(49))),
        ])
        items = build.parse_feed("BBC", raw)
        self.assertEqual([i["title"] for i in items], ["Fresh story alpha"])

    def test_good_sources_get_168h_window(self):
        raw = rss2_bytes([
            ("Slow sunshine story alpha", "http://example.com/a", rfc(hours_ago(100))),
            ("Ancient sunshine story bravo", "http://example.com/b", rfc(hours_ago(200))),
        ])
        items = build.parse_feed("GOOD NEWS NETWORK", raw)
        self.assertEqual([i["title"] for i in items], ["Slow sunshine story alpha"])
        self.assertAlmostEqual(items[0]["age_hours"], 100.0, delta=0.05)
        # The same 100h-old item from a normal source is stale.
        self.assertEqual(build.parse_feed("BBC", rss2_bytes(
            [("Slow sunshine story alpha", "http://example.com/a", rfc(hours_ago(100)))])), [])

    def test_undated_item_gets_479_default(self):
        raw = rss2_bytes([("Undated mystery story", "http://example.com/u", None)])
        items = build.parse_feed("BBC", raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["age_hours"], 47.9)

    def test_future_dated_treated_as_undated(self):
        raw = rss2_bytes([("Time traveler story", "http://example.com/f",
                           rfc(utcnow() + timedelta(hours=2)))])
        items = build.parse_feed("BBC", raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["age_hours"], 47.9)

    def test_slightly_future_dated_clamps_to_zero(self):
        # Within the 1h clock-skew allowance the date is kept, age clamped >= 0.
        raw = rss2_bytes([("Skewed clock story", "http://example.com/s",
                           rfc(utcnow() + timedelta(minutes=30)))])
        items = build.parse_feed("BBC", raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["age_hours"], 0.0)

    def test_rdf_rss10_feed_parses(self):
        raw = rdf_bytes([
            ("Deutsche welle story alpha", "http://example.com/dw1",
             hours_ago(2).isoformat()),
            ("Stale welle story bravo", "http://example.com/dw2",
             hours_ago(50).isoformat()),
        ])
        items = build.parse_feed("DW", raw)
        self.assertEqual([i["title"] for i in items], ["Deutsche welle story alpha"])
        self.assertAlmostEqual(items[0]["age_hours"], 2.0, delta=0.05)

    def test_atom_feed_parses(self):
        raw = atom_bytes([
            ("Atom entry story alpha", "https://example.com/at1",
             hours_ago(3).isoformat()),
        ])
        items = build.parse_feed("TIME", raw)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Atom entry story alpha")
        self.assertEqual(items[0]["link"], "https://example.com/at1")
        self.assertAlmostEqual(items[0]["age_hours"], 3.0, delta=0.05)

    def test_atom_link_without_href_dropped(self):
        raw = atom_bytes([("Hrefless atom story", None, hours_ago(1).isoformat())])
        self.assertEqual(build.parse_feed("TIME", raw), [])


# ── 2. fetch hardening ──────────────────────────────────────────────────────

class FakeResp:
    """Context-manager response whose read() serves chunks, optionally slowly."""

    def __init__(self, chunk, n_chunks=100, delay=0.0):
        self.chunk, self.left, self.delay = chunk, n_chunks, delay

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n=-1):
        if self.delay:
            time.sleep(self.delay)
        if self.left <= 0:
            return b""
        self.left -= 1
        return self.chunk


class TestFetchHardening(unittest.TestCase):

    def test_oversize_feed_aborts_with_cap_error(self):
        resp = FakeResp(b"x" * (2 ** 20), n_chunks=10)
        with mock.patch.object(urllib.request, "urlopen",
                               lambda req, timeout=20: resp):
            with self.assertRaisesRegex(ValueError, "cap"):
                build.fetch("http://example.com/huge")

    def test_tarpit_drip_aborts_with_deadline_error(self):
        resp = FakeResp(b"drip", n_chunks=100, delay=0.12)
        with mock.patch.object(build, "FETCH_DEADLINE", 0.05), \
                mock.patch.object(urllib.request, "urlopen",
                                  lambda req, timeout=20: resp):
            with self.assertRaisesRegex(ValueError, "deadline"):
                build.fetch("http://example.com/tarpit")


# ── 3. bail-out gate boundaries (via main() in a tempdir) ───────────────────

class TestBailoutGate(TempDirCase, MainRunnerMixin):

    def assert_no_rebuild(self):
        self.assertFalse(os.path.exists("index.html"))
        self.assertFalse(os.path.exists("feed.xml"))
        self.assertFalse(os.path.exists("state.json"))

    def test_nine_items_five_sources_holds(self):
        rc = self.run_main(gate_feeds([2, 2, 2, 2, 1]))
        self.assertEqual(rc, 0)
        self.assert_no_rebuild()

    def test_ten_items_four_sources_holds(self):
        rc = self.run_main(gate_feeds([3, 3, 2, 2]))
        self.assertEqual(rc, 0)
        self.assert_no_rebuild()

    def test_ten_items_five_sources_rebuilds(self):
        day_before = utcnow().strftime("%Y-%m-%d")
        rc = self.run_main(gate_feeds([2, 2, 2, 2, 2]))
        day_after = utcnow().strftime("%Y-%m-%d")
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists("index.html"))
        self.assertTrue(os.path.exists("feed.xml"))
        self.assertTrue(os.path.exists("state.json"))
        with open("index.html", encoding="utf-8") as f:
            self.assertIn("THE GRUDGE REPORT", f.read())
        with open("state.json", encoding="utf-8") as f:
            state = json.load(f)
        self.assertEqual(state["v"], 2)
        self.assertIsInstance(state["history"], list)
        self.assertEqual(len(state["history"]), 1)
        entry = state["history"][0]
        self.assertIn(entry["d"], {day_before, day_after})
        for key in ("d", "rosy", "trump", "fw"):
            self.assertIn(key, entry)


# ── 4. load_state per-key salvage ───────────────────────────────────────────

class TestLoadState(TempDirCase):

    def write_state(self, obj):
        with open("state.json", "w", encoding="utf-8") as f:
            if isinstance(obj, str):
                f.write(obj)
            else:
                json.dump(obj, f)

    def load_quiet(self):
        with contextlib.redirect_stderr(io.StringIO()):
            return build.load_state()

    def test_missing_file_gives_empty_shape(self):
        self.assertEqual(build.load_state(),
                         {"clusters": [], "lead": None, "history": []})

    def test_json_list_gives_empty_shape(self):
        self.write_state("[1, 2, 3]")
        self.assertEqual(self.load_quiet(),
                         {"clusters": [], "lead": None, "history": []})

    def test_clusters_dict_salvaged_history_preserved(self):
        good_history = [hist_entry(1), hist_entry(0)]
        self.write_state({"v": 2,
                          "clusters": {"not": "a list"},
                          "lead": "not a dict",
                          "history": good_history})
        state = self.load_quiet()
        self.assertEqual(state["clusters"], [])
        self.assertIsNone(state["lead"])
        self.assertEqual(state["history"], good_history)

    def test_malformed_history_dropped_clusters_preserved(self):
        good_cluster = mk_state_entry("salvage cluster story", hours_ago(4))
        self.write_state({"v": 2,
                          "clusters": [good_cluster,
                                       {"toks": "not-a-list"},
                                       "junk"],
                          "history": [42,
                                      "nope",
                                      {"trump": 5},          # dict without d
                                      {"d": 3}]})            # d not a str
        state = self.load_quiet()
        self.assertEqual(state["clusters"], [good_cluster])
        self.assertEqual(state["history"], [])


# ── 5. save/load round-trip ─────────────────────────────────────────────────

class TestSaveLoadRoundTrip(TempDirCase):

    def test_round_trip_uncapped_atomic_same_day_replaced(self):
        now = utcnow()
        today = now.strftime("%Y-%m-%d")
        old_days = [hist_entry(i) for i in range(400, 0, -1)]
        stale_today = {"d": today, "rosy": 1, "trump": 2, "fw": 3}
        state = {"clusters": [], "lead": None,
                 "history": old_days + [stale_today]}
        item = mk_item("roundtrip fixture story", score=18.0, cluster=2)
        item["first_seen"] = now.isoformat()
        item["peak_outlets"] = 2
        build.save_state(state, [item], item, now, 61, 33, 27)

        self.assertFalse(os.path.exists("state.json.tmp"))  # atomic write
        with open("state.json", encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(raw["v"], 2)

        loaded = build.load_state()
        hist = loaded["history"]
        self.assertEqual(len(hist), 401)  # 400 old days survive: UNCAPPED
        todays = [h for h in hist if h["d"] == today]
        self.assertEqual(todays, [{"d": today, "rosy": 61, "trump": 33, "fw": 27}])
        self.assertEqual(loaded["clusters"][0]["toks"], sorted(item["toks"]))
        self.assertEqual(loaded["clusters"][0]["peak_outlets"], 2)
        self.assertEqual(sorted(loaded["lead"]["toks"]), sorted(item["toks"]))


# ── 6. apply_state: the night editor ────────────────────────────────────────

class TestApplyState(TempDirCase):

    def test_tenure_decay_past_soft_limit(self):
        now = utcnow()
        title = "lingering front page story"
        state = {"clusters": [mk_state_entry(title, now - timedelta(hours=20))],
                 "lead": None, "history": []}
        item = mk_item(title, score=20.0, cluster=1)
        with contextlib.redirect_stderr(io.StringIO()):
            on_page, tracked = build.apply_state([item], state, now)
        # 20h tenure -> 8h past TENURE_SOFT_H at 0.75/h = -6.0
        self.assertAlmostEqual(item["tenure_h"], 20.0, places=5)
        self.assertAlmostEqual(item["score"], 14.0, places=5)
        self.assertIn(item, on_page)

    def test_hard_pull_past_30h_still_tracked(self):
        now = utcnow()
        title = "overstaying welcome story"
        state = {"clusters": [mk_state_entry(title, now - timedelta(hours=31))],
                 "lead": None, "history": []}
        item = mk_item(title, score=20.0, cluster=1)
        with contextlib.redirect_stderr(io.StringIO()):
            on_page, tracked = build.apply_state([item], state, now)
        self.assertNotIn(item, on_page)   # pulled off the page
        self.assertIn(item, tracked)      # but still tracked: no NEW comeback
        self.assertFalse(item["fresh"])

    def test_rising_exemption_keeps_on_page_and_boosts(self):
        now = utcnow()
        title = "story gaining outlets fast"
        state = {"clusters": [mk_state_entry(title, now - timedelta(hours=31),
                                             peak_outlets=1)],
                 "lead": None, "history": []}
        item = mk_item(title, score=20.0, cluster=3)
        with contextlib.redirect_stderr(io.StringIO()):
            on_page, tracked = build.apply_state([item], state, now)
        self.assertTrue(item["rising"])
        self.assertIn(item, on_page)  # tenure 31h > 30h, but rising exempts
        # +RISING_BONUS*(3-1)=12, then -(31-12)*0.75=-14.25 -> 17.75
        self.assertAlmostEqual(item["score"], 17.75, places=5)
        self.assertEqual(item["peak_outlets"], 3)

    def test_peak_ratchet_persists_through_save(self):
        now = utcnow()
        title = "ratcheting peak outlets story"
        state = {"clusters": [mk_state_entry(title, now - timedelta(hours=2),
                                             peak_outlets=1)],
                 "lead": None, "history": []}
        item = mk_item(title, score=20.0, cluster=3)
        with contextlib.redirect_stderr(io.StringIO()):
            on_page, tracked = build.apply_state([item], state, now)
        self.assertTrue(item["rising"])
        build.save_state(state, tracked, None, now, 50, 0, 0)

        state2 = build.load_state()
        self.assertEqual(state2["clusters"][0]["peak_outlets"], 3)
        # Outlets fall back to 2: peak stays 3, story is no longer rising.
        item2 = mk_item(title, score=20.0, cluster=2)
        with contextlib.redirect_stderr(io.StringIO()):
            build.apply_state([item2], state2, now + timedelta(hours=1))
        self.assertFalse(item2["rising"])
        self.assertEqual(item2["peak_outlets"], 3)


# ── 7. choose_lead ──────────────────────────────────────────────────────────

class TestChooseLead(unittest.TestCase):

    def fixture(self, top_score=30.0, challenger_score=25.0, prev_score=29.0,
                crowned_hours_ago=5.0):
        now = utcnow()
        top = mk_item("alpha earthquake devastates region", score=top_score,
                      cluster=2)
        challenger = mk_item("zebra festival delights children",
                             score=challenger_score, cluster=2)
        state = {"clusters": [], "history": [],
                 "lead": {"toks": sorted(top["toks"]),
                          "since": (now - timedelta(hours=crowned_hours_ago)).isoformat(),
                          "score": prev_score}}
        return [top, challenger], state, now, top, challenger

    def test_fatigued_lead_rotates_to_challenger(self):
        ranked, state, now, top, challenger = self.fixture()
        # 5h crowned > LEAD_FATIGUE_H=4; 25 >= 0.75*30; different story.
        with contextlib.redirect_stderr(io.StringIO()):
            result = build.choose_lead(ranked, state, now)
        self.assertIs(result[0], challenger)
        self.assertIs(result[1], top)

    def test_growing_lead_keeps_crown(self):
        # Score grew by >5 since crowning: fatigue does not apply.
        ranked, state, now, top, challenger = self.fixture(prev_score=20.0)
        result = build.choose_lead(ranked, state, now)
        self.assertIs(result[0], top)

    def test_weak_challenger_cannot_take_crown(self):
        ranked, state, now, top, challenger = self.fixture(challenger_score=20.0)
        # 20 < 0.75*30=22.5: crown stays even though the lead is fatigued.
        result = build.choose_lead(ranked, state, now)
        self.assertIs(result[0], top)


# ── 8. yesterday_dose ───────────────────────────────────────────────────────

class TestYesterdayDose(unittest.TestCase):

    def test_exact_yesterday_entry_returns_trump_value(self):
        state = {"history": [hist_entry(2, trump=99), hist_entry(1, trump=37)]}
        self.assertEqual(build.yesterday_dose(state, utcnow()), 37)

    def test_three_day_gap_returns_none(self):
        state = {"history": [hist_entry(3, trump=44)]}
        self.assertIsNone(build.yesterday_dose(state, utcnow()))

    def test_missing_trump_key_returns_none(self):
        y = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        state = {"history": [{"d": y, "rosy": 50, "fw": 20}]}
        self.assertIsNone(build.yesterday_dose(state, utcnow()))

    def test_empty_history_returns_none(self):
        self.assertIsNone(build.yesterday_dose({"history": []}, utcnow()))


# ── 9. wire stats ───────────────────────────────────────────────────────────

class TestWireStats(unittest.TestCase):

    def test_full_wire_dose_empty_is_zero(self):
        self.assertEqual(build.full_wire_dose([]), 0)

    def test_full_wire_dose_rounding(self):
        clusters = [{"trump": True}, {"trump": False}, {"trump": True}]
        self.assertEqual(build.full_wire_dose(clusters), 67)  # round(200/3)

    def test_wire_stats_front_page_definitions(self):
        ranked = [{"tone": 2, "trump": True}, {"tone": -3, "trump": False},
                  {"tone": 0, "trump": False}, {"tone": 1, "trump": True}]
        natural, nat_dose = build.wire_stats(ranked)
        self.assertEqual(natural, 67)   # 2 rosy / (2 rosy + 1 grim); neutral excluded
        self.assertEqual(nat_dose, 50)  # 2 trump / 4 stories

    def test_wire_stats_empty(self):
        self.assertEqual(build.wire_stats([]), (50, 0))


# ── 10. sparklines ──────────────────────────────────────────────────────────

class TestSparkline(unittest.TestCase):

    def test_svg_empty_series_is_empty(self):
        self.assertEqual(build.sparkline_svg([], "#c00"), "")

    def test_svg_single_point_is_empty_no_zero_division(self):
        self.assertEqual(build.sparkline_svg([50], "#c00"), "")

    def test_svg_two_and_thirty_points(self):
        two = build.sparkline_svg([10, 90], "#c00")
        self.assertIn("<svg", two)
        self.assertIn("<polyline", two)
        thirty = build.sparkline_svg([(i * 7) % 101 for i in range(30)], "#070")
        self.assertIn("<polyline", thirty)

    def test_spark_html_accruing_below_two_entries(self):
        self.assertIn("ACCRUING", build.spark_html([]))
        self.assertIn("ACCRUING", build.spark_html([hist_entry(0)]))

    def test_spark_html_skips_malformed_entries(self):
        history = [hist_entry(2), {"d": "malformed"}, hist_entry(1)]
        out = build.spark_html(history)
        self.assertIn("<svg", out)
        self.assertIn("LAST 2 DAYS", out)  # the malformed entry was skipped


# ── 11. render output contract ──────────────────────────────────────────────

RENDER_KEYWORDS = ["trump", "maga", "congress", "ukraine", "russia", "gaza",
                   "stocks", "inflation", "tariffs", "nasa", "spacex", "nvidia",
                   "picnic", "quilt", "garden"]


def render_fixture():
    """A small ranked list via dedupe_and_rank (real fields incl. k), spanning
    all five desks; titles share only one token so nothing clusters."""
    items = [wire_item(f"{kw} chronicle piece{i}x piece{i}y")
             for i, kw in enumerate(RENDER_KEYWORDS)]
    return build.dedupe_and_rank(items)


class TestRenderContract(unittest.TestCase):

    def render(self, prev_dose=42, history=None, natural=60, nat_dose=35):
        ranked = render_fixture()
        if history is None:
            history = [hist_entry(2), hist_entry(1), hist_entry(0)]
        return ranked, build.render(ranked, ["BBC", "CNN"], utcnow(),
                                    natural, nat_dose, prev_dose, history)

    def test_page_contract(self):
        ranked, page = self.render()
        self.assertIn("var YDAY = ", page)
        self.assertIn("var BUILT = ", page)
        expected_k = hashlib.sha1(ranked[0]["link"].encode()).hexdigest()[:10]
        self.assertIn(f'data-k="{expected_k}"', page)
        self.assertIn(f'<link rel="canonical" href="{build.SITE_URL}">', page)
        self.assertIn('data:image/svg+xml', page)  # favicon data URI
        self.assertIn(f'<meta property="og:image" content="{build.SITE_URL}og-card.png">', page)
        self.assertIn('type="application/rss+xml"', page)
        self.assertIn(f'{build.SITE_URL}feed.xml', page)
        self.assertIn("NOT AFFILIATED WITH THE DRUDGE REPORT", page)
        self.assertIn("<svg", page)  # history has >= 2 entries

    def test_goatcounter_only_when_configured(self):
        with mock.patch.object(build, "GOATCOUNTER_CODE", "testcode"):
            _, page = self.render()
        self.assertIn('data-goatcounter="https://testcode.goatcounter.com/count"', page)
        self.assertIn("gc.zgo.at/count.js", page)
        self.assertIn("GOATCOUNTER", page)  # footer disclosure
        # Default (empty code): no analytics anywhere on the page.
        _, page = self.render()
        self.assertNotIn("goatcounter", page.lower())
        self.assertNotIn("gc.zgo.at", page)

    def test_yesterday_suffix_toggles_on_prev_dose(self):
        _, page = self.render(prev_dose=42)
        self.assertIn("YESTERDAY 42%", page)
        _, page = self.render(prev_dose=None)
        self.assertNotIn("YESTERDAY", page)

    def test_accruing_when_history_short(self):
        _, page = self.render(history=[hist_entry(0)])
        self.assertIn("ACCRUING", page)
        self.assertNotIn("<svg", page.split("</style>")[1])  # no sparkline


# ── 12. write_feed ──────────────────────────────────────────────────────────

ATOM_NS = "{http://www.w3.org/2005/Atom}"


class TestWriteFeed(TempDirCase):

    def setUp(self):
        super().setUp()
        self.query_link = "http://example.com/story?a=1&b=2"
        self.ranked = build.dedupe_and_rank([
            wire_item("quasar bakery reopens downtown", link=self.query_link),
            wire_item("velvet marathon stuns spectators"),
        ])

    def test_feed_round_trip(self):
        now = utcnow()
        build.write_feed(self.ranked, now, 60, 35, 25)
        with open("feed.xml", "rb") as f:
            raw = f.read()
        self.assertIn(b"?a=1&amp;b=2", raw)  # escaped on disk...

        ch = ET.parse("feed.xml").getroot().find("channel")
        items = ch.findall("item")
        self.assertEqual(len(items), 1 + len(self.ranked))

        stat = items[0]
        self.assertEqual(stat.find("guid").text, f"stat-{now:%Y-%m-%d}")
        self.assertEqual(stat.find("guid").get("isPermaLink"), "false")
        stat_when = parsedate_to_datetime(stat.find("pubDate").text)
        self.assertIsNotNone(stat_when.tzinfo)

        by_link = {it.find("link").text: it for it in items[1:]}
        self.assertIn(self.query_link, by_link)  # ...unescaped on parse-back
        for link, it in by_link.items():
            self.assertEqual(it.find("guid").text, link)  # story guid == link
            when = parsedate_to_datetime(it.find("pubDate").text)
            self.assertLess(abs((when - hours_ago(2)).total_seconds()), 300)

        self_link = ch.find(f"{ATOM_NS}link")
        self.assertIsNotNone(self_link)
        self.assertEqual(self_link.get("rel"), "self")
        self.assertEqual(self_link.get("href"), build.SITE_URL + "feed.xml")

    def test_stat_guid_stable_across_same_day_runs(self):
        now = utcnow()
        t1 = now.replace(hour=3, minute=0, second=0, microsecond=0)
        t2 = now.replace(hour=21, minute=45, second=0, microsecond=0)

        def stat_guid():
            ch = ET.parse("feed.xml").getroot().find("channel")
            return ch.findall("item")[0].find("guid").text

        build.write_feed(self.ranked, t1, 60, 35, 25)
        guid1 = stat_guid()
        build.write_feed(self.ranked, t2, 55, 40, 30)
        guid2 = stat_guid()
        self.assertEqual(guid1, guid2)  # 48 rebuilds must not spam subscribers


# ── 13. undated-item retirement property ────────────────────────────────────

class TestUndatedRetirement(TempDirCase):

    def test_reserved_undated_item_retires_and_stays_retired(self):
        title = "orbiting teapot mystery unresolved"
        base = utcnow()
        on_page_runs, tracked_runs, fresh_runs = [], [], []
        for i in range(5):  # runs at tenure 0, 12, 24, 36, 48 hours
            now_i = base + timedelta(hours=12 * i)
            state = build.load_state()
            # The feed re-serves the same undated item every run: parse_feed
            # would stamp it 47.9h each time.
            item = {"source": "CNN", "title": title,
                    "link": "http://example.com/teapot", "age_hours": 47.9}
            ranked = build.dedupe_and_rank([item])
            with contextlib.redirect_stderr(io.StringIO()):
                on_page, tracked = build.apply_state(ranked, state, now_i)
            on_page_runs.append(any(it["title"] == title for it in on_page))
            tracked_runs.append(any(it["title"] == title for it in tracked))
            fresh_runs.append(ranked[0]["fresh"])
            build.save_state(state, tracked, on_page[0] if on_page else None,
                             now_i, 50, 0, 0)
        # On page until cumulative tenure crosses TENURE_HARD_H=30, then gone.
        self.assertEqual(on_page_runs, [True, True, True, False, False])
        self.assertEqual(tracked_runs, [True] * 5)   # never forgotten...
        self.assertEqual(fresh_runs, [False] * 5)    # ...never badged NEW


# ── 14. end-to-end: two-run YESTERDAY ───────────────────────────────────────

class TestTwoRunYesterday(TempDirCase, MainRunnerMixin):

    def test_second_day_run_shows_yesterday(self):
        feeds = gate_feeds([2, 2, 2, 2, 2])
        self.assertEqual(self.run_main(feeds), 0)
        with open("index.html", encoding="utf-8") as f:
            self.assertNotIn("YESTERDAY", f.read())  # day one: no history yet

        # Overnight: hand-roll the history entry back to yesterday's date.
        with open("state.json", encoding="utf-8") as f:
            state = json.load(f)
        yesterday = (utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        for h in state["history"]:
            h["d"] = yesterday
        with open("state.json", "w", encoding="utf-8") as f:
            json.dump(state, f)

        self.assertEqual(self.run_main(feeds), 0)
        with open("index.html", encoding="utf-8") as f:
            self.assertIn("YESTERDAY", f.read())


# ── Node-based tests: the client-side app JS ────────────────────────────────

# DOM stub adapted from the live-run harness: extracts the pool JSON block and
# the /*GRUDGE-APP*/ block, stubs document/localStorage, fires the slider
# input handlers including a drag through the natural dose value (the
# historical YDAY ReferenceError crash path), and probes window.__grudge.
# With argv[3] = fixture path it additionally prints partition parity JSON.
STUB_JS = r"""
// Minimal DOM stub for THE GRUDGE REPORT app script (test harness).
const fs = require("fs");
const page = fs.readFileSync(process.argv[2], "utf-8");
const poolMatch = page.match(/<script id="pool" type="application\/json">([\s\S]*?)<\/script>/);
const appMatch = page.match(/<script>\s*\/\*GRUDGE-APP\*\/([\s\S]*?)<\/script>/);
if (!poolMatch || !appMatch) { console.error("FAIL: script blocks not found"); process.exit(1); }
if (!appMatch[1].includes("function readout")) { console.error("FAIL: sentinel missing"); process.exit(1); }

function el(id) {
  return {
    id, value: "0", textContent: "",
    innerHTML: "", className: "", children: [], handlers: {},
    addEventListener(type, fn) { (this.handlers[type] ||= []).push(fn); },
    appendChild(c) { this.children.push(c); },
    insertBefore(c) { this.children.unshift(c); },
    setAttribute() {}, getAttribute() { return ""; },
    get firstChild() { return this.children[0] || null; },
  };
}
const els = {};
["pool","mix","dose","jread","dread","leadbox","col0","col1","col2"].forEach(id => els[id] = el(id));
els.pool.textContent = poolMatch[1];
global.window = global;
global.document = {
  getElementById: id => els[id] || el(id),
  createElement: tag => el(tag),
  addEventListener() {},
  body: el("body"),
};
global.localStorage = { getItem: () => null, setItem: () => {} };

eval(appMatch[1]);

// Reproduce the old YDAY crash path: drag mix while dose sits at NATDOSE.
const natdose = els.dose.value = String(page.match(/var NATDOSE = (\d+);/)[1]);
els.mix.value = "80";
els.mix.handlers.input.forEach(fn => fn());
// Then drag dose through several values including back to natural.
for (const v of ["0", "50", natdose, "100"]) {
  els.dose.value = v;
  els.dose.handlers.input.forEach(fn => fn());
}
if (!global.__grudge || typeof global.__grudge.partition !== "function") {
  console.error("FAIL: __grudge test hooks missing"); process.exit(1);
}
if (process.argv[3]) {
  const fixture = JSON.parse(fs.readFileSync(process.argv[3], "utf-8"));
  const cols = global.__grudge.partition(fixture);
  console.log("PARITY:" + JSON.stringify(cols.map(c => c.map(s => s[0]))));
} else {
  const cols = global.__grudge.partition(JSON.parse(poolMatch[1]).slice(1));
  console.log("PASS: no exceptions; partition cols: " + cols.map(c => c.length).join("/"));
}
"""

# Distinct desk sizes (7/5/4/3/2) make the bin packing deterministic, so the
# Python and JS partitions must agree exactly, tie-breaking included.
PARITY_SIZES = [("WASHINGTON", 7), ("WORLD", 5), ("MONEY", 4),
                ("TECH & SCIENCE", 3), ("LIFE & CULTURE", 2)]
PARITY_EXPECTED = [["WASHINGTON"],
                   ["WORLD", "LIFE & CULTURE"],
                   ["MONEY", "TECH & SCIENCE"]]


def parity_sequence():
    """Interleaved topic sequence — grouping order must not matter."""
    remaining = dict(PARITY_SIZES)
    seq = []
    while any(remaining.values()):
        for topic, _ in PARITY_SIZES:
            if remaining[topic]:
                seq.append(topic)
                remaining[topic] -= 1
    return seq


@unittest.skipUnless(NODE, "node not available")
class TestNodeApp(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        d = cls._td.name
        ranked = render_fixture()
        history = [hist_entry(2), hist_entry(1), hist_entry(0)]
        page = build.render(ranked, ["BBC", "CNN"], utcnow(), 60, 35, 42, history)
        cls.page_path = os.path.join(d, "page.html")
        cls.stub_path = os.path.join(d, "stub.js")
        cls.fixture_path = os.path.join(d, "parity_fixture.json")
        with open(cls.page_path, "w", encoding="utf-8") as f:
            f.write(page)
        with open(cls.stub_path, "w", encoding="utf-8") as f:
            f.write(STUB_JS)
        seq = parity_sequence()
        js_fixture = [{"tp": t, "u": f"u{i}", "sc": float(100 - i)}
                      for i, t in enumerate(seq)]
        with open(cls.fixture_path, "w", encoding="utf-8") as f:
            json.dump(js_fixture, f)
        cls.page = page

    @classmethod
    def tearDownClass(cls):
        cls._td.cleanup()

    def node(self, *args):
        return subprocess.run([NODE, *args], capture_output=True, text=True,
                              timeout=30)

    def test_app_script_passes_node_check(self):
        m = re.search(r"<script>\s*/\*GRUDGE-APP\*/(.*?)</script>", self.page, re.S)
        self.assertIsNotNone(m)
        self.assertIn("function readout", m.group(1))
        app_path = os.path.join(self._td.name, "app.js")
        with open(app_path, "w", encoding="utf-8") as f:
            f.write(m.group(1))
        proc = self.node("--check", app_path)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dom_stub_execution_no_exceptions(self):
        proc = self.node(self.stub_path, self.page_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PASS", proc.stdout)

    def test_partition_parity_python_vs_js(self):
        seq = parity_sequence()
        py_cols = build.partition([{"topic": t} for t in seq])
        py_names = [[name for name, _items in col] for col in py_cols]
        self.assertEqual(py_names, PARITY_EXPECTED)

        proc = self.node(self.stub_path, self.page_path, self.fixture_path)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        parity_lines = [ln for ln in proc.stdout.splitlines()
                        if ln.startswith("PARITY:")]
        self.assertEqual(len(parity_lines), 1, proc.stdout)
        js_names = json.loads(parity_lines[0][len("PARITY:"):])
        self.assertEqual(js_names, py_names)


if __name__ == "__main__":
    unittest.main()
