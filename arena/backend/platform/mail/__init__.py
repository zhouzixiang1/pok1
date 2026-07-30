"""邮件发送:SMTP + 模板渲染。"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_template(text: str, ctx: dict[str, Any]) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1)
        return str(ctx.get(key, m.group(0)))
    return _PLACEHOLDER_RE.sub(repl, text or "")


class MailConfig:
    def __init__(self) -> None:
        self.host = os.environ.get("SMTP_HOST", "smtp.qiye.aliyun.com")
        self.port = int(os.environ.get("SMTP_PORT", "465"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.from_addr = os.environ.get("SMTP_FROM", self.user)
        self.from_name = os.environ.get("SMTP_FROM_NAME", "pok-arena")
        self.code_ttl_minutes = int(os.environ.get("EMAIL_CODE_TTL_MINUTES", "30"))

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.from_addr)


class Mailer:
    """同步 SMTP SSL 发信。"""

    def __init__(self, config: MailConfig | None = None) -> None:
        self.config = config or MailConfig()

    def send(self, to_addr: str, subject: str, *,
             body_text: str = "", body_html: str = "") -> None:
        if not self.config.configured:
            raise RuntimeError("SMTP 未配置(检查 SMTP_USER/SMTP_PASSWORD)")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((self.config.from_name, self.config.from_addr))
        msg["To"] = to_addr
        if body_text:
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.config.host, self.config.port,
                              context=context, timeout=30) as server:
            server.login(self.config.user, self.config.password)
            server.sendmail(self.config.from_addr, [to_addr], msg.as_string())
        logger.info("mail sent to=%s subject=%s", to_addr, subject)


__all__ = ["MailConfig", "Mailer", "render_template"]
