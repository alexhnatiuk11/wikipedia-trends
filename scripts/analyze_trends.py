#!/usr/bin/env python3
"""Turn a raw pageviews time series into a trend + trust assessment.

Reads the JSON produced by fetch_pageviews.py (stdin or --input file) and
computes growth, a linear trend, outlier detection, and a plain-language
confidence rating. All numbers a cheap model would otherwise have to
eyeball (and likely get wrong - see: partial months, single-spike growth
illusions) are computed here so the agent only has to relay them.

Usage:
    python fetch_pageviews.py --lang uk --article Астрономія ... | python analyze_trends.py
    python analyze_trends.py --input pageviews.json

Output (stdout, JSON): see README section "analyze_trends.py contract".
"""
import argparse
import calendar
import json
import sys
from datetime import date
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_pageviews import fetch_aggregate  # noqa: E402

MIN_VIEWS_PER_DAY = 15  # below this, a "trend" is mostly noise
OUTLIER_Z_THRESHOLD = 3.0
DAYS_IN_GRANULARITY = {"daily": 1, "monthly": 30.44}  # avg month length

# `reasons[]` ends up embedded verbatim in the PDF (make_report.py), so if
# this script always wrote Ukrainian regardless of the report's language,
# every non-Ukrainian report would still have Ukrainian sentences mixed in
# no matter what make_report.py's own strings were translated to. Falls back
# to English for any language not listed here.
STRINGS = {
    "uk": {
        "insufficient_periods": "Недостатньо періодів даних (після відкидання неповних) для будь-якого аналізу тренду.",
        "low_sample": "Середній трафік ~{avg:.0f} переглядів/день — замало для статистично надійного висновку.",
        "noisy": "Дані надто шумні: лінійний тренд практично не пояснює коливання (низький R²).",
        "few_periods": "Мало періодів даних для аналізу ({n}) — оцінка тренду попередня.",
        "outliers_found": "Виявлено та виключено аномальні сплески/провали: {dates} (ймовірно разові події, не тренд).",
        "dropped_partial": "Відкинуто неповні періоди на межах діапазону: {periods}.",
        "article_ref": "стаття «{article}» ({project})",
        "riding_baseline": "Зміна {article_label} ({growth:.1f}%) близька до зміни всього {project} загалом ({baseline:.1f}%) — це схоже на загальний тренд трафіку проєкту, а не специфічний інтерес саме до теми.",
        "growing_faster": "{article_label} зростає помітно швидше за проєкт загалом ({growth:.1f}% проти {baseline:.1f}% у {project}) — це специфічне зростання інтересу до теми, а не просто загальний тренд трафіку.",
        "falling_faster": "{article_label} падає помітно швидше, ніж весь {project} загалом ({growth:.1f}% проти {baseline:.1f}%) — це специфічне падіння інтересу саме до теми, а не просто загальний тренд трафіку проєкту.",
        "clean_trend": "Дані охоплюють достатньо періодів, стабільний трафік, чіткий лінійний тренд без аномалій.",
    },
    "en": {
        "insufficient_periods": "Not enough data periods (after dropping incomplete ones) for any trend analysis.",
        "low_sample": "Average traffic ~{avg:.0f} views/day - too little for a statistically reliable conclusion.",
        "noisy": "Data is too noisy: the linear trend barely explains the variation (low R²).",
        "few_periods": "Few data periods available ({n}) - trend estimate is preliminary.",
        "outliers_found": "Anomalous spikes/dips detected and excluded: {dates} (likely one-off events, not trend).",
        "dropped_partial": "Dropped incomplete periods at the edges of the range: {periods}.",
        "article_ref": "article \"{article}\" ({project})",
        "riding_baseline": "The change for {article_label} ({growth:.1f}%) is close to the change for all of {project} ({baseline:.1f}%) - this looks like the project's general traffic trend, not specific interest in the topic.",
        "growing_faster": "{article_label} is growing noticeably faster than the project overall ({growth:.1f}% vs {baseline:.1f}% for {project}) - this is topic-specific growing interest, not just a general traffic trend.",
        "falling_faster": "{article_label} is falling noticeably faster than all of {project} ({growth:.1f}% vs {baseline:.1f}%) - this is topic-specific declining interest, not just the project's general traffic trend.",
        "clean_trend": "Data covers enough periods, traffic is stable, a clear linear trend with no anomalies.",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    table = STRINGS.get(lang, STRINGS["en"])
    template = table.get(key, STRINGS["en"][key])
    return template.format(**kwargs) if kwargs else template


def _parse_date(d: str) -> date:
    y, m, day = d.split("-")
    return date(int(y), int(m), int(day))


def drop_partial_periods(series: list, granularity: str, start: str, end: str) -> tuple:
    """Wikimedia buckets by calendar month/day; a range like 20250926-20260925
    produces a September bucket with only 5 real days in it, which silently
    wrecks any first-vs-last comparison. Drop buckets that don't fully cover
    a calendar period implied by the request's start/end."""
    if granularity != "monthly" or not series:
        return series, []

    start_date = _parse_date(f"{start[0:4]}-{start[4:6]}-{start[6:8]}")
    end_date = _parse_date(f"{end[0:4]}-{end[4:6]}-{end[6:8]}")

    dropped = []
    cleaned = list(series)

    if cleaned and start_date.day != 1:
        dropped.append(cleaned[0]["date"][:7])
        cleaned = cleaned[1:]

    if cleaned:
        last_date = _parse_date(cleaned[-1]["date"])
        last_day_of_month = calendar.monthrange(last_date.year, last_date.month)[1]
        # Checking against the *requested* end date alone isn't enough: if the
        # caller asked for a range running past today (e.g. the last day of
        # the current, still-ongoing month), the request looks calendar-
        # complete even though the real world hasn't reached that date yet -
        # this actually happened (a request for "this month" through its
        # nominal last day, submitted a few days before the month was over).
        # Cross-check against the real current date too, not just the string
        # the caller happened to pass.
        today = date.today()
        month_has_really_ended = (last_date.year, last_date.month) < (today.year, today.month)
        if end_date.day != last_day_of_month or not month_has_really_ended:
            dropped.append(cleaned[-1]["date"][:7])
            cleaned = cleaned[:-1]

    return cleaned, dropped


def detect_outliers(views: list) -> list:
    """Median + MAD (median absolute deviation) is robust to the very
    outliers we're trying to detect, unlike mean/stddev which the outlier
    itself would distort."""
    if len(views) < 4:
        return []
    arr = np.array(views, dtype=float)
    median = np.median(arr)
    mad = np.median(np.abs(arr - median))
    if mad == 0:
        return []
    # 0.6745 makes MAD comparable to a standard deviation for normal data.
    robust_z = 0.6745 * (arr - median) / mad
    return [i for i, z in enumerate(robust_z) if abs(z) > OUTLIER_Z_THRESHOLD]


def linear_trend(x: list, y: list) -> dict:
    if len(x) < 2:
        return {"slope": 0.0, "r_squared": 0.0}
    x_arr, y_arr = np.array(x, dtype=float), np.array(y, dtype=float)
    slope, intercept = np.polyfit(x_arr, y_arr, 1)
    predicted = slope * x_arr + intercept
    ss_res = np.sum((y_arr - predicted) ** 2)
    ss_tot = np.sum((y_arr - np.mean(y_arr)) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return {"slope": float(slope), "r_squared": float(r_squared)}


def edge_average_growth(views: list) -> float:
    """Compare the average of the first ~quarter of periods to the average of
    the last ~quarter, instead of raw first-vs-last, so a single noisy month
    at either edge doesn't dominate the whole growth figure."""
    n = len(views)
    if n < 2:
        return 0.0
    chunk = max(1, n // 4)
    start_avg = sum(views[:chunk]) / chunk
    end_avg = sum(views[-chunk:]) / chunk
    if start_avg == 0:
        return float("inf") if end_avg > 0 else 0.0
    return (end_avg - start_avg) / start_avg * 100


def _baseline_summary(baseline_data: dict) -> dict:
    """Same cleaning pipeline (drop partial periods, drop outliers) applied to
    a whole-project series, reduced to the growth (for the relative-growth
    comparison) and average traffic (for the cross-language share metric)."""
    granularity = baseline_data["granularity"]
    cleaned, _ = drop_partial_periods(baseline_data["series"], granularity, baseline_data["start"], baseline_data["end"])
    views = [p["views"] for p in cleaned]
    if len(views) < 2:
        return None
    outlier_idx = set(detect_outliers(views))
    clean_y = [v for i, v in enumerate(views) if i not in outlier_idx]
    day_length = DAYS_IN_GRANULARITY[granularity]
    return {
        "growth_pct": edge_average_growth(clean_y),
        "avg_views_per_day": (sum(clean_y) / len(clean_y)) / day_length if clean_y else 0.0,
    }


def analyze(data: dict, baseline_data: dict = None, lang: str = "en") -> dict:
    granularity = data["granularity"]
    series = data["series"]

    cleaned, dropped_periods = drop_partial_periods(series, granularity, data["start"], data["end"])

    if len(cleaned) < 2:
        return {
            "project": data["project"],
            "article": data["article"],
            "total_periods": len(cleaned),
            "dropped_partial_periods": dropped_periods,
            "confidence": "low",
            "reasons": [t(lang, "insufficient_periods")],
        }

    views = [p["views"] for p in cleaned]
    outlier_idx = set(detect_outliers(views))
    outlier_details = [
        {"date": cleaned[i]["date"], "views": views[i]}
        for i in sorted(outlier_idx)
    ]

    clean_x = [i for i in range(len(views)) if i not in outlier_idx]
    clean_y = [views[i] for i in clean_x]

    trend = linear_trend(clean_x, clean_y)
    growth_pct = edge_average_growth(clean_y)

    day_length = DAYS_IN_GRANULARITY[granularity]
    avg_views_per_day = (sum(clean_y) / len(clean_y)) / day_length if clean_y else 0.0

    if trend["slope"] > 0 and trend["r_squared"] > 0.1:
        direction = "increasing"
    elif trend["slope"] < 0 and trend["r_squared"] > 0.1:
        direction = "decreasing"
    else:
        direction = "flat_or_noisy"

    reasons = []
    low_sample = avg_views_per_day < MIN_VIEWS_PER_DAY
    if low_sample:
        reasons.append(t(lang, "low_sample", avg=avg_views_per_day))
    if trend["r_squared"] < 0.1:
        reasons.append(t(lang, "noisy"))
    if len(clean_y) < 6:
        reasons.append(t(lang, "few_periods", n=len(clean_y)))
    if outlier_details:
        dates = ", ".join(o["date"] for o in outlier_details)
        reasons.append(t(lang, "outliers_found", dates=dates))
    if dropped_periods:
        reasons.append(t(lang, "dropped_partial", periods=", ".join(dropped_periods)))

    relative_growth_pct = None
    share_of_project_pct = None
    baseline_summary = _baseline_summary(baseline_data) if baseline_data is not None else None

    if baseline_summary is not None and baseline_summary["avg_views_per_day"] > 0:
        # Normalized "penetration" of the topic within its own language
        # edition - lets you compare topic interest across languages with
        # wildly different total audience sizes (a small wiki community's
        # low absolute traffic isn't automatically "less interest").
        share_of_project_pct = round(avg_views_per_day / baseline_summary["avg_views_per_day"] * 100, 4)

    if baseline_summary is not None and growth_pct not in (None, float("inf")):
        baseline_growth = baseline_summary["growth_pct"]
        if baseline_growth is not None and baseline_growth != float("inf"):
            relative_growth_pct = round(growth_pct - baseline_growth, 1)
            project = data["project"]
            # A small gap just means the article is tracking the project's own
            # traffic trend - not a topic-specific signal either way. A large
            # gap (either direction) means the topic itself is gaining or
            # losing interest faster than Wikipedia readership as a whole.
            article_label = t(lang, "article_ref", article=data["article"], project=project)
            if abs(relative_growth_pct) <= 5:
                reasons.append(t(lang, "riding_baseline", article_label=article_label,
                                  growth=growth_pct, project=project, baseline=baseline_growth))
            elif relative_growth_pct > 0:
                reasons.append(t(lang, "growing_faster", article_label=article_label,
                                  growth=growth_pct, project=project, baseline=baseline_growth))
            else:
                reasons.append(t(lang, "falling_faster", article_label=article_label,
                                  growth=growth_pct, project=project, baseline=baseline_growth))

    if low_sample or trend["r_squared"] < 0.1:
        confidence = "low"
    elif len(clean_y) < 6 or trend["r_squared"] < 0.4:
        confidence = "medium"
    else:
        confidence = "high"

    if not reasons:
        reasons.append(t(lang, "clean_trend"))

    return {
        "project": data["project"],
        "article": data["article"],
        "total_periods": len(cleaned),
        "periods_used_for_trend": len(clean_y),
        "dropped_partial_periods": dropped_periods,
        "growth_pct": round(growth_pct, 1) if growth_pct != float("inf") else None,
        "relative_growth_pct": relative_growth_pct,
        "trend": direction,
        "r_squared": round(trend["r_squared"], 3),
        "avg_views_per_day": round(avg_views_per_day, 1),
        "share_of_project_pct": share_of_project_pct,
        "outliers": outlier_details,
        "confidence": confidence,
        "reasons": reasons,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", help="Path to fetch_pageviews.py JSON output. Defaults to stdin.")
    parser.add_argument(
        "--baseline",
        help="Path to fetch_pageviews.py --aggregate JSON output for the same project/period. "
             "By default this is fetched automatically - only pass this to reuse an already-fetched file.",
    )
    parser.add_argument(
        "--no-baseline", action="store_true",
        help="Skip the automatic whole-project baseline comparison (growth vs project rides on faith instead).",
    )
    parser.add_argument(
        "--language", default="en",
        help="Language for the human-readable 'reasons' text (not a Wikipedia project code) - match the "
             "user's language so the eventual report doesn't mix languages. Defaults to English; falls "
             "back to English for any language without a translation table.",
    )
    args = parser.parse_args()

    raw = open(args.input).read() if args.input else sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"status": "error", "message": f"Invalid JSON input: {exc}"}))
        sys.exit(1)

    if "series" not in data:
        print(json.dumps({"status": "error", "message": "Input JSON has no 'series' field - is this fetch_pageviews.py output?"}))
        sys.exit(1)

    baseline_data = None
    if args.baseline:
        baseline_data = json.loads(open(args.baseline).read())
    elif not args.no_baseline and data.get("article"):
        # Auto-fetch the whole-project baseline for the same period, so growth
        # vs "the whole project also just changed by this much" is always
        # checked by default, not an opt-in extra step an agent might skip.
        lang = data["project"].split(".")[0]
        try:
            baseline_data = fetch_aggregate(
                lang, data["start"], data["end"], granularity=data["granularity"],
                access=data.get("access", "all-access"), agent=data.get("agent", "user"),
            )
        except RuntimeError:
            pass  # baseline is a nice-to-have; don't fail the whole analysis over it

    print(json.dumps(analyze(data, baseline_data, lang=args.language), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
