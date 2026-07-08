#!/usr/bin/env python3
"""THE GRUDGE REPORT — an auto-populated Drudge Report competitor.

Fetches headlines from 25 news RSS feeds (stdlib only, no dependencies),
scores each for drama AND judges its tone (grim vs. rosy), dedupes across
outlets, picks a lead story, and renders a classic three-column, all-caps,
Courier-font front page to index.html — topped by THE JUDGMENT, a slider
that lets readers dial the mix of negative and positive news, and TRUMP
DENSITY, a dial that sets how much administration coverage the page
carries. The measured share is published as a live stat with an uncapped
daily history — the only consumer front page that prints its own number
and hands the reader the dial.

The editor never sleeps: state.json is the paper's memory between runs.
Stories that are being picked up by more outlets get boosted and badged
RISING; stories that have sat on the page too long decay and get pulled;
a lead that stops growing loses the siren after a few hours.

The page itself lives in template.html (string.Template, still stdlib);
build.py fills it in. Outputs: index.html, feed.xml, state.json.

Run:   python3 build.py
Test:  python3 -m unittest -v
"""

import concurrent.futures
import hashlib
import html
import json
import os
import re
import string
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime

FEEDS = [
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("BBC US", "https://feeds.bbci.co.uk/news/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    ("GUARDIAN", "https://www.theguardian.com/world/rss"),
    ("CNN", "http://rss.cnn.com/rss/cnn_topstories.rss"),
    ("FOX", "https://moxie.foxnews.com/google-publisher/latest.xml"),
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("AL JAZEERA", "https://www.aljazeera.com/xml/rss/all.xml"),
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("THE HILL", "https://thehill.com/news/feed/"),
    ("POLITICO", "https://rss.politico.com/politics-news.xml"),
    ("ABC", "https://abcnews.go.com/abcnews/topstories"),
    ("CBS", "https://www.cbsnews.com/latest/rss/main"),
    ("WSJ", "https://feeds.a.dj.com/rss/RSSWorldNews.xml"),
    ("SKY", "https://feeds.skynews.com/feeds/rss/world.xml"),
    ("DW", "https://rss.dw.com/rdf/rss-en-all"),
    ("FRANCE 24", "https://www.france24.com/en/rss"),
    ("INDEPENDENT", "https://www.independent.co.uk/news/world/rss"),
    ("TIME", "https://time.com/feed/"),
    ("AXIOS", "https://api.axios.com/feed/"),
    ("ECONOMIST", "https://www.economist.com/latest/rss.xml"),
    ("MARKETWATCH", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("GOOD NEWS NETWORK", "https://www.goodnewsnetwork.org/feed/"),
    ("POSITIVE.NEWS", "https://www.positive.news/feed/"),
    ("REASONS TO BE CHEERFUL", "https://reasonstobecheerful.world/feed/"),
]

# Dedicated good-news outlets: they publish slowly (so they get a 7-day
# freshness window instead of 48h) and their stories are positive by
# construction (so they get a +1 tone prior on top of the lexicon).
GOOD_SOURCES = {"GOOD NEWS NETWORK", "POSITIVE.NEWS", "REASONS TO BE CHEERFUL"}

MAX_PER_SOURCE = 40   # stop one chatty feed from flooding the page
POOL_SIZE = 150       # stories embedded for the client-side judgment mixer
PAGE_STORIES = 60     # stories shown below the lead

SITE_URL = "https://jlgreen11.github.io/drudge/"   # canonical; swap on custom domain

# Analytics (optional): create a free GoatCounter account and put its site
# code here (e.g. "grudgereport"). Empty string = no analytics, no external
# scripts — the page stays fully self-contained. See README.
GOATCOUNTER_CODE = ""

# The page template lives next to this script so build.py can run from any
# working directory (tests run it from a temp dir).
TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "template.html")

FEED_FILE = "feed.xml"
FEED_ITEMS = 30       # stories in the RSS output feed

MIN_ITEMS = 10        # rebuild only with this many stories...
MIN_SOURCES = 5       # ...from this many distinct outlets (one chatty feed
                      # must not rebuild a single-outlet front page)

FETCH_MAX_BYTES = 5 * 2 ** 20   # a feed bigger than 5MB is not a feed
FETCH_DEADLINE = 60.0           # wall-clock seconds per feed (timeout= is per
                                # socket op; a tarpit drip never trips it)
FETCH_ALL_TIMEOUT = 300         # global budget: ceil(25/8) waves x ~80s + margin

# ── The night editor's rulebook: when to put on, when to pull off ──────────
STATE_FILE = "state.json"
STATE_PRUNE_H = 72.0        # forget clusters not seen for this long
TENURE_SOFT_H = 12.0        # a story starts bleeding score after this long on page
TENURE_PENALTY = 0.75       # points lost per hour past the soft limit
TENURE_HARD_H = 30.0        # pulled off the page after this long, unless rising
RISING_BONUS = 6.0          # score bonus per outlet gained since the story's peak
FRESH_BADGE_H = 3.0         # unseen stories younger than this get the NEW badge
LEAD_FATIGUE_H = 4.0        # max hours a non-growing lead keeps the siren
LEAD_MIN_OUTLETS = 2        # a lead must be confirmed by 2+ outlets...
LEAD_SOLO_SCORE = 40.0      # ...or be scorching hot on its own
LEAD_RIVAL_RATIO = 0.75     # a challenger this close (or better) can take a tired crown
HISTORY_DAYS = 30           # sparkline display window; the history series
                            # itself is kept UNCAPPED (~15KB/yr) — the daily
                            # time series is the asset, never FIFO it away

# Words that make a headline siren-worthy. Weight = drama.
HOT_WORDS = {
    "breaking": 10, "dead": 8, "dies": 8, "killed": 8, "death": 7,
    "war": 7, "attack": 7, "strike": 6, "strikes": 6, "crisis": 6,
    "emergency": 6, "explosion": 7, "crash": 6, "shooting": 7,
    "resigns": 7, "fired": 6, "impeach": 8, "indicted": 8, "arrested": 6,
    "collapse": 6, "record": 4, "shock": 6, "chaos": 6, "fury": 5,
    "slams": 4, "warns": 4, "threat": 5, "nuclear": 7, "invasion": 8,
    "hurricane": 6, "earthquake": 7, "wildfire": 5, "outbreak": 6,
    "election": 5, "president": 4, "supreme court": 6, "scandal": 6,
    "leaked": 5, "exclusive": 4, "revealed": 3, "surge": 4, "plunge": 5,
    "soars": 4, "historic": 4, "unprecedented": 5, "massive": 4,
    "riot": 6, "protest": 4, "hostage": 7, "missile": 7, "troops": 5,
    "banned": 4, "lawsuit": 4, "verdict": 5, "guilty": 6, "billion": 3,
    "trillion": 4, "ai": 3, "hack": 5, "breach": 5,
    "breakthrough": 6, "cure": 6, "rescue": 6, "miracle": 6, "triumph": 5,
}

# THE JUDGMENT: tone lexicons. tone = sum(pos) - sum(neg); >0 rosy, <0 grim.
GRIM_WORDS = {
    "dead": 3, "dies": 3, "death": 3, "killed": 3, "kills": 3, "murder": 3,
    "war": 3, "massacre": 3, "genocide": 3, "terror": 3, "bomb": 3,
    "suicide": 3, "rape": 3,
    "attack": 2, "crisis": 2, "crash": 2, "shooting": 2, "shot": 2,
    "explosion": 2, "missile": 2, "nuclear": 2, "invasion": 2, "hostage": 2,
    "riot": 2, "violence": 2, "violent": 2, "deadly": 2, "fatal": 2,
    "tragedy": 2, "tragic": 2, "disaster": 2, "famine": 2, "outbreak": 2,
    "pandemic": 2, "collapse": 2, "wildfire": 2, "hurricane": 2,
    "earthquake": 2, "flood": 2, "evacuation": 2, "destroyed": 2,
    "guilty": 2, "fraud": 2, "corruption": 2, "arrested": 2, "indicted": 2,
    "prison": 2, "abuse": 2, "assault": 2, "victims": 2, "victim": 2,
    "wounded": 2, "injured": 2, "toll": 2, "grim": 2, "dire": 2,
    "worst": 2, "fears": 2, "threat": 2, "sanctions": 2, "layoffs": 2,
    "recession": 2, "torture": 2, "kidnap": 2, "kidnapped": 2,
    "warns": 1, "warning": 1, "fear": 1, "cuts": 1, "debt": 1,
    "inflation": 1, "lawsuit": 1, "sued": 1, "banned": 1, "ban": 1,
    "protest": 1, "clash": 1, "scandal": 1, "slams": 1, "backlash": 1,
    "fury": 1, "outrage": 1, "anger": 1, "angry": 1, "feud": 1, "row": 1,
    "crackdown": 1, "resigns": 1, "fired": 1, "ousted": 1, "impeach": 1,
    "coup": 1, "plunge": 1, "plummets": 1, "slump": 1, "tumble": 1,
    "losses": 1, "loses": 1, "missing": 1, "homeless": 1, "cancer": 1,
    "disease": 1, "virus": 1, "drought": 1, "smuggling": 1, "overdose": 1,
    "custody": 1, "chaos": 1, "struggling": 1, "shortage": 1, "blackout": 1,
    "fighting": 2, "displaced": 2, "fraudsters": 2, "scam": 2,
    "hospitalized": 1, "lose": 1, "divided": 1, "concerns": 1,
    "ruined": 2, "ruins": 2, "wrecked": 2, "slammed": 1, "mocks": 1,
    "criticism": 1, "tensions": 1,
}
ROSY_WORDS = {
    "breakthrough": 3, "cure": 3, "cured": 3, "rescue": 3, "rescued": 3,
    "saves": 3, "saved": 3, "hero": 3, "heroes": 3, "reunited": 3,
    "triumph": 3, "miracle": 3,
    "wins": 2, "win": 2, "won": 2, "victory": 2, "celebrates": 2,
    "celebration": 2, "joy": 2, "hope": 2, "hopeful": 2, "recovery": 2,
    "recovers": 2, "survives": 2, "survivor": 2, "success": 2,
    "successful": 2, "award": 2, "awarded": 2, "prize": 2, "honored": 2,
    "milestone": 2, "discovery": 2, "donates": 2, "donation": 2,
    "kindness": 2, "inspiring": 2, "uplifting": 2, "beloved": 2,
    "peace": 2, "ceasefire": 2, "treaty": 2, "thriving": 2, "revival": 2,
    "restored": 2, "champions": 2, "champion": 2, "medal": 2,
    "happy": 1, "happiness": 1, "love": 1, "adorable": 1, "cute": 1,
    "smile": 1, "laughter": 1, "generous": 1, "volunteer": 1,
    "volunteers": 1, "charity": 1, "festival": 1, "wedding": 1,
    "birth": 1, "born": 1, "baby": 1, "graduates": 1, "scholarship": 1,
    "boost": 1, "boosts": 1, "gains": 1, "rally": 1, "soars": 1,
    "deal": 1, "agreement": 1, "growth": 1, "expands": 1, "hiring": 1,
    "anniversary": 1, "celebrate": 1, "welcomes": 1, "blooming": 1,
    "renewable": 1, "protects": 1, "protected": 1, "cleaner": 1,
}

# ── Topic desks: every story gets filed to exactly one ─────────────────────
# Highest lexicon score wins the headline; below TOPIC_MIN it goes to the
# catch-all desk (sports, celebs, weather, oddities, good news).
TOPIC_CATCHALL = "LIFE & CULTURE"
TOPIC_MIN = 2
TOPICS = [
    ("WASHINGTON", {
        "trump": 3, "maga": 3, "white house": 3, "oval office": 3, "potus": 3,
        "vance": 3, "congress": 3, "senate": 2, "supreme court": 3, "scotus": 3,
        "executive order": 3, "impeach": 3, "impeachment": 3, "gop": 2,
        "republicans": 2, "republican": 2, "democrats": 2, "democrat": 2,
        "pentagon": 2, "doj": 2, "fbi": 2, "cia": 2, "irs": 2,
        "deportation": 2, "deportations": 2, "immigration": 2,
        "election": 1, "elections": 1, "campaign": 1, "governor": 1,
        "senator": 2, "congressman": 1, "congresswoman": 1, "capitol": 2,
        "federal judge": 2, "attorney general": 2, "biden": 2, "obama": 2,
        "medicaid": 1, "medicare": 1, "national guard": 2, "border": 1,
        "washington": 1, "filibuster": 3, "lawmakers": 2, "veto": 2,
    }),
    ("WORLD", {
        "ukraine": 3, "russia": 3, "russian": 2, "putin": 3, "zelensky": 3,
        "kyiv": 3, "moscow": 3, "gaza": 3, "israel": 3, "israeli": 2,
        "netanyahu": 3, "hamas": 3, "hezbollah": 3, "iran": 3, "tehran": 3,
        "china": 3, "chinese": 2, "beijing": 3, "taiwan": 3,
        "north korea": 3, "south korea": 3, "korea": 2, "nato": 3,
        "kremlin": 3, "united nations": 3, "europe": 2, "european": 2,
        "britain": 2, "uk": 2, "brexit": 3, "parliament": 2, "mp": 2,
        "le pen": 3, "macron": 3, "starmer": 3, "farage": 2,
        "london": 2, "france": 2, "paris": 2, "germany": 2,
        "berlin": 2, "india": 2, "pakistan": 2, "japan": 2, "tokyo": 2,
        "australia": 2, "canada": 2, "mexico": 2, "brazil": 2,
        "venezuela": 2, "cuba": 2, "syria": 2, "lebanon": 2, "yemen": 2,
        "iraq": 2, "afghanistan": 2, "taliban": 3, "africa": 2,
        "nigeria": 2, "kenya": 2, "south africa": 2, "ethiopia": 2,
        "sudan": 2, "refugee": 2, "refugees": 2, "migrants": 2,
        "minister": 1, "embassy": 2, "ambassador": 2,
    }),
    ("MONEY", {
        "stocks": 3, "stock": 2, "stock market": 3, "dow": 3, "nasdaq": 3,
        "wall street": 3, "fed": 3, "federal reserve": 3, "interest rates": 3,
        "inflation": 3, "tariff": 3, "tariffs": 3, "economy": 3,
        "economic": 2, "recession": 3, "jobs": 2, "jobless": 2,
        "unemployment": 3, "layoffs": 2, "hiring": 1, "earnings": 2,
        "profits": 2, "bitcoin": 3, "crypto": 3, "ethereum": 3, "oil": 2,
        "opec": 3, "housing": 2, "mortgage": 2, "bank": 2, "banks": 2,
        "banking": 2, "ipo": 2, "merger": 2, "dollar": 2, "treasury": 2,
        "deficit": 2, "billion": 1, "trillion": 1, "ceo": 1, "retail": 1,
        "consumer": 1, "prices": 1, "markets": 2, "investors": 2,
        "trade deal": 2, "debt": 1,
    }),
    ("TECH & SCIENCE", {
        "ai": 3, "artificial intelligence": 3, "openai": 3, "chatgpt": 3,
        "anthropic": 3, "google": 2, "apple": 2, "meta": 2, "microsoft": 2,
        "amazon": 2, "tesla": 2, "musk": 2, "spacex": 3, "nasa": 3,
        "rocket": 2, "satellite": 2, "chip": 2, "chips": 2,
        "semiconductor": 3, "nvidia": 3, "robot": 2, "robots": 2,
        "robotics": 3, "cyberattack": 3, "hackers": 2, "hacked": 2,
        "software": 2, "iphone": 2, "android": 2, "quantum": 3,
        "scientists": 3, "science": 2, "study": 2, "researchers": 3,
        "telescope": 3, "mars": 2, "moon": 2, "space": 2, "asteroid": 3,
        "vaccine": 2, "fda": 2, "medical": 2, "climate": 2, "warming": 2,
        "emissions": 2, "solar": 2, "crispr": 3, "dna": 2, "startup": 2,
    }),
]

# THE DOSAGE: stories filed under the president, whatever the desk.
TRUMP_RE = re.compile(r"\btrump\b|\bmaga\b|\bpotus\b|white house|oval office", re.I)

USER_AGENT = "Mozilla/5.0 (compatible; GrudgeReport/1.0; +https://github.com/jlgreen11/drudge)"
STOPWORDS = frozenset(
    "a an the of in on at to for with as by is are was were be been from "
    "and or but not this that it its his her their he she they after over "
    "under about into out up down new says say said will would could".split()
)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    # Read in bounded chunks against a wall-clock deadline: urlopen's timeout
    # covers each socket operation, so a hijacked feed dripping one byte per
    # second (or serving a multi-GB body) would otherwise wedge the run and,
    # via CI cancel-in-progress, every run after it. Oversize/overtime feeds
    # are aborted and skipped like any other broken feed.
    deadline = time.monotonic() + FETCH_DEADLINE
    data = bytearray()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        while True:
            chunk = resp.read(2 ** 20)
            if not chunk:
                break
            data += chunk
            if len(data) > FETCH_MAX_BYTES:
                raise ValueError(f"feed exceeds {FETCH_MAX_BYTES >> 20}MB cap")
            if time.monotonic() > deadline:
                raise ValueError(f"feed exceeded {FETCH_DEADLINE:.0f}s deadline")
    return bytes(data)


def text_of(el):
    return (el.text or "").strip() if el is not None else ""


def find_date(node):
    """First date-ish child of an item: pubDate, dc:date, published, updated."""
    for child in node:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in ("pubdate", "date", "published", "updated"):
            return text_of(child)
    return ""


def parse_feed(source, raw):
    """Parse RSS 2.0, RSS 1.0/RDF, or Atom into a list of item dicts."""
    # Strip default-namespace so RSS 1.0 / Atom tags are addressable plainly.
    raw = re.sub(rb'xmlns="[^"]+"', b"", raw, count=1)
    root = ET.fromstring(raw)
    items = []
    now = datetime.now(timezone.utc)

    for node in root.iter("item"):  # RSS 2.0 and RSS 1.0/RDF
        title = text_of(node.find("title"))
        link = text_of(node.find("link"))
        items.append((title, link, find_date(node)))
    if not items:
        for node in root.iter("entry"):  # Atom
            title = text_of(node.find("title"))
            link_el = node.find("link")
            link = link_el.get("href", "") if link_el is not None else ""
            items.append((title, link, find_date(node)))

    out = []
    for title, link, pub in items[:MAX_PER_SOURCE]:
        title = re.sub(r"\s+", " ", html.unescape(title)).strip()
        if not title or not link.startswith("http"):
            continue
        when = None
        if pub:
            try:
                when = parsedate_to_datetime(pub)
            except (TypeError, ValueError):
                try:
                    when = datetime.fromisoformat(pub.replace("Z", "+00:00"))
                except ValueError:
                    when = None
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when is not None and (when - now).total_seconds() > 3600:
            when = None  # clock-broken feed: don't let it squat at max freshness
        # Undated items get 47.9h: inside the window but never fresh enough
        # for the NEW badge or freshness bonus. (The default recurs every run;
        # actual re-injection is contained by the tenure system's 30h pull.)
        age_hours = (now - when).total_seconds() / 3600 if when else 47.9
        max_age = 168 if source in GOOD_SOURCES else 48
        if age_hours > max_age:  # stale news is no news
            continue
        out.append({
            "source": source,
            "title": title,
            "link": link,
            "age_hours": max(age_hours, 0.0),
        })
    return out


def tokens(title):
    words = re.findall(r"[a-z0-9']+", title.lower())
    return frozenset(w for w in words if w not in STOPWORDS and len(w) > 2)


def lexicon_score(title, lexicon):
    padded = " " + re.sub(r"[^a-z0-9 ]", " ", title.lower()) + " "
    return sum(w for word, w in lexicon.items() if f" {word} " in padded)


def judge(title):
    """THE JUDGMENT: tone of a headline. >0 rosy, <0 grim, 0 neutral."""
    return lexicon_score(title, ROSY_WORDS) - lexicon_score(title, GRIM_WORDS)


def classify(title):
    """File the story to a desk: highest topic-lexicon score wins."""
    best_topic, best = TOPIC_CATCHALL, 0
    for topic, lexicon in TOPICS:
        s = lexicon_score(title, lexicon)
        if s > best:
            best_topic, best = topic, s
    return best_topic if best >= TOPIC_MIN else TOPIC_CATCHALL


def score(item, cluster_size):
    s = float(lexicon_score(item["title"], HOT_WORDS))
    s += max(0.0, 12.0 - item["age_hours"])          # fresher is hotter
    s += (cluster_size - 1) * 8                       # multiple outlets = big story
    if item["title"].isupper():
        s += 3                                        # already shouting
    return s


def dedupe_and_rank(items):
    """Cluster near-duplicate headlines across sources; rank clusters."""
    clusters = []  # list of lists
    for item in items:
        toks = tokens(item["title"])
        if not toks:
            continue
        item["toks"] = toks
        placed = False
        for cluster in clusters:
            ref = cluster[0]["toks"]
            union = len(toks | ref)
            if union and len(toks & ref) / union >= 0.5:
                cluster.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])

    ranked = []
    for cluster in clusters:
        best = min(cluster, key=lambda i: i["age_hours"])
        n_sources = len({i["source"] for i in cluster})
        best["score"] = score(best, n_sources)
        best["cluster"] = n_sources
        best["tone"] = judge(best["title"]) + (1 if best["source"] in GOOD_SOURCES else 0)
        best["topic"] = classify(best["title"])
        best["trump"] = bool(TRUMP_RE.search(best["title"]))
        # Per-link key for the optional click beacons. Computed here because
        # browser JS has no synchronous SHA-1; the pool ships it ready-made.
        best["k"] = hashlib.sha1(best["link"].encode("utf-8")).hexdigest()[:10]
        ranked.append(best)
    ranked.sort(key=lambda i: i["score"], reverse=True)
    return ranked


# ── Editorial memory: state.json survives between runs via the CI commit ───

def load_state():
    """Load and shape-validate the desk's memory. Salvage PER KEY: state.json
    is committed and reloaded forever, so a single corrupt section must never
    nuke the others — especially "history", the daily stat series (the asset).
    """
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    def _list(key):
        v = raw.get(key)
        return v if isinstance(v, list) else []

    clusters = [e for e in _list("clusters")
                if isinstance(e, dict) and isinstance(e.get("toks"), list)]
    history = [h for h in _list("history")
               if isinstance(h, dict) and isinstance(h.get("d"), str)]
    lead = raw.get("lead")
    if not (isinstance(lead, dict) and isinstance(lead.get("toks"), list)):
        lead = None
    dropped = (len(_list("clusters")) - len(clusters)) + (len(_list("history")) - len(history))
    if dropped:
        print(f"  [state] salvaged: dropped {dropped} malformed entries", file=sys.stderr)
    return {"clusters": clusters, "lead": lead, "history": history}


def parse_iso(s, fallback):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return fallback


def jaccard(a, b):
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def match_state(toks, entries):
    """Find the tracked cluster this headline continues, if any. Looser
    threshold than intra-run clustering: headlines drift between runs."""
    best, best_j = None, 0.4
    for e in entries:
        j = jaccard(toks, e["_toks"])
        if j > best_j:
            best, best_j = e, j
    return best


def apply_state(ranked, state, now):
    """The night editor: boost what's rising, decay what's been sitting,
    pull off what's gone stale. Returns (on_page, tracked) — tracked keeps
    retired clusters so they can't sneak back on as 'new' next run."""
    entries = state.get("clusters", [])
    for e in entries:
        e["_toks"] = frozenset(e.get("toks", []))

    on_page, tracked, n_rising, n_retired = [], [], 0, 0
    for item in ranked:
        prev = match_state(item["toks"], entries)
        if prev is None:
            item["tenure_h"] = 0.0
            item["rising"] = False
            item["fresh"] = item["age_hours"] <= FRESH_BADGE_H
            item["first_seen"] = now.isoformat()
            item["peak_outlets"] = item["cluster"]
        else:
            prev["_claimed"] = True
            first = parse_iso(prev.get("first_seen"), now)
            item["tenure_h"] = max(0.0, (now - first).total_seconds() / 3600)
            item["first_seen"] = prev.get("first_seen") or now.isoformat()
            peak = int(prev.get("peak_outlets", 1))
            item["rising"] = item["cluster"] > peak
            item["fresh"] = False
            item["peak_outlets"] = max(peak, item["cluster"])
            if item["rising"]:
                item["score"] += RISING_BONUS * (item["cluster"] - peak)
                n_rising += 1
        item["score"] -= max(0.0, item["tenure_h"] - TENURE_SOFT_H) * TENURE_PENALTY
        tracked.append(item)
        if item["tenure_h"] > TENURE_HARD_H and not item["rising"]:
            n_retired += 1
            continue  # pulled off: it had its run
        on_page.append(item)

    on_page.sort(key=lambda i: i["score"], reverse=True)
    if n_rising or n_retired:
        print(f"  [desk] {n_rising} rising, {n_retired} pulled off the page",
              file=sys.stderr)
    return on_page, tracked


def choose_lead(ranked, state, now):
    """Crown the lead. Rules: it must be confirmed by LEAD_MIN_OUTLETS
    outlets (or be scorching), and a lead that has stopped growing loses
    the siren after LEAD_FATIGUE_H hours to the best fresh challenger."""
    if not ranked:
        return ranked

    def eligible(i):
        return i["cluster"] >= LEAD_MIN_OUTLETS or i["score"] >= LEAD_SOLO_SCORE

    order = [i for i in ranked if eligible(i)] or ranked
    top = order[0]

    prev = state.get("lead")
    if prev:
        ptoks = frozenset(prev.get("toks", []))
        crowned_h = (now - parse_iso(prev.get("since"), now)).total_seconds() / 3600
        same_story = jaccard(top["toks"], ptoks) >= 0.4
        grown = top["score"] > float(prev.get("score", 0)) + 5
        if same_story and crowned_h > LEAD_FATIGUE_H and not top.get("rising") and not grown:
            for challenger in order[1:]:
                if (challenger["score"] >= LEAD_RIVAL_RATIO * top["score"]
                        and jaccard(challenger["toks"], ptoks) < 0.4):
                    print(f"  [desk] lead fatigued after {crowned_h:.1f}h — rotating",
                          file=sys.stderr)
                    top = challenger
                    break

    return [top] + [i for i in ranked if i is not top]


def wire_stats(ranked):
    """FRONT PAGE stats: rosy share of tone-committed top stories, and Trump
    share of the top stories. These are the dials' defaults and the page's
    self-published stat line. (The methodology note in the README defines
    this and the full-wire number side by side.)"""
    top = ranked[:PAGE_STORIES + 1]
    n_rosy = sum(1 for i in top if i["tone"] > 0)
    n_grim = sum(1 for i in top if i["tone"] < 0)
    natural = round(100 * n_rosy / (n_rosy + n_grim)) if (n_rosy + n_grim) else 50
    nat_dose = round(100 * sum(1 for i in top if i["trump"]) / len(top)) if top else 0
    return natural, nat_dose


def full_wire_dose(clusters):
    """FULL WIRE stat: Trump share of every unique story cluster fetched this
    run (post-dedup — pre-dedup would double-weight multi-outlet stories)."""
    if not clusters:
        return 0
    return round(100 * sum(1 for i in clusters if i["trump"]) / len(clusters))


def yesterday_dose(state, now):
    """Yesterday's published front-page Trump share — only from a history
    entry dated EXACTLY yesterday. After an outage gap the stat is omitted
    rather than mislabeling a week-old number 'YESTERDAY'."""
    want = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for h in reversed(state.get("history", [])):
        if h.get("d") == want:
            v = h.get("trump")
            return v if isinstance(v, (int, float)) else None
    return None


def save_state(state, tracked, lead, now, natural, nat_dose, fw_dose):
    """Persist the desk's memory. Carries over unclaimed recent clusters so
    a one-run feed hiccup doesn't reset a story's tenure. Also appends the
    day's wire stats to the (uncapped) daily history series."""
    entries = []
    for item in tracked[:400]:
        entries.append({
            "toks": sorted(item["toks"]),
            "first_seen": item.get("first_seen") or now.isoformat(),
            "last_seen": now.isoformat(),
            "peak_outlets": item.get("peak_outlets", item["cluster"]),
        })
    for e in state.get("clusters", []):
        if e.get("_claimed"):
            continue
        last = parse_iso(e.get("last_seen"), now)
        if (now - last).total_seconds() / 3600 <= STATE_PRUNE_H:
            entries.append({k: v for k, v in e.items() if not k.startswith("_")})

    lead_entry = None
    if lead is not None:
        lead_entry = {"toks": sorted(lead["toks"]), "since": now.isoformat(),
                      "score": round(lead["score"], 1)}
        prev = state.get("lead")
        if prev and jaccard(frozenset(prev.get("toks", [])), lead["toks"]) >= 0.4:
            # Same story keeps its original crowning time and score.
            lead_entry["since"] = prev.get("since", lead_entry["since"])
            lead_entry["score"] = prev.get("score", lead_entry["score"])

    today = now.strftime("%Y-%m-%d")
    history = [h for h in state.get("history", []) if h.get("d") != today]
    history.append({"d": today, "rosy": natural, "trump": nat_dose, "fw": fw_dose})
    # Uncapped on purpose (~15KB/yr): HISTORY_DAYS only windows the sparkline.

    payload = {"v": 2, "clusters": entries[:500], "lead": lead_entry,
               "history": history}
    # Atomic write: a crash mid-dump must never leave a truncated state.json
    # to be committed and reloaded forever.
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)


# ── Rendering ───────────────────────────────────────────────────────────────

def headline_case(title):
    return html.escape(title.upper())


def tone_tag(tone):
    if tone > 0:
        return ' &middot; <span class="rosy">ROSY</span>'
    if tone < 0:
        return ' &middot; <span class="grim">GRIM</span>'
    return ""


def partition(stories):
    """Group stories by desk (score order preserved within each), then
    bin-pack the desks onto three columns so the page stays balanced.
    Mirrored by the client-side mixer — keep the two in sync."""
    by_topic, names = {}, []
    for s in stories:
        t = s["topic"]
        if t not in by_topic:
            by_topic[t] = []
            names.append(t)
        by_topic[t].append(s)
    sections = sorted(by_topic.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    cols = [[], [], []]
    counts = [0, 0, 0]
    for name, items in sections:
        c = counts.index(min(counts))
        cols[c].append((name, items))
        counts[c] += len(items) + 2  # a header costs about two rows
    return cols


def sparkline_svg(series, color, width=220, height=28):
    """Inline SVG polyline for a 0-100 percentage series. Empty below two
    points — a one-entry polyline is a division by zero, not a chart."""
    if len(series) < 2:
        return ""
    step = width / (len(series) - 1)
    pts = " ".join(f"{i * step:.1f},{height - 2 - (height - 4) * v / 100:.1f}"
                   for i, v in enumerate(series))
    return (f'<svg width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" role="img" aria-label="daily history">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" '
            f'stroke-width="1.5"/></svg>')


def spark_html(history):
    """The stat-history block inside THE JUDGMENT box. HISTORY_DAYS is only
    the display window; the underlying series in state.json is uncapped."""
    trump_w = [h["trump"] for h in history
               if isinstance(h.get("trump"), (int, float))][-HISTORY_DAYS:]
    rosy_w = [h["rosy"] for h in history
              if isinstance(h.get("rosy"), (int, float))][-HISTORY_DAYS:]
    trump = sparkline_svg(trump_w, "#c00")
    rosy = sparkline_svg(rosy_w, "#070")
    if not trump and not rosy:
        return ('    <div class="spark"><div class="accruing">'
                'STAT HISTORY ACCRUING &middot; CHECK BACK TOMORROW</div></div>\n')
    out = ['    <div class="spark">']
    if trump:
        out.append(f'<div class="slabel">TRUMP SHARE OF THE FRONT PAGE, '
                   f'LAST {len(trump_w)} DAYS</div>{trump}')
    if rosy:
        out.append(f'<div class="slabel">ROSY SHARE, LAST {len(rosy_w)} DAYS</div>{rosy}')
    out.append('</div>\n')
    return "".join(out)


# Client-side beacons, injected only when GOATCOUNTER_CODE is set. Dials
# count on "change" (not "input" — a drag would fire dozens of beacons) and
# every call is guarded: an ad-blocked counter is a silent no-op.
GC_JS = """
    function gcount(path) {
      if (window.goatcounter && goatcounter.count) {
        goatcounter.count({path: path, event: true});
      }
    }
    document.addEventListener("click", function (e) {
      var a = e.target && e.target.closest ? e.target.closest("a[data-k]") : null;
      if (a) gcount("click/" + a.getAttribute("data-k"));
    });
    mix.addEventListener("change", function () { gcount("dial/mix/" + mix.value); });
    dose.addEventListener("change", function () { gcount("dial/dose/" + dose.value); });
"""


def render(ranked, sources_ok, now, natural, nat_dose, prev_dose, history):
    lead = ranked[0] if ranked else None
    rest = ranked[1:]

    def badge_bits(item):
        bits = []
        if item.get("rising"):
            bits.append('<span class="rise">RISING &#9650;</span>')
        elif item.get("fresh"):
            bits.append('<span class="fresh">NEW</span>')
        if item["cluster"] >= 3:
            bits.append(f'{item["cluster"]} OUTLETS')
        return bits

    def link_html(item, cls=""):
        klass = f' class="{cls}"' if cls else ""
        src = " &middot; ".join([html.escape(item["source"])] + badge_bits(item))
        return (
            f'<div class="story"><a{klass} href="{html.escape(item["link"])}" '
            f'data-k="{item.get("k", "")}" '
            f'target="_blank" rel="noopener">{headline_case(item["title"])}</a>'
            f'<span class="src">{src}{tone_tag(item["tone"])}</span></div>'
        )

    def section_html(name, items):
        rows = [f'<div class="schead">{html.escape(name)}</div>']
        for i, item in enumerate(items):
            cls = "hot" if item["score"] >= 25 else ""
            rows.append(link_html(item, cls))
            if (i + 1) % 6 == 0 and i + 1 < len(items):
                rows.append('<hr class="rule">')
        return '<div class="sec">' + "\n".join(rows) + "</div>"

    col_html = []
    for col in partition(rest[:PAGE_STORIES]):
        col_html.append("\n".join(section_html(name, items) for name, items in col))
    while len(col_html) < 3:
        col_html.append("")

    lead_html = ""
    if lead:
        siren = '<div class="siren">🚨</div>' if lead["score"] >= 30 else ""
        lead_bits = [html.escape(lead["source"])]
        if lead["cluster"] > 1:
            lead_bits.append(f'REPORTED BY {lead["cluster"]} OUTLETS')
        if lead.get("rising"):
            lead_bits.append('<span class="rise">RISING &#9650;</span>')
        lead_html = (
            f'{siren}<a class="lead" href="{html.escape(lead["link"])}" '
            f'data-k="{lead.get("k", "")}" '
            f'target="_blank" rel="noopener">{headline_case(lead["title"])}</a>'
            f'<div class="lead-src">{" &middot; ".join(lead_bits)}</div>'
        )

    yday = f" &middot; YESTERDAY {prev_dose}%" if prev_dose is not None else ""

    # Pool for the client-side mixer: top stories by rank, plus extra rosy
    # stories from further down so the sunshine end of the slider has
    # inventory (drama scoring naturally buries the gentle stuff).
    pool_items = list(ranked[:POOL_SIZE])
    rosy_extra = [i for i in ranked[POOL_SIZE:] if i["tone"] > 0][:PAGE_STORIES]
    pool_items = sorted(pool_items + rosy_extra, key=lambda i: i["score"], reverse=True)

    pool = [
        {
            "t": item["title"],
            "u": item["link"],
            "k": item.get("k", ""),
            "s": item["source"],
            "sc": round(item["score"], 1),
            "tn": item["tone"],
            "cl": item["cluster"],
            "tp": item["topic"],
            "tr": 1 if item["trump"] else 0,
            "rs": 1 if item.get("rising") else 0,
            "nw": 1 if item.get("fresh") else 0,
        }
        for item in pool_items
    ]
    pool_json = json.dumps(pool, ensure_ascii=False).replace("</", "<\\/")

    stamp = now.strftime("%A %B %d, %Y").upper() + now.strftime(" &middot; %H:%M UTC")
    src_line = " &middot; ".join(html.escape(s) for s in sources_ok)
    og_desc = html.escape(
        (lead["title"].upper() + " — " if lead else "")
        + "The only front page you can tune: dial the doom with THE JUDGMENT. "
          "Rebuilt from 25 wires every 30 minutes.", quote=True)

    gc_head = ""
    gc_js = ""
    disclosure = ""
    if GOATCOUNTER_CODE:
        gc_head = (f'<script data-goatcounter="https://{GOATCOUNTER_CODE}'
                   '.goatcounter.com/count" async src="//gc.zgo.at/count.js">'
                   '</script>\n')
        gc_js = GC_JS
        disclosure = (' &middot; ANONYMOUS, COOKIELESS USAGE COUNTS BY '
                      '<a href="https://www.goatcounter.com">GOATCOUNTER</a>')

    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        tpl = string.Template(f.read())
    return tpl.substitute(
        og_desc=og_desc,
        site_url=SITE_URL,
        stamp=stamp,
        year=now.year,
        natural=natural,
        grim_pct=100 - natural,
        nat_dose=nat_dose,
        yday=yday,
        yday_js=json.dumps(yday),
        built=int(now.timestamp()),
        lead_html=lead_html,
        col0=col_html[0],
        col1=col_html[1],
        col2=col_html[2],
        src_line=src_line,
        pool_json=pool_json,
        page_stories=PAGE_STORIES,
        catchall=TOPIC_CATCHALL,
        spark_html=spark_html(history),
        gc_head=gc_head,
        gc_js=gc_js,
        disclosure=disclosure,
    )


def write_feed(ranked, now, natural, nat_dose, fw_dose):
    """feed.xml: the day's stat line + top stories. Built with ElementTree so
    titles and query-string links are escaped correctly — hand-rolled XML is
    how feeds break. The stat item's guid is stable per day so 48 rebuilds
    don't spam subscribers with 48 "new" items."""
    rss = ET.Element("rss", version="2.0")
    rss.set("xmlns:atom", "http://www.w3.org/2005/Atom")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "THE GRUDGE REPORT"
    ET.SubElement(ch, "link").text = SITE_URL
    ET.SubElement(ch, "description").text = (
        "The only front page you can tune. Holding a grudge against slow news.")
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(now)
    self_link = ET.SubElement(ch, "atom:link")
    self_link.set("href", SITE_URL + "feed.xml")
    self_link.set("rel", "self")
    self_link.set("type", "application/rss+xml")

    day = now.strftime("%Y-%m-%d")
    stat = ET.SubElement(ch, "item")
    ET.SubElement(stat, "title").text = (
        f"TODAY'S WIRE: {100 - natural}% GRIM / {natural}% ROSY &#8212; "
        f"{nat_dose}% TRUMP ON THE FRONT PAGE, {fw_dose}% ON THE FULL WIRE"
    ).replace("&#8212;", "—")
    ET.SubElement(stat, "link").text = SITE_URL
    guid = ET.SubElement(stat, "guid", isPermaLink="false")
    guid.text = f"stat-{day}"
    ET.SubElement(stat, "pubDate").text = format_datetime(
        now.replace(hour=0, minute=0, second=0, microsecond=0))

    for item in ranked[:FEED_ITEMS]:
        it = ET.SubElement(ch, "item")
        ET.SubElement(it, "title").text = f"{item['title']} ({item['source']})"
        ET.SubElement(it, "link").text = item["link"]
        guid = ET.SubElement(it, "guid", isPermaLink="false")
        guid.text = item["link"]
        ET.SubElement(it, "pubDate").text = format_datetime(
            now - timedelta(hours=item["age_hours"]))

    ET.indent(rss)
    with open(FEED_FILE, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write(ET.tostring(rss, encoding="utf-8"))


def main():
    all_items = []
    sources_ok = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch, url): (source, url) for source, url in FEEDS}
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=FETCH_ALL_TIMEOUT):
                source, url = futures[fut]
                try:
                    items = parse_feed(source, fut.result())
                except Exception as e:
                    print(f"  [skip] {source}: {e}", file=sys.stderr)
                    continue
                if items:
                    sources_ok.append(source)
                    all_items.extend(items)
                    print(f"  [ok]   {source}: {len(items)} items", file=sys.stderr)
        except concurrent.futures.TimeoutError:
            n_left = sum(1 for f in futures if not f.done())
            print(f"  [skip] global {FETCH_ALL_TIMEOUT}s budget spent: "
                  f"{n_left} stragglers dropped", file=sys.stderr)
            pool.shutdown(wait=False, cancel_futures=True)

    if len(all_items) < MIN_ITEMS or len(sources_ok) < MIN_SOURCES:
        # A broad outage is not a crash: keep the last good page and exit
        # green (the CI commit step's diff guard no-ops). Nonzero exits are
        # reserved for real bugs so red runs stay meaningful. The source
        # floor stops one chatty feed from rebuilding a single-outlet page.
        reason = (f"only {len(all_items)} items from {len(sources_ok)} sources "
                  f"(need >={MIN_ITEMS} from >={MIN_SOURCES}) — keeping existing page")
        print(f"  [hold] {reason}", file=sys.stderr)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"Skipped rebuild: {reason}\n")
        return 0

    now = datetime.now(timezone.utc)
    state = load_state()
    ranked = dedupe_and_rank(all_items)
    fw_dose = full_wire_dose(ranked)
    on_page, tracked = apply_state(ranked, state, now)
    on_page = choose_lead(on_page, state, now)
    # Stats come AFTER lead selection: choose_lead can promote a below-fold
    # challenger into the top window, and the published number must match
    # the rendered page.
    natural, nat_dose = wire_stats(on_page)
    prev_dose = yesterday_dose(state, now)

    today = now.strftime("%Y-%m-%d")
    history = ([h for h in state.get("history", []) if h.get("d") != today]
               + [{"d": today, "rosy": natural, "trump": nat_dose, "fw": fw_dose}])

    sources_ok.sort()
    page = render(on_page, sources_ok, now, natural, nat_dose, prev_dose, history)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(page)
    write_feed(on_page, now, natural, nat_dose, fw_dose)
    save_state(state, tracked, on_page[0] if on_page else None, now,
               natural, nat_dose, fw_dose)

    n_rosy = sum(1 for i in on_page if i["tone"] > 0)
    n_grim = sum(1 for i in on_page if i["tone"] < 0)
    n_trump = sum(1 for i in on_page[:PAGE_STORIES + 1] if i["trump"])
    print(f"Wrote index.html + feed.xml: 1 lead + "
          f"{min(len(on_page) - 1, PAGE_STORIES)} stories "
          f"from {len(sources_ok)} sources "
          f"({len(on_page)} clusters: {n_grim} grim / {n_rosy} rosy; "
          f"{n_trump}/{min(len(on_page), PAGE_STORIES + 1)} front-page stories are "
          f"Trump; full wire {fw_dose}%).",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
