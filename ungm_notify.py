"""Standard-library-only SMTP delivery and workflow failure notification.

Uses the watcher's existing SMTP/MAIL_* settings; never prints secret values or
raw exception messages. This can run even when dependency installation fails.
"""

from __future__ import annotations

import html
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path

ALERT_MARKER = Path(".ungm-alert-sent")


def smtp_config() -> dict[str, str | int]:
    config: dict[str, str | int] = {}
    for key, name in {
        "host": "SMTP_HOST", "port": "SMTP_PORT", "user": "SMTP_USER",
        "password": "SMTP_PASSWORD", "mail_from": "MAIL_FROM", "mail_to": "MAIL_TO",
    }.items():
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Missing required environment variable: {name}")
        config[key] = value
    try:
        config["port"] = int(config["port"])
    except ValueError as exc:
        raise RuntimeError("SMTP_PORT must be an integer") from exc
    return config


def workflow_run_url() -> str:
    repository, run_id = os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_RUN_ID", "")
    if repository and run_id:
        return f"https://github.com/{repository}/actions/runs/{run_id}"
    return ""


def send_message(subject: str, plain_text: str, html_text: str) -> None:
    config = smtp_config()
    recipients = [item.strip() for item in str(config["mail_to"]).split(",") if item.strip()]
    if not recipients:
        raise RuntimeError("MAIL_TO must contain at least one recipient")
    message = EmailMessage()
    message["Subject"], message["From"], message["To"] = subject, str(config["mail_from"]), ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message.set_content(plain_text)
    message.add_alternative(html_text, subtype="html")
    host, port = str(config["host"]), int(config["port"])
    connection = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
    with connection(host, port, timeout=30) as server:
        if port != 465:
            server.ehlo()
            server.starttls()
            server.ehlo()
        server.login(str(config["user"]), str(config["password"]))
        refused = server.send_message(message)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def mark_alert_sent() -> None:
    # Set only after SMTP accepted a fatal-run alert for every recipient.
    # A degraded daily digest must not suppress a later workflow failure.
    ALERT_MARKER.write_text("sent\n", encoding="utf-8")


def notify_workflow_failure() -> int:
    if ALERT_MARKER.exists():
        print("Failure alert was already sent by the watcher; skipping duplicate.")
        return 0
    body = (
        "UNGM 监控运行失败，未能正常完成。不能将本次运行视为没有匹配机会。\n"
        "可能发生在依赖安装、浏览器启动、抓取、筛选、邮件投递或状态保存阶段。\n"
        "请检查 GitHub Actions 运行日志。若 SMTP 也不可用，请以 Actions 失败状态为准。\n"
        f"运行详情：{workflow_run_url()}\n"
        "最近 3 天的筛选范围未改变；未进行历史补查。\n"
    )
    try:
        send_message("[运行失败] UNGM 监控异常提醒", body, f"<pre>{html.escape(body)}</pre>")
        mark_alert_sent()
    except Exception as exc:
        print(f"::error::Workflow failure email could not be delivered ({type(exc).__name__}); inspect Actions.")
        return 1
    print("Workflow failure alert accepted by SMTP.")
    return 0


if __name__ == "__main__":
    sys.exit(notify_workflow_failure())
