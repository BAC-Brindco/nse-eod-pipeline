"""Alert delivery to a Slack-compatible webhook.

Delivery is best-effort and never raises: a webhook outage must not turn a
successful ingest into a failed run. The exit code is set from the findings
themselves, so alerting failures cannot mask a real problem either.
"""

from __future__ import annotations

import datetime as dt

import httpx

from ..config import Settings, get_settings
from ..logging_setup import get_logger
from .checks import CRITICAL, Finding, WARNING

log = get_logger(__name__)

_EMOJI = {CRITICAL: ":rotating_light:", WARNING: ":warning:", "info": ":information_source:"}
MAX_LINES = 12


def format_slack(
    findings: list[Finding],
    command: str,
    trade_date: dt.date | None,
    run_uid: str,
    summary_counters: dict | None = None,
) -> dict:
    """Build a Slack ``blocks`` payload. Also renders as plain text elsewhere."""
    crit = [f for f in findings if f.severity == CRITICAL]
    warn = [f for f in findings if f.severity == WARNING]
    head_sev = CRITICAL if crit else (WARNING if warn else "info")

    title = (
        f"{_EMOJI[head_sev]} NSE EOD pipeline: {len(crit)} critical, {len(warn)} warning"
        f" — {command}"
        + (f" {trade_date}" if trade_date else "")
    )

    lines: list[str] = []
    for f in (crit + warn)[:MAX_LINES]:
        tag = f.check_name
        lines.append(f"• *{tag}* — {f.detail}")
    hidden = len(crit + warn) - min(len(crit + warn), MAX_LINES)
    if hidden > 0:
        lines.append(f"• _...and {hidden} more; see ops.validation_finding_")

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": title[:150]}},
    ]
    if lines:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)[:2900]}}
        )
    if summary_counters:
        kv = "  ".join(f"`{k}={v}`" for k, v in summary_counters.items() if v)
        if kv:
            blocks.append(
                {"type": "context", "elements": [{"type": "mrkdwn", "text": kv[:2900]}]}
            )
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"run `{run_uid}`"}]}
    )
    return {"text": title, "blocks": blocks}


def send(
    findings: list[Finding],
    command: str,
    trade_date: dt.date | None,
    run_uid: str,
    summary_counters: dict | None = None,
    settings: Settings | None = None,
) -> bool:
    """Post the alert. Returns True if delivered.

    No webhook configured is a normal, non-error state: findings are still
    persisted to ``ops.validation_finding`` and the exit code is still set.
    """
    s = settings or get_settings()
    actionable = [f for f in findings if f.severity in (CRITICAL, WARNING)]
    if not actionable:
        return False

    payload = format_slack(actionable, command, trade_date, run_uid, summary_counters)

    if not s.alert_webhook_url:
        log.info(
            "alert_not_sent_no_webhook",
            critical=sum(1 for f in actionable if f.severity == CRITICAL),
            warning=sum(1 for f in actionable if f.severity == WARNING),
            hint="set ALERT_WEBHOOK_URL to enable delivery",
        )
        for f in actionable:
            log.warning("finding", check=f.check_name, severity=f.severity, detail=f.detail)
        return False

    if s.alert_dry_run:
        log.info("alert_dry_run", payload_text=payload["text"])
        return False

    try:
        r = httpx.post(s.alert_webhook_url, json=payload, timeout=15.0)
        if r.status_code >= 300:
            log.error("alert_delivery_failed", status=r.status_code, body=r.text[:200])
            return False
        log.info("alert_sent", count=len(actionable))
        return True
    except Exception as exc:
        # Never let alerting break the pipeline.
        log.error("alert_delivery_error", error=str(exc))
        return False
