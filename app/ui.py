"""Shared UI helpers: KPI cards, figure theming, table styles."""
from dash import html


def apply_theme(fig, dark):
    fig.update_layout(
        template="plotly_dark" if dark else "plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=40, r=20, t=50, b=40),
    )
    return fig


def kpi_card(label, value, accent=None):
    style = {"borderTop": f"4px solid {accent}"} if accent else {}
    return html.Div([
        html.Div(label, className="kpi-label"),
        html.Div(value, className="kpi-value"),
    ], className="kpi-card", style=style)


def table_styles(dark):
    base = {
        "style_table": {"overflowX": "auto"},
        "style_cell": {
            "fontFamily": "Segoe UI, sans-serif",
            "fontSize": "13px",
            "padding": "6px 10px",
            "textAlign": "left",
        },
    }
    if dark:
        base["style_header"] = {"backgroundColor": "#1f2733", "color": "#e8eaed",
                                "fontWeight": "bold", "border": "1px solid #333d4d"}
        base["style_data"] = {"backgroundColor": "#161c26", "color": "#e8eaed",
                              "border": "1px solid #2a3340"}
        base["style_filter"] = {"backgroundColor": "#1f2733", "color": "#e8eaed"}
    else:
        base["style_header"] = {"backgroundColor": "#f1f3f4", "fontWeight": "bold"}
    return base


def status_pill(running, text_on="Running", text_off="Stopped"):
    return html.Span(
        ("● " + text_on) if running else ("● " + text_off),
        className="status-pill " + ("status-on" if running else "status-off"))
