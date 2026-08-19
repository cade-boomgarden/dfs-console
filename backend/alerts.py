"""Failure alerting (section 11e: a missed 10:30am inactives pull silently
produces a bad slate -- it must be loud instead).

One function, one optional webhook. The payload carries both `text` and
`content`, which covers Slack-style and Discord-style webhooks without a
provider switch. No webhook configured = log only, never raise: alerting must
not be able to take down the thing it is watching.
"""
from __future__ import annotations

import json
import logging
import urllib.request

from .settings import get_settings

log = logging.getLogger("dfs.alerts")


def send_alert(text: str) -> bool:
    """Returns True if delivered to a webhook, False otherwise. Never raises."""
    log.warning("ALERT: %s", text)
    url = get_settings().alert_webhook_url
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"text": text, "content": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception:                                    # noqa: BLE001
        log.exception("alert webhook delivery failed")
        return False
