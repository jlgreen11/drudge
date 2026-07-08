# THE DAILY MALAISE 🚨

*All the news that makes you feel vaguely unwell.*

Somewhere in a GitHub datacenter, every thirty minutes, a small Python script
wakes up, reads 25 newspapers, forms opinions about all of them, rearranges
the front page, files its memory in a JSON envelope, and goes back to sleep.

It has no server. It has no database. It has no dependencies. It is not
feeling its best.

The result is a [Drudge Report](https://drudgereport.com)-style front page —
three fixed desks-in-columns, ALL CAPS, condensed tabloid headlines over
wire-service mono — that assembles itself from
the world's news wires and hands **you** the editorial controls. (Not
affiliated with the Daily Mail, the Drudge Report, or your sense of
well-being.)

## ⚖ THE JUDGMENT

Every story on the wire gets a verdict: **ROSY or GRIM, no neutral.** The
rule is editorial policy, stated proudly: *a story is grim until it proves
otherwise.* The rosy lexicon grants pardons; everything else stands
convicted. (This is a doom aggregator. Innocence must be demonstrated.)

The slider at the top is an inclusion dial running from **😱 all doom** to
**😊 all sunshine**:

1. **Center shows everything** — 100% of grim and 100% of rosy, exactly as
   the wire ranked it, with today's measured cycle printed beside it.
2. **Drag right and grim stories are progressively removed** (the
   biggest-scoring ones survive longest); drag left and the rosy ones go.
   The extremes genuinely empty one side — and the readout always tells
   you exactly what fraction of each you're seeing. A mood dial with a
   truth-in-labeling sticker.

Your verdict persists in localStorage, so the page remembers how much doom
you can take.

## TRUMP DENSITY

A second dial. The page measures what share of the day's coverage mentions
the administration, prints the number in public — **"TODAY'S WIRE IS 38%
TRUMP"** — and lets you turn the knob from 0% to 100%. Yesterday's reading
sits next to it, and a pair of 30-day sparklines (density + doom) accumulates
inside the box, one day at a time, forever. The full daily series lives
uncapped in `state.json`, in the open, in version control.

## The night editor

Nobody runs this page. A tenure system does:

- A story that lands on the page starts a clock (`state.json` remembers it
  between runs — the editor's memory survives its own death every half hour).
- After **12 hours** on the page, a story starts bleeding score. Old news is
  a smell.
- At **30 hours** it gets pulled — *unless it's RISING*, meaning more outlets
  are picking it up than ever before. Momentum earns a stay of execution and
  a red ▲.
- A lead that stops growing loses the crown after **4 hours** to the
  hungriest challenger. The crown is rented, never owned.
- Stories the wire stops mentioning are quietly forgotten after 72 hours.
  They can't sneak back in wearing a NEW badge. The editor remembers.

No clicks are tracked, no visitors are counted, no cookies are set. The only
"popularity" signal is other newsrooms' behavior — 27 editorial staffs
unwittingly working the night desk for free.

## The desks

A fixed layout, like a paper that knows its own sections: WORLD and MONEY
down the left; SATIRICAL on top of SCIENCE & TECH and LIFESTYLE in the
middle (the jokes get the fold they deserve); US and WASHINGTON down the
right. Every desk is guaranteed its top three stories even on a loud news
day, and each desk's top story gets its thumbnail in newsprint grayscale —
one photo per section, the rest is type. The lead gets the big picture.

The SATIRICAL desk (The Onion, Babylon Bee) plays by containment rules:
satire always files to its own desk, can never take the lead — a fabricated
headline as the top story is misinformation, not comedy — and is excluded
from every published stat, so the density number never counts fake
headlines.

## The Sunday edition

The topbar has an **EDITION** switch. **TABLOID** (the default, and what
no-JS readers get) is the paper you're looking at: all-caps gothic,
newsprint-grayscale photos. **SUNDAY** resets the same page in serif
sentence case with calmer links — and the photography goes to color,
because the Sunday supplement got the color press first. Same DOM, same
dials, same stories: the editions are pure CSS, and the choice persists in
localStorage. For readers who love the paper but can only take it in
lowercase.

## No servers were harmed

```
25 RSS wires ──▶ build.py + template.html ──▶ index.html + feed.xml ──▶ GitHub Pages
                 (one stdlib Python file:         (a static page          (free, CDN'd,
                  fetch, cluster, score,           with the dials          nobody to
                  judge, remember, render)         baked in)               page at 3am)
                        ▲      │
                     state.json  ← the editor's memory, committed to git
```

The whole backend is a GitHub Actions cron job. Every 30 minutes it runs the
test suite, rebuilds the page, and commits `index.html`, `state.json`, and
`feed.xml` back to this repo — which means **the git history is the archive**:
every front page ever published, and the editor's complete state of mind at
every tick, is one `git show` away. The database is a JSON file. The backup
strategy is the publication mechanism. The hosting bill is $0.

It's engineered like it matters, because breaking at 3am with nobody watching
is this system's whole threat model: 66 stdlib tests gate every PR (including
one that boots the page's JavaScript in a DOM stub under node and drags both
dials through their historically fatal path), feeds are capped at 5MB/60s so
one tarpit can't wedge the presses, a broad wire outage holds the last good
page instead of panicking, state is shape-checked and salvaged key-by-key on
load, and if the page ever does go stale, readers get an honest **"WIRE
SILENT"** banner while the repo files exactly one `wire-down` issue at the
owner. No silent failures. The editor may be a cron job, but it has
professional standards.

## The stat, honestly

- **FRONT PAGE n%** — share of the top ~61 ranked stories matching the
  administration regex (`trump`, `maga`, `potus`, `white house`, `oval
  office`) — strictly speaking it measures *administration* coverage density.
- **FULL WIRE n%** — the same share across every unique story cluster fetched
  that run (typically 500+). Satire is excluded from both numbers.
- **ROSY / GRIM** — word-lexicon tone judgment (`GRIM_WORDS` / `ROSY_WORDS`),
  binary by editorial rule: a headline with a positive rosy-minus-grim score
  is ROSY; everything else — including headlines the lexicons don't reach —
  is GRIM by default. The rosy share is computed over ALL top stories.
  (Methodology note: before 2026-07-08 the stat excluded zero-signal
  headlines from the denominator; the daily series changed definition then.)

Both formulas are ~10 lines each (`wire_stats()`, `full_wire_dose()` in
`build.py`); the feed list is right at the top of the same file. Academics
(GDELT, Stanford's Cable TV News Analyzer) publish heavier-duty coverage
series; this is the only *consumer front page* that prints its own number and
then hands you the dial.

## Run your own

```sh
python3 -m unittest -v   # the test suite (node optional, for the JS tests)
python3 build.py         # writes index.html, feed.xml, state.json
open index.html
```

Fork it, then: **Settings → Pages → Deploy from a branch → `main` / root.**
The cron does the rest, forever.

One sharp edge to respect: **don't add classic branch protection with required
status checks to `main`** — the night editor pushes its own commits straight
to main every half hour, and required checks would bounce them (breaking the
presses in the name of protecting them). The test suite already gates every
pull request via the workflow's `pull_request` trigger. If you want a harder
gate, use a repo *ruleset* that requires the check on PRs with a bypass for
GitHub Actions — or wait for the roadmap's `deploy-pages` migration, after
which the bot stops committing entirely.

## Tune the outrage

Everything editorial is a constant at the top of `build.py`:

- `FEEDS` — add or remove wires.
- `HOT_WORDS` — the drama dictionary. Set `"ai": 3` to `50` and enjoy a very
  different newspaper.
- `GRIM_WORDS` / `ROSY_WORDS` — recalibrate THE JUDGMENT to your own despair.
- Siren at lead score ≥ 30; red links at ≥ 25.

Analytics are **off by default** (the page ships with zero tracking). To
count readers anonymously: free [GoatCounter](https://www.goatcounter.com)
account, set `GOATCOUNTER_CODE` in `build.py`. One script tag, no cookies,
disclosed in the footer.

## Someday (the roadmap)

Deploy without committing artifacts (`actions/deploy-pages`); a daily stat
bot for Bluesky/Mastodon; a custom domain; retiring the gloriously
period-accurate `<meta http-equiv="refresh">`; demand-side aging (stories
readers stop clicking age *faster* — never a click boost, no rich-get-richer);
generalizing the density dial beyond one politician, because malaise is
renewable.

---

All headlines link to and belong to their original publishers. This page
merely holds them at arm's length and squints.
