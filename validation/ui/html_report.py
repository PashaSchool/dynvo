"""Generates a standalone HTML validation report from progress.json."""

import json
import webbrowser
from pathlib import Path

from ..config import PROGRESS_FILE


def _load_progress() -> dict:
    if not PROGRESS_FILE.exists():
        raise FileNotFoundError(f"No progress file at {PROGRESS_FILE}")
    return json.loads(PROGRESS_FILE.read_text())


def _f1_color(f1: float) -> str:
    if f1 >= 0.7:
        return "#22c55e"
    if f1 >= 0.4:
        return "#eab308"
    return "#ef4444"


def _badge(text: str, color: str) -> str:
    return (
        f'<span style="background:{color};color:#fff;padding:2px 8px;'
        f'border-radius:4px;font-size:12px;font-weight:600">{text}</span>'
    )


def generate_html(progress: dict) -> str:
    repos = progress.get("repos", [])
    summary = progress.get("summary", {})

    avg_p = summary.get("avg_precision", 0)
    avg_r = summary.get("avg_recall", 0)
    avg_f1 = summary.get("avg_f1", 0)
    total = summary.get("total_repos", 0)
    success = summary.get("successful_repos", 0)

    # Summary cards
    cards_html = f"""
    <div class="cards">
        <div class="card">
            <div class="card-value">{success}/{total}</div>
            <div class="card-label">Repos</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{_f1_color(avg_p)}">{avg_p:.0%}</div>
            <div class="card-label">Avg Precision</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{_f1_color(avg_r)}">{avg_r:.0%}</div>
            <div class="card-label">Avg Recall</div>
        </div>
        <div class="card">
            <div class="card-value" style="color:{_f1_color(avg_f1)}">{avg_f1:.0%}</div>
            <div class="card-label">Avg F1</div>
        </div>
    </div>
    """

    # Repo rows
    repo_rows = []
    for rp in repos:
        vr = rp.get("validation_result")
        name = rp["repo"]["name"]
        error = rp.get("error", "")

        if not vr:
            repo_rows.append(f"""
            <tr class="repo-row">
                <td><strong>{name}</strong>{f'<br><small class="error">{error[:80]}</small>' if error else ''}</td>
                <td colspan="5" class="center dim">No results</td>
            </tr>""")
            continue

        p, r, f1 = vr["precision"], vr["recall"], vr["f1_score"]
        detected = vr.get("detected_features", [])
        expected = vr.get("expected_features", [])
        matched = vr.get("matched_features", [])
        missed = vr.get("missed_features", [])
        spurious = vr.get("spurious_features", [])

        matched_expected = {m["expected"] for m in matched}
        matched_detected = {m["detected"] for m in matched}
        unmatched_detected = sorted(set(detected) - matched_detected)
        unmatched_expected = sorted(set(expected) - matched_expected)

        # Match details
        match_html = ""
        for m in sorted(matched, key=lambda x: x["expected"]):
            conf = m.get("confidence", 0)
            match_html += (
                f'<div class="match-row">'
                f'<span class="match-expected">{m["expected"]}</span>'
                f' <span class="arrow">&#8592;</span> '
                f'<span class="match-detected">{m["detected"]}</span>'
                f' <span class="conf">{conf:.0%}</span>'
                f'</div>'
            )

        # Missed features
        missed_html = ""
        if unmatched_expected:
            missed_html = '<div class="tag-group"><strong>Missed:</strong> '
            missed_html += " ".join(f'<span class="tag missed">{f}</span>' for f in unmatched_expected)
            missed_html += "</div>"

        # Spurious features
        spurious_html = ""
        if unmatched_detected:
            spurious_html = '<div class="tag-group"><strong>Extra:</strong> '
            spurious_html += " ".join(f'<span class="tag spurious">{f}</span>' for f in unmatched_detected)
            spurious_html += "</div>"

        repo_rows.append(f"""
        <tr class="repo-row" onclick="this.nextElementSibling.classList.toggle('hidden')">
            <td><strong>{name}</strong></td>
            <td class="center">{len(detected)}</td>
            <td class="center">{len(matched)}/{len(expected)}</td>
            <td class="num">{p:.0%}</td>
            <td class="num">{r:.0%}</td>
            <td class="num" style="color:{_f1_color(f1)};font-weight:700">{f1:.0%}</td>
        </tr>
        <tr class="detail-row hidden">
            <td colspan="6">
                <div class="detail-content">
                    <h4>Matches ({len(matched)})</h4>
                    {match_html or '<div class="dim">None</div>'}
                    {missed_html}
                    {spurious_html}
                </div>
            </td>
        </tr>""")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Faultline Validation Report</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
           background: #0a0a0a; color: #e5e5e5; padding: 24px; max-width: 1100px; margin: 0 auto; }}
    h1 {{ font-size: 24px; margin-bottom: 4px; }}
    .subtitle {{ color: #737373; margin-bottom: 24px; font-size: 14px; }}
    .cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 24px; }}
    .card {{ background: #171717; border: 1px solid #262626; border-radius: 8px; padding: 16px; text-align: center; }}
    .card-value {{ font-size: 28px; font-weight: 700; }}
    .card-label {{ font-size: 12px; color: #737373; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.05em; }}
    table {{ width: 100%; border-collapse: collapse; background: #171717; border-radius: 8px; overflow: hidden; }}
    th {{ background: #1a1a1a; text-align: left; padding: 10px 12px; font-size: 12px;
         text-transform: uppercase; letter-spacing: 0.05em; color: #737373; border-bottom: 1px solid #262626; }}
    td {{ padding: 10px 12px; border-bottom: 1px solid #1a1a1a; }}
    .repo-row {{ cursor: pointer; transition: background 0.15s; }}
    .repo-row:hover {{ background: #1a1a1a; }}
    .detail-row td {{ padding: 0; }}
    .detail-content {{ padding: 12px 16px 16px; background: #111; }}
    .hidden {{ display: none; }}
    .center {{ text-align: center; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .dim {{ color: #525252; }}
    .error {{ color: #ef4444; }}
    h4 {{ font-size: 13px; margin-bottom: 8px; color: #a3a3a3; }}
    .match-row {{ display: flex; align-items: center; gap: 6px; padding: 3px 0; font-size: 13px; }}
    .match-expected {{ color: #22c55e; font-weight: 600; min-width: 160px; }}
    .arrow {{ color: #525252; }}
    .match-detected {{ color: #60a5fa; }}
    .conf {{ color: #525252; font-size: 11px; }}
    .tag-group {{ margin-top: 10px; line-height: 2; }}
    .tag {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin: 2px; }}
    .tag.missed {{ background: #7f1d1d; color: #fca5a5; }}
    .tag.spurious {{ background: #1e3a5f; color: #93c5fd; }}
</style>
</head>
<body>
    <h1>Faultline Validation Report</h1>
    <div class="subtitle">Feature detection accuracy across open-source repositories</div>
    {cards_html}
    <table>
        <thead>
            <tr>
                <th>Repository</th>
                <th class="center">Features</th>
                <th class="center">Matched</th>
                <th class="num">Precision</th>
                <th class="num">Recall</th>
                <th class="num">F1</th>
            </tr>
        </thead>
        <tbody>
            {"".join(repo_rows)}
        </tbody>
    </table>
    <div class="subtitle" style="margin-top: 16px; text-align: right;">
        Click a row to expand match details
    </div>
</body>
</html>"""


def write_report(output_path: Path | None = None, open_browser: bool = True) -> Path:
    """Generates HTML report and optionally opens in browser."""
    progress = _load_progress()
    html = generate_html(progress)

    if output_path is None:
        output_path = PROGRESS_FILE.parent / "report.html"

    output_path.write_text(html)

    if open_browser:
        webbrowser.open(f"file://{output_path.resolve()}")

    return output_path


if __name__ == "__main__":
    path = write_report()
    print(f"Report: {path}")
