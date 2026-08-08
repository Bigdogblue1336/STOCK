"""Single-page Bloomberg-terminal-style dashboard for the portfolio monitor.

Read-only: renders whatever the latest `python -m portfolio.cli` run wrote to
portfolio.db. Run with:

    streamlit run dashboard/app.py [-- --db portfolio.db]

Nothing on this page recommends a trade -- it reports marks, P&L versus your
own targets/stops, IV/greek state, newly-listed strikes/expiries, and
Claude's factual news summaries. Purely informational.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
for p in (_REPO_ROOT, _THIS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import data as dash_data  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from portfolio import config as pconfig  # noqa: E402
from portfolio.decay import portfolio_decay_projection  # noqa: E402
from portfolio.market_data import find_contract  # noqa: E402
from portfolio.models import Position  # noqa: E402

SEVERITY_COLOR = {"high": "#ff4d4d", "warn": "#ffa500", "info": "#4db8ff"}
NEW_STRIKE_ACCENT = "#ffd700"

# ---------------------------------------------------------------------------
# Page setup / terminal-style CSS
# ---------------------------------------------------------------------------

st.set_page_config(page_title="Portfolio Monitor", page_icon="\U0001F4C8", layout="wide")

st.markdown(
    """
<style>
:root {
    --bg: #0b0e11;
    --panel: #12161c;
    --border: #262b33;
    --text: #d7dde3;
    --muted: #7d8794;
    --amber: #ffa500;
    --green: #3ddc84;
    --red: #ff4d4d;
    --cyan: #4db8ff;
    --yellow: #ffd700;
}
html, body, [class*="css"] {
    font-family: "Consolas", "Menlo", "Courier New", monospace !important;
}
.stApp { background-color: var(--bg); color: var(--text); }
.mono-table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
.mono-table th, .mono-table td { border-bottom: 1px solid var(--border); padding: 4px 8px; text-align: right; }
.mono-table th:first-child, .mono-table td:first-child { text-align: left; }
.mono-table th { color: var(--muted); font-weight: normal; text-transform: uppercase; font-size: 0.72rem; }
.status-tile { background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
    padding: 10px 14px; }
.status-tile .label { color: var(--muted); font-size: 0.7rem; text-transform: uppercase; }
.status-tile .value { font-size: 1.35rem; font-weight: bold; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 0.75rem; }
.pill-green { background: rgba(61,220,132,0.15); color: var(--green); }
.pill-yellow { background: rgba(255,215,0,0.15); color: var(--yellow); }
.pill-red { background: rgba(255,77,77,0.15); color: var(--red); }
.progress-bar-outer { background: #1c2128; border-radius: 4px; width: 100px; height: 10px; display: inline-block; }
.progress-bar-inner { height: 10px; border-radius: 4px; }
.alert-box { border: 1px solid var(--border); border-left: 4px solid; border-radius: 3px;
    padding: 6px 10px; margin-bottom: 6px; background: var(--panel); }
.new-badge { background: var(--yellow); color: #000; font-size: 0.65rem; padding: 0 5px;
    border-radius: 3px; font-weight: bold; margin-left: 4px; }
.held-row { background: rgba(255,165,0,0.12) !important; }
a { color: var(--cyan); }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pnl_color(value: float | None) -> str:
    if value is None:
        return "var(--muted)"
    return "var(--green)" if value >= 0 else "var(--red)"


def _dte_pill(dte: int | None) -> str:
    if dte is None:
        return "-"
    cls = "pill-green" if dte > 45 else ("pill-yellow" if dte > 7 else "pill-red")
    return f'<span class="pill {cls}">{dte}d</span>'


def _progress_bar(pct: float | None, color: str) -> str:
    if pct is None:
        return "-"
    clamped = max(0.0, min(100.0, pct))
    return (
        f'<div class="progress-bar-outer"><div class="progress-bar-inner" '
        f'style="width:{clamped:.0f}%; background:{color};"></div></div> {pct:.0f}%'
    )


def _fmt_money(v: float | None) -> str:
    return f"{v:,.2f}" if v is not None else "n/a"


def _fmt_pct(v: float | None, digits: int = 1) -> str:
    return f"{v:.{digits}f}%" if v is not None else "n/a"


# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------


def load_dashboard_data(db_path: str, requested_asof: str | None = None):
    if not dash_data.db_exists(db_path):
        return None

    conn = dash_data.connect_readonly(db_path)
    asof_date = requested_asof or dash_data.latest_asof_date(conn)
    if asof_date is None:
        return {"conn": conn, "asof_date": None}

    raw_positions = dash_data.load_positions(conn)
    positions = {pid: Position.from_dict(row) for pid, row in raw_positions.items()}

    return {
        "conn": conn,
        "asof_date": asof_date,
        "available_dates": dash_data.list_asof_dates(conn),
        "positions": positions,
        "valuations": dash_data.load_valuations(conn, asof_date),
        "allocations": dash_data.load_allocations(conn, asof_date),
        "portfolio_greeks": dash_data.load_portfolio_greeks(conn, asof_date),
        "iv_environment": dash_data.load_iv_environment(conn, asof_date),
        "macro_gate": dash_data.load_macro_gate(conn, asof_date),
        "news_analysis": dash_data.load_news_analysis(conn, asof_date),
        "alerts": dash_data.load_alerts(conn, asof_date),
        "chain_diffs": dash_data.load_chain_diffs(conn, asof_date),
    }


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def render_status_strip(d: dict) -> None:
    valuations = d["valuations"]
    greeks = d["portfolio_greeks"] or {}
    macro = d["macro_gate"]
    nav = sum(v["current_value"] or 0.0 for v in valuations)
    pnl = sum(v["unrealized_pnl"] or 0.0 for v in valuations)
    macro_score = f"{macro['composite_score']:.0f}" if macro and macro["composite_score"] is not None else "n/a"

    tiles = [
        ("NAV", f"${nav:,.0f}", "var(--text)"),
        ("P&L", f"${pnl:,.0f}", _pnl_color(pnl)),
        ("Positions", str(len(valuations)), "var(--text)"),
        ("Alerts", str(len(d["alerts"])), "var(--red)" if d["alerts"] else "var(--muted)"),
        ("Macro Score", macro_score, "var(--amber)"),
        ("Net Delta", f"{greeks.get('net_delta_shares', 0.0):,.0f}", "var(--text)"),
        ("Daily Theta", f"${greeks.get('daily_theta_dollars', 0.0):,.0f}", _pnl_color(greeks.get("daily_theta_dollars"))),
        ("As Of", d["asof_date"], "var(--muted)"),
    ]
    cols = st.columns(len(tiles))
    for col, (label, value, color) in zip(cols, tiles):
        col.markdown(
            f'<div class="status-tile"><div class="label">{label}</div>'
            f'<div class="value" style="color:{color}">{value}</div></div>',
            unsafe_allow_html=True,
        )


def render_alerts_panel(alerts: list[dict]) -> None:
    st.subheader("Active Alerts")
    if not alerts:
        st.markdown('<span style="color:var(--muted)">No alerts today.</span>', unsafe_allow_html=True)
        return
    for a in alerts:
        color = SEVERITY_COLOR.get(a["severity"], "var(--muted)")
        accent = NEW_STRIKE_ACCENT if a["alert_type"] in ("new_strikes", "new_expiry") else color
        driver_html = f' <span style="color:var(--muted)">[{a["driver"]}]</span>' if a.get("driver") else ""
        st.markdown(
            f'<div class="alert-box" style="border-left-color:{accent}">'
            f'<b style="color:{color}">[{a["severity"].upper()}]</b> '
            f'<span style="color:var(--muted)">{a["alert_type"]}</span> {a["message"]}{driver_html}</div>',
            unsafe_allow_html=True,
        )


def render_positions_grid(d: dict) -> None:
    st.subheader("Positions")
    positions = d["positions"]
    iv_env = d["iv_environment"]
    rows_html = []
    for v in d["valuations"]:
        pos = positions.get(v["position_id"])
        if pos is None:
            continue
        iv_row = iv_env.get(v["position_id"], {})
        label = f"{pos.strike:g} {pos.option_type}" if pos.is_option else "shares"
        expiry = pos.expiry or "-"
        delta_cell = f"{v['delta']:.2f}" if v["delta"] is not None else "-"
        theta_cell = f"{v['theta']:.2f}" if v["theta"] is not None else "-"
        vega_cell = f"{v['vega']:.2f}" if v["vega"] is not None else "-"
        iv_cell = f"{v['iv'] * 100:.1f}%" if v["iv"] is not None else "-"
        row = (
            "<tr>"
            f"<td>{pos.ticker}</td><td>{label}</td><td>{expiry}</td>"
            f"<td>{_dte_pill(v['dte'])}</td>"
            f"<td>{_fmt_money(pos.entry_price)}</td><td>{_fmt_money(v['mark'])}</td>"
            f"<td>{_fmt_money(v['current_value'])}</td>"
            f'<td style="color:{_pnl_color(v["unrealized_pnl"])}">{_fmt_money(v["unrealized_pnl"])}</td>'
            f'<td style="color:{_pnl_color(v["unrealized_pnl_pct"])}">{_fmt_pct(v["unrealized_pnl_pct"])}</td>'
            f"<td>{delta_cell}</td><td>{theta_cell}</td><td>{vega_cell}</td><td>{iv_cell}</td>"
            f"<td>{_progress_bar(v['progress_to_target_pct'], 'var(--green)')}</td>"
            f"<td>{_progress_bar(v['progress_to_stop_pct'], 'var(--red)')}</td>"
            "</tr>"
        )
        rows_html.append(row)

    header = (
        "<tr><th>Ticker</th><th>Type</th><th>Expiry</th><th>DTE</th><th>Entry</th><th>Mark</th>"
        "<th>Value</th><th>P&L $</th><th>P&L %</th><th>Delta</th><th>Theta/day</th><th>Vega</th>"
        "<th>IV</th><th>To Target</th><th>To Stop</th></tr>"
    )
    st.markdown(f'<table class="mono-table">{header}{"".join(rows_html)}</table>', unsafe_allow_html=True)


def render_theta_decay(d: dict) -> None:
    st.subheader("Theta Decay (illustrative -- constant spot & IV, not a forecast)")
    positions = d["positions"]
    valuations_by_id = {v["position_id"]: v for v in d["valuations"]}

    spot_by_ticker = {}
    for pos in positions.values():
        snap = dash_data.load_snapshot_on_or_before(d["conn"], pos.ticker, d["asof_date"])
        if snap:
            spot_by_ticker[pos.ticker] = snap["data"].get("spot", {}).get("mark")

    iv_by_position_id = {pid: v.get("iv") for pid, v in valuations_by_id.items()}

    rows = portfolio_decay_projection(
        list(positions.values()), spot_by_ticker, iv_by_position_id, _asof_date_obj(d["asof_date"])
    )
    if not rows:
        st.markdown('<span style="color:var(--muted)">No option positions with usable spot/IV to project.</span>', unsafe_allow_html=True)
        return

    by_position: dict[str, list[dict]] = {}
    for r in rows:
        by_position.setdefault(r["position_id"], []).append(r)

    fig_dollars = go.Figure()
    for position_id, curve in by_position.items():
        fig_dollars.add_trace(
            go.Scatter(
                x=[c["date"] for c in curve],
                y=[c["extrinsic_dollars"] for c in curve],
                stackgroup="one",
                name=position_id,
            )
        )
    fig_dollars.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0b0e11",
        plot_bgcolor="#0b0e11",
        height=320,
        margin=dict(l=10, r=10, t=30, b=10),
        title="Aggregate extrinsic value ($) by position, stacked to longest expiry",
    )
    st.plotly_chart(fig_dollars, use_container_width=True)

    fig_per_share = go.Figure()
    for position_id, curve in by_position.items():
        fig_per_share.add_trace(
            go.Scatter(
                x=[c["date"] for c in curve],
                y=[c["extrinsic_per_share"] for c in curve],
                mode="lines",
                name=position_id,
            )
        )
    fig_per_share.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0b0e11",
        plot_bgcolor="#0b0e11",
        height=280,
        margin=dict(l=10, r=10, t=30, b=10),
        title="Extrinsic value per share (BS, constant spot+IV)",
    )
    st.plotly_chart(fig_per_share, use_container_width=True)


def _asof_date_obj(asof_date: str):
    return datetime.strptime(asof_date, "%Y-%m-%d").date()


def render_strike_ladder(d: dict) -> None:
    st.subheader("Strike Ladder")
    positions = d["positions"]
    chain_diffs = d["chain_diffs"]

    for pos in positions.values():
        if not pos.is_option:
            continue
        snap = dash_data.load_snapshot_on_or_before(d["conn"], pos.ticker, d["asof_date"])
        if snap is None:
            continue
        expiry_chain = snap["data"].get("expiries", {}).get(pos.expiry)
        if not expiry_chain:
            continue
        side = "calls" if pos.option_type == "call" else "puts"
        rows = sorted(expiry_chain.get(side, []), key=lambda r: r["strike"])
        strikes = [r["strike"] for r in rows]
        if pos.strike not in strikes:
            continue
        idx = strikes.index(pos.strike)
        window = rows[max(0, idx - 6): idx + 7]

        new_strikes_for_expiry = set(chain_diffs.get(pos.ticker, {}).get("new_strikes", {}).get(pos.expiry, []))

        st.markdown(f"**{pos.ticker} {pos.expiry} {pos.option_type.upper()}** (held strike {pos.strike:g})")
        header = "<tr><th>Strike</th><th>Bid</th><th>Ask</th><th>Last</th><th>IV</th><th>Vol</th><th>OI</th></tr>"
        body = []
        for r in window:
            is_held = r["strike"] == pos.strike
            is_new = r["strike"] in new_strikes_for_expiry
            row_class = ' class="held-row"' if is_held else ""
            new_badge = ' <span class="new-badge">NEW</span>' if is_new else ""
            iv_str = f"{r['iv']*100:.1f}%" if r.get("iv") is not None else "-"
            body.append(
                f"<tr{row_class}><td>{r['strike']:g}{new_badge}</td><td>{_fmt_money(r.get('bid'))}</td>"
                f"<td>{_fmt_money(r.get('ask'))}</td><td>{_fmt_money(r.get('last'))}</td>"
                f"<td>{iv_str}</td><td>{r.get('volume') if r.get('volume') is not None else '-'}</td>"
                f"<td>{r.get('open_interest') if r.get('open_interest') is not None else '-'}</td></tr>"
            )
        st.markdown(f'<table class="mono-table">{header}{"".join(body)}</table>', unsafe_allow_html=True)


def render_allocation(d: dict) -> None:
    st.subheader("Allocation")
    alloc = d["allocations"]
    col1, col2 = st.columns(2)

    for col, dimension, title in ((col1, "ticker", "By Ticker"), (col2, "sector", "By Sector")):
        rows = alloc.get(dimension, [])
        fig = go.Figure(
            go.Bar(
                x=[r["key"] for r in rows],
                y=[r["pct_of_total"] for r in rows],
                marker_color=["#ff4d4d" if r["over_cap"] else "#ffa500" for r in rows],
            )
        )
        fig.update_layout(
            template="plotly_dark", paper_bgcolor="#0b0e11", plot_bgcolor="#0b0e11",
            height=260, margin=dict(l=10, r=10, t=30, b=10), title=title, yaxis_title="% of book",
        )
        col.plotly_chart(fig, use_container_width=True)
        for r in rows:
            if r["over_cap"]:
                col.markdown(
                    f'<span style="color:var(--red)">** {r["key"]} over cap: '
                    f'{r["pct_of_total"]:.1f}% (cap {r["cap_pct"]:.0f}%) **</span>',
                    unsafe_allow_html=True,
                )

    flagged = [n for n in d["news_analysis"] if n.get("position_flag")]
    if flagged:
        st.markdown("**Position-specific news flags**")
        for n in flagged:
            st.markdown(f"- **{n['ticker']}**: {n['position_flag_detail'] or n['summary']}")
            for h in n.get("headlines", []):
                if h.get("link"):
                    st.markdown(f"&nbsp;&nbsp;&nbsp;[{h['title']}]({h['link']})", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    st.sidebar.title("Portfolio Monitor")
    db_path = st.sidebar.text_input("Database path", value=pconfig.DEFAULT_DB_PATH)

    if not dash_data.db_exists(db_path):
        st.title("Portfolio Monitor")
        st.warning(f"No database found at `{db_path}`. Run `python -m portfolio.cli` first.")
        return

    conn_probe = dash_data.connect_readonly(db_path)
    latest = dash_data.latest_asof_date(conn_probe)
    if latest is None:
        st.title("Portfolio Monitor")
        st.warning("Database exists but has no valuation runs yet.")
        return
    available_dates = dash_data.list_asof_dates(conn_probe)
    asof_choice = st.sidebar.selectbox("As of date", available_dates, index=0)

    d = load_dashboard_data(db_path, requested_asof=asof_choice)

    st.title("Portfolio Monitor")
    render_status_strip(d)
    st.divider()
    render_alerts_panel(d["alerts"])
    st.divider()
    render_positions_grid(d)
    st.divider()
    render_theta_decay(d)
    st.divider()
    render_strike_ladder(d)
    st.divider()
    render_allocation(d)


main()
