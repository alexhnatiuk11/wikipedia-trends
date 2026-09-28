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

## Possible future improvements

- **Missing-language fallback.** Right now, if a topic has no Wikipedia article in a requested language, the skill just says so plainly and excludes that language. An earlier version tried a fallback: translate the query into the missing language and re-search Wikidata for a related concept that might have an article there (this can genuinely find something — e.g. "intermittent fasting" has no Polish article, but a related "Głodówka lecznicza" / therapeutic fasting entity does, invisible to an English-language search since it has no English label). It was removed because the substitute is often a meaningfully different topic, not a confirmed equivalent, and risks quietly comparing two different things under one label. A better version of this idea would need a stronger way to confirm the substitute is actually equivalent (or at least comparable) before using it, rather than accepting any plausible-looking match - e.g. checking Wikidata's own semantic relations between the two QIDs, or requiring explicit user confirmation before including a substitute in a report.
