"""Cross-project summary page for multi-project (--gcp-project all) runs.

Writes one SUMMARY.html at the root of the output directory consolidating
every successfully assessed project: headline stats (savings, auto-migrate %,
SQL portability), a per-project comparison table with the recommended AWS
target, and auto-generated key takeaways.

Theme matches the main assessment report (Cloudscape design system).
"""
from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from bq_assess.core.units import fmt_size as _fmt_size  # canonical size formatter
from bq_assess.models import Assessment


def _fmt_money(value: float) -> str:
    """$1,234 / $12.3K / $1.2M — compact but unambiguous."""
    if abs(value) >= 1_000_000:
        return f"${value / 1_000_000:,.1f}M"
    if abs(value) >= 10_000:
        return f"${value / 1_000:,.1f}K"
    return f"${value:,.0f}"


def _project_row(a: Assessment) -> dict:
    """Extract the summary-table row for one project's Assessment."""
    s = a.summary
    total_tables = s.total_tables
    auto = s.effort_counts.get("AUTO", 0)
    scored = sum(s.effort_counts.values())
    auto_pct = (auto / scored * 100) if scored else 0.0

    # SQL complexity scoped to entities that OWN SQL (population REBUILT —
    # views/MVs/routines), mirroring the individual report's Query Complexity
    # default scope. summary.complexity_counts spans ALL entities, where every
    # plain table scores PORTABLE by definition — at fleet scale that rendered
    # "100% SQL portable across 12,599 entities" when only ~300 own SQL
    # (2026-07-31 sandbox validation).
    sql_complexity: dict[str, int] = {"PORTABLE": 0, "ADAPT": 0, "REWRITE": 0}
    for e in a.entities:
        if getattr(e.population, "value", e.population) != "REBUILT":
            continue
        comp = getattr(e, "complexity", None)
        if comp is None:
            continue
        cat = getattr(comp.category, "value", comp.category)
        if cat in sql_complexity:
            sql_complexity[cat] += 1

    # Match the individual report's headline basis: the conservative (low) savings,
    # i.e. BigQuery vs the high end of the AWS range (monthly_delta_low).
    # annual_saving_high is kept so a range that straddles zero (worst case
    # costs more, steady state saves — the Intelligent-Tiering spread) renders
    # as a range instead of a bare "(higher)" verdict (2026-07-31 sandbox validation).
    bq_monthly = a.cost.bigquery_monthly
    aws_monthly = a.cost.aws_monthly_high
    annual_saving = a.cost.annual_savings_low
    annual_saving_high = a.cost.annual_savings_high

    if a.cost.recommendation and a.cost.recommendation.recommended_scenario:
        recommendation = a.cost.recommendation.recommended_scenario
    elif a.engine_recommendation:
        recommendation = a.engine_recommendation.primary_engine.capitalize()
    else:
        recommendation = "—"

    return {
        "project_id": a.project_id,
        "tables": total_tables,
        "entities": s.total_entities,
        "size_gb": s.total_logical_size_gb or s.total_size_gb,
        "auto_pct": auto_pct,
        "auto_count": auto,
        "scored_count": scored,
        "bq_monthly": bq_monthly,
        "aws_monthly": aws_monthly,
        "annual_saving": annual_saving,
        "annual_saving_high": annual_saving_high,
        "recommendation": recommendation,
        "effort_counts": s.effort_counts,
        "complexity_counts": sql_complexity,
        "report_href": f"{a.project_id}_{a.generated_at.strftime('%Y-%m-%d')}/report/{a.project_id}-assessment.html",
    }


def _key_takeaways(rows: list[dict]) -> list[str]:
    """Auto-generate the takeaway bullets from the aggregate numbers."""
    takeaways: list[str] = []
    saving_rows = [r for r in rows if r["annual_saving"] > 0]
    if len(rows) > 1:
        takeaways.append(
            f"{len(saving_rows)} of {len(rows)} projects save money on AWS."
        )
    neutral = [r for r in rows if r["annual_saving"] <= 0]
    for r in neutral:
        high = r.get("annual_saving_high", r["annual_saving"])
        if high > 0:
            takeaways.append(
                f"{r['project_id']} ranges from {_fmt_money(abs(r['annual_saving']))}/yr "
                f"higher (worst case, all storage hot) to {_fmt_money(high)}/yr saved at "
                f"storage-tiering steady state — see its report's storage derivation."
            )
        else:
            takeaways.append(
                f"{r['project_id']} is cost-neutral or higher on AWS — review its "
                f"workload profile before migrating."
            )
    if saving_rows:
        best = max(saving_rows, key=lambda r: r["annual_saving"])
        takeaways.append(
            f"{best['project_id']} delivers the largest annual saving "
            f"({_fmt_money(best['annual_saving'])}/yr) — {best['recommendation']}."
        )
    low_auto = [r for r in rows if r["scored_count"] and r["auto_pct"] < 80]
    for r in low_auto:
        takeaways.append(
            f"{r['project_id']} has the most manual migration surface "
            f"({r['auto_pct']:.0f}% auto) — budget engineering time accordingly."
        )
    return takeaways


def write_summary(assessments: Sequence[Assessment], output_dir: str) -> str:
    """Write SUMMARY.html consolidating all assessments. Returns the file path."""
    rows = [_project_row(a) for a in assessments]

    total_tables = sum(r["tables"] for r in rows)
    total_entities = sum(r["entities"] for r in rows)
    total_size_gb = sum(r["size_gb"] for r in rows)
    total_annual_saving = sum(r["annual_saving"] for r in rows)
    total_bq_monthly = sum(r["bq_monthly"] for r in rows)
    total_aws_monthly = sum(r["aws_monthly"] for r in rows)

    total_auto = sum(r["auto_count"] for r in rows)
    total_scored = sum(r["scored_count"] for r in rows)
    auto_pct = (total_auto / total_scored * 100) if total_scored else 0.0

    portable = sum(r["complexity_counts"].get("PORTABLE", 0) for r in rows)
    adapt = sum(r["complexity_counts"].get("ADAPT", 0) for r in rows)
    rewrite = sum(r["complexity_counts"].get("REWRITE", 0) for r in rows)
    complexity_total = portable + adapt + rewrite
    portable_or_adapt_pct = (
        ((portable + adapt) / complexity_total * 100) if complexity_total else 0.0
    )

    generated = datetime.now(timezone.utc)

    table_rows_html = ""
    for r in sorted(rows, key=lambda r: r["annual_saving"], reverse=True):
        high = r.get("annual_saving_high", r["annual_saving"])
        if r["annual_saving"] > 0:
            saving_html = f'<span class="pos">{_fmt_money(r["annual_saving"])}</span>'
        elif r["annual_saving"] < 0 and high > 0:
            # Range straddles zero: worst case costs more, steady state saves.
            saving_html = (
                f'<span class="neg">{_fmt_money(abs(r["annual_saving"]))} higher</span>'
                f' <span class="neutral">to</span> '
                f'<span class="pos">{_fmt_money(high)} saved</span>'
            )
        elif r["annual_saving"] < 0:
            saving_html = f'<span class="neg">+{_fmt_money(abs(r["annual_saving"]))} (higher)</span>'
        else:
            saving_html = '<span class="neutral">—</span>'
        table_rows_html += f"""
      <tr>
        <td><a href="{r['report_href']}">{r['project_id']}</a></td>
        <td>{r['tables']:,}</td>
        <td>{_fmt_size(r['size_gb'])}</td>
        <td>{r['auto_pct']:.1f}%</td>
        <td>${r['bq_monthly']:,.0f}/mo</td>
        <td>${r['aws_monthly']:,.0f}/mo</td>
        <td>{saving_html}</td>
        <td>{r['recommendation']}</td>
      </tr>"""

    takeaways = _key_takeaways(rows)
    takeaways_section = ""
    if takeaways:
        bullets = "\n".join(f"    <li>{t}</li>" for t in takeaways)
        takeaways_section = f"""
<div class="takeaways">
  <h3 class="takeaways-title">Key Points</h3>
  <ul class="takeaways-list">
{bullets}
  </ul>
</div>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Migration Assessment Summary — {len(rows)} Projects</title>
  <style>
    /* Summary page — Cloudscape design system (matches assessment report) */
    :root {{
      --color-bg-layout: #f2f8fd;
      --color-bg-default: #ffffff;
      --color-bg-header: #0f1b2a;
      --color-text-body: #000716;
      --color-text-heading: #0f1b2a;
      --color-text-secondary: #5f6b7a;
      --color-text-muted: #7d8998;
      --color-text-inverse: #ffffff;
      --color-text-link: #0972d3;
      --color-border-divider: #e9ebed;
      --color-border-card: #e9ebed;
      --color-brand-orange: #ff9900;
      --color-severity-success: #037f0c;
      --color-severity-warning: #d97706;
      --color-severity-error: #d91515;
      --shadow-xs: 0 1px 2px 0 rgba(0, 7, 22, 0.05);
      --radius-md: 12px;
      --font-body: "Amazon Ember", -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --space-xs: 4px; --space-sm: 8px; --space-md: 16px; --space-lg: 24px; --space-xl: 32px;
    }}
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      font-family: var(--font-body);
      font-size: 14px;
      line-height: 1.6;
      color: var(--color-text-body);
      background: var(--color-bg-layout);
      margin: 0; padding: 0;
      -webkit-font-smoothing: antialiased;
    }}
    .container {{ max-width: 1100px; margin: 0 auto; padding: 0 var(--space-lg); }}
    h1, h2 {{ margin-top: 0; font-weight: 700; line-height: 1.25; color: var(--color-text-heading); }}
    a {{ color: var(--color-text-link); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}

    .header {{ background: var(--color-bg-header); padding: 0; }}
    .header-inner {{ display: flex; align-items: center; padding: var(--space-md) 0; min-height: 56px; }}
    .header-logo {{ display: flex; align-items: center; gap: var(--space-sm); color: var(--color-text-inverse); font-weight: 700; }}
    .header-logo .aws-cube {{
      width: 32px; height: 32px; background: var(--color-brand-orange); border-radius: 6px;
      display: flex; align-items: center; justify-content: center;
      font-weight: 800; font-size: 11px; color: var(--color-bg-header); letter-spacing: 0.5px;
    }}
    .header-meta {{ margin-left: auto; color: rgba(255,255,255,0.6); font-size: 0.8125rem; }}
    .header-meta strong {{ color: rgba(255,255,255,0.9); }}

    .content {{ padding: var(--space-xl) 0; }}
    .subtitle {{ color: var(--color-text-secondary); font-size: 0.9375rem; margin: 0 0 var(--space-lg); }}

    .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: var(--space-md); margin-bottom: var(--space-xl); }}
    .stat-card {{
      background: var(--color-bg-default); border: 1px solid var(--color-border-card);
      border-radius: var(--radius-md); box-shadow: var(--shadow-xs); padding: var(--space-lg);
      border-top: 3px solid var(--color-brand-orange);
    }}
    .stat-card .value {{ font-size: 2.25rem; font-weight: 700; line-height: 1.1; color: var(--color-text-heading); letter-spacing: -0.02em; }}
    .stat-card .label {{ font-size: 0.8125rem; font-weight: 600; color: var(--color-text-secondary); margin-top: var(--space-xs); }}
    .stat-card .detail {{ font-size: 0.75rem; color: var(--color-text-muted); margin-top: var(--space-xs); }}

    .table-card {{
      background: var(--color-bg-default); border: 1px solid var(--color-border-card);
      border-radius: var(--radius-md); box-shadow: var(--shadow-xs); overflow: hidden;
      margin-bottom: var(--space-lg);
    }}
    table {{ width: 100%; border-collapse: collapse; }}
    th, td {{ border-bottom: 1px solid var(--color-border-divider); padding: 0.625rem 0.875rem; text-align: left; font-size: 0.8125rem; }}
    th {{ background: #f4f4f4; font-weight: 600; color: var(--color-text-secondary); text-transform: uppercase; letter-spacing: 0.4px; font-size: 0.6875rem; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: #fafbfc; }}
    .pos {{ color: var(--color-severity-success); font-weight: 600; }}
    .neg {{ color: var(--color-severity-warning); font-weight: 600; }}
    .neutral {{ color: var(--color-text-muted); }}

    .takeaways {{
      background: var(--color-bg-default); border: 1px solid var(--color-border-card);
      border-left: 4px solid var(--color-brand-orange);
      border-radius: var(--radius-md); box-shadow: var(--shadow-xs);
      padding: var(--space-lg); font-size: 0.875rem;
      margin-bottom: var(--space-lg);
    }}
    .takeaways-title {{
      font-size: 0.9375rem; font-weight: 700; margin: 0 0 var(--space-sm);
      color: var(--color-text-heading);
    }}
    .takeaways-list {{
      margin: 0; padding-left: 1.25em; list-style: disc;
    }}
    .takeaways-list li {{
      margin-bottom: var(--space-xs); line-height: 1.5;
    }}
    .takeaways-list li:last-child {{ margin-bottom: 0; }}

    .footer {{ padding: var(--space-lg) 0; border-top: 1px solid var(--color-border-divider); font-size: 0.75rem; color: var(--color-text-muted); }}
  </style>
</head>
<body>

<div class="header">
  <div class="container">
    <div class="header-inner">
      <div class="header-logo">
        <div class="aws-cube">BQ</div>
        <div>
          <h1 style="color:#fff;margin:0;font-size:1.125rem">Migration Assessment Summary</h1>
          <p style="color:rgba(255,255,255,.5);margin:2px 0 0 0;font-size:.75rem">BigQuery &rarr; AWS &middot; consolidated across all projects</p>
        </div>
      </div>
      <div class="header-meta">
        {generated.strftime('%B %Y')}
      </div>
    </div>
  </div>
</div>

<div class="content">
<div class="container">

<p class="subtitle">Consolidated findings across <strong>{len(rows)} project{"s" if len(rows) != 1 else ""}</strong>
&nbsp;|&nbsp; {_fmt_size(total_size_gb)} &nbsp;|&nbsp; {total_tables:,} tables &nbsp;|&nbsp; {total_entities:,} entities</p>

<div class="stats">
  <div class="stat-card">
    <div class="value">{_fmt_money(abs(total_annual_saving))}</div>
    <div class="label">{"annual cost savings on AWS" if total_annual_saving >= 0 else "annual cost increase on AWS"}</div>
    <div class="detail">${total_bq_monthly:,.0f}/mo (BQ) &rarr; ${total_aws_monthly:,.0f}/mo (AWS)</div>
  </div>
  <div class="stat-card">
    <div class="value">{auto_pct:.1f}%</div>
    <div class="label">entities auto-migrate</div>
    <div class="detail">{total_auto:,} of {total_scored:,} auto-migrate</div>
  </div>
  <div class="stat-card">
    <div class="value">{portable_or_adapt_pct:.1f}%</div>
    <div class="label">SQL portable or minor adapt</div>
    <div class="detail">{rewrite:,} of {complexity_total:,} SQL entities (views/routines) need rewrite</div>
  </div>
</div>

<div class="table-card">
  <table>
    <thead>
      <tr>
        <th>Project</th>
        <th>Tables</th>
        <th>Size</th>
        <th>Auto-migrate %</th>
        <th>BQ Cost</th>
        <th>AWS Cost</th>
        <th>Annual Saving</th>
        <th>Recommendation</th>
      </tr>
    </thead>
    <tbody>{table_rows_html}
    </tbody>
  </table>
</div>
{takeaways_section}

<div class="footer">
  Source: BigQuery Migration Assessment Tool &nbsp;|&nbsp; {generated.strftime('%B %Y')}
  &nbsp;|&nbsp; Estimates are directional, not a pricing quote — see each project report's disclaimer.
</div>

</div>
</div>

</body>
</html>"""

    out_path = Path(output_dir) / "SUMMARY.html"
    out_path.write_text(html, encoding="utf-8")
    return str(out_path)
