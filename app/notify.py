"""Outbound notifications via a webhook (Teams / Slack / Discord / generic).

One config field (notify.webhook_url) covers the common services, with the
payload shape chosen per service:

- **Teams (Workflows / Power Automate)** — Microsoft retired the classic
  Office 365 "Incoming Webhook" connector; modern Teams webhooks are created
  via the Workflows app and expect an **Adaptive Card** payload. Detected from
  logic.azure.com / powerplatform.com URLs, or set webhook_format = "teams".
- **Discord** — {"content": ...}, detected from discord.com URLs.
- **Slack / classic Teams connectors / generic** — {"text": ...}.

send() is deliberately fire-and-forget: a notification failure is logged and
never allowed to break a collector or watchdog cycle.
"""
import logging

import requests

from app.config import load_config

log = logging.getLogger(__name__)

# Message kinds map to per-kind enable toggles in config["notify"].
KIND_TOGGLES = {
    "power_alert": "on_power_alert",
    "flood_alert": "on_flood_alert",
    "fire_alert": "on_fire_alert",
    "weather_alert": "on_weather_alert",
    "storm_alert": "on_storm_alert",
    "roads_alert": "on_roads_alert",
    "watchdog": "on_watchdog",
}


def configured(cfg=None):
    cfg = cfg or load_config()
    return bool((cfg.get("notify", {}).get("webhook_url") or "").strip())


def _detect_format(url, fmt):
    fmt = (fmt or "auto").lower()
    if fmt != "auto":
        return fmt
    if "discord.com" in url:
        return "discord"
    if "logic.azure.com" in url or "powerplatform.com" in url:
        return "teams"
    return "text"


def _build_payload(url, fmt, text):
    fmt = _detect_format(url, fmt)
    if fmt == "discord":
        return {"content": text}
    if fmt == "teams":
        # Adaptive Card wrapped in a Teams message, as the Workflows
        # "when a webhook request is received" template expects.
        return {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                },
            }],
        }
    return {"text": text}


def send(message, kind="watchdog", cfg=None, force=False):
    """Post a message to the configured webhook. Returns True on success.

    ``force=True`` skips the per-kind toggle (used by the admin test button)
    but still requires a webhook URL."""
    cfg = cfg or load_config()
    ncfg = cfg.get("notify", {})
    # Master pause suppresses everything except forced sends (admin test button).
    if not force and ncfg.get("paused"):
        return False
    if not force and not ncfg.get(KIND_TOGGLES.get(kind, "on_watchdog"), True):
        return False
    url = (ncfg.get("webhook_url") or "").strip()
    if not url:
        return False

    payload = _build_payload(url, ncfg.get("webhook_format"),
                             f"[Passive Monitor] {message}")
    try:
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log.warning("Notification failed (%s): %s", kind, e)
        return False
