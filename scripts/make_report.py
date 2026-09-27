#!/usr/bin/env python3
"""Turn fetch_pageviews.py + analyze_trends.py output into a one-page PDF
report plus two chart files: a static PNG and an interactive HTML (click a
legend entry to hide/show its curve, drag to zoom the time axis, hover for
exact values - a static image can't do any of that).

For each language/topic being compared, pass the pair of JSON files produced
earlier in the pipeline. This script draws the charts and lays out the PDF -
it does not recompute any numbers, it only presents what analyze_trends.py
already decided (growth, confidence, reasons).

Usage (single topic):
    python make_report.py --title "Astronomy interest in Ukrainian Wikipedia" \
        --question "Чи зростає інтерес до астрономії?" \
        --data uk:astro_data.json:astro_analysis.json \
        --output report.pdf

Usage (comparing languages):
    python make_report.py --title "..." --question "..." \
        --data pl:pl_data.json:pl_analysis.json \
        --data cs:cs_data.json:cs_analysis.json \
        --output report.pdf
"""
import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import plotly.graph_objects as go
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer

DATA_SOURCE_NOTE = "Wikimedia Pageviews API (wikimedia.org/api/rest_v1/metrics/pageviews)"

# All fixed/boilerplate report text (headings, disclaimers, template
# sentences) lives here per language, so the whole report - not just the
# agent-supplied title/recommendation - comes out in one consistent
# language. Previously these were hardcoded in Ukrainian while the caller's
# title/recommendation could be in any language, producing mixed-language
# reports. Falls back to English for any language not listed here, so an
# unsupported language still stays internally consistent rather than
# silently reverting to Ukrainian.
STRINGS = {
    "uk": {
        "chart_note": "Графік динаміки збережено окремо: {name}",
        "request_label": "Запит: {question}",
        "methodology_heading": "Методологія порівняння",
        "methodology_single": "Один запис — дані взято зі спеціалізованої статті на цю тему (відповідник поняття у Wikidata), порівняння між мовами тут не застосовується.",
        "methodology_clean": "Кожен запис - це трафік окремої спеціалізованої статті на Wikipedia (не сурогатний/ширший показник) - це пряме, методологічно чисте порівняння без додаткових застережень щодо зіставності даних.",
        "methodology_tier2": "Для {names} немає власної спеціалізованої статті на цю тему, тому використано ширшу/загальну статтю як наближений показник. Це слабший сигнал з двох причин: (1) широка стаття зазвичай описує тему загалом, а не саме ту практику/намір, що порівнюється, тому може відображати геть інший інтерес читачів; (2) широкі \"хабові\" статті отримують значну частку переглядів випадково - через посилання з інших статей, а не через цілеспрямований пошук, тому їхній трафік зашумленіший і менш показовий, ніж трафік вузької, свідомо знайденої статті. Порівнюйте ці записи з рештою обережно.",
        "summary_heading": "Підсумок",
        "change_word": "зміна",
        "confidence_word": "довіра",
        "share_suffix": ", частка від проєкту: {pct:.4f}%",
        "na": "н/д",
        "recommendation_heading": "Рекомендація",
        "risk_lead": "Ризиковано покладатись на:",
        "risk_low_confidence": "низька статистична довіра",
        "risk_tier2": "дані лише по ширшому/суміжному поняттю, не по спеціалізованій темі",
        "assumptions_heading": "Припущення та обмеження",
        "missing_data": "Дані відсутні для: {names} - статті на цю тему немає у відповідному мовному розділі Wikipedia, тому вони не включені в порівняння вище.",
        "standing_disclaimer": "Перегляди Wikipedia відображають цікавість/обізнаність, а не намір навчатись чи платити - трактуйте як сигнал для подальшої перевірки, а не остаточний доказ попиту.",
        "outlier_note": "На графіку можуть впадати в очі різкі сплески — {parts}. Це разові аномалії (детальніше - у \"Припущення та обмеження\" нижче), вони НЕ враховані в тренд/зміну %, зазначені в підсумку - тому підсумок може виглядати інакше, ніж загальна форма графіка.",
        "views_axis": "Перегляди",
        "views_axis_log": "Перегляди (лог. шкала)",
        "footer": "Джерело даних: {source}. Період: {period}. Згенеровано: {generated}.",
    },
    "en": {
        "chart_note": "Chart saved separately: {name}",
        "request_label": "Request: {question}",
        "methodology_heading": "Comparison methodology",
        "methodology_single": "Single entry - data comes from the specialized article on this topic (its Wikidata concept match); cross-language comparison doesn't apply here.",
        "methodology_clean": "Every entry is traffic for its own specialized Wikipedia article (not a broader/surrogate proxy) - this is a direct, methodologically clean comparison with no additional caveats about data comparability.",
        "methodology_tier2": "{names} has no specialized article of its own on this topic, so a broader/generic article was used as an approximate proxy. That's a weaker signal for two reasons: (1) a broad article usually describes the topic in general, not the specific practice/intent being compared, so its readership may reflect a completely different interest; (2) broad \"hub\" articles get a large share of their traffic incidentally - via links from other articles, not deliberate search - so their traffic is noisier and less indicative than a narrowly, deliberately-found article's traffic. Compare these entries against the rest with caution.",
        "summary_heading": "Summary",
        "change_word": "change",
        "confidence_word": "confidence",
        "share_suffix": ", share of project: {pct:.4f}%",
        "na": "n/a",
        "recommendation_heading": "Recommendation",
        "risk_lead": "Risky to rely on:",
        "risk_low_confidence": "low statistical confidence",
        "risk_tier2": "data only for a broader/related concept, not the specialized topic",
        "assumptions_heading": "Assumptions & limitations",
        "missing_data": "No data for: {names} - no article on this topic exists in the corresponding language edition of Wikipedia, so they aren't included in the comparison above.",
        "standing_disclaimer": "Wikipedia pageviews reflect curiosity/awareness, not intent to learn or pay - treat this as a signal worth further validation, not final proof of demand.",
        "outlier_note": "The chart may show sharp spikes that catch the eye — {parts}. These are one-off anomalies (see \"Assumptions & limitations\" below for details) and are NOT included in the trend/% change reported in the summary - so the summary can look different from the chart's overall shape.",
        "views_axis": "Views",
        "views_axis_log": "Views (log scale)",
        "footer": "Data source: {source}. Period: {period}. Generated: {generated}.",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    table = STRINGS.get(lang, STRINGS["en"])
    template = table.get(key, STRINGS["en"][key])
    return template.format(**kwargs) if kwargs else template

# reportlab's built-in fonts (Helvetica etc.) have no Cyrillic glyphs, which
# silently renders Ukrainian/Polish/Czech text as black boxes. matplotlib
# already ships DejaVu Sans (full Unicode coverage incl. Cyrillic) as a
# dependency, so reuse it instead of adding a new font asset to the skill.
_DEJAVU_DIR = Path(matplotlib.__file__).resolve().parent / "mpl-data" / "fonts" / "ttf"
pdfmetrics.registerFont(TTFont("DejaVuSans", str(_DEJAVU_DIR / "DejaVuSans.ttf")))
pdfmetrics.registerFont(TTFont("DejaVuSans-Bold", str(_DEJAVU_DIR / "DejaVuSans-Bold.ttf")))
# So that <b>...</b> inside Paragraph markup picks the Cyrillic-capable bold
# variant instead of falling back to Helvetica-Bold.
pdfmetrics.registerFontFamily("DejaVuSans", normal="DejaVuSans", bold="DejaVuSans-Bold",
                               italic="DejaVuSans", boldItalic="DejaVuSans-Bold")


def _load_supported_codepoints(ttf_path: Path) -> set:
    """DejaVu Sans covers Latin/Cyrillic/Greek and a lot more, but not CJK,
    Thai, Arabic, etc. Relying on an instruction to "never put unsupported
    script in a label" isn't reliable enough on its own (observed the same
    mistake happen twice, across two different models) - so check the font's
    actual glyph coverage and sanitize text ourselves instead of trusting
    the caller to remember."""
    from fontTools.ttLib import TTFont as FTFont
    font = FTFont(str(ttf_path), lazy=True)
    cmap = font.getBestCmap()
    return set(cmap.keys())


_SUPPORTED_CODEPOINTS = _load_supported_codepoints(_DEJAVU_DIR / "DejaVuSans.ttf")


def sanitize_text(text: str) -> str:
    """Replace any run of characters the report's font can't render (e.g.
    CJK, Thai, Arabic script) with a single placeholder, instead of letting
    them silently render as blank boxes. ASCII whitespace/punctuation always
    passes through untouched."""
    if not text:
        return text
    out = []
    in_gap = False
    for ch in text:
        if ord(ch) < 128 or ord(ch) in _SUPPORTED_CODEPOINTS:
            out.append(ch)
            in_gap = False
        elif not in_gap:
            out.append("□")
            in_gap = True
    return "".join(out)


def parse_data_arg(raw: str) -> dict:
    parts = raw.split(":", 2)
    if len(parts) != 3:
        raise ValueError(f"--data must be 'label:fetch_json_path:analysis_json_path', got: {raw}")
    label, fetch_path, analysis_path = parts
    fetch_data = json.loads(Path(fetch_path).read_text())
    analysis_data = json.loads(Path(analysis_path).read_text())
    return {"label": sanitize_text(label), "fetch": fetch_data, "analysis": analysis_data}


def render_chart(entries: list, chart_path: Path, lang: str = "en") -> None:
    fig, ax = plt.subplots(figsize=(7.2, 3.2), dpi=150)
    for entry in entries:
        series = entry["fetch"]["series"]
        dates = [p["date"] for p in series]
        views = [p["views"] for p in series]
        ax.plot(dates, views, marker="o", markersize=2, linewidth=1.5, label=entry["label"])

    ax.set_ylabel(t(lang, "views_axis"))
    # Placing the legend inside the plot (e.g. "upper right") covers up
    # whichever curve happens to be there, and placing it to the *right* of
    # the plot breaks the layout when labels are long (e.g. a Tier 2
    # parenthetical explanation) - the legend column can end up wider than
    # the chart itself. Put it below the plot instead, wrapped into a few
    # columns: long labels just add height, never squeeze the chart width.
    ncol = 1 if len(entries) <= 2 else min(3, len(entries))
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.32), fontsize=7, ncol=ncol, frameon=False)
    ax.tick_params(axis="x", labelrotation=45, labelsize=7)
    ax.tick_params(axis="y", labelsize=8)
    # Wikipedia pageviews for a niche topic can be tiny for one language and
    # huge for another - a shared linear y-axis would flatten the smaller
    # ones into an invisible line, hiding their trend entirely.
    if len(entries) > 1:
        max_views = [max((p["views"] for p in e["fetch"]["series"]), default=0) for e in entries]
        min_views = [min((p["views"] for p in e["fetch"]["series"]), default=0) for e in entries]
        if max_views and min(min_views) > 0 and max(max_views) / max(min(max_views), 1) > 20:
            ax.set_yscale("log")
            ax.set_ylabel(t(lang, "views_axis_log"))

    if len(dates) > 12:
        step = max(1, len(dates) // 12)
        ax.set_xticks(range(0, len(dates), step))
        ax.set_xticklabels([dates[i] for i in range(0, len(dates), step)])

    fig.tight_layout()
    fig.savefig(chart_path, bbox_inches="tight")
    plt.close(fig)


def render_interactive_chart(entries: list, html_path: Path, lang: str = "en") -> None:
    """A static PNG can't do what's often actually needed when comparing
    several series: hide one curve to see another clearly, zoom into a date
    range, or read exact values. Plotly produces a single self-contained
    HTML file (no server, opens in any browser) with all of that built in -
    click a legend entry to toggle its curve, drag to zoom the time axis,
    double-click to reset, hover for exact values."""
    fig = go.Figure()
    for entry in entries:
        series = entry["fetch"]["series"]
        fig.add_trace(go.Scatter(
            x=[p["date"] for p in series],
            y=[p["views"] for p in series],
            mode="lines+markers",
            name=entry["label"],
            marker=dict(size=4),
            line=dict(width=2),
        ))

    fig.update_layout(
        yaxis_title=t(lang, "views_axis"),
        xaxis_title=None,
        template="plotly_white",
        hovermode="x unified",
        legend=dict(orientation="v", yanchor="top", y=1, xanchor="left", x=1.02),
        margin=dict(l=60, r=180, t=30, b=40),
        xaxis=dict(rangeslider=dict(visible=True), type="date"),
    )
    # Embed plotly.js inline (not "cdn") so the file is fully self-contained
    # and works offline for whoever the report is shared with.
    fig.write_html(str(html_path), include_plotlyjs=True)


TIER2_LABEL_HINTS = ("proxy", "broad", "related concept", "not identical qid", "different concept", "суміжне поняття")


def _effective_tier2_labels(entries: list, tier2_labels: list) -> list:
    """Safety net for a real, observed mistake: the caller writes a label
    like "pt (... broad proxy)" but forgets to also pass it via --tier2,
    which used to make methodology_note() falsely claim a "clean, directly
    comparable" report while the label right below contradicts it. Auto-treat
    any label whose own text already admits it's a proxy/different concept as
    Tier 2, whether or not it was explicitly passed."""
    explicit = set(tier2_labels or [])
    auto = {e["label"] for e in entries if any(h in e["label"].lower() for h in TIER2_LABEL_HINTS)}
    return list(explicit | auto)


def methodology_note(entries: list, tier2_labels: list, lang: str = "en") -> str:
    """Explain, in the reader's own report (not just chat), whether this is a
    clean apples-to-apples comparison of the same specialized concept across
    languages, or whether some languages fall back to a broader/generic proxy
    article - and why that matters. A "Tier 2" fallback article measures a
    different, weaker signal for two reasons: (1) it's usually a broad
    descriptive topic rather than the specific practice/intent being
    compared, and (2) broad "hub" articles get heavily inflated by incidental
    in-article links, not people deliberately searching for that topic - so
    its traffic is noisier and less meaningful than a narrowly-searched
    specialized article's traffic."""
    tier2 = [e for e in entries if e["label"] in (tier2_labels or [])]
    if len(entries) == 1:
        return t(lang, "methodology_single")
    if not tier2:
        return t(lang, "methodology_clean")
    tier2_names = ", ".join(e["label"] for e in tier2)
    return t(lang, "methodology_tier2", names=tier2_names)


def risk_flags(entries: list, tier2_labels: list = None, lang: str = "en") -> list:
    """Objective, data-backed reasons an entry is risky to lean on - low
    statistical confidence, or a Tier 2 (broad/generic proxy) concept match.
    This is computed from numbers already in hand, not a judgment call - the
    judgment call (what to actually prioritize) is the caller's job via
    --recommendation, this just gives it solid ground to stand on."""
    tier2_set = set(tier2_labels or [])
    flags = []
    for entry in entries:
        reasons = []
        if entry["analysis"].get("confidence") == "low":
            reasons.append(t(lang, "risk_low_confidence"))
        if entry["label"] in tier2_set:
            reasons.append(t(lang, "risk_tier2"))
        if reasons:
            flags.append(f"<b>{entry['label']}</b>: {'; '.join(reasons)}")
    return flags


TREND_WORDS = {
    "uk": {"increasing": "зростає", "decreasing": "спадає", "flat_or_noisy": "стабільно/шумно"},
}
CONFIDENCE_WORDS = {
    "uk": {"high": "висока", "medium": "середня", "low": "низька"},
}


def _trend_word(trend: str, lang: str) -> str:
    return TREND_WORDS.get(lang, {}).get(trend, trend or "n/a")


def _confidence_word(confidence: str, lang: str) -> str:
    return CONFIDENCE_WORDS.get(lang, {}).get(confidence, confidence or "n/a")


def summary_bullets(entries: list, lang: str = "en") -> list:
    bullets = []
    na = t(lang, "na")
    for entry in entries:
        a = entry["analysis"]
        growth = a.get("growth_pct")
        growth_str = f"{growth:+.1f}%" if growth is not None else na
        share = a.get("share_of_project_pct")
        share_str = t(lang, "share_suffix", pct=share) if share is not None else ""
        trend_str = _trend_word(a.get("trend"), lang)
        confidence_str = _confidence_word(a.get("confidence"), lang) if a.get("confidence") else na
        bullets.append(
            f"<b>{entry['label']}</b>: {t(lang, 'change_word')} {growth_str} ({trend_str}), "
            f"{t(lang, 'confidence_word')}: <b>{confidence_str}</b>{share_str}"
        )
    return bullets


def assumptions_and_limitations(entries: list, missing_labels: list = None, lang: str = "en") -> list:
    seen = []
    if missing_labels:
        seen.append(t(lang, "missing_data", names=", ".join(missing_labels)))
    multiple = len(entries) > 1
    for entry in entries:
        for reason in entry["analysis"].get("reasons", []):
            # Prefix with the entry's label when comparing 2+ entries, so a
            # generic-sounding reason (e.g. "avg traffic ~8 views/day") is
            # never left ambiguous about which language/article it's about.
            labeled = f"<b>{entry['label']}</b>: {reason}" if multiple else reason
            if labeled not in seen:
                seen.append(labeled)
    seen.append(t(lang, "standing_disclaimer"))
    return seen


def outlier_chart_note(entries: list, lang: str = "en") -> str:
    """The chart plots every raw point, including outlier spikes - if one
    lands late in the period, the line visually shoots up at the end, which
    reads as "growth" even when growth_pct (computed with those points
    excluded) says decline. Say this up front, next to the chart reference,
    instead of leaving the reader to reconcile it themselves from a
    buried date list further down."""
    multiple = len(entries) > 1
    parts = []
    for entry in entries:
        outliers = entry["analysis"].get("outliers", [])
        if outliers:
            dates = ", ".join(o["date"][:7] for o in outliers)
            prefix = f"{entry['label']}: " if multiple else ""
            parts.append(f"{prefix}{dates}")
    if not parts:
        return None
    return t(lang, "outlier_note", parts="; ".join(parts))


def build_pdf(output_path: Path, title: str, question: str, entries: list, chart_path: Path,
              missing_labels: list = None, tier2_labels: list = None, recommendation: str = None,
              lang: str = "en") -> None:
    tier2_labels = _effective_tier2_labels(entries, tier2_labels)

    # More languages/topics means more summary + assumptions bullets, and a
    # long free-text --recommendation paragraph can itself take up as much
    # vertical space as several bullets. A flat bullet count undercounts that
    # (a "+1" for a 5-sentence recommendation is way off), so weight bullets
    # by roughly how many lines they'd wrap to (~80 chars/line at this font
    # size) instead of just counting entries.
    all_bullets = (summary_bullets(entries, lang) + assumptions_and_limitations(entries, missing_labels, lang)
                   + risk_flags(entries, tier2_labels, lang))
    content_score = sum(max(1, len(b) // 80 + 1) for b in all_bullets)
    if recommendation:
        content_score += len(recommendation) // 80 + 1
    outlier_note = outlier_chart_note(entries, lang)
    if outlier_note:
        content_score += len(outlier_note) // 80 + 1
    if content_score > 26:
        scale = 0.65
    elif content_score > 18:
        scale = 0.75
    elif content_score > 12:
        scale = 0.85
    elif content_score > 8:
        scale = 0.92
    else:
        scale = 1.0

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReportTitle", parent=styles["Title"], fontName="DejaVuSans-Bold", fontSize=16 * scale, spaceAfter=4)
    question_style = ParagraphStyle("Question", parent=styles["Normal"], fontName="DejaVuSans", fontSize=10 * scale, textColor=colors.grey, spaceAfter=10 * scale)
    heading_style = ParagraphStyle("SectionHeading", parent=styles["Heading3"], fontName="DejaVuSans-Bold", fontSize=11 * scale, spaceBefore=8 * scale, spaceAfter=4 * scale)
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontName="DejaVuSans", fontSize=9.5 * scale, leading=13 * scale)
    footer_style = ParagraphStyle("Footer", parent=styles["Normal"], fontName="DejaVuSans", fontSize=7, textColor=colors.grey)

    doc = SimpleDocTemplate(
        str(output_path), pagesize=A4,
        topMargin=1.5 * cm, bottomMargin=1.2 * cm, leftMargin=1.5 * cm, rightMargin=1.5 * cm,
    )

    story = [Paragraph(title, title_style)]
    if question:
        story.append(Paragraph(t(lang, "request_label", question=question), question_style))
    story.append(Paragraph(
        t(lang, "chart_note", name=chart_path.name),
        ParagraphStyle("ChartNote", parent=question_style, spaceAfter=3 * scale),
    ))
    if outlier_note:
        story.append(Paragraph(
            outlier_note,
            ParagraphStyle("OutlierNote", parent=body_style, textColor=colors.HexColor("#8a5a00"), spaceAfter=8 * scale),
        ))

    story.append(Paragraph(t(lang, "methodology_heading"), heading_style))
    story.append(Paragraph(methodology_note(entries, tier2_labels, lang), body_style))

    story.append(Paragraph(t(lang, "summary_heading"), heading_style))
    story.append(ListFlowable(
        [ListItem(Paragraph(b, body_style)) for b in summary_bullets(entries, lang)],
        bulletType="bullet",
    ))

    if recommendation:
        story.append(Paragraph(t(lang, "recommendation_heading"), heading_style))
        story.append(Paragraph(recommendation, body_style))
        flags = risk_flags(entries, tier2_labels, lang)
        if flags:
            story.append(Paragraph(t(lang, "risk_lead"), ParagraphStyle(
                "RiskLead", parent=body_style, spaceBefore=3 * scale, fontName="DejaVuSans-Bold")))
            story.append(ListFlowable(
                [ListItem(Paragraph(f, body_style)) for f in flags],
                bulletType="bullet",
            ))

    story.append(Paragraph(t(lang, "assumptions_heading"), heading_style))
    story.append(ListFlowable(
        [ListItem(Paragraph(r, body_style)) for r in assumptions_and_limitations(entries, missing_labels, lang)],
        bulletType="bullet",
    ))

    story.append(Spacer(1, 0.6 * cm * scale))
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    periods = {(e["fetch"]["start"], e["fetch"]["end"]) for e in entries}
    period_str = ", ".join(f"{s}–{e}" for s, e in periods)
    story.append(Paragraph(t(lang, "footer", source=DATA_SOURCE_NOTE, period=period_str, generated=generated), footer_style))

    doc.build(story)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--title", required=True, help="Report title")
    parser.add_argument("--question", default="", help="Original user question, shown under the title")
    parser.add_argument(
        "--data", action="append", required=True,
        help="'label:fetch_pageviews_json_path:analyze_trends_json_path', repeatable for comparisons",
    )
    parser.add_argument("--output", required=True, help="Output PDF path")
    parser.add_argument(
        "--recommendation", required=True,
        help="Required. Your own judgment call in plain language: what to prioritize, which option you'd "
             "pick and why, given this data - not just the raw numbers restated. Risky entries (low "
             "confidence / Tier 2 proxy) are listed automatically beneath this text, no need to repeat them.",
    )
    parser.add_argument(
        "--missing", default="",
        help="Comma-separated labels that were requested but had no data (e.g. no article in that "
             "language) - shown as an explicit limitation instead of silently vanishing from the report.",
    )
    parser.add_argument(
        "--tier2", default="",
        help="Comma-separated --data labels (must match exactly) that use a broader/generic fallback "
             "article instead of the same specialized concept as the rest - triggers an explicit "
             "methodology caveat in the report instead of presenting them as directly comparable.",
    )
    parser.add_argument(
        "--language", default="en",
        help="Language for ALL fixed report text (headings, disclaimers, etc.) - match the user's own "
             "language so the report doesn't mix languages (previously this was hardcoded Ukrainian "
             "regardless of the title/recommendation's language). Falls back to English if unsupported. "
             "Must match the --language you passed to analyze_trends.py for the same reason.",
    )
    args = parser.parse_args()

    try:
        entries = [parse_data_arg(d) for d in args.data]
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        sys.exit(1)

    missing_labels = [sanitize_text(x.strip()) for x in args.missing.split(",") if x.strip()]
    tier2_labels = [sanitize_text(x.strip()) for x in args.tier2.split(",") if x.strip()]
    title = sanitize_text(args.title)
    question = sanitize_text(args.question)
    recommendation = sanitize_text(args.recommendation)

    output_path = Path(args.output)
    chart_path = output_path.with_suffix(".chart.png")
    chart_html_path = output_path.with_suffix(".chart.html")
    render_chart(entries, chart_path, args.language)
    render_interactive_chart(entries, chart_html_path, args.language)
    build_pdf(output_path, title, question, entries, chart_path, missing_labels, tier2_labels,
              recommendation, args.language)

    print(json.dumps({
        "status": "ok", "output": str(output_path),
        "chart": str(chart_path), "interactive_chart": str(chart_html_path),
    }))


if __name__ == "__main__":
    main()
