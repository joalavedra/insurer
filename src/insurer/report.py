"""Self-contained HTML dashboard for simulation results."""

import json
import math
from html import escape
from pathlib import Path
from typing import Any

MONTH_WIDTH = 760
MONTH_HEIGHT = 280
CHART_PAD = {"left": 64, "right": 56, "top": 20, "bottom": 36}

CSS = """
:root{--ink:#16202a;--muted:#5d6b78;--line:#dde3e8;--bg:#f6f8fa;--card:#fff;
--gwp:#b9cde4;--earned:#2f5d8a;--loss:#d9822b;--good:#2e8b57;--bad:#c0392b}
*{box-sizing:border-box}
body{margin:0;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,
sans-serif;color:var(--ink);background:var(--bg)}
main{max-width:1180px;margin:0 auto;padding:28px 24px 48px}
header h1{margin:0;font-size:24px}
header p{margin:4px 0 0;color:var(--muted)}
.badge{display:inline-block;padding:2px 8px;border-radius:10px;background:#fff3cd;
color:#7a5b00;font-size:12px;font-weight:600;margin-left:8px;vertical-align:middle}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px;
margin:22px 0}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:12px 14px}
.kpi .label{color:var(--muted);font-size:12px;text-transform:uppercase;
letter-spacing:.04em}
.kpi .value{font-size:22px;font-weight:650;margin-top:2px}
.kpi .sub{color:var(--muted);font-size:12px}
.good{color:var(--good)}.bad{color:var(--bad)}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:16px 18px;margin-top:16px;overflow-x:auto}
section h2{margin:0 0 4px;font-size:17px}
section p.hint{margin:0 0 12px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid2>section{margin-top:0}
@media (max-width:900px){.grid2{grid-template-columns:1fr}}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right;
white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:12px}
td.l,th.l{text-align:left}
td.wrap{white-space:normal;text-align:left;min-width:260px}
tr.total td{font-weight:650}
.bar{height:10px;border-radius:5px;background:var(--loss);display:inline-block;
vertical-align:middle}
.legend{display:flex;gap:16px;color:var(--muted);font-size:12px;margin:4px 0 8px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:2px;
margin-right:5px;vertical-align:-2px}
svg text{font-size:11px;fill:var(--muted)}
.tri td{min-width:58px}
.controls{display:flex;gap:8px;align-items:center;margin-bottom:10px;flex-wrap:wrap}
.controls button{border:1px solid var(--line);background:#fff;border-radius:6px;
padding:4px 10px;cursor:pointer;font:inherit}
.controls button.on{background:var(--earned);color:#fff;border-color:var(--earned)}
.controls input{border:1px solid var(--line);border-radius:6px;padding:5px 9px;
font:inherit;min-width:220px}
.pill{padding:1px 7px;border-radius:8px;font-size:12px;font-weight:600}
.pill.approve{background:#e3f4ea;color:var(--good)}
.pill.deny{background:#fbe5e3;color:var(--bad)}
.pill.refer{background:#fff3cd;color:#7a5b00}
.scroll{max-height:460px;overflow:auto}
footer{color:var(--muted);font-size:12px;margin-top:20px}
"""

SCRIPT = """
(function(){
  var rows=[].slice.call(document.querySelectorAll('#claims tbody tr'));
  var buttons=[].slice.call(document.querySelectorAll('#claim-filter button'));
  var search=document.getElementById('claim-search');
  var count=document.getElementById('claim-count');
  var decision='all';
  function apply(){
    var q=search.value.trim().toLowerCase(),shown=0;
    rows.forEach(function(r){
      var ok=(decision==='all'||r.dataset.decision===decision)&&
        (!q||r.textContent.toLowerCase().indexOf(q)!==-1);
      r.style.display=ok?'':'none';if(ok){shown++;}
    });
    count.textContent=shown+' of '+rows.length+' claims';
  }
  buttons.forEach(function(b){b.addEventListener('click',function(){
    decision=b.dataset.decision;
    buttons.forEach(function(x){x.classList.toggle('on',x===b);});
    apply();
  });});
  search.addEventListener('input',apply);
  apply();
})();
"""


def eur(cents: int | float) -> str:
    value = float(cents) / 100
    sign = "−" if value < 0 else ""
    return f"{sign}€{abs(value):,.2f}"


def eur_short(cents: int | float) -> str:
    value = float(cents) / 100
    sign = "−" if value < 0 else ""
    value = abs(value)
    if value >= 1_000_000:
        return f"{sign}€{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{sign}€{value / 1_000:.1f}k"
    return f"{sign}€{value:.0f}"


def pct(ratio: float) -> str:
    return f"{ratio * 100:.1f}%"


def _kpi(label: str, value: str, sub: str = "", tone: str = "") -> str:
    tone_class = f" {tone}" if tone else ""
    sub_html = f'<div class="sub">{escape(sub)}</div>' if sub else ""
    return (
        f'<div class="kpi"><div class="label">{escape(label)}</div>'
        f'<div class="value{tone_class}">{escape(value)}</div>{sub_html}</div>'
    )


def _kpis(book: dict[str, Any]) -> str:
    combined = float(book["combined_ratio"])
    result = int(book["underwriting_result_cents"])
    ibnr = int(book["ibnr_cents"])
    true_ibnr = int(book["true_ibnr_cents"])
    ibnr_error = (ibnr - true_ibnr) / true_ibnr if true_ibnr else 0.0
    cards = [
        _kpi(
            "Gross written premium",
            eur_short(book["gwp_cents"]),
            f"{book['policies_written']} policies written",
        ),
        _kpi(
            "Earned premium",
            eur_short(book["earned_premium_cents"]),
            f"{eur_short(book['unearned_premium_cents'])} still unearned",
        ),
        _kpi(
            "Loss ratio",
            pct(float(book["loss_ratio"])),
            f"{eur_short(book['incurred_losses_cents'])} incurred",
        ),
        _kpi(
            "Expense ratio",
            pct(float(book["expense_ratio"])),
            f"LAE {pct(float(book['lae_ratio']))}",
        ),
        _kpi(
            "Combined ratio",
            pct(combined),
            "below 100% = underwriting profit",
            "good" if combined < 1 else "bad",
        ),
        _kpi(
            "Underwriting result",
            eur(result),
            "earned − losses − LAE − expenses",
            "good" if result >= 0 else "bad",
        ),
        _kpi(
            "IBNR (chain ladder)",
            eur(ibnr),
            f"true {eur(true_ibnr)} ({ibnr_error:+.1%})",
        ),
        _kpi(
            "Policies in force",
            f"{book['policies_in_force_end']:,}",
            f"{book['cancellations']} cancelled, {book['declined']} declined",
        ),
    ]
    return f'<div class="kpis">{"".join(cards)}</div>'


def _monthly_chart(monthly: list[dict[str, Any]]) -> str:
    if not monthly:
        return "<p class='hint'>No months simulated.</p>"
    left, right = CHART_PAD["left"], CHART_PAD["right"]
    top, bottom = CHART_PAD["top"], CHART_PAD["bottom"]
    plot_w = MONTH_WIDTH - left - right
    plot_h = MONTH_HEIGHT - top - bottom
    money_max = max(
        max(int(m["gwp_cents"]), int(m["earned_premium_cents"])) for m in monthly
    )
    money_max = max(money_max, 1)
    ratio_values = [float(m["loss_ratio"]) for m in monthly] + [
        float(m["loss_ratio_ytd"]) for m in monthly
    ]
    ratio_max = max(1.5, math.ceil(max(ratio_values) * 2) / 2)
    ratio_step = 0.25 if ratio_max <= 2 else 0.5
    grid_steps = round(ratio_max / ratio_step)
    slot = plot_w / len(monthly)
    bar_w = slot * 0.32
    parts: list[str] = [
        f'<svg viewBox="0 0 {MONTH_WIDTH} {MONTH_HEIGHT}" width="100%" '
        'role="img" aria-label="Monthly premium and loss ratio">'
    ]
    for step in range(grid_steps + 1):
        y = top + plot_h - plot_h * step / grid_steps
        parts.append(
            f'<line x1="{left}" x2="{left + plot_w}" y1="{y:.1f}" y2="{y:.1f}" '
            'stroke="#eef1f4"/>'
        )
        parts.append(
            f'<text x="{left - 6}" y="{y + 4:.1f}" text-anchor="end">'
            f"{escape(eur_short(money_max * step / grid_steps))}</text>"
        )
        parts.append(
            f'<text x="{left + plot_w + 6}" y="{y + 4:.1f}">'
            f"{ratio_step * step * 100:.0f}%</text>"
        )
    ref_y = top + plot_h - plot_h * (1.0 / ratio_max)
    parts.append(
        f'<line x1="{left}" x2="{left + plot_w}" y1="{ref_y:.1f}" y2="{ref_y:.1f}" '
        'stroke="#c0392b" stroke-dasharray="2 4" stroke-opacity=".5"/>'
    )
    monthly_points: list[str] = []
    ytd_points: list[str] = []
    for index, month in enumerate(monthly):
        x0 = left + slot * index + slot * 0.18
        for offset, key, color in (
            (0.0, "gwp_cents", "var(--gwp)"),
            (bar_w, "earned_premium_cents", "var(--earned)"),
        ):
            value = int(month[key])
            height = plot_h * value / money_max
            parts.append(
                f'<rect x="{x0 + offset:.1f}" y="{top + plot_h - height:.1f}" '
                f'width="{bar_w:.1f}" height="{height:.1f}" fill="{color}">'
                f"<title>{escape(month['month'])} {key.split('_')[0]}: "
                f"{escape(eur(value))}</title></rect>"
            )
        center = left + slot * index + slot / 2
        parts.append(
            f'<text x="{center:.1f}" y="{MONTH_HEIGHT - 14}" text-anchor="middle">'
            f"{escape(str(month['month'])[2:])}</text>"
        )
        lr_y = top + plot_h - plot_h * float(month["loss_ratio"]) / ratio_max
        ytd_y = top + plot_h - plot_h * float(month["loss_ratio_ytd"]) / ratio_max
        monthly_points.append(f"{center:.1f},{lr_y:.1f}")
        ytd_points.append(f"{center:.1f},{ytd_y:.1f}")
        parts.append(
            f'<circle cx="{center:.1f}" cy="{lr_y:.1f}" r="3" fill="var(--loss)">'
            f"<title>{escape(month['month'])} loss ratio "
            f"{pct(float(month['loss_ratio']))}</title></circle>"
        )
    parts.append(
        f'<polyline points="{" ".join(monthly_points)}" fill="none" '
        'stroke="var(--loss)" stroke-width="2"/>'
    )
    parts.append(
        f'<polyline points="{" ".join(ytd_points)}" fill="none" '
        'stroke="var(--ink)" stroke-width="1.5" stroke-dasharray="5 4"/>'
    )
    parts.append("</svg>")
    legend = (
        '<div class="legend"><span><i style="background:var(--gwp)"></i>GWP</span>'
        '<span><i style="background:var(--earned)"></i>Earned premium</span>'
        '<span><i style="background:var(--loss)"></i>Monthly loss ratio</span>'
        '<span><i style="background:var(--ink)"></i>Year-to-date loss ratio</span>'
        '<span><i style="background:#c0392b;opacity:.5"></i>100% loss ratio</span>'
        "</div>"
    )
    return legend + "".join(parts)


def _monthly_table(monthly: list[dict[str, Any]]) -> str:
    header = (
        "<tr><th class='l'>Month</th><th>GWP</th><th>Earned</th><th>Incurred</th>"
        "<th>Paid</th><th>Reported</th><th>Approved</th><th>Denied</th>"
        "<th>Referred</th><th>In force</th><th>LR</th><th>LR YTD</th></tr>"
    )
    rows = "".join(
        "<tr>"
        f"<td class='l'>{escape(str(m['month']))}</td>"
        f"<td>{eur(m['gwp_cents'])}</td>"
        f"<td>{eur(m['earned_premium_cents'])}</td>"
        f"<td>{eur(m['incurred_losses_cents'])}</td>"
        f"<td>{eur(m['paid_losses_cents'])}</td>"
        f"<td>{m['claims_reported']}</td><td>{m['claims_approved']}</td>"
        f"<td>{m['claims_denied']}</td><td>{m['claims_referred']}</td>"
        f"<td>{m['policies_in_force']}</td>"
        f"<td>{pct(float(m['loss_ratio']))}</td>"
        f"<td>{pct(float(m['loss_ratio_ytd']))}</td>"
        "</tr>"
        for m in monthly
    )
    return f"<div class='scroll'><table>{header}{rows}</table></div>"


def _segments(segments: list[dict[str, Any]]) -> str:
    if not segments:
        return "<p class='hint'>No segments.</p>"
    max_ratio = max(max(float(s["loss_ratio"]) for s in segments), 1.0)
    header = (
        "<tr><th class='l'>Factor</th><th class='l'>Level</th><th>Policies</th>"
        "<th>Earned</th><th>Incurred</th><th class='l'>Loss ratio</th>"
        "<th>Priced factor</th><th>True factor</th><th>Mispricing</th></tr>"
    )
    rows: list[str] = []
    for segment in segments:
        ratio = float(segment["loss_ratio"])
        priced = float(segment["priced_factor"])
        true = float(segment["true_factor"])
        mispricing = true / priced - 1 if priced else 0.0
        tone = "bad" if mispricing > 0.05 else "good" if mispricing < -0.05 else ""
        rows.append(
            "<tr>"
            f"<td class='l'>{escape(str(segment['factor']))}</td>"
            f"<td class='l'>{escape(str(segment['level']))}</td>"
            f"<td>{segment['policies']}</td>"
            f"<td>{eur(segment['earned_premium_cents'])}</td>"
            f"<td>{eur(segment['incurred_losses_cents'])}</td>"
            f"<td class='l'><span class='bar' style='width:"
            f"{120 * ratio / max_ratio:.0f}px'></span> {pct(ratio)}</td>"
            f"<td>{priced:.2f}</td><td>{true:.2f}</td>"
            f"<td class='{tone}'>{mispricing:+.0%}</td>"
            "</tr>"
        )
    return f"<table>{header}{''.join(rows)}</table>"


def _triangle(triangle: dict[str, Any]) -> str:
    origins: list[str] = list(triangle.get("origins", []))
    values: list[list[int]] = list(triangle.get("values_cents", []))
    if not origins:
        note = triangle.get("note") or "No reported claims yet."
        return f"<p class='hint'>{escape(str(note))}</p>"
    dev_count = max((len(row) for row in values), default=0)
    peak = max((max(row) for row in values if row), default=0) or 1
    header = (
        "<tr><th class='l'>Accident month</th>"
        + "".join(f"<th>Dev {dev}</th>" for dev in range(dev_count))
        + "<th>Ultimate</th><th>IBNR</th></tr>"
    )
    rows: list[str] = []
    ultimates = list(triangle.get("ultimate_cents", []))
    ibnrs = list(triangle.get("ibnr_cents", []))
    for index, origin in enumerate(origins):
        row = values[index] if index < len(values) else []
        cells = []
        for dev in range(dev_count):
            if dev < len(row):
                alpha = 0.08 + 0.55 * row[dev] / peak
                cells.append(
                    f"<td style='background:rgba(47,93,138,{alpha:.2f})'>"
                    f"{eur_short(row[dev])}</td>"
                )
            else:
                cells.append("<td></td>")
        ultimate = ultimates[index] if index < len(ultimates) else 0
        ibnr = ibnrs[index] if index < len(ibnrs) else 0
        rows.append(
            f"<tr><td class='l'>{escape(origin)}</td>{''.join(cells)}"
            f"<td>{eur_short(ultimate)}</td><td>{eur_short(ibnr)}</td></tr>"
        )
    ldfs = list(triangle.get("ldf", []))
    ldf_cells = "".join(
        f"<td>{ldfs[dev]:.3f}</td>" if dev < len(ldfs) else "<td></td>"
        for dev in range(dev_count)
    )
    rows.append(
        f"<tr><th class='l'>Age-to-age factor</th>{ldf_cells}<td></td><td></td></tr>"
    )
    return (
        f"<div class='scroll'><table class='tri'>{header}{''.join(rows)}</table></div>"
    )


def _claims_by_reason(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<p class='hint'>No claims reported.</p>"
    peak = max(int(item["count"]) for item in items) or 1
    rows = "".join(
        "<tr>"
        f"<td class='l'><span class='pill {escape(str(item['decision']))}'>"
        f"{escape(str(item['decision']))}</span></td>"
        f"<td class='l'>{escape(str(item['clause']))}</td>"
        f"<td class='l'><span class='bar' style='background:var(--earned);width:"
        f"{160 * int(item['count']) / peak:.0f}px'></span> {item['count']}</td>"
        "</tr>"
        for item in items
    )
    header = "<tr><th class='l'>Decision</th><th class='l'>Clause</th>"
    header += "<th class='l'>Claims</th></tr>"
    return f"<table>{header}{rows}</table>"


def _capital(capital: dict[str, Any]) -> str:
    net = capital.get("net_of_quota_share", {})
    rows = [
        ("Expected annual loss", "expected_loss_cents"),
        ("1-in-200 loss (99.5%)", "p995_loss_cents"),
        ("SCR proxy (99.5% − mean)", "scr_proxy_cents"),
    ]
    body = "".join(
        f"<tr><td class='l'>{label}</td><td>{eur(capital.get(key, 0))}</td>"
        f"<td>{eur(net.get(key, 0))}</td></tr>"
        for label, key in rows
    )
    body += (
        f"<tr><td class='l'>1-in-20 loss (95%)</td>"
        f"<td>{eur(capital.get('p95_loss_cents', 0))}</td><td></td></tr>"
    )
    cession = float(net.get("cession", 0))
    commission = float(net.get("commission", 0))
    header = (
        "<tr><th class='l'>Metric</th><th>Gross</th>"
        f"<th>Net of {cession:.0%} quota share</th></tr>"
    )
    return (
        f"<table>{header}{body}</table>"
        f"<p class='hint' style='margin-top:10px'>{capital.get('years', 0):,} "
        f"simulated years. The reinsurer pays a {commission:.0%} ceding commission "
        "on ceded premium.</p>"
    )


def _trial_balance(rows: list[dict[str, Any]]) -> str:
    debit = sum(int(row["debit_cents"]) for row in rows)
    credit = sum(int(row["credit_cents"]) for row in rows)
    body = "".join(
        f"<tr><td class='l'>{escape(str(row['account']))}</td>"
        f"<td>{eur(row['debit_cents'])}</td><td>{eur(row['credit_cents'])}</td>"
        f"<td>{eur(row['balance_cents'])}</td></tr>"
        for row in rows
    )
    tone = "good" if debit == credit else "bad"
    body += (
        f"<tr class='total'><td class='l'>Total</td><td>{eur(debit)}</td>"
        f"<td>{eur(credit)}</td><td class='{tone}'>"
        f"{'balanced' if debit == credit else 'out of balance'}</td></tr>"
    )
    header = "<tr><th class='l'>Account</th><th>Debits</th><th>Credits</th>"
    header += "<th>Balance</th></tr>"
    return f"<table>{header}{body}</table>"


def _claims(sample: list[dict[str, Any]]) -> str:
    buttons = "".join(
        f"<button type='button' data-decision='{value}'"
        f"{' class=on' if value == 'all' else ''}>{label}</button>"
        for value, label in (
            ("all", "All"),
            ("approve", "Approved"),
            ("deny", "Denied"),
            ("refer", "Referred"),
        )
    )
    controls = (
        f"<div class='controls'><span id='claim-filter'>{buttons}</span>"
        "<input id='claim-search' type='search' "
        "placeholder='Search claim, policy, cause, reason…'>"
        "<span id='claim-count' class='hint'></span></div>"
    )
    header = (
        "<thead><tr><th class='l'>Claim</th><th class='l'>Policy</th>"
        "<th class='l'>Cause</th><th>Loss</th><th>Notified</th><th>Claimed</th>"
        "<th>Paid</th><th class='l'>Decision</th><th class='l'>Clauses</th>"
        "<th class='l'>Adjuster</th><th class='l'>Fraud (truth)</th>"
        "<th class='l'>Reason</th></tr></thead>"
    )
    rows = "".join(
        f"<tr data-decision='{escape(str(claim['decision']))}'>"
        f"<td class='l'>{escape(str(claim['claim_id']))}</td>"
        f"<td class='l'>{escape(str(claim['policy_id']))}</td>"
        f"<td class='l'>{escape(str(claim['cause']))}</td>"
        f"<td>{escape(str(claim['loss_date']))}</td>"
        f"<td>{escape(str(claim['notified']))}</td>"
        f"<td>{eur(claim['claimed_cents'])}</td><td>{eur(claim['paid_cents'])}</td>"
        f"<td class='l'><span class='pill {escape(str(claim['decision']))}'>"
        f"{escape(str(claim['decision']))}</span></td>"
        f"<td class='l'>{escape(', '.join(claim.get('clause_ids', [])))}</td>"
        f"<td class='l'>{escape(str(claim['adjuster']))}</td>"
        f"<td class='l'>{'yes' if claim.get('fraud_truth') else ''}</td>"
        f"<td class='wrap'>{escape(str(claim['reason']))}</td>"
        "</tr>"
        for claim in sorted(
            sample, key=lambda item: (str(item["notified"]), str(item["claim_id"]))
        )
    )
    return (
        f"{controls}<div class='scroll'><table id='claims'>{header}"
        f"<tbody>{rows}</tbody></table></div>"
    )


def render_report(results: dict[str, Any]) -> str:
    params = results["params"]
    book = results["book"]
    title = f"Agent Spend Cover · {params.get('product', '')}"
    subtitle = (
        f"{params.get('agents', 0):,} agents · {params.get('months', 0)} months "
        f"from {params.get('start', '')} · seed {params.get('seed', '')}"
    )
    params_json = escape(json.dumps(params, sort_keys=True))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<header>
<h1>{escape(title)}<span class="badge">Synthetic sandbox data</span></h1>
<p>{escape(subtitle)}</p>
</header>
{_kpis(book)}
<section>
<h2>Premium and loss ratio by month</h2>
<p class="hint">Premium is written at bind and earned day by day over the term,
so earned premium trails GWP in a growing book. Early monthly loss ratios are
noisy because little premium has been earned.</p>
{_monthly_chart(results.get("monthly", []))}
{_monthly_table(results.get("monthly", []))}
</section>
<section>
<h2>Pricing vs experience by segment</h2>
<p class="hint">Priced factors come from the product file; true factors drive the
simulator. A segment priced below its true risk shows a higher loss ratio.</p>
{_segments(results.get("segments", []))}
</section>
<section>
<h2>Reported-incurred loss triangle</h2>
<p class="hint">Cumulative reported incurred losses by accident month and months of
development. Chain ladder projects the latest diagonal to ultimate; the gap is
IBNR.</p>
{_triangle(results.get("triangle", {}))}
</section>
<div class="grid2">
<section>
<h2>Claim decisions</h2>
<p class="hint">Rules adjuster outcome by policy clause or exclusion.</p>
{_claims_by_reason(results.get("claims_by_reason", []))}
</section>
<section>
<h2>Capital</h2>
<p class="hint">Aggregate annual losses for the in-force book from Monte Carlo
years.</p>
{_capital(results.get("capital", {}))}
</section>
</div>
<section>
<h2>Claims sample</h2>
<p class="hint">Claims from the simulation with the adjuster's decision and reason.
"Fraud (truth)" is known only to the simulator.</p>
{_claims(results.get("claims_sample", []))}
</section>
<section>
<h2>Trial balance</h2>
<p class="hint">Every journal entry balances, so total debits equal total
credits.</p>
{_trial_balance(results.get("trial_balance", []))}
</section>
<footer>Generated by <code>insurer report</code> from <code>{params_json}</code>.
</footer>
</main>
<script>{SCRIPT}</script>
</body>
</html>
"""


def write_report(results_path: str | Path, out: str | Path) -> None:
    results = json.loads(Path(results_path).read_text(encoding="utf-8"))
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_report(results), encoding="utf-8")
