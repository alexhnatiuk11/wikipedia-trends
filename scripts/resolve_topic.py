#!/usr/bin/env python3
"""Resolve a free-text topic to exact Wikipedia article titles per language.

Wikidata holds one language-independent "entity" (QID) per concept, with
`sitelinks` pointing to the exact article title in each language's Wikipedia
(if an article exists there at all). The Wikimedia pageviews API only accepts
an exact article title, so this script bridges "topic the user typed" ->
"QID" -> "exact article title per language".

Usage:
    python resolve_topic.py "intermittent fasting" --langs pl,cs,uk
    python resolve_topic.py "astronomy" --langs uk --search-language en

Output (stdout, JSON):
    {
      "status": "resolved" | "ambiguous" | "not_found",
      "query": "...",
      "qid": "Q..." | null,
      "candidates": [{"qid", "label", "description"}, ...],
      "articles": {"cs": "Přerušovaný_půst", "uk": "Інтервальне_голодування"},
      "missing_languages": ["pl"]
    }
"""
import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

WIKIDATA_API = "https://www.wikidata.org/w/api.php"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache"
CACHE_FILE = CACHE_DIR / "resolve_topic_cache.json"
USER_AGENT = "wikipedia-trends-skill/0.1 (https://github.com/; contact via repo)"



def _load_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def _cache_key(query: str, search_language: str) -> str:
    raw = f"{query.strip().lower()}|{search_language}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _get(url: str, params: dict, retries: int = 4) -> dict:
    """GET with retries. Wikidata enforces a tight anonymous rate limit
    (a short request burst empties fast and needs ~10s to refill), so on 429
    we back off much longer than for generic network errors, and honor
    Retry-After when the server sends one."""
    last_error = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url, params=params, headers={"User-Agent": USER_AGENT}, timeout=10
            )
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 5 * (attempt + 1)))
                last_error = "429 Too Many Requests"
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Request to {url} failed after {retries} attempts: {last_error}")


def search_entities(query: str, search_language: str, limit: int = 10) -> list:
    data = _get(
        WIKIDATA_API,
        {
            "action": "wbsearchentities",
            "search": query,
            "language": search_language,
            "type": "item",
            "limit": limit,
            "format": "json",
        },
    )
    results = []
    for item in data.get("search", []):
        match = item.get("match", {})
        results.append(
            {
                "qid": item["id"],
                "label": item.get("label", ""),
                "description": item.get("description", ""),
                "match_type": match.get("type", ""),
                "match_language": match.get("language", ""),
                "match_text": match.get("text", ""),
            }
        )
    return results


def strongest_match_pool(query: str, search_language: str, candidates: list) -> list:
    """Group candidates by textual match strength (direct label match in the
    query's own language > everything else) and return only the strongest
    tier. We deliberately do NOT try to guess relevance from `description`
    keywords (e.g. "journal", "song by") - that list can never be complete
    and silently misclassifies whatever it doesn't recognize (see: a Conan
    Gray song and an astronomy magazine both slipping through as "astronomy").
    Real disambiguation between same-tier candidates happens via language
    coverage in resolve(), not text heuristics.
    """
    query_lower = query.strip().lower()
    direct_label_matches = [
        c for c in candidates
        if c["match_type"] == "label"
        and c["match_language"] == search_language
        and c["match_text"].strip().lower() == query_lower
    ]
    return direct_label_matches if direct_label_matches else candidates


# Wikidata sitelinks include non-encyclopedia projects (Commons, Wiktionary,
# Wikidata itself, etc). When counting "how many language Wikipedias cover
# this concept" as a popularity/notability proxy, only count keys that look
# like an actual Wikipedia language edition (e.g. "plwiki", "cswiki").
NON_LANGUAGE_SITE_KEYS = {
    "commonswiki", "specieswiki", "wikidatawiki", "metawiki", "mediawikiwiki",
    "wikimaniawiki", "incubatorwiki", "outreachwiki", "sourceswiki",
    "wikifunctionswiki", "testwiki", "votewiki", "foundationwiki",
}


def count_language_sitelinks(qids: list) -> dict:
    """How many language-Wikipedia editions cover each QID. Used as a cheap
    notability proxy to catch cases where a technically-exact label match
    (e.g. "Eurovision" the TV network) is actually a much more obscure
    meaning than another candidate (e.g. "Eurovision Song Contest")."""
    if not qids:
        return {}
    data = _get(
        WIKIDATA_API,
        {
            "action": "wbgetentities",
            "ids": "|".join(qids),
            "props": "sitelinks",
            "format": "json",
        },
    )
    counts = {}
    for qid, entity in data.get("entities", {}).items():
        sitelinks = entity.get("sitelinks", {})
        counts[qid] = sum(
            1 for key in sitelinks
            if key.endswith("wiki") and key not in NON_LANGUAGE_SITE_KEYS
        )
    return counts


def check_dominant_coverage(qid: str, candidates: list, dominance_ratio: float = 3.0) -> dict:
    """Cross-check a tentatively-resolved QID's language coverage against the
    other top candidates. Returns {"dominant": bool, "counts": {...}} - if
    another candidate covers meaningfully more languages, the tentative match
    is probably the less common meaning of the term and should not be
    auto-resolved silently."""
    other_qids = [c["qid"] for c in candidates if c["qid"] != qid]
    if not other_qids:
        return {"dominant": True, "counts": {}}

    counts = count_language_sitelinks([qid] + other_qids)
    my_count = counts.get(qid, 0)
    best_other = max((counts.get(q, 0) for q in other_qids), default=0)

    # Not dominant only if some other candidate covers meaningfully more languages.
    dominant = best_other <= my_count * dominance_ratio
    return {"dominant": dominant, "counts": counts}


def get_sitelinks(qid: str, langs: list) -> dict:
    site_filter = "|".join(f"{lang}wiki" for lang in langs)
    data = _get(
        WIKIDATA_API,
        {
            "action": "wbgetentities",
            "ids": qid,
            "props": "sitelinks",
            "sitefilter": site_filter,
            "format": "json",
        },
    )
    entity = data.get("entities", {}).get(qid, {})
    sitelinks = entity.get("sitelinks", {})
    articles = {}
    for lang in langs:
        site_key = f"{lang}wiki"
        if site_key in sitelinks:
            articles[lang] = sitelinks[site_key]["title"].replace(" ", "_")
    return articles


def _public_candidates(candidates: list) -> list:
    return [{"qid": c["qid"], "label": c["label"], "description": c["description"]} for c in candidates]


def resolve(query: str, langs: list, search_language: str, use_cache: bool = True) -> dict:
    cache = _load_cache() if use_cache else {}
    key = _cache_key(query, search_language)
    cached = cache.get(key)

    if cached and cached.get("status") == "resolved":
        qid = cached["qid"]
        articles = get_sitelinks(qid, langs)
        missing = [l for l in langs if l not in articles]
        result = {
            "status": "resolved",
            "query": query,
            "qid": qid,
            "candidates": _public_candidates(cached.get("candidates", [])),
            "articles": articles,
            "missing_languages": missing,
            "cache_hit": True,
        }
        cached_at = cached.get("cached_at")
        if cached_at:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached_at)
            result["cache_age_days"] = round(age.total_seconds() / 86400, 1)
        return result

    candidates = search_entities(query, search_language)
    if not candidates:
        result = {"status": "not_found", "query": query, "qid": None, "candidates": [],
                   "articles": {}, "missing_languages": list(langs)}
        return result

    pool = strongest_match_pool(query, search_language, candidates)
    # The eventual winner (from the pool) must always be present in the list
    # we report/cross-check against, even if it wasn't among the raw top 5
    # (e.g. it matched by label but a bunch of unrelated aliases outranked it).
    reported_candidates = list({c["qid"]: c for c in (candidates[:5] + pool)}.values())
    pool_qids = [c["qid"] for c in pool]

    status = "ambiguous"
    qid = None
    note = None

    # A coverage "winner" only counts if it's overwhelmingly dominant (>=3x
    # the runner-up). Coverage systematically favors old, well-established
    # entities (a centuries-old town) over something brand new that a user
    # may well mean (a product launched this year has few sitelinks yet
    # simply because it's new, not because it's obscure) - e.g. "Sora" the
    # town (64 editions) vs "Sora" the OpenAI model (31 editions) is only a
    # 2x gap, not a real landslide, and should be surfaced, not auto-picked.
    POOL_DOMINANCE_RATIO = 3.0

    if len(pool) == 1:
        qid = pool[0]["qid"]
        status = "resolved"
    else:
        # Several candidates matched the query text equally well (e.g. three
        # different entities are all literally labeled "astronomy"). Use
        # language coverage to find the one that's clearly the real/primary
        # meaning, if any.
        counts = count_language_sitelinks(pool_qids)
        ranked = sorted(pool_qids, key=lambda q: counts.get(q, 0), reverse=True)
        top, runner_up = ranked[0], (counts.get(ranked[1], 0) if len(ranked) > 1 else 0)
        if counts.get(top, 0) > 0 and counts.get(top, 0) >= runner_up * POOL_DOMINANCE_RATIO:
            qid, status = top, "resolved"
        else:
            pool_summary = ", ".join(f"{c['label']} ({c['description']}) [{counts.get(c['qid'], 0)}]" for c in pool)
            note = (
                f"Multiple candidates matched '{query}' with equal text relevance and no candidate is "
                f"overwhelmingly more covered than the others ({pool_summary}). Ask the user which one they mean."
            )

    if status == "resolved" and len(reported_candidates) > 1:
        # Even a single unambiguous textual/coverage winner might still be a
        # narrower meaning than something else in the raw result list (e.g.
        # "Eurovision" the TV network vs "Eurovision Song Contest", which
        # wasn't in the same match-strength pool at all). Final safety net.
        coverage = check_dominant_coverage(qid, reported_candidates)
        if not coverage["dominant"]:
            counts = coverage["counts"]
            chosen_label = next(c["label"] for c in reported_candidates if c["qid"] == qid)
            better = max((c for c in reported_candidates if c["qid"] != qid), key=lambda c: counts.get(c["qid"], 0))
            note = (
                f"'{chosen_label}' ({counts.get(qid, 0)} language editions) matched the search term, "
                f"but '{better['label']}' ({counts.get(better['qid'], 0)} language editions) is far "
                f"more widely covered and may be what the user actually means. Ask the user to confirm."
            )
            status = "ambiguous"
            qid = None

    result = {
        "status": status,
        "query": query,
        "qid": qid,
        "candidates": _public_candidates(reported_candidates),
        "articles": {},
        "missing_languages": list(langs),
        "cache_hit": False,
    }
    if note:
        result["note"] = note

    if status == "resolved":
        articles = get_sitelinks(qid, langs)
        result["articles"] = articles
        result["missing_languages"] = [l for l in langs if l not in articles]
        cache[key] = {
            "status": status, "qid": qid, "candidates": reported_candidates,
            "cached_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_cache(cache)

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query", help="Topic to resolve, e.g. 'intermittent fasting'")
    parser.add_argument(
        "--langs", required=True,
        help="Comma-separated Wikipedia language codes, e.g. pl,cs,uk",
    )
    parser.add_argument(
        "--search-language", default="en",
        help="Language the query text is written in / to search Wikidata labels against (default: en)",
    )
    parser.add_argument("--no-cache", action="store_true", help="Bypass the local resolve cache")
    args = parser.parse_args()

    langs = [l.strip() for l in args.langs.split(",") if l.strip()]
    if not langs:
        print(json.dumps({"status": "error", "message": "--langs must contain at least one language code"}))
        sys.exit(1)

    try:
        result = resolve(args.query, langs, args.search_language, use_cache=not args.no_cache)
    except RuntimeError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
