---
name: wikipedia-trends
description: Analyzes Wikipedia pageview trends to gauge interest in a topic across languages/countries, with statistically-grounded growth and confidence numbers, and generates a one-page PDF report with a chart. Use when the user asks whether interest in a topic is growing on Wikipedia, wants to compare interest across language editions of Wikipedia, is deciding which language/market to prioritize for a product, or asks for a PDF report on Wikipedia pageview trends.
compatibility: Requires Python 3.9+ with requests, numpy, matplotlib, reportlab installed (see requirements.txt), and network access to wikimedia.org and wikidata.org.
license: MIT-0
---

# Wikipedia Trends

Turns a free-text topic + one or more Wikipedia languages into a growth/confidence analysis and an optional one-page PDF report, backed by the Wikimedia Pageviews API and Wikidata.

All math (growth, trend, confidence, outlier detection) is done by the scripts, not by you. Your job is orchestration, translation when needed, and turning the JSON into a plain-language answer — never recompute or eyeball numbers yourself, and never guess at what the user meant when a script tells you it's unsure.

**Never write new Python (or any) code to do this analysis, and never edit the scripts in `scripts/`.** Always call the four existing scripts exactly as documented below via `bash`, for every query, including multi-language/multi-topic ones — loop over languages with plain bash (`for lang in ...; do ... done`), don't write a Python script to orchestrate it. This applies even if a request seems complex or the existing CLI feels inconvenient: the existing scripts already encode two days of hard-won statistical and data-quality fixes (partial-period handling, outlier detection, disambiguation, rate-limit backoff, the missing-language/tiering logic below) that a freshly-written script will not have and will silently get wrong. If a script's output genuinely doesn't have a field you need, work around it in your bash/answer, don't rewrite the underlying logic — resolving the gap belongs in a real conversation about extending the script, not a one-off reimplementation.

## Pipeline

Run these from the skill's root directory (where `scripts/` lives), using the venv created from `requirements.txt`. Each script prints JSON to stdout.

### Step 1 — resolve the topic to an exact article title per language

```bash
python scripts/resolve_topic.py "intermittent fasting" --langs pl,cs,uk --search-language en
```

`--search-language` is the language the query text itself is written in (default `en`), not the language you're looking for articles in.

Response `status` tells you what to do next:

- **`resolved`** — `articles` has an exact title per language. Still tell the user what topic/QID you resolved to before running the rest of the pipeline, so they can correct you if it's wrong — don't just silently proceed on a big multi-step analysis.
- **`ambiguous`** — `candidates` lists the plausible entities (with a `note` explaining why none was picked automatically, e.g. two unrelated meanings share the same word, or a coverage mismatch was detected). **Ask the user to pick one before continuing.** Never guess.
- **`not_found`** — no matching Wikidata entity. Tell the user plainly; suggest rephrasing.

`missing_languages` lists requested languages with no article for the resolved topic. **This is never the end of the step for that language — you must always attempt an alternative before moving on.** See **Missing language** below for the mandatory procedure (propose a translated/related concept, warn it may not be equivalent, ask before including it). Skipping straight to "no article exists for X" without at least attempting this is not acceptable, even under time/turn pressure.

### Step 2 — fetch the pageviews time series

```bash
python scripts/fetch_pageviews.py --lang pl --article "Sztuczna_inteligencja" \
    --start 20240926 --end 20260925 --granularity monthly
```

- Use the exact `articles[lang]` value from step 1 (underscores and all).
- Prefer `--granularity monthly` for periods over ~3 months; `daily` for shorter windows.
- Dates are `YYYYMMDD`. If you request an `--end` date after today, `fetch_pageviews.py` automatically caps it to today and says so in a `note` field — relay that to the user if present. `analyze_trends.py` then also drops incomplete calendar months (checking against today's real date, not just whether the requested end date looks calendar-complete). Both exist as a safety net; still try to pick a sensible `--end` (today or earlier) yourself rather than relying on it.
- **Cache never expires on its own.** Every script (`resolve_topic.py`, `fetch_pageviews.py`, `device_profile.py`) returns `cache_hit` and, when true, `cache_age_days`. Don't silently serve old cached data without saying so: if `cache_age_days` is more than a few days, tell the user data this old was used and **ask whether to refetch fresh (`--no-cache`) or keep the cached numbers** — don't decide for them. For a brand-new question (first time asking about this topic/language), a cache hit means someone already asked this recently; mention it in passing rather than making it the headline.
- Add `--aggregate` (drop `--article`) to fetch whole-project traffic instead of one article — normally you don't need this yourself, `analyze_trends.py` does it automatically (see step 3).

### Step 3 — analyze growth and confidence

```bash
python scripts/fetch_pageviews.py --lang pl --article "..." --start ... --end ... --granularity monthly \
    | python scripts/analyze_trends.py --language <user's language, e.g. uk, en, pl>
```

**Always pass `--language` matching the language the user is writing in** (an ISO code like `uk`, `en`, `pl` — whatever they're using, not necessarily the Wikipedia language being analyzed, which is unrelated). This controls the language of `reasons[]` and every fixed string in the eventual PDF (see step 4) — omitting it, or passing a mismatched one, produces a report mixing languages (this happened for real: Ukrainian boilerplate text next to an English title/recommendation). Only `uk` and `en` have full translations right now; anything else falls back to English, which is still internally consistent, just not the user's language — mention that limitation if it applies.

By default this **automatically** fetches the whole-project baseline for the same period and period-cleans/outlier-cleans the data — you don't need a separate step. Key output fields:

- `growth_pct` — headline growth number (edge-quartile comparison, not naive first-vs-last).
- `relative_growth_pct` — growth minus the whole project's growth over the same period. Large and positive means genuine topic-specific interest; large and negative means the topic is fading faster than the platform itself; near zero means the article is just riding the project's own traffic trend (which can include platform-wide effects like readers shifting to AI chat tools instead of Wikipedia).
- `share_of_project_pct` — topic's traffic as a share of its whole language edition. **Use this, not raw `avg_views_per_day`, when comparing across languages of very different community sizes** — a small wiki's low absolute traffic doesn't mean less relative interest.
- `confidence` (`high`/`medium`/`low`) + `reasons[]` — **always relay `confidence` and at least the key `reasons` to the user verbatim in substance, never round "low" up to "medium" or drop caveats to make an answer sound cleaner.**
- `outliers[]` — spikes/dips excluded from the trend (e.g. a news event) — mention them if the user asks why a specific month looks off, don't just hide them. **If `outliers[]` has 2+ entries, don't silently treat them as one-off noise in your answer.** Multiple elevated months isn't what a single random spike looks like — it can mean a real recent event/shift worth flagging on its own (a possible viral moment, a news cycle, a product launch), separate from whatever the background `growth_pct` says. Say so explicitly rather than only reporting the post-exclusion trend number (this happened for real: a topic with two large complete-month spikes had its background trend correctly reported as declining, but the spikes themselves deserved their own mention as a possible real signal, not just statistical noise to discard).

**`confidence` is statistical confidence in the trend, not business confidence that Wikipedia pageviews reflect real demand.** Always keep these two separate in your answer. Never let a `confidence: high` growth number stand alone as "so you should invest here" — pageviews measure curiosity/awareness, not intent to pay or learn. State this explicitly whenever you give a recommendation based on this data (see **Standing disclaimer** below).

Optional: `python scripts/device_profile.py --lang uk --start ... --end ...` gives desktop/mobile split for a whole language edition (not per-topic — see the script's own docstring). Only pull this in if the user's question is actually about device/platform strategy for a language's general readership; don't run it by default.

### Step 4 — build the PDF report

**This step is mandatory, always — run it for every analysis, even a single topic in a single language, even if the user never said the word "PDF", "report", or "chart".** "There's only one series" or "the user didn't ask for a PDF" are never valid reasons to skip it: the request to analyze a topic's trend is itself the trigger, full stop. This includes the case where some requested languages came back `not_found`/`missing_languages` and only one (or even zero comparison) language actually resolved — build the report and chart anyway for whatever did resolve; a one-curve chart is still the deliverable, not a fallback to skip. If you catch yourself reasoning "a text answer was probably enough here" or "there's nothing to compare so no chart is needed," that reasoning is wrong for this skill — generate the PDF and chart anyway.

**"Comparing" isn't just multiple languages of one topic — it's just as much multiple different topics in one language, or both at once.** Put every item being compared into **one report with one chart** via multiple `--data` entries, regardless of whether they're the same Wikidata concept in different languages or genuinely different topics — that's what lets the reader actually compare them side by side. Don't produce a separate report per item unless the user explicitly asks for separate files; a user asking "which of these 3 course topics should we launch first" wants one chart with three curves, not three standalone reports.

```bash
python scripts/make_report.py \
    --title "..." --question "<the user's original question>" \
    --data "pl:pl_pageviews.json:pl_analysis.json" \
    --data "cs:cs_pageviews.json:cs_analysis.json" \
    --missing "uk (стаття не знайдена)" \
    --recommendation "<your own judgment call - see below, required>" \
    --language <same code you passed to analyze_trends.py> \
    --output reports/<slug>.pdf
```

**`--language` here must match what you passed to `analyze_trends.py`** — this controls every fixed heading/sentence in the PDF and chart axis labels (the title/question/recommendation/labels you write yourself should already be in that language too, since you write those directly). Write `--title`, `--question`, `--recommendation`, and `--data` labels in the user's language regardless of what language the Wikipedia articles themselves are in.

- One `--data label:fetch_json_path:analysis_json_path` per language/topic being compared; save each script's JSON to a file first (e.g. via `> file.json`) since this script reads them by path, not stdin. Save these intermediate JSON files under `data/` in the skill directory (create it if missing), named so the user can tell what they are (e.g. `data/<topic-slug>_<lang>_pageviews.json`, `data/<topic-slug>_<lang>_analysis.json`) — this is a persistent archive of every analysis, not a scratch location, so don't put it in `/tmp`.
- **`--recommendation` is required and must be an actual opinion, not a restatement of the numbers.** Say what you'd prioritize and why, given the data. The script automatically computes and lists which entries are risky to lean on (low confidence, or a Tier 2 broad-proxy match) right under your text — you don't need to repeat those, just factor them into your judgment (e.g. don't recommend prioritizing an option you know is flagged low-confidence without saying why you'd still take that risk, if you would).
- `label` can (and for mismatched-concept cases below, should) include a short parenthetical, e.g. `"fr (Musculation — related but distinct concept)"`. **Never put the native-script article title itself in a label** (e.g. a Japanese/Korean/Chinese/Arabic/Thai title) — the report's fonts only cover Latin and Cyrillic, so anything else renders as blank boxes in both the chart legend and the PDF text. Use the language code plus a Latin/transliterated gloss instead (e.g. `"ja (TEFL-specific article)"`, not `"ja (英語学習 — ...)"`).
- Always pass `--missing` for any requested language that had no data, with a short reason — otherwise it silently vanishes from the report with no explanation to the reader.
- **If your `--data` label's parenthetical says anything like "proxy", "broad", "related concept", "not identical QID", or "different concept" for an entry, you must also pass that exact label via `--tier2`.** These two have to match — a label that already says "broad proxy" but isn't also listed in `--tier2` produces a self-contradicting report (the auto-generated methodology paragraph will falsely claim "clean, directly comparable" while the summary line right below it says otherwise). This is a real mistake that has happened before — double check the two lists agree before running the command.
- The PDF contains only text (methodology, summary, recommendation, risks, assumptions) — the chart is never embedded in it. Every run always produces **three files**: the PDF, a static `<output>.chart.png`, and an interactive `<output>.chart.html` (self-contained, works offline, click a legend entry to hide/show its curve, drag to zoom the time axis, hover for exact values). **Always mention and point to all three to the user**, not just the PDF — the HTML is usually the most useful one for actually exploring a multi-series comparison. Save reports under `reports/` in the skill directory (create it if missing) so the user can find them — never leave the only copy in `/tmp`.
- **One page is a soft target, not a hard limit.** With many entries (e.g. 15+ languages/topics), the font-scaling logic in `make_report.py` can only shrink text so far before it stops being readable — beyond that, `reportlab` simply flows the extra content onto a second (or third) page on its own; nothing in the script truncates or drops content to force one page. This is expected and fine — if you notice (or the script's own stderr/output says) the report likely ran long, just tell the user plainly that it spans multiple pages because of the number of items compared, don't treat that as a failure or a reason to skip generating it.
- **Never describe a report, chart, or numbers as generated/ready without having actually run the script and confirmed the output files exist** (e.g. `ls reports/`). Answering with report-shaped prose (headline totals, tiers, links to `reports/<name>.html`) without having run `make_report.py` and verified its output on disk is a fabricated result, not a shortcut — even under time pressure or when the pipeline feels slow with many languages, run it for real rather than composing what the output would probably look like. Only reference file paths that `ls`/the script's own printed output actually confirmed exist.
- **Report the numbers `analyze_trends.py`/`make_report.py` actually computed — don't invent your own summary statistics or ranking scheme on top of them.** A made-up "total pageviews across all languages," headline growth-% average, or a custom tiering scheme not grounded in the **Choosing a "promising" criterion** table above is not something these scripts produce; stick to the real fields (`growth_pct`, `relative_growth_pct`, `share_of_project_pct`, `avg_views_per_day`, `confidence`) per language/topic, and if you want to group results into tiers, base the grouping explicitly on one of the documented criteria, stated as such.
- The chart auto-switches to a log y-axis when compared series differ by >20x in scale, so small-language lines stay visible next to a big one.
- `data/` and `reports/` accumulate indefinitely (nothing auto-deletes old files — see `README.md`). If either has grown large or clearly contains stale/orphaned files (e.g. a `data/` entry with no matching report anymore), mention it to the user and offer to clean up rather than silently leaving clutter.

## Missing language — say so plainly, then offer (don't impose) an alternative

When `resolve_topic.py` returns a language in `missing_languages`, **tell the user explicitly that no Wikipedia article/topic was found for that language** and leave it out of the comparison for now (use `make_report.py --missing` as documented below). **Never silently substitute a different concept and pass it off as the same topic.**

But don't just stop there — **for every single language in `missing_languages`, before you write your final answer, you must actually run a second `resolve_topic.py` call with a translated query for that language and report what it found (or didn't).** This is a required action, not an optional suggestion to consider — "no article for pl" alone is an incomplete answer to this step even if everything else about the analysis is correct. Concretely, for each missing language:
1. Translate the topic query into that language yourself (using your own language knowledge).
2. Re-run `resolve_topic.py --search-language <lang>` with the translated text.
3. Report the outcome either way — a candidate found (see below) or genuinely nothing found (also see below) — you cannot skip straight from "missing" to the final answer without having done step 2.

If step 2 turns up a candidate, tell the user plainly: this is a related concept, not a confirmed equivalent, and mixing it into the comparison risks comparing two different things under one label — then **ask whether they want it included anyway**, and **warn what relying on it could mean** (e.g. skewed comparison, weaker signal) before they decide.

- **If the user says yes** — fetch it and include it in the comparison, but label it clearly as a substitute (e.g. `"pl (Głodówka lecznicza — related but distinct concept, not a direct match)"`) and pass that label to `make_report.py --tier2` so the report's methodology section explains it too.
- **If the user says no, or doesn't respond** (and you're not blocked from continuing without them) — leave that language out, exactly as `--missing` already documents, and move on. Don't invent a substitute and fetch it unasked.
- **If no plausible alternative concept exists at all** — say so plainly ("для цієї мови не знайдено ні самої теми, ні спорідненого поняття") and leave it at that; don't force a weak match just to have something to offer.

**The missing-language outcome must also land in the PDF itself, not just the chat reply.** Always pass `--missing` for the excluded language (per Step 4 below), and make the text you put there reflect what actually happened, not just "not found" — e.g. `--missing "uk (стаття не знайдена; знайдено споріднене поняття 'Х', користувач вирішив не включати)"` or `--missing "uk (стаття не знайдена; спорідненого поняття теж немає)"`. A reader of the PDF alone (who never saw the chat) should be able to tell the language was considered and why it's absent, not just that a language is silently missing from the chart.

If a language legitimately *does* have its own specialized article but it happens to be a broader/more general one than the other languages' articles (a normal Wikidata modeling quirk, not something you searched for) — that's a genuine **Tier 2** case, distinct from the above:

- **Tier 1 — specialized match**: the article's scope and intent genuinely match what's being compared.
- **Tier 2 — broad/generic proxy**: the available article is much broader than the topic being compared (e.g. the general "English language" article standing in for "learning English"). This is a materially weaker signal for two concrete reasons worth explaining to the user: (1) a broad article's content usually isn't about the specific activity/intent being measured, so its readership may want something else entirely; (2) broad "hub" articles get heavily inflated by incidental in-article links from unrelated pages, not people deliberately searching for that topic, so their traffic is structurally noisier than a narrowly, deliberately-searched article's traffic. Pass this entry to `make_report.py --tier2` so the report explains this in the reader's own methodology section, not just in your chat reply — and be cautious about including Tier 2 entries in any ranked recommendation (a Tier 2 language shouldn't win a "most promising" ranking against Tier 1 languages on the strength of noisy hub traffic).

**Never downgrade a language that already has a Tier 1 match just to make the comparison look uniform.** Resolve each language independently and let the tiers be mixed in the report (e.g. 1 Tier 1 + 4 Tier 2 is normal and fine, and is *more* informative than flattening everyone to Tier 2) — a report with mixed tiers, correctly labeled per language, is more useful than a falsely-uniform one that throws away a better data point you already had.

## Choosing a "promising" criterion for multi-language/topic comparisons

"Perspective/promising" means different things to different users, and ranking the same numbers by different criteria gives genuinely different top picks (verified: three criteria on the same 10-language dataset produced three different top-3 lists). If the user asks you to recommend a direction without specifying what matters to them, **ask which of these applies** rather than picking one silently:

| user's criterion | sort/filter on |
|---|---|
| fastest growth | `growth_pct` or `relative_growth_pct` |
| biggest market already | `avg_views_per_day` |
| reliable, not hype-driven | filter to `confidence: high` first, then by growth |
| fair comparison across differently-sized language communities | `share_of_project_pct` |
| an emerging niche, not just noise | small `avg_views_per_day` **and** positive `relative_growth_pct` |

When ranking many languages/topics at once (10+), mention that the single "winner" could partly be due to chance (multiple-comparisons effect) rather than presenting it as a definitive pick.

## Standing disclaimer

Include the substance of this in any answer that recommends a direction based on this data: Wikipedia pageviews reflect curiosity/awareness, not purchase intent or willingness to learn/pay — treat conclusions as a signal worth further validation, not proof of demand.

## Talking to the user

- **Every answer that compares 2+ languages/topics must end with an explicit recommendation, in the chat reply itself, not only in the PDF**: what you'd prioritize, which option you'd personally pick and why, and which options are risky to rely on and why. Don't just hand back the numbers and let the user infer a conclusion — commit to a stance grounded in the data (you can and should still hedge on confidence, but give an actual answer to "what would you do"). This is the same text you pass to `make_report.py --recommendation`, said in your own words in chat.
- **That recommendation must always call out exclusions and special cases explicitly** - outlier spikes, dropped partial periods, Tier 2 proxy entries, missing languages - not just the headline number. Don't make the reader reconcile "the chart looks like X but the text says Y" themselves (this happened for real: a topic's chart visually looked like growth because of two spike months, while the reported trend was decline once those spikes were excluded - the recommendation should have said this plainly instead of only listing the dates in a buried assumptions bullet).
- Never expose internal script/file names or implementation mechanics in your answer to the user (e.g. don't say "analyze_trends.py would catch this") — give the data and conclusion in plain language, as a human analyst would.
- Give a short, scannable answer by default: the numbers, the confidence caveat, and a direct answer to what was asked. Save exhaustive detail for when the user asks for it.
- If the user's question can't be answered with pageview data at all (e.g. asks about reader age/gender/income — this API has no demographic data), say so plainly rather than fabricating a plausible-sounding answer. `device_profile.py`'s desktop/mobile split is the closest honest proxy available, and only for a whole language edition, not a topic.
- When a report compares 3+ languages/topics, the combined `.chart.png` (all series on one static image) can get hard to read even with the legend placed outside the plot area. Point the user to the interactive `.chart.html` first — clicking a legend entry there already hides/shows individual curves, which solves most of this. **Only if they specifically want separate static images** (e.g. for pasting into a slide), offer to split into one PNG per series — one extra `make_report.py` call per series, reusing the JSON you already have. Don't do this by default.

## Follow-up / clarifying questions

Users will keep asking after seeing a result — "why the spike in November?", "what about just the last 6 months?", "add German too", "is that also true for the desktop audience?". Don't restart the whole pipeline from scratch and don't re-fetch anything blindly. For each follow-up:

1. **Work out what the question actually needs**, against what you already have in hand from this conversation (the `resolve_topic.py`/`analyze_trends.py` JSON you already produced):
   - Answerable directly from already-fetched data (e.g. "why the spike in November?" → look at `outliers[]` you already have, or the raw series you already fetched) → **no new script call at all**, just re-read what you have.
   - Needs the same topic/article but a different date range, granularity, or an added language → re-run only `fetch_pageviews.py` (+ `analyze_trends.py`) for the *new* part. `fetch_pageviews.py`/`resolve_topic.py` cache under `cache/`, so re-running with overlapping parameters is cheap even if you do it defensively.
   - Needs a different topic entirely (even if related) → treat it as a fresh `resolve_topic.py` call, don't assume it shares the prior QID.
   - Asks something pageview data structurally cannot answer (see **Talking to the user** above) → say so, don't fetch anything.
2. **Don't silently reuse stale context for a materially different question.** If the user's follow-up changes the topic, language set, or time window, re-derive rather than patching your previous prose answer.
3. If the follow-up implies a criterion change for a ranking you already gave (e.g. "no I meant which one is already big, not which is growing fastest") — re-sort the data you already have per **Choosing a "promising" criterion** above, no new fetch needed at all.
4. If the follow-up is specifically about "is this still true today", treat it as an explicit request for fresh data — use `--no-cache` rather than asking, since the user already told you they want current numbers (see the cache freshness rule in Step 2).

## Further reading

See `references/REFERENCE.md` for: exact Wikimedia/Wikidata endpoints used, rate-limit behavior, the statistical thresholds each script uses and why, known methodological limitations to keep in mind before trusting a borderline result, and the roadmap for scaling this to bigger/harder research.
