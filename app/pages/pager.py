"""CFA Pager page (public, read-only): pager messages from Mazzanet.

Three views of the same stored messages:
- **Escalations**: jobs with a make-up request (MAKE TANKERS 5) or appliances
  requested by type (TANKER TRAWT1 REQUIRED), showing where the two coincide.
- **Jobs**: every F-number active in the window, one row each. Clicking a job
  opens its full message history below.
- **Message log**: every message as received, escalations highlighted.

Collection is always-on and managed from the Admin page.
"""
import pandas as pd
from dash import Input, Output, State, dash_table, dcc, html

from app import ui
from app.collector import manager
from app.modules.pager import data as pager_data

WINDOWS = [(1, "Last hour"), (6, "Last 6 hours"), (24, "Last 24 hours"),
           (72, "Last 3 days"), (168, "Last 7 days")]
ESC_COLOUR = "#d62728"

ESC_COLUMNS = [("f_number", "F-number"), ("brigade", "Brigade"),
               ("incident_type", "Type"), ("escalation", "Escalation"),
               ("make_matched", "MAKE + paged"), ("first_sent", "Started"),
               ("last_sent", "Last page")]
JOB_COLUMNS = [("f_number", "F-number"), ("brigade", "Brigade"),
               ("incident_type", "Type"), ("priority", "Priority"),
               ("messages", "Msgs"), ("escalation", "Escalation"),
               ("first_sent", "Started"), ("last_sent", "Last page"),
               ("first_message", "First message")]
LOG_COLUMNS = [("sent_at", "Time"), ("capcode", "Capcode"), ("alias", "Alias"),
               ("f_number", "F-number"), ("message", "Message"),
               ("escalation", "Escalation")]
DETAIL_COLUMNS = [("sent_at", "Time"), ("capcode", "Capcode"),
                  ("alias", "Alias"), ("message", "Message"),
                  ("escalation", "Escalation")]


def _table(id_, page_size=15):
    return dash_table.DataTable(
        id=id_, page_size=page_size, filter_action="native",
        sort_action="native",
        style_cell_conditional=[{"if": {"column_id": c},
                                 "whiteSpace": "normal", "minWidth": "260px"}
                                for c in ("message", "first_message")])


def layout():
    return html.Div([
        html.H2("CFA Pager"),
        html.Div([
            html.Div([
                html.H4("Collector"),
                html.Div(id="pager-collector-status"),
                html.Div("CFA pager messages from mazzanet.net.au, logged "
                         "every few minutes.", className="muted",
                         style={"marginTop": "8px", "fontSize": "12px"}),
            ], className="panel"),
            html.Div([
                html.H4("Filters"),
                dcc.Dropdown(id="pager-window", clearable=False, value=24,
                             options=[{"label": l, "value": h}
                                      for h, l in WINDOWS],
                             className="dropdown"),
                dcc.Checklist(
                    id="pager-esc-only",
                    options=[{"label": " Escalations only (jobs + log)",
                              "value": "yes"}],
                    value=[], style={"marginTop": "8px"}),
            ], className="panel"),
        ], className="panel-row"),
        dcc.Interval(id="pager-interval", interval=60_000, n_intervals=0),
        dcc.Store(id="pager-selected-job"),
        html.Div(id="pager-summary", className="muted", style={"margin": "10px 0"}),
        html.Div(id="pager-kpis", className="kpi-row"),

        html.H3("Escalations", style={"marginTop": "16px"}),
        html.Div("Jobs with a MAKE request (e.g. MAKE TANKERS 5) or tankers, "
                 "pumpers or ultralights requested by call sign. “MAKE + paged” "
                 "lists the appliance types where both happened on the same "
                 "job. Click a row for the job's messages.",
                 className="muted", style={"fontSize": "12px",
                                           "marginBottom": "6px"}),
        _table("pager-esc-table", page_size=10),

        html.H3("Jobs by F-number", style={"marginTop": "16px"}),
        _table("pager-jobs-table"),

        html.Div(id="pager-job-detail", style={"marginTop": "16px"}),

        html.H3("Message log", style={"marginTop": "16px"}),
        _table("pager-log-table", page_size=25),
    ])


def _fmt_ts(series):
    return pd.to_datetime(series, errors="coerce").dt.strftime("%d %b %H:%M:%S")


def _records(df, columns, ts_cols=(), id_col=None):
    if df is None or df.empty:
        return []
    out = df.copy()
    for c in ts_cols:
        out[c] = _fmt_ts(out[c])
    keep = [c for c, _ in columns]
    recs = out[keep].astype(object).where(out[keep].notna(), None)
    recs = recs.to_dict("records")
    # The highlight rule tests {escalation} != '' — None would match it.
    for r in recs:
        for c in ("escalation", "make_matched"):
            if c in r and r[c] is None:
                r[c] = ""
    if id_col:
        for r, key in zip(recs, df[id_col]):
            r["id"] = key
    return recs


def _cols(columns):
    return [{"name": n, "id": c} for c, n in columns]


def _esc_row_style(dark):
    return [{"if": {"filter_query": "{escalation} != ''"},
             "backgroundColor": "#3a1f22" if dark else "#fdecea"},
            {"if": {"filter_query": "{make_matched} != ''",
                    "column_id": "make_matched"},
             "color": ESC_COLOUR, "fontWeight": "bold"}]


def register_callbacks(app):
    @app.callback(
        Output("pager-collector-status", "children"),
        Input("pager-interval", "n_intervals"))
    def collector_status(_):
        s = manager.status()["pager"]
        parts = [html.Strong("Status: "), ui.status_pill(s["running"])]
        if s.get("last_run"):
            parts.append(html.Span(
                f" — last cycle {s['last_run']} ({s.get('runs', 0)} total)"))
        if s.get("last_error"):
            parts.append(html.Div(f"⚠ {s['last_error']}", className="error-text",
                                  style={"marginTop": "4px"}))
        return html.Div(parts)

    table_outputs = []
    for tid in ("pager-esc-table", "pager-jobs-table", "pager-log-table"):
        table_outputs += [Output(tid, "data"), Output(tid, "columns"),
                          Output(tid, "style_table"), Output(tid, "style_cell"),
                          Output(tid, "style_header"), Output(tid, "style_data"),
                          Output(tid, "style_data_conditional")]

    @app.callback(
        Output("pager-summary", "children"),
        Output("pager-kpis", "children"),
        *table_outputs,
        Input("pager-interval", "n_intervals"),
        Input("pager-window", "value"),
        Input("pager-esc-only", "value"),
        Input("theme-store", "data"))
    def refresh(_, hours, esc_only, dark):
        dark = bool(dark)
        hours = int(hours or 24)
        esc_only = bool(esc_only)
        st = ui.table_styles(dark)
        style = (st["style_table"], st["style_cell"],
                 st.get("style_header", {}), st.get("style_data", {}),
                 _esc_row_style(dark))

        c = pager_data.counts(hours)
        kpis = [
            ui.kpi_card("Messages", f"{c['messages']:,}"),
            ui.kpi_card("Jobs (F-numbers)", f"{c['jobs']:,}"),
            ui.kpi_card("Escalated jobs", str(c["esc_jobs"]),
                        ESC_COLOUR if c["esc_jobs"] else None),
            ui.kpi_card("Escalation pages", str(c["esc_messages"]),
                        ESC_COLOUR if c["esc_messages"] else None),
        ]

        jobs = pager_data.jobs(hours)
        esc = jobs[jobs["escalated"]] if not jobs.empty else jobs
        shown_jobs = esc if esc_only else jobs
        log_df = pager_data.recent_messages(hours, escalations_only=esc_only)
        ts = ("first_sent", "last_sent")

        cycles, last_hb = pager_data.heartbeat_summary()
        label = dict(WINDOWS).get(hours, f"{hours} h").lower()
        summary = (f"{c['messages']:,} message(s) across {c['jobs']:,} job(s) "
                   f"in the {label}. ")
        summary += (f"Collector ran {cycles} cycle(s), last {last_hb}."
                    if cycles else "No collection cycles yet.")

        return (summary, kpis,
                _records(esc, ESC_COLUMNS, ts, "f_number"), _cols(ESC_COLUMNS),
                *style,
                _records(shown_jobs, JOB_COLUMNS, ts, "f_number"),
                _cols(JOB_COLUMNS), *style,
                _records(log_df, LOG_COLUMNS, ("sent_at",)), _cols(LOG_COLUMNS),
                *style)

    @app.callback(
        Output("pager-selected-job", "data"),
        Input("pager-esc-table", "active_cell"),
        Input("pager-jobs-table", "active_cell"),
        State("pager-selected-job", "data"),
        prevent_initial_call=True)
    def select_job(esc_cell, job_cell, current):
        from dash import ctx
        cell = esc_cell if ctx.triggered_id == "pager-esc-table" else job_cell
        return (cell or {}).get("row_id") or current

    @app.callback(
        Output("pager-job-detail", "children"),
        Input("pager-selected-job", "data"),
        Input("pager-interval", "n_intervals"),
        Input("theme-store", "data"))
    def job_detail(f_number, _, dark):
        if not f_number:
            return html.Div("Select a job above to see all of its messages.",
                            className="muted")
        msgs = pager_data.job_messages(f_number)
        if msgs.empty:
            return html.Div(f"No messages stored for {f_number}.",
                            className="muted")
        esc = msgs[msgs["is_escalation"] == 1]
        head = [html.H3(f"Job {f_number}"),
                html.Div(f"{msgs['message'].nunique()} distinct message(s) "
                         f"on {msgs['capcode'].nunique()} capcode(s), "
                         f"{_fmt_ts(msgs['sent_at']).iloc[0]} → "
                         f"{_fmt_ts(msgs['sent_at']).iloc[-1]}.",
                         className="muted")]
        if not esc.empty:
            text = pager_data.escalation_text(
                pager_data.summarise_escalation(esc))
            head.append(html.Div(f"Escalation: {text}",
                                 style={"color": ESC_COLOUR,
                                        "fontWeight": "bold",
                                        "margin": "6px 0"}))
        st = ui.table_styles(bool(dark))
        table = dash_table.DataTable(
            data=_records(msgs, DETAIL_COLUMNS, ("sent_at",)),
            columns=_cols(DETAIL_COLUMNS), page_size=30, sort_action="native",
            style_table=st["style_table"], style_cell=st["style_cell"],
            style_header=st.get("style_header", {}),
            style_data=st.get("style_data", {}),
            style_cell_conditional=[{"if": {"column_id": "message"},
                                     "whiteSpace": "normal",
                                     "minWidth": "320px"}],
            style_data_conditional=_esc_row_style(bool(dark))[:1])
        return html.Div(head + [table], className="panel")
