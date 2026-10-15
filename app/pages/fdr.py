# Passive Monitor — Copyright (c) 2026 SirTophamMatt. All rights reserved.
"""Fire Danger Ratings — Intel Tool page (``/intel/fdr``).

Behind the Intel Tool's shared password, like the Operational Summary: every
callback re-checks ``intel.unlocked()`` server-side, because Dash serves every
callback whether or not its page was rendered.

Reads only what the FDR collector stored (``app.modules.fdr``) — opening the
page never fetches CFA. "Fetch now" runs one collection inline.
"""
import base64
import datetime
import logging

from dash import Input, Output, State, ctx, dash_table, dcc, html, no_update
from dash.exceptions import PreventUpdate

from app import ui
from app.config import load_config
from app.modules.fdr import model, resolve, store, workbook
from app.pages import intel

log = logging.getLogger(__name__)

PATH = "/intel/fdr"
FORECAST_DAYS = 4
SHORT = {"Mallee": "Mallee", "Wimmera": "Wimmera", "Northern Country": "N Country",
         "North Central": "N Central", "North East": "N East",
         "East Gippsland": "E Gipps", "West and South Gippsland": "W&S Gipps",
         "Central": "Central", "South West": "S West"}
STATE_MARK = {"preliminary": "·", "conflict": "⚠", "unrecognised": "?"}


def _today():
    return model.today()


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def layout():
    return html.Div([
        html.H2("Intel Tool"),
        html.Div(intel.body_for(PATH), id="intel-body"),
    ])


def body():
    t = _today()
    return html.Div([
        intel.tabs(PATH),
        dcc.Interval(id="fdr-tick", interval=60_000),
        html.Div([
            html.Div([
                html.H4("Fire Danger Ratings & Total Fire Bans",
                        style={"margin": "0 0 4px"}),
                html.Div(id="fdr-status", className="muted"),
            ], style={"flex": "1"}),
            html.Button("Fetch now", id="fdr-fetch", className="btn btn-primary"),
        ], className="panel", style={"display": "flex", "alignItems": "center",
                                     "gap": "12px"}),
        html.Div(id="fdr-fetch-msg", className="muted", style={"margin": "6px 0"}),

        html.Div([
            html.H4("Current forecast"),
            html.Div(id="fdr-forecast"),
            html.Div(["Source: ",
                      html.A("CFA — Total Fire Bans and Fire Danger Ratings",
                             href="https://www.cfa.vic.gov.au/warnings-restrictions/"
                                  "total-fire-bans-and-ratings",
                             target="_blank", rel="noopener"),
                      ". · = preliminary (not yet confirmed on the day) · "
                      "⚠ = sources disagree · ? = unrecognised value. Always check "
                      "CFA for the current official rating."],
                     className="muted", style={"marginTop": "6px"}),
        ], className="panel"),

        html.Div([
            html.H4("Season record"),
            html.Div([
                dcc.DatePickerRange(
                    id="fdr-range", display_format="DD/MM/YYYY",
                    start_date=(t - datetime.timedelta(days=20)).isoformat(),
                    end_date=t.isoformat()),
            ], className="stats-filters", style={"marginBottom": "8px"}),
            html.Div(id="fdr-season"),
        ], className="panel"),

        html.Div([
            html.H4("Revision history"),
            html.Div([
                dcc.DatePickerSingle(id="fdr-hist-date", date=t.isoformat(),
                                     display_format="DD/MM/YYYY"),
            ], className="stats-filters", style={"marginBottom": "8px"}),
            html.Div(id="fdr-history"),
        ], className="panel"),

        html.Div([
            html.H4("Needs review"),
            _static_table("fdr-flag-table", FLAG_COLS),
            html.Button("Mark selected resolved", id="fdr-flag-resolve", className="btn",
                        style={"marginTop": "8px"}),
            html.Span(id="fdr-flag-msg", className="muted", style={"marginLeft": "8px"}),
        ], className="panel"),

        html.Div([
            html.H4("Correct a record"),
            html.P("Corrections never change what the source published: they are "
                   "stored beside it with a reason, shown as corrections, and can be "
                   "revoked.", className="muted"),
            html.Div([
                dcc.DatePickerSingle(id="fdr-ovr-date", date=t.isoformat(),
                                     display_format="DD/MM/YYYY"),
                dcc.Dropdown(id="fdr-ovr-field", clearable=False, value="rating",
                             options=[{"label": "Rating", "value": "rating"},
                                      {"label": "Total Fire Ban", "value": "tfb"},
                                      {"label": "Multi Day (whole day)", "value": "multi"},
                                      {"label": "Comment (whole day)", "value": "comment"}],
                             style={"width": "200px"}),
                dcc.Dropdown(id="fdr-ovr-district", clearable=False, value=model.DISTRICTS[0],
                             options=[{"label": d, "value": d} for d in model.DISTRICTS]
                             + [{"label": "Whole day", "value": "*"}],
                             style={"width": "220px"}),
                dcc.Input(id="fdr-ovr-value", placeholder="e.g. High, High/Extreme, YES",
                          className="text-input"),
                dcc.Input(id="fdr-ovr-reason", placeholder="Reason (required)",
                          className="text-input", style={"minWidth": "260px"}),
                html.Button("Save correction", id="fdr-ovr-save", className="btn btn-primary"),
            ], className="stats-filters",
                style={"display": "flex", "gap": "8px", "flexWrap": "wrap",
                       "alignItems": "center"}),
            html.Div(id="fdr-ovr-msg", className="muted", style={"margin": "6px 0"}),
            _static_table("fdr-ovr-table", OVR_COLS),
            html.Button("Revoke selected", id="fdr-ovr-revoke", className="btn",
                        style={"marginTop": "8px"}),
        ], className="panel"),

        html.Div([
            html.H4("Workbook"),
            html.P(["Upload the FDR Daily Statistics workbook to see exactly which cells "
                    "would change, then download the filled copy. Only confirmed values "
                    "for days that have started are written; formulas, formatting, "
                    "dropdowns and anything typed by hand are left alone. ",
                    html.Code("[FDR]"), " marks this tool's part of a comment."],
                   className="muted"),
            dcc.Upload(html.Button("Choose workbook…", className="btn"),
                       id="fdr-upload", accept=".xlsx,.xlsm", max_size=30 * 1024 * 1024),
            html.Div(id="fdr-wb-plan", style={"marginTop": "10px"}),
            html.Button("Download filled workbook", id="fdr-wb-fill",
                        className="btn btn-primary", style={"marginTop": "8px"}),
            dcc.Download(id="fdr-wb-download"),
            html.Div(id="fdr-wb-msg", className="muted", style={"marginTop": "6px"}),
            html.Hr(),
            html.Div(id="fdr-wb-server"),
            html.Button("Update server workbook now", id="fdr-wb-server-run",
                        className="btn", style={"marginTop": "6px"}),
            html.Div(id="fdr-wb-server-msg", className="muted", style={"marginTop": "6px"}),
        ], className="panel"),
    ])


# --------------------------------------------------------------------------- #
# Renderers
# --------------------------------------------------------------------------- #
def _cell(dd, compact=False):
    """A coloured rating cell for one DistrictDay."""
    if not dd.ratings:
        txt = {"unrecognised": "?", "conflict": "⚠"}.get(dd.state, "—")
        return html.Td(txt, className="fdr-cell fdr-none",
                       title="; ".join(dd.notes) or "no rating collected")
    top = model.highest(dd.ratings)
    label = model.display(dd.ratings)
    mark = STATE_MARK.get(dd.state, "")
    tfb = " TFB" if dd.tfb == model.TFB_YES else ""
    tip = [f"{dd.district}: {label}", f"state: {dd.state}",
           f"source: {resolve.SOURCE_LABEL.get(dd.source, dd.source)}"]
    if dd.multi_detail:
        tip.append(dd.multi_detail)
    if dd.tfb:
        tip.append(f"Total Fire Ban: {dd.tfb} ({dd.tfb_state})")
    tip += dd.notes
    style = {"background": model.COLOURS[top], "color": model.TEXT_ON[top]}
    if dd.state == "preliminary":
        style["opacity"] = 0.75
        style["fontStyle"] = "italic"
    return html.Td(
        [html.Span((label if not compact else label.replace("No Rating", "NR"))
                   + mark),
         html.B(tfb, className="fdr-tfb") if tfb else None,
         html.Span(" M", className="fdr-multi") if dd.multi else None],
        className="fdr-cell", style=style, title="\n".join(tip))


def forecast_table(now=None):
    now = now or model.now()
    days = resolve.resolve_range(now.date(), now.date() + datetime.timedelta(
        days=FORECAST_DAYS - 1), now=now)
    head = html.Tr([html.Th("District")] + [
        html.Th(d.strftime("%a %d %b") + (" (today)" if d == now.date() else ""))
        for d in days])
    rows = [html.Tr([html.Th(dist, className="fdr-dist")] +
                    [_cell(rec.districts[dist]) for rec in days.values()])
            for dist in model.DISTRICTS]
    tfb = html.Tr([html.Th("Total Fire Ban")] + [
        html.Td(", ".join(rec.tfb_districts) if rec.tfb_any == "YES" else
                ("None declared" if rec.tfb_any == "NO" else "Not confirmed"),
                className="fdr-cell")
        for rec in days.values()])
    if not any(rec.districts[d].ratings for rec in days.values() for d in model.DISTRICTS):
        return html.Div("No forecast collected yet — use Fetch now, or check the "
                        "collector status above.", className="muted")
    return html.Table([html.Thead(head), html.Tbody(rows + [tfb])], className="fdr-grid")


def season_table(start, end, now=None):
    start, end = model.as_date(start), model.as_date(end)
    if (end - start).days > 400:
        return html.Div("Pick a range of at most 400 days.", className="error-text")
    days = resolve.resolve_range(start, end, now=now)
    head = html.Tr([html.Th("Date"), html.Th("TFB"), html.Th("Multi")] +
                   [html.Th(SHORT[d], title=d) for d in model.DISTRICTS] +
                   [html.Th("Review")])
    rows = []
    for d in sorted(days, reverse=True):
        rec = days[d]
        tfb = (", ".join(rec.tfb_districts) if rec.tfb_any == "YES" else
               "No" if rec.tfb_any == "NO" else "?")
        rows.append(html.Tr(
            [html.Td(d.strftime("%a %d/%m/%Y"), className="fdr-date"),
             html.Td(tfb, className="fdr-cell", title=tfb),
             html.Td({"YES": "Yes", "NO": "No"}.get(rec.multi, "?"),
                     className="fdr-cell",
                     title=rec.comment() or "")] +
            [_cell(rec.districts[x], compact=True) for x in model.DISTRICTS] +
            [html.Td("⚠ " + str(len(rec.review)) if rec.review else "",
                     title="\n".join(rec.review), className="fdr-cell")]))
    return html.Div(html.Table([html.Thead(head), html.Tbody(rows)],
                               className="fdr-grid fdr-season"),
                    style={"overflowX": "auto"})


FLAG_COLS = [("valid_date", "Date"), ("district", "District"), ("kind", "Kind"),
             ("detail", "Detail"), ("created_at", "Raised")]
OVR_COLS = [("valid_date", "Date"), ("district", "District"), ("field", "Field"),
            ("value", "Value"), ("reason", "Reason"), ("entered_at", "Entered")]


def _static_table(id_, columns):
    """A selectable table that exists from the first render (a callback whose
    State component is missing never fires); callbacks only replace its data."""
    styles = ui.table_styles(True)
    return dash_table.DataTable(
        id=id_, data=[], columns=[{"name": n, "id": c} for c, n in columns],
        row_selectable="multi", selected_rows=[], page_size=15,
        style_cell={**styles["style_cell"], "whiteSpace": "normal", "height": "auto"},
        **{k: v for k, v in styles.items() if k != "style_cell"})


def _table(df, id_, dark, columns, selectable=False):
    styles = ui.table_styles(dark)
    return dash_table.DataTable(
        id=id_, data=df.to_dict("records"),
        columns=[{"name": n, "id": c} for c, n in columns],
        row_selectable="multi" if selectable else None, page_size=15,
        style_cell={**styles["style_cell"], "whiteSpace": "normal", "height": "auto"},
        **{k: v for k, v in styles.items() if k != "style_cell"})


def history_tables(d, dark):
    fc, tf = resolve.history(d)
    if fc.empty and tf.empty:
        return html.Div("Nothing stored for this date.", className="muted")
    fc = fc.assign(current=fc["is_current"].map({1: "✓", 0: ""}))
    tf = tf.assign(current=tf["is_current"].map({1: "✓", 0: ""}))
    return html.Div([
        _table(fc, "fdr-hist-fc", dark, [
            ("district", "District"), ("source", "Source"), ("revision", "Rev"),
            ("rating", "Rating"), ("status", "Status"), ("raw_text", "As published"),
            ("retrieved_at", "First seen"), ("last_seen_at", "Last seen"),
            ("seen_count", "Reads"), ("current", "Current")]),
        html.Div(style={"height": "10px"}),
        _table(tf, "fdr-hist-tfb", dark, [
            ("district", "District"), ("source", "Source"), ("revision", "Rev"),
            ("declaration_status", "TFB"), ("basis", "Basis"), ("raw_text", "As published"),
            ("retrieved_at", "First seen"), ("last_seen_at", "Last seen"),
            ("current", "Current")]),
    ])


def status_line():
    from app.collector import manager
    st = manager.status().get("fdr", {})
    last = store.last_run()
    bits = ["Collector " + ("running" if st.get("running") else "stopped")]
    cfg = load_config()["fdr"]
    bits.append(f"every {cfg['interval_minutes']} min")
    if last:
        bits.append(f"last run #{last['run_id']} {last['status']} at "
                    f"{(last.get('completed_at') or last['started_at'])[:16].replace('T', ' ')}")
    ok = store.last_success()
    if ok:
        bits.append(f"last successful read {ok[:16].replace('T', ' ')}")
    if st.get("last_error"):
        bits.append("error: " + st["last_error"][:300])
    return " · ".join(bits)


def plan_view(res):
    if res.error:
        return html.Div(res.error, className="error-text")
    lay, p = res.layout, res.plan
    changes = [{"cell": c.cell, "date": c.valid_date.strftime("%d/%m/%Y"),
                "old": "" if c.old is None else str(c.old),
                "new": "" if c.new is None else str(c.new), "why": c.reason}
               for c in p.changes]
    blocked = [{"cell": c, "date": d.strftime("%d/%m/%Y"),
                "old": "" if o is None else str(o), "new": str(n), "why": w}
               for c, d, o, n, w in p.blocked]
    styles = ui.table_styles(True)
    cols = [{"name": n, "id": i} for i, n in
            [("cell", "Cell"), ("date", "Date"), ("old", "Now"), ("new", "Would write"),
             ("why", "Why")]]

    def tbl(rows):
        return dash_table.DataTable(data=rows, columns=cols, page_size=12,
                                    style_cell={**styles["style_cell"],
                                                "whiteSpace": "normal"},
                                    **{k: v for k, v in styles.items()
                                       if k != "style_cell"})
    out = [html.Ul([html.Li(x) for x in lay.describe()], className="muted"),
           html.B(f"{len(changes)} cell(s) to write")]
    if changes:
        out.append(tbl(changes))
    if blocked:
        out += [html.B(f"{len(blocked)} cell(s) left alone", style={"display": "block",
                                                                   "marginTop": "8px"}),
                tbl(blocked)]
    if p.no_data_summary():
        out.append(html.P(p.no_data_summary(), className="muted",
                          style={"marginTop": "8px"}))
    if p.review:
        out += [html.B(f"{len(p.review)} item(s) need a person",
                       style={"display": "block", "marginTop": "8px"}),
                html.Ul([html.Li(f"{d.strftime('%d/%m/%Y')}: {n}")
                         for d, n in p.review[:60]], className="muted")]
    return html.Div(out)


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def _guard():
    if not intel.unlocked():
        raise PreventUpdate


def _upload_bytes(contents):
    _, b64 = contents.split(",", 1)
    return base64.b64decode(b64)


def register_callbacks(app):
    @app.callback(
        Output("fdr-status", "children"),
        Output("fdr-forecast", "children"),
        Output("fdr-fetch-msg", "children"),
        Input("fdr-tick", "n_intervals"),
        Input("fdr-fetch", "n_clicks"))
    def refresh(_n, fetch):
        _guard()
        msg = no_update
        if ctx.triggered_id == "fdr-fetch" and fetch:
            from app.collector import manager
            _, msg = manager.fetch_fdr_now()
        return status_line(), forecast_table(), msg

    @app.callback(
        Output("fdr-season", "children"),
        Input("fdr-range", "start_date"),
        Input("fdr-range", "end_date"),
        Input("fdr-tick", "n_intervals"),
        Input("fdr-ovr-msg", "children"))
    def season(start, end, _n, _m):
        _guard()
        if not start or not end:
            raise PreventUpdate
        return season_table(start, end)

    @app.callback(
        Output("fdr-history", "children"),
        Input("fdr-hist-date", "date"),
        Input("fdr-tick", "n_intervals"),
        State("theme-store", "data"))
    def hist(d, _n, theme):
        _guard()
        if not d:
            raise PreventUpdate
        return history_tables(d, theme != "light")

    @app.callback(
        Output("fdr-flag-table", "data"),
        Output("fdr-flag-table", "selected_rows"),
        Output("fdr-flag-msg", "children"),
        Input("fdr-tick", "n_intervals"),
        Input("fdr-flag-resolve", "n_clicks"),
        State("fdr-flag-table", "selected_rows"),
        State("fdr-flag-table", "data"))
    def flags(_n, resolve_clicks, selected, data):
        _guard()
        msg = ""
        if ctx.triggered_id == "fdr-flag-resolve" and resolve_clicks and selected and data:
            for i in selected:
                store.resolve_flag(data[i]["id"])
            msg = f"{len(selected)} resolved."
        elif ctx.triggered_id == "fdr-tick":
            # Keep a selection made between refreshes.
            msg = no_update
        df = store.open_flags()
        if df.empty:
            return [], [], msg if msg is not no_update else "Nothing needs review."
        keep = selected if ctx.triggered_id == "fdr-tick" else []
        return df.to_dict("records"), keep or [], msg

    @app.callback(
        Output("fdr-ovr-msg", "children"),
        Output("fdr-ovr-table", "data"),
        Output("fdr-ovr-table", "selected_rows"),
        Input("fdr-ovr-save", "n_clicks"),
        Input("fdr-ovr-revoke", "n_clicks"),
        State("fdr-ovr-date", "date"),
        State("fdr-ovr-district", "value"),
        State("fdr-ovr-field", "value"),
        State("fdr-ovr-value", "value"),
        State("fdr-ovr-reason", "value"),
        State("fdr-ovr-table", "selected_rows"),
        State("fdr-ovr-table", "data"))
    def overrides(save, revoke, d, district, field, value, reason, selected, data):
        _guard()
        msg = ""
        if ctx.triggered_id == "fdr-ovr-save" and save:
            ok, msg = store.add_override(d, district, field, value, reason,
                                         entered_by="intel")
            if not ok:
                msg = "Not saved: " + msg
        elif ctx.triggered_id == "fdr-ovr-revoke" and revoke and selected and data:
            for i in selected:
                store.revoke_override(data[i]["id"])
            msg = f"{len(selected)} correction(s) revoked."
        df = store.overrides()
        if df.empty and not msg:
            msg = "No corrections in force."
        return msg, df.to_dict("records"), []

    @app.callback(
        Output("fdr-wb-plan", "children"),
        Input("fdr-upload", "contents"),
        State("fdr-upload", "filename"))
    def wb_plan(contents, filename):
        _guard()
        if not contents:
            raise PreventUpdate
        res, _ = workbook.fill_bytes(_upload_bytes(contents), filename or "workbook.xlsx",
                                     dry_run=True)
        return plan_view(res)

    @app.callback(
        Output("fdr-wb-download", "data"),
        Output("fdr-wb-msg", "children"),
        Input("fdr-wb-fill", "n_clicks"),
        State("fdr-upload", "contents"),
        State("fdr-upload", "filename"),
        prevent_initial_call=True)
    def wb_fill(n, contents, filename):
        _guard()
        if not n:
            raise PreventUpdate
        if not contents:
            return no_update, "Choose a workbook first."
        name = filename or "workbook.xlsx"
        res, data = workbook.fill_bytes(_upload_bytes(contents), name)
        if not res.ok:
            return no_update, "Not filled: " + (res.error or "unknown error")
        n_changes = len(res.plan.changes)
        return (dcc.send_bytes(data, name),
                f"{n_changes} cell(s) written; {len(res.plan.blocked)} left alone; "
                f"{len(res.plan.review)} need review.")

    @app.callback(
        Output("fdr-wb-server", "children"),
        Output("fdr-wb-server-msg", "children"),
        Input("fdr-wb-server-run", "n_clicks"),
        Input("fdr-tick", "n_intervals"))
    def wb_server(n, _t):
        _guard()
        cfg = load_config()["fdr"]
        path = cfg.get("workbook_path")
        info = (html.Div(["Server workbook: ", html.Code(path),
                          " — updated after every run" if cfg.get("auto_update_workbook")
                          else " — updated only on demand"], className="muted")
                if path else html.Div("No server workbook is configured "
                                      "(config fdr.workbook_path). Use the upload above, "
                                      "or the command-line tool on your own PC.",
                                      className="muted"))
        if ctx.triggered_id != "fdr-wb-server-run" or not n:
            return info, no_update
        if not path:
            return info, "Nothing to update."
        res = workbook.update_file(path)
        if not res.ok:
            return info, "Not updated: " + res.error
        s = res.summary()
        return info, (f"{s['changes']} cell(s) written, {s['blocked']} left alone, "
                      f"{s['review']} need review."
                      + (f" Backup: {s['backup']}" if s["backup"] else ""))
