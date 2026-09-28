# Reference: APIs, thresholds, and known limitations

## APIs used

**Wikidata Action API** (`https://www.wikidata.org/w/api.php`)
- `action=wbsearchentities&search=<text>&language=<lang>&type=item&limit=10&format=json` — text/prefix match against labels and aliases in the given language only. Not semantic search: it will surface unrelated entities whose label merely contains the query text (a research paper, an album, a Hogwarts class), and it will completely miss an entity that has no label/alias in the given language, no matter how relevant it is.
- `action=wbgetentities&ids=Q1|Q2&props=sitelinks&sitefilter=langwiki|langwiki&format=json` — exact article titles per language for one or more QIDs in one call. `sitefilter` uses `<lang>wiki` keys (e.g. `plwiki`), which differ from the pageviews API's `<lang>.wikipedia` project naming — the scripts already handle this conversion.

**Wikimedia Pageviews REST API** (`https://wikimedia.org/api/rest_v1/metrics/pageviews/`)
- `per-article/{project}/{access}/{agent}/{article}/{granularity}/{start}/{end}` — the core per-article series. `agent=user` excludes bots/crawlers.
- `aggregate/{project}/{access}/{agent}/{granularity}/{start}/{end}` — whole-project traffic, used as the growth baseline.
- `top/{project}/{access}/{year}/{month}/{day}` — most-viewed articles for a project/period (`day=all-days` for a monthly ranking). Not currently wired into any script; potential future "what else is trending" feature.
- `top-by-country/{project}/{access}/{year}/{month}` and `top-per-country/{country}/{access}/{year}/{month}/{day}` — top countries for a whole project, and top articles for a country. Neither gives a per-article-per-country breakdown (that data does not exist in this API at all — confirmed against the full spec at `wikimedia.org/api/rest_v1/metrics/pageviews/api-spec.json`). Not used.
- `v3/per_editor/...` and `v3/top_pages_per_editor/...` — about editor activity, not reader interest. Not used.
- `/legacy/pagecounts/aggregate/...` — pre-2015 legacy format. Not used; superseded by `aggregate`.

**Wikimedia unique-devices API** (`https://wikimedia.org/api/rest_v1/metrics/unique-devices/{project}/{access-site}/{granularity}/{start}/{end}`), used by `device_profile.py`. `access-site` is `desktop-site` / `mobile-site` / `all-sites`. Whole-project only, no per-article breakdown, and no data for editions under ~1000 unique devices/period.

## Rate limits

Both Wikidata and the pageviews API enforce a tight anonymous rate limit — a small request burst empties fast and needs roughly 10+ seconds to refill; under heavy use the server-supplied `Retry-After` can be considerably longer. All scripts retry on 429 honoring `Retry-After` when present (up to 4 attempts). If you're issuing many requests in a loop (e.g. resolving 10+ languages), expect it to take noticeably longer than the request count alone would suggest — this is the script being polite, not a bug.

## Statistical thresholds (heuristics, not statistically calibrated)

These were picked by hand against ~20 manually-run test queries during development, not derived from a calibration study. Treat borderline results near these thresholds with extra skepticism.

| constant | value | meaning |
|---|---|---|
| `MIN_VIEWS_PER_DAY` | 15 | below this, `low_sample` flag fires → confidence capped at low |
| outlier z-threshold | 3.0 (on median/MAD, not mean/stddev) | flags a period as an anomaly, excluded from trend/growth calc |
| R² for a real trend | 0.1 (below → noisy/no real trend), 0.4 (below → medium confidence at best) | |
| `growth_pct` "close to baseline" band | ±5 percentage points | inside this band, article growth is treated as just riding the project's own trend, not topic-specific |
| pool dominance ratio (`resolve_topic.py`) | 3.0x | a coverage "winner" among equally-matched candidates must have ≥3x the sitelinks of the runner-up to auto-resolve; otherwise `ambiguous` |

## Known methodological limitations

- **Outlier detection is a global median/MAD test**, not trend-aware. On a series with a strong secular decline, the highest early points can get flagged as "outliers" even though they're just the natural start of the trend, not an anomaly. Conversely, a recurring seasonal spike (e.g. September back-to-school bump) is sometimes missed because it isn't extreme relative to the *whole* series. Don't present outlier flags as infallible. Also: 2+ outliers isn't necessarily 2+ unrelated one-off events — see the `outliers[]` guidance in `SKILL.md`.
- **Fixed (was a real bug, not just a theoretical risk):** requesting an `--end` date after today used to silently return a partial, still-accumulating month that looked calendar-complete, badly skewing `growth_pct`. `fetch_pageviews.py` now caps `--end` to today automatically (with a `note`), and `analyze_trends.py`'s partial-period check cross-references the real current date independently, not just the requested end date string — so this is now defended at two layers even if a caller (human or agent) still asks for a future date by mistake.
- **No normalization for community size beyond `share_of_project_pct`.** That field helps for one topic across languages, but absolute cross-language ranking of raw traffic still favors large wiki communities regardless of genuine relative interest.
- **Article existence and quality varies structurally by language**, not just randomly — a small wiki community's shorter/older article isn't just "noisier data," it's a systematically different measurement instrument. Low confidence there reflects "hard to trust this specific number," not "this language has less real-world interest."
- **No correction for multiple comparisons.** Ranking 10+ languages/topics and presenting the top pick risks treating a chance winner as meaningful.
- **The Wikidata concept graph doesn't always match how a person casually thinks about a topic.** Related-but-distinct concepts (e.g. "intermittent fasting" vs "therapeutic fasting") can exist as separate QIDs with only partial language coverage each — see the missing-language fallback section in `SKILL.md`.
- **Cache has no TTL.** `cache/` (under `resolve_topic_cache.json`, `pageviews/`, `unique-devices/`) never expires. A repeated query weeks later will silently return stale data unless `--no-cache` is passed.
- **No automated test suite yet.** All verification so far has been manual, ad hoc runs against real API data during development.

## Roadmap: developing this further for bigger/harder research

The current pipeline handles single comparisons well (a few topics/languages, a few years of monthly data). Scaling it to serious ongoing research would mean:

- **SQLite instead of flat JSON files in `cache/`** once the number of cached requests grows large enough that filesystem scans or duplicate storage become a real cost, and to support querying across past analyses instead of just exact-match lookups.
- **Cache TTL.** Right now cache never expires (see below) and freshness is handled entirely by informing the user and letting them opt into `--no-cache`. A real TTL (e.g. auto-refetch anything older than N days) would remove that manual step for routine use.
- **Trend-aware outlier/seasonality handling** (e.g. STL decomposition) instead of the current global median/MAD test, which — as documented below — can misfire on strongly-trending series and miss recurring seasonal spikes.
- **Calibrate the statistical thresholds** (`MIN_VIEWS_PER_DAY`, R² cutoffs, the dominance ratios in `resolve_topic.py`) against a real, larger set of known-good/known-bad cases instead of the current hand-picked values from ad hoc development testing.
- **Related-topic discovery via Wikidata's own graph** (e.g. `subclass of` / `part of` statements) to proactively suggest adjacent topics worth checking, building on the "top most-viewed pages" endpoint already scoped but not yet wired in (see APIs used, above).
- **Parallel, rate-limit-aware fetching** for large language/topic sets. Sequential fetching is fine up to roughly 15 languages (~40s observed); it would need to be parallelized with its own concurrency-aware backoff to stay practical much beyond that.
- **A real automated test suite** (there isn't one yet — see below) so future changes to the statistical/disambiguation logic can be checked against a fixed set of known cases instead of only manual, ad hoc verification.
- **Multi-page reports** as an option alongside the always-one-page PDF, for research that outgrows what fits on a single page. (The interactive-chart part of this idea is already done — every report ships a self-contained `.chart.html` alongside the PDF; a richer charting library or dashboard-style layout, e.g. Dash or fuller use of Plotly's features - linked multi-panel views, dropdown filters - is a separate, still-open idea, since today's chart is a fairly basic line-plot.)

## Cache layout

```
cache/
├── resolve_topic_cache.json     # topic+search-language -> QID (only on resolved status)
├── pageviews/<sha256>.json      # per (project, article|aggregate, access, agent, granularity, start, end)
└── unique-devices/<sha256>.json
```
