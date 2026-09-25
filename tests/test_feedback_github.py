"""Feedback -> GitHub issues: payload, privacy/inertness, outcomes recorded,
retry without duplicates, labels fallback, and never losing the report."""
import io
import json
import urllib.error

import pytest

from app import database, feedback, github_issues


@pytest.fixture
def gh_cfg():
    return {"feedback": {"recipient": "", "email_enabled": False, "max_per_hour": 0,
                         "github_enabled": True,
                         "github_repo": "SirTophamMatt/PassiveMotintor",
                         "github_labels": ["feedback"], "github_include_name": False},
            "smtp": {}}


@pytest.fixture
def api(monkeypatch):
    """Fake GitHub API: records calls, answers from a queue of responses."""
    monkeypatch.setenv("UM_GITHUB_TOKEN", "test-token")
    calls, replies = [], []

    def fake(method, path, payload=None):
        calls.append((method, path, payload))
        r = replies.pop(0) if replies else {"html_url": "https://github.com/o/r/issues/7"}
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(github_issues, "_request", fake)
    return calls, replies


def _http(code, message=""):
    return urllib.error.HTTPError("u", code, "x", {},
                                  io.BytesIO(json.dumps({"message": message}).encode()))


def _submit(cfg, **kw):
    args = dict(kind="bug", message="The flood map never loads for me @octocat",
                subject="Map broken", reporter_name="Jo Citizen",
                reporter_email="jo@example.com", severity="high",
                page_path="/map", user_agent="Firefox", ip_prefix="81.2.69.0")
    args.update(kw)
    return feedback.submit(cfg=cfg, **args)


def _stored(ref):
    return feedback.normalise(database.read_df(
        "SELECT * FROM feedback_reports WHERE ref = ?", [ref]).iloc[0].to_dict())


def test_report_opens_issue_and_records_url(db, gh_cfg, api):
    calls, _ = api
    row = _submit(gh_cfg)
    assert row["github_status"] == "sent"
    method, path, payload = calls[0]
    assert (method, path) == ("POST", "/repos/SirTophamMatt/PassiveMotintor/issues")
    assert payload["labels"] == ["feedback", "bug"]
    assert row["ref"] in payload["title"] and "Map broken" in payload["title"]
    s = _stored(row["ref"])
    assert s["github_status"] == "sent"
    assert s["github_issue_url"] == "https://github.com/o/r/issues/7"
    assert s["github_error"] is None


def test_issue_never_carries_email_or_network(db, gh_cfg, api):
    calls, _ = api
    _submit(gh_cfg)
    body = calls[0][2]["body"]
    assert "jo@example.com" not in body and "81.2.69" not in body
    assert "Jo Citizen" not in body                     # name off by default
    gh_cfg["feedback"]["github_include_name"] = True
    _submit(gh_cfg)
    assert "Jo Citizen" in calls[1][2]["body"]


def test_report_text_is_inert(db, gh_cfg, api):
    calls, _ = api
    nasty = "hi @octocat ``` <img src=x> [x](http://evil) ```` end"
    _submit(gh_cfg, message=nasty, subject="@team **boom** <b>")
    body, title = calls[0][2]["body"], calls[0][2]["title"]
    # the message sits inside a fence longer than any backtick run in it
    assert "`````" + " text\n" + nasty + "\n`````" in body
    assert "@​team" in title and "@team" not in title
    assert "<b>" not in title and "\\*\\*boom\\*\\*" in title


def test_suggestion_label(db, gh_cfg, api):
    calls, _ = api
    _submit(gh_cfg, kind="suggestion", severity="high")
    assert calls[0][2]["labels"] == ["feedback", "enhancement"]
    assert "Severity" not in calls[0][2]["body"]


def test_labels_rejected_retries_without(db, gh_cfg, api):
    calls, replies = api
    replies.extend([_http(422, "Validation Failed"),
                    {"html_url": "https://github.com/o/r/issues/8"}])
    row = _submit(gh_cfg)
    assert row["github_status"] == "sent"
    assert "labels" in calls[0][2] and "labels" not in calls[1][2]


def test_failure_is_recorded_and_report_kept(db, gh_cfg, api):
    _, replies = api
    replies.append(_http(403, "Resource not accessible by personal access token"))
    row = _submit(gh_cfg)
    assert row["ref"] and row["github_status"] == "failed"
    s = _stored(row["ref"])
    assert "Issues write access" in s["github_error"]
    assert s["message"].startswith("The flood map")


def test_network_error_never_raises(db, gh_cfg, api):
    _, replies = api
    replies.append(OSError("unreachable"))
    assert _submit(gh_cfg)["github_status"] == "failed"


def test_skipped_without_token_or_when_off(db, gh_cfg, monkeypatch):
    monkeypatch.delenv("UM_GITHUB_TOKEN", raising=False)
    row = _submit(gh_cfg)
    assert row["github_status"] == "skipped" and "UM_GITHUB_TOKEN" in row["github_error"]
    monkeypatch.setenv("UM_GITHUB_TOKEN", "t")
    gh_cfg["feedback"]["github_enabled"] = False
    assert _submit(gh_cfg)["github_status"] == "skipped"
    gh_cfg["feedback"]["github_enabled"] = True
    gh_cfg["feedback"]["github_repo"] = "not a repo"
    assert _submit(gh_cfg)["github_status"] == "skipped"


def test_resend_github_then_refuses_duplicate(db, gh_cfg, api, monkeypatch):
    calls, replies = api
    replies.append(_http(401, "Bad credentials"))
    row = _submit(gh_cfg)
    ok, msg = feedback.resend_github(row["ref"], gh_cfg)
    assert ok and "issues/7" in msg
    ok, msg = feedback.resend_github(row["ref"], gh_cfg)
    assert not ok and "already has an issue" in msg
    assert len(calls) == 2
    assert _stored(row["ref"])["github_error"] is None


def test_existing_email_only_config_is_unaffected(db, monkeypatch):
    """Configs written before this feature have no github keys: no issue, no error."""
    monkeypatch.setenv("UM_GITHUB_TOKEN", "t")
    cfg = {"feedback": {"recipient": "", "email_enabled": False, "max_per_hour": 0},
           "smtp": {}}
    row = feedback.submit("bug", "Something is broken here.", cfg=cfg)
    assert row["github_status"] == "skipped"


def test_check_reports_visibility(api, gh_cfg):
    _, replies = api
    replies.append({"has_issues": True, "private": False})
    ok, msg = github_issues.check(gh_cfg)
    assert ok and "PUBLIC" in msg
    replies.append(_http(404))
    ok, msg = github_issues.check(gh_cfg)
    assert not ok and "not found" in msg


def test_fence_outgrows_backticks():
    assert github_issues.fence("a ```` b").startswith("````` text")
    assert github_issues.fence("plain").startswith("``` text")
