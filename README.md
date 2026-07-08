# THE GRUDGE REPORT 🚨

*The only front page you can argue with.*

Somewhere in a GitHub datacenter, every thirty minutes, a small Python script
wakes up, reads 25 newspapers, forms opinions about all of them, rearranges
the front page, files its memory in a JSON envelope, and goes back to sleep.

It has no server. It has no database. It has no dependencies. It has a grudge.

The result is a [Drudge Report](https://drudgereport.com)-style front page —
three columns, ALL CAPS, Courier, flashing siren — that assembles itself from
the world's news wires and hands **you** the editorial controls. (Not
affiliated with the Drudge Report. We hold our grudges independently.)

## ⚖ THE JUDGMENT

The news has a mood. Most days the mood is 😱. At the top of the page sits a
slider running from **😱 100% doom** to **😊 100% sunshine**. Drag it and the
entire front page re-mixes itself live — lead story, all three columns — to
serve exactly the ratio of catastrophe to kittens you can stomach today.

Two honesty rules are built in:

1. **The slider starts where the news actually is.** If the wire is 74% grim,
   the slider wakes up at 74% grim. You have to *choose* your delusion.
2. **The biggest stories can't be hidden.** Even at 100% sunshine, the war
   stays on the page. This is a mood dial, not a blindfold. (A fully rosy
   lead does trade the 🚨 for a 🌈, though. You've earned it.)

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
- A lead that stops growing loses the siren after **4 hours** to the
  hungriest challenger. The crown is rented, never owned.
- Stories the wire stops mentioning are quietly forgotten after 72 hours.
  They can't sneak back in wearing a NEW badge. The editor remembers.

No clicks are tracked, no visitors are counted, no cookies are set. The only
"popularity" signal is other newsrooms' behavior — 25 editorial staffs
unwittingly working the night desk for free.

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
is this system's whole threat model: 51 stdlib tests gate every PR (including
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
  that run (typically 500+).
- **ROSY / GRIM** — word-lexicon tone judgment (`GRIM_WORDS` / `ROSY_WORDS`).

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
The cron does the rest, forever. Recommended: require the PR test check under
Settings → Branches, so nothing untested ever reaches your presses.

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
generalizing the density dial beyond one politician, because grudges should
be renewable.

---

All headlines link to and belong to their original publishers. This page
merely holds them at arm's length and squints.
