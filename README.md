# wikipedia-trends

Agent Skill (see `SKILL.md`) for analyzing Wikipedia pageview trends. This file is for the human maintaining the repo — the agent reads `SKILL.md` instead.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## Directory purposes

| directory | what's in it | persists? |
|---|---|---|
| `scripts/` | the skill's actual code | yes — source, keep |
| `references/` | API/threshold/limitations documentation for the agent | yes — source, keep |
| `cache/` | automatic API response cache, keyed by request params | yes, but **never expires on its own** |
| `data/` | archived intermediate JSON (pageviews + analysis) from past runs | yes, but grows unbounded |
| `reports/` | generated PDF reports + chart PNGs | yes, but grows unbounded |

`cache/`, `data/`, and `reports/` are all runtime output, not skill source — they're gitignored. None of the three are cleaned up automatically:

- `cache/` can go stale (the agent is instructed to check `cache_age_days` and ask before trusting old cached data, but the file itself is never deleted).
- `data/` and `reports/` accumulate one set of files per analysis/report ever run — nothing removes old ones.

**Periodically clean these out by hand** (`rm -rf cache data reports`, or prune selectively) if the directory is getting cluttered or you want to force fully fresh data. Safe to delete anytime — everything in them is reproducible by re-running the pipeline.
