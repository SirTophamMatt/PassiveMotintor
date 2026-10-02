"""Admin data must not be readable through a direct callback POST.

Dash serves every registered callback at /_dash-update-component whether or not
the page that owns it was rendered, so hiding /admin or /analytics behind a
login gates nothing on its own. These tests call the callbacks the way an
anonymous client could and check the admin-only data does not come back.
"""
import pytest

from app import auth, feedback


@pytest.fixture(scope="module")
def client():
    from app.factory import create_app
    return create_app(autostart=False).server.test_client()


def _call(client, outputs, inputs, changed):
    output = "..%s.." % "...".join(
        "%s.%s" % (o["id"], o["property"]) for o in outputs)
    return client.post("/_dash-update-component", json={
        "output": output, "outputs": outputs, "inputs": inputs,
        "changedPropIds": changed, "state": []})


FB_OUTPUTS = [{"id": "admin-fb-list", "property": "children"},
              {"id": "admin-fb-heading", "property": "children"},
              {"id": "admin-fb-status", "property": "children"}]
FB_INPUTS = [{"id": "admin-fb-status-filter", "property": "value", "value": "new"},
             {"id": "admin-fb-kind-filter", "property": "value", "value": "all"},
             [], [], []]


def test_feedback_queue_hidden_from_anonymous(client):
    feedback.submit(kind="bug", message="private message body text here",
                    subject="Secret subject", reporter_name="Jane Reporter",
                    reporter_email="jane@example.org", page_path="/")
    r = _call(client, FB_OUTPUTS, FB_INPUTS, ["admin-fb-status-filter.value"])
    body = r.get_data(as_text=True)
    assert "jane@example.org" not in body
    assert "Jane Reporter" not in body
    assert "private message body" not in body


def test_feedback_queue_shown_to_admin(client, monkeypatch):
    monkeypatch.setattr(auth, "is_admin", lambda: True)
    feedback.submit(kind="bug", message="visible to the admin queue",
                    reporter_email="admin-view@example.org", page_path="/")
    r = _call(client, FB_OUTPUTS, FB_INPUTS, ["admin-fb-status-filter.value"])
    assert "admin-view@example.org" in r.get_data(as_text=True)


@pytest.mark.parametrize("outputs", [
    [{"id": "analytics-kpis", "property": "children"},
     {"id": "analytics-trend", "property": "figure"},
     {"id": "analytics-top", "property": "figure"}],
])
def test_analytics_hidden_from_anonymous(client, outputs):
    r = _call(client, outputs,
              [{"id": "analytics-interval", "property": "n_intervals", "value": 1},
               {"id": "theme-store", "property": "data", "value": True}],
              ["analytics-interval.n_intervals"])
    assert r.status_code == 204
