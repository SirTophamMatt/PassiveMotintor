"""Operational Summary — Intel Tool page that builds the SCC State Operational
Summary deck (``/intel/summary``).

Behind the Intel Tool's shared password (same Flask-session flag as ``/intel``;
every callback and the image route re-check it server-side, so a hidden layout
is never the only thing standing between a visitor and the content). On the
web deployment it additionally refuses to open while the Intel password is
still the built-in default — the deck is marked Official: Sensitive.

The form is generated from ``app.opsum.SLIDES`` and the download is rendered
by ``app.opsum_pptx`` into the SCC's own template, so what is edited here and
what lands in PowerPoint come from one field map.
"""
import base64
import datetime
import logging

import flask
from dash import ALL, MATCH, Input, Output, State, ctx, dash_table, dcc, html, no_update
from dash.exceptions import PreventUpdate

from app import opsum, opsum_pptx, ui
from app.pages import intel

log = logging.getLogger(__name__)

PATH = "/intel/summary"
IMAGE_ROUTE = "/intel/summary/image"

try:
    from zoneinfo import ZoneInfo
    _MELB = ZoneInfo("Australia/Melbourne")
except Exception:          # tzdata missing (bare Windows build): local time
    _MELB = None

SYNTAX_HELP = (
    "Text boxes: one line per bullet (the slide keeps its own bullet style). "
    "Indent a line with two spaces for a sub-bullet · “# Heading” for a heading · "
    "a blank line for a gap · **bold** · [link text](https://…). "
    "Fields marked ↻ carry forward to the next day's summary.")


def _today():
    now = datetime.datetime.now(_MELB) if _MELB else datetime.datetime.now()
    return now.date()


def available():
    """Whether the summary may be opened on this deployment."""
    return intel.DESKTOP or intel.INTEL_PASSWORD != intel.DEFAULT_PASSWORD


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def layout():
    return html.Div([
        html.H2("Intel Tool"),
        html.Div(intel.body_for(PATH), id="intel-body"),
    ])


def body():
    if not available():
        return html.Div([
            intel.tabs(PATH),
            html.Div([
                html.H4("Operational Summary is switched off"),
                html.P(["The State Operational Summary is marked Official: Sensitive, so "
                        "this page stays closed while the Intel Tool still uses its "
                        "built-in default password. Set the ", html.Code("UM_INTEL_PASSWORD"),
                        " environment variable on the server and restart to enable it."]),
            ], className="panel", style={"maxWidth": "640px"}),
        ])
    d = _today()
    return html.Div([
        intel.tabs(PATH),
        html.Div([
            html.Div([
                html.Label("Summary date", className="muted",
                           style={"display": "block", "marginBottom": "4px"}),
                dcc.DatePickerSingle(id="opsum-date", date=d.isoformat(),
                                     display_format="YYYY-MM-DD",
                                     first_day_of_week=1, clearable=False),
            ], style={"marginRight": "18px"}),
            html.Div([
                html.Button("Save draft", id="opsum-save", n_clicks=0,
                            className="btn btn-primary", style={"marginRight": "8px"}),
                html.Button("Download .pptx", id="opsum-download-btn", n_clicks=0,
                            className="btn", style={"marginRight": "8px"}),
                html.Button("Mark issued", id="opsum-issue", n_clicks=0, className="btn"),
                dcc.Download(id="opsum-download"),
            ], style={"alignSelf": "flex-end"}),
        ], className="panel", style={"display": "flex", "flexWrap": "wrap",
                                     "alignItems": "flex-start", "gap": "8px"}),
        html.Div(id="opsum-status", className="muted", style={"margin": "6px 0 10px"}),
        html.P(SYNTAX_HELP, className="muted", style={"fontSize": "12px"}),
        dcc.Loading(html.Div(id="opsum-form")),
    ])


def _status_text(d, meta, carried_from, extra=None):
    bits = []
    if meta is None:
        bits.append(f"New summary for {d:%A %d %B %Y} — not saved yet.")
        bits.append(f" Carried forward from {carried_from:%d %B}." if carried_from
                    else " Nothing to carry forward (no earlier summary).")
    else:
        bits.append(f"Saved draft v{meta['version']}, last saved {meta['updated_at']}.")
        if meta.get("status") == "issued":
            bits.append(f" Issued {meta['issued_at']}.")
    if extra:
        bits.append(" " + extra)
    recent = [r for r in opsum.recent(7) if r["summary_date"] != d.isoformat()]
    if recent:
        bits.append("  Recent: " + ", ".join(
            r["summary_date"] + (" (issued)" if r["status"] == "issued" else "")
            for r in recent))
    return "".join(bits)


def _label(f):
    return f.label + (" ↻" if f.carry else "")


def _table(f, value, d, dark=True):
    rows, cols = opsum.row_labels(f, d), opsum.column_labels(f, d)
    columns = [{"name": "", "id": "label", "editable": False}]
    dropdown = {}
    for j, c in enumerate(cols):
        col = {"name": c, "id": f"c{j}", "editable": True}
        if f.kind == "colors":
            col["presentation"] = "dropdown"
            dropdown[f"c{j}"] = {"options": [{"label": k or "—", "value": k}
                                             for k in opsum.WORKLOAD_COLOURS],
                                 "clearable": False}
        columns.append(col)
    data = [{"label": rows[i], **{f"c{j}": value[i][j] for j in range(len(cols))}}
            for i in range(len(rows))]
    styles = ui.table_styles(dark)
    styles["style_cell"] = {**styles["style_cell"], "minWidth": "70px",
                            "whiteSpace": "normal", "height": "auto"}
    return dash_table.DataTable(
        id={"type": "opsum-t", "key": f.key}, columns=columns, data=data,
        editable=True, dropdown=dropdown, **styles)


def _grid(f, value, dark=True):
    styles = ui.table_styles(dark)
    return dash_table.DataTable(
        id={"type": "opsum-g", "key": f.key},
        columns=[{"name": "Centre", "id": "label", "editable": False},
                 {"name": "State", "id": "state", "editable": True,
                  "presentation": "dropdown"}],
        data=[{"label": k, "state": value.get(k, opsum.GRID_STATES[0])} for k in f.labels],
        dropdown={"state": {"options": [{"label": s, "value": s} for s in opsum.GRID_STATES],
                            "clearable": False}},
        editable=True, page_size=40,
        style_data_conditional=[
            {"if": {"filter_query": '{state} = "Active"', "column_id": "state"},
             "backgroundColor": "#00B050", "color": "#000"},
            {"if": {"filter_query": '{state} = "Readiness"', "column_id": "state"},
             "backgroundColor": "#FFC000", "color": "#000"},
        ], **styles)


def _preview(ref):
    if not ref:
        return html.Div("No image — this slot is left out of the deck.", className="muted")
    return html.Div([
        html.Img(src=f"{IMAGE_ROUTE}/{ref['sha']}.{ref['ext']}",
                 style={"maxWidth": "320px", "maxHeight": "180px",
                        "border": "1px solid var(--border)", "display": "block"}),
        html.Span(ref.get("name") or "", className="muted", style={"fontSize": "12px"}),
    ])


def _image(f, value):
    return html.Div([
        dcc.Store(id={"type": "opsum-img", "key": f.key}, data=value),
        html.Div(_preview(value), id={"type": "opsum-prev", "key": f.key}),
        html.Div([
            dcc.Upload(html.Button("Upload image…", className="btn"),
                       id={"type": "opsum-up", "key": f.key}, multiple=False,
                       accept="image/png,image/jpeg,image/gif,image/bmp"),
            html.Button("Remove", id={"type": "opsum-clr", "key": f.key}, n_clicks=0,
                        className="btn", style={"marginLeft": "8px"}),
        ], style={"display": "flex", "marginTop": "6px"}),
        html.Div(id={"type": "opsum-imgmsg", "key": f.key}, className="error-text"),
    ])


def _field(f, value, d, dark):
    if f.kind == "text":
        control = dcc.Textarea(id={"type": "opsum-f", "key": f.key}, value=value,
                               style={"width": "100%", "minHeight": "90px"},
                               className="text-input")
    elif f.kind == "line":
        control = dcc.Input(id={"type": "opsum-f", "key": f.key}, value=value,
                            type="text", debounce=False, className="text-input wide",
                            style={"width": "100%"})
    elif f.kind in ("table", "colors"):
        control = _table(f, value, d, dark)
    elif f.kind == "grid":
        control = _grid(f, value, dark)
    else:
        control = _image(f, value)
    return html.Div([
        html.Label(_label(f), style={"fontWeight": "600", "display": "block",
                                     "marginBottom": "4px"}),
        control,
        html.Div(f.help, className="muted", style={"fontSize": "12px"}) if f.help else None,
    ], style={"marginBottom": "14px"})


def form(d, data, dark=True):
    sections = []
    for n, slide in enumerate(opsum.SLIDES, 1):
        summary = [html.Strong(slide.title)]
        if slide.optional:
            summary.append(html.Span("  (optional slide)", className="muted"))
        children = [html.Summary(summary, style={"cursor": "pointer", "padding": "4px 0"})]
        if slide.optional:
            children.append(dcc.Checklist(
                id={"type": "opsum-opt", "key": slide.key},
                options=[{"label": " Include this slide in today's deck", "value": "on"}],
                value=["on"] if data["optional"].get(slide.key) else [],
                style={"margin": "6px 0 10px"}))
        if slide.note:
            children.append(html.P(slide.note, className="muted"))
        children += [_field(f, data["fields"][f.key], d, dark) for f in slide.fields]
        sections.append(html.Details(children, open=(n <= 2), className="panel",
                                     style={"marginBottom": "10px"}))
    return html.Div(sections)


# --------------------------------------------------------------------------- #
# Collecting the form
# --------------------------------------------------------------------------- #
def collect(f_ids, f_vals, t_ids, t_data, g_ids, g_data, i_ids, i_data, o_ids, o_vals):
    """Rebuild a draft dict from the pattern-matched form components.
    ``opsum.normalise`` (called by save/render) does the validation."""
    fields = {}
    for cid, v in zip(f_ids or [], f_vals or []):
        fields[cid["key"]] = v
    for cid, rows in zip(t_ids or [], t_data or []):
        f = opsum.FIELDS.get(cid["key"])
        if f is None:
            continue
        fields[f.key] = [[(r or {}).get(f"c{j}", "") for j in range(len(f.cols))]
                         for r in (rows or [])]
    for cid, rows in zip(g_ids or [], g_data or []):
        fields[cid["key"]] = {r.get("label"): r.get("state") for r in (rows or [])}
    for cid, ref in zip(i_ids or [], i_data or []):
        fields[cid["key"]] = ref
    optional = {cid["key"]: bool(v) for cid, v in zip(o_ids or [], o_vals or [])}
    return {"fields": fields, "optional": optional}


_FORM_STATES = [
    State({"type": "opsum-f", "key": ALL}, "id"),
    State({"type": "opsum-f", "key": ALL}, "value"),
    State({"type": "opsum-t", "key": ALL}, "id"),
    State({"type": "opsum-t", "key": ALL}, "data"),
    State({"type": "opsum-g", "key": ALL}, "id"),
    State({"type": "opsum-g", "key": ALL}, "data"),
    State({"type": "opsum-img", "key": ALL}, "id"),
    State({"type": "opsum-img", "key": ALL}, "data"),
    State({"type": "opsum-opt", "key": ALL}, "id"),
    State({"type": "opsum-opt", "key": ALL}, "value"),
]


# --------------------------------------------------------------------------- #
# Callbacks
# --------------------------------------------------------------------------- #
def register_callbacks(app):
    @app.server.route(f"{IMAGE_ROUTE}/<filename>")
    def opsum_image(filename):
        # Same gate as the page: uploaded images are summary content.
        if not intel.unlocked() or not available():
            flask.abort(403)
        path = opsum.image_file(filename)
        if not path:
            flask.abort(404)
        return flask.send_file(path, max_age=3600)

    @app.callback(
        Output("opsum-form", "children"),
        Output("opsum-status", "children"),
        Input("opsum-date", "date"),
        State("theme-store", "data"))
    def load(date, dark):
        if not date or not intel.unlocked() or not available():
            raise PreventUpdate
        d = opsum.parse_date(date)
        data, meta, carried_from = opsum.open_draft(d)
        return form(d, data, dark if dark is not None else True), \
            _status_text(d, meta, carried_from)

    @app.callback(
        Output({"type": "opsum-img", "key": MATCH}, "data"),
        Output({"type": "opsum-prev", "key": MATCH}, "children"),
        Output({"type": "opsum-imgmsg", "key": MATCH}, "children"),
        Input({"type": "opsum-up", "key": MATCH}, "contents"),
        Input({"type": "opsum-clr", "key": MATCH}, "n_clicks"),
        State({"type": "opsum-up", "key": MATCH}, "filename"),
        prevent_initial_call=True)
    def image(contents, clears, name):
        if not intel.unlocked() or not available():
            raise PreventUpdate
        trig = ctx.triggered_id or {}
        if trig.get("type") == "opsum-clr":
            if not clears:
                raise PreventUpdate
            return None, _preview(None), ""
        if not contents:
            raise PreventUpdate
        try:
            blob = base64.b64decode(contents.split(",", 1)[1])
            ref = opsum.store_image(blob, name or "")
        except (IndexError, ValueError) as e:
            msg = str(e) if isinstance(e, ValueError) and str(e) else "Could not read that file."
            return no_update, no_update, msg
        return ref, _preview(ref), ""

    @app.callback(
        Output("opsum-status", "children", allow_duplicate=True),
        Output("opsum-download", "data"),
        Input("opsum-save", "n_clicks"),
        Input("opsum-download-btn", "n_clicks"),
        Input("opsum-issue", "n_clicks"),
        State("opsum-date", "date"),
        *_FORM_STATES,
        prevent_initial_call=True)
    def act(save_n, dl_n, issue_n, date, *states):
        if not (save_n or dl_n or issue_n) or not date:
            raise PreventUpdate
        if not intel.unlocked() or not available():
            return "Locked — unlock the Intel Tool again.", None
        d = opsum.parse_date(date)
        data = collect(*states)
        trig = ctx.triggered_id
        try:
            if trig == "opsum-issue":
                opsum.issue(d, data)
                extra = "Marked issued — the issued copy is kept even if you edit again."
            else:
                opsum.save(d, data)
                extra = "Saved."
            download = None
            if trig in ("opsum-download-btn", "opsum-issue"):
                blob = opsum_pptx.render(d, data)
                download = dcc.send_bytes(blob, opsum.filename(d))
                extra += " Deck generated."
        except Exception as e:           # never lose the form over a render error
            log.exception("Operational Summary %s failed", trig)
            return f"⚠ {trig} failed: {e}", None
        _, meta = opsum.load(d)
        return _status_text(d, meta, None, extra), download

