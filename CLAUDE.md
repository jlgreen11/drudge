# THE DAILY MALAISE

Auto-populated Drudge-style news front page. One dependency-free Python script
(`build.py`, stdlib only) fetches 25 RSS wires, clusters/scores/judges headlines,
and renders a static `index.html` served by GitHub Pages. A GitHub Actions cron
rebuilds every 30 minutes. `state.json` is the editor's memory between runs.

Hard constraints: Python stdlib only at runtime (no pip deps), single generated
static page, no server. CI-only tooling (node on the runner) is allowed.

## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore
- Author a backlog-ready spec/issue → invoke /spec
