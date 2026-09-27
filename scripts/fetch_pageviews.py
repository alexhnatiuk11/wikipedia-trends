#!/usr/bin/env python3
"""Fetch daily/monthly pageview counts for one exact Wikipedia article.

Wraps the Wikimedia "per-article" pageviews endpoint. Results are cached to
disk so repeated or related follow-up requests (a wider date range, an
already-fetched language) don't re-hit the network.

Usage:
    python fetch_pageviews.py --lang cs --article "Přerušovaný_půst" \
        --start 20240101 --end 20241231

Output (stdout, JSON):
    {
      "project": "cs.wikipedia",
      "article": "Přerušovaný_půst",
      "granularity": "daily",
      "start": "20240101",
      "end": "20241231",
      "series": [{"date": "2024-01-01", "views": 41}, ...],
      "cache_hit": true
    }
"""
import argparse
import hashlib
import json
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import requests

PAGEVIEWS_API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article"
AGGREGATE_API = "https://wikimedia.org/api/rest_v1/metrics/pageviews/aggregate"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "pageviews"
USER_AGENT = "wikipedia-trends-skill/0.1 (https://github.com/; contact via repo)"


def _cache_path(*parts) -> Path:
    raw = "|".join(parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _get_with_retries(url: str, not_found_message: str, retries: int = 4) -> dict:
    """GET with retries. Wikimedia enforces a tight anonymous rate limit (a
    short request burst empties fast and needs ~10s to refill), so on 429 we
    back off much longer than for generic network errors, honoring
    Retry-After when the server sends one."""
    last_error = None
    for attempt in range(retries):
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if resp.status_code == 404:
            raise RuntimeError(not_found_message)
        if resp.status_code == 429:
            wait = float(resp.headers.get("Retry-After", 5 * (attempt + 1)))
            last_error = "429 Too Many Requests"
            time.sleep(wait)
            continue
        try:
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as exc:
            last_error = exc
            time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Request to {url} failed after {retries} attempts: {last_error}")


def _load_from_cache(cache_file: Path) -> dict:
    """Cache never expires on its own - always report how old it is (in
    days) so the caller can decide whether stale data is acceptable instead
    of silently serving it as if it were fresh."""
    raw = json.loads(cache_file.read_text())
    raw["cache_hit"] = True
    cached_at = raw.get("cached_at")
    if cached_at:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(cached_at)
        raw["cache_age_days"] = round(age.total_seconds() / 86400, 1)
    return raw


def _save_to_cache(cache_file: Path, result: dict) -> None:
    result["cached_at"] = datetime.now(timezone.utc).isoformat()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))


def _series_from_items(items: list) -> list:
    series = []
    for item in items:
        # timestamp format: YYYYMMDDHH -> normalize to YYYY-MM-DD
        ts = item["timestamp"]
        date = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        series.append({"date": date, "views": item["views"]})
    return series


def _cap_end_to_today(end: str) -> tuple:
    """A request for data through a future date (e.g. the nominal last day
    of the current, still-ongoing month) doesn't error - the API just
    returns whatever partial data exists so far, which silently looks like
    a complete period and can badly skew a trend/growth calculation. Cap the
    request itself instead of only relying on analyze_trends.py to notice
    downstream (observed: it's easy to ask for "this month" a few days
    before the month is actually over)."""
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    if end > today_str:
        return today_str, True
    return end, False


def fetch(lang: str, article: str, start: str, end: str, granularity: str = "daily",
          access: str = "all-access", agent: str = "user", use_cache: bool = True) -> dict:
    end, capped = _cap_end_to_today(end)
    project = f"{lang}.wikipedia"
    cache_file = _cache_path("per-article", project, article, access, agent, granularity, start, end)

    if use_cache and cache_file.exists():
        return _load_from_cache(cache_file)

    encoded_article = urllib.parse.quote(article, safe="")
    url = f"{PAGEVIEWS_API}/{project}/{access}/{agent}/{encoded_article}/{granularity}/{start}/{end}"
    not_found = (
        f"404 Not Found: no data for article '{article}' in project '{project}' "
        f"for this range. Check the exact title (case/underscores) and that it "
        f"isn't a redirect."
    )
    raw_response = _get_with_retries(url, not_found)

    result = {
        "project": project,
        "article": article,
        "access": access,
        "agent": agent,
        "granularity": granularity,
        "start": start,
        "end": end,
        "series": _series_from_items(raw_response.get("items", [])),
        "cache_hit": False,
        **({"note": f"Requested end date was in the future; capped to today ({end})."} if capped else {}),
    }

    if use_cache:
        _save_to_cache(cache_file, result)

    return result


def fetch_aggregate(lang: str, start: str, end: str, granularity: str = "daily",
                     access: str = "all-access", agent: str = "user", use_cache: bool = True) -> dict:
    """Whole-project traffic (no single article) for the same period, used as
    a baseline: if an article's growth is no better than the whole language
    edition's growth, it isn't really "rising interest in the topic" - the
    project is just getting more readers overall."""
    end, capped = _cap_end_to_today(end)
    project = f"{lang}.wikipedia"
    cache_file = _cache_path("aggregate", project, access, agent, granularity, start, end)

    if use_cache and cache_file.exists():
        return _load_from_cache(cache_file)

    url = f"{AGGREGATE_API}/{project}/{access}/{agent}/{granularity}/{start}/{end}"
    not_found = f"404 Not Found: no aggregate data for project '{project}' for this range."
    raw_response = _get_with_retries(url, not_found)

    result = {
        "project": project,
        "article": None,
        "access": access,
        "agent": agent,
        "granularity": granularity,
        "start": start,
        "end": end,
        "series": _series_from_items(raw_response.get("items", [])),
        "cache_hit": False,
        **({"note": f"Requested end date was in the future; capped to today ({end})."} if capped else {}),
    }

    if use_cache:
        _save_to_cache(cache_file, result)

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", required=True, help="Wikipedia language code, e.g. cs, pl, uk, en")
    parser.add_argument("--article", help="Exact article title, e.g. Přerušovaný_půst")
    parser.add_argument(
        "--aggregate", action="store_true",
        help="Fetch whole-project traffic instead of one article (baseline for relative growth); --article is ignored",
    )
    parser.add_argument("--start", required=True, help="Start date YYYYMMDD")
    parser.add_argument("--end", required=True, help="End date YYYYMMDD")
    parser.add_argument("--granularity", default="daily", choices=["daily", "monthly"])
    parser.add_argument("--access", default="all-access", choices=["all-access", "desktop", "mobile-web", "mobile-app"])
    parser.add_argument("--agent", default="user", choices=["user", "bot", "spider", "all-agents"])
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    if not args.aggregate and not args.article:
        print(json.dumps({"status": "error", "message": "--article is required unless --aggregate is set"}))
        sys.exit(1)

    try:
        if args.aggregate:
            result = fetch_aggregate(
                args.lang, args.start, args.end,
                granularity=args.granularity, access=args.access, agent=args.agent,
                use_cache=not args.no_cache,
            )
        else:
            result = fetch(
                args.lang, args.article, args.start, args.end,
                granularity=args.granularity, access=args.access, agent=args.agent,
                use_cache=not args.no_cache,
            )
    except RuntimeError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
