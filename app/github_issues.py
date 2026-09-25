"""Open GitHub issues from feedback reports (stdlib only — no new dependency).

The counterpart of ``app/mailer.py``: :func:`create_issue` never raises and
returns ``(ok, url_or_error)``, so :mod:`app.feedback` can store a report first
and deliver it second, recording the outcome either way.

**The token comes only from the ``UM_GITHUB_TOKEN`` environment variable** —
never config.json, which sits on a mounted volume and is rewritten whole by the
Settings page. Use a fine-grained personal access token scoped to the one
repository with *Issues: Read and write* and nothing else.

**What goes into the issue is chosen for a repository that may be public.** The
report text arrives from an anonymous public form, so it is placed inside a
code fence (longer than any backtick run in the text): nothing in it renders —
no @-mentions that notify people, no links, no images, no HTML. The reporter's
email address is never included; their name only when
``feedback.github_include_name`` is on. Both stay in the Admin queue, which is
where replies are handled.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.request

from app.config import load_config

log = logging.getLogger(__name__)

API = "https://api.github.com"
TIMEOUT_SECONDS = 15
_REPO_RE = re.compile(r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")


def token():
    return (os.environ.get("UM_GITHUB_TOKEN") or "").strip()


def repo(cfg=None):
    cfg = cfg or load_config()
    value = (cfg.get("feedback", {}).get("github_repo") or "").strip()
    return value if _REPO_RE.match(value) else ""


def enabled(cfg=None):
    cfg = cfg or load_config()
    return bool(cfg.get("feedback", {}).get("github_enabled", True))


def configured(cfg=None):
    """Enough to attempt a create: a valid owner/repo and a token."""
    return bool(repo(cfg) and token())


def _request(method, path, payload=None):
    data = json.dumps(payload).encode("utf8") if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": "Bearer " + token(),
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "Watchdesk-feedback",
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf8") or "{}")


def _http_error(e):
    try:
        detail = json.loads(e.read().decode("utf8")).get("message", "")
    except Exception:
        detail = ""
    hint = {401: "the token is invalid or expired",
            403: "the token lacks Issues write access to this repository "
                 "(or is rate limited)",
            404: "repository not found, or the token cannot see it",
            410: "issues are disabled on this repository"}.get(e.code, "")
    return "GitHub HTTP %s%s%s" % (e.code, ": " + hint if hint else "",
                                   " (%s)" % detail if detail else "")


def create_issue(title, body, labels=None, cfg=None):
    """Open an issue. Returns ``(True, html_url)`` or ``(False, error)``.

    Labels are best-effort: if GitHub rejects them (422 — e.g. a token that may
    not set labels), the issue is created again without them rather than lost."""
    cfg = cfg or load_config()
    target = repo(cfg)
    if not target:
        return False, "No valid GitHub repository (owner/name) configured."
    if not token():
        return False, "UM_GITHUB_TOKEN is not set."
    payload = {"title": title[:250], "body": body}
    if labels:
        payload["labels"] = list(labels)
    for attempt in (1, 2):
        try:
            issue = _request("POST", "/repos/%s/issues" % target, payload)
            return True, issue.get("html_url") or ""
        except urllib.error.HTTPError as e:
            if e.code == 422 and attempt == 1 and "labels" in payload:
                payload = {k: v for k, v in payload.items() if k != "labels"}
                continue
            return False, _http_error(e)
        except Exception as e:           # network, timeout, bad JSON
            return False, "%s: %s" % (e.__class__.__name__, e)
    return False, "GitHub rejected the issue."


def check(cfg=None):
    """``(ok, message)`` — can this token open issues on the configured repo?
    Reads the repository only; creates nothing."""
    cfg = cfg or load_config()
    target = repo(cfg)
    if not target:
        return False, "No valid GitHub repository (owner/name) configured."
    if not token():
        return False, "UM_GITHUB_TOKEN is not set on the server."
    try:
        info = _request("GET", "/repos/%s" % target)
    except urllib.error.HTTPError as e:
        return False, _http_error(e)
    except Exception as e:
        return False, "%s: %s" % (e.__class__.__name__, e)
    if not info.get("has_issues", True):
        return False, "Issues are turned off on %s." % target
    visibility = "private" if info.get("private") else "PUBLIC"
    return True, "Token can see %s (%s repository)." % (target, visibility)


def fence(text):
    """Wrap ``text`` in a code fence longer than any backtick run inside it,
    so no report content can close the fence and render as markdown."""
    longest = max((len(m) for m in re.findall(r"`+", text or "")), default=0)
    ticks = "`" * max(3, longest + 1)
    return "%s text\n%s\n%s" % (ticks, text or "", ticks)


def plain(text):
    """Single-line field text (subject, page, browser) made inert: no
    @-mentions, no markdown/HTML control characters, no line breaks."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = text.replace("@", "@​")
    return re.sub(r"([\\`*_{}\[\]()<>#+!|~])", r"\\\1", text)
