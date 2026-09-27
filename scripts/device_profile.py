#!/usr/bin/env python3
"""Desktop vs mobile audience profile for a whole language Wikipedia edition.

IMPORTANT LIMITATION: the Wikimedia unique-devices API has no per-article
breakdown - this is NOT "who reads article X on mobile vs desktop", it's
"what does the mobile/desktop split look like for this language edition as
a whole". Use it only as background context about a language's overall
readership (e.g. "uk.wikipedia's audience is mostly mobile"), never as a
device breakdown for a specific topic - that data does not exist.

Wikimedia also only publishes this data for editions with >=1000 unique
devices/period; smaller editions return no usable data.

Usage:
    python device_profile.py --lang uk --start 20250101 --end 20250601 --granularity monthly

Output (stdout, JSON):
    {
      "project": "uk.wikipedia",
      "periods": [{"date": "2025-01-01", "desktop": 6513590, "mobile": 15691017, "mobile_share_pct": 70.7}, ...],
      "avg_mobile_share_pct": 68.4
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

UNIQUE_DEVICES_API = "https://wikimedia.org/api/rest_v1/metrics/unique-devices"
CACHE_DIR = Path(__file__).resolve().parent.parent / "cache" / "unique-devices"
USER_AGENT = "wikipedia-trends-skill/0.1 (https://github.com/; contact via repo)"


def _cache_path(*parts) -> Path:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{digest}.json"


def _get_with_retries(url: str, retries: int = 4) -> dict:
    """Same tight-rate-limit handling as fetch_pageviews.py: Wikimedia's
    anonymous rate limit empties fast and needs ~10s to refill on a 429."""
    last_error = None
    for attempt in range(retries):
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
        if resp.status_code == 404:
            raise RuntimeError(
                "404 Not Found: no unique-devices data for this project/range "
                "(editions with <1000 unique devices/period return nothing usable)."
            )
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


def _fetch_access_site(project: str, access_site: str, granularity: str, start: str, end: str) -> dict:
    """timestamp -> devices count, for one access-site (desktop-site or mobile-site)."""
    url = f"{UNIQUE_DEVICES_API}/{project}/{access_site}/{granularity}/{start}/{end}"
    raw = _get_with_retries(url)
    return {item["timestamp"][:8]: item["devices"] for item in raw.get("items", [])}


def device_profile(lang: str, start: str, end: str, granularity: str = "monthly", use_cache: bool = True) -> dict:
    project = f"{lang}.wikipedia"
    cache_file = _cache_path(project, granularity, start, end)

    if use_cache and cache_file.exists():
        raw = json.loads(cache_file.read_text())
        raw["cache_hit"] = True
        cached_at = raw.get("cached_at")
        if cached_at:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(cached_at)
            raw["cache_age_days"] = round(age.total_seconds() / 86400, 1)
        return raw

    desktop = _fetch_access_site(project, "desktop-site", granularity, start, end)
    mobile = _fetch_access_site(project, "mobile-site", granularity, start, end)

    periods = []
    for ts in sorted(set(desktop) | set(mobile)):
        d, m = desktop.get(ts, 0), mobile.get(ts, 0)
        date = f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}"
        total = d + m
        mobile_share = round(m / total * 100, 1) if total else None
        periods.append({"date": date, "desktop": d, "mobile": m, "mobile_share_pct": mobile_share})

    shares = [p["mobile_share_pct"] for p in periods if p["mobile_share_pct"] is not None]
    avg_mobile_share = round(sum(shares) / len(shares), 1) if shares else None

    result = {
        "project": project,
        "periods": periods,
        "avg_mobile_share_pct": avg_mobile_share,
        "note": (
            "This is the mobile/desktop split for the whole language edition, not for any specific "
            "article or topic - use only as background context about the language's general readership."
        ),
        "cache_hit": False,
    }

    if use_cache:
        result["cached_at"] = datetime.now(timezone.utc).isoformat()
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=2))

    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lang", required=True, help="Wikipedia language code, e.g. uk, pl, en")
    parser.add_argument("--start", required=True, help="Start date YYYYMMDD")
    parser.add_argument("--end", required=True, help="End date YYYYMMDD")
    parser.add_argument("--granularity", default="monthly", choices=["daily", "monthly"])
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    try:
        result = device_profile(args.lang, args.start, args.end, args.granularity, use_cache=not args.no_cache)
    except RuntimeError as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
