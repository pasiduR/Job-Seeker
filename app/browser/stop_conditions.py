"""Code-side stop conditions: CAPTCHA, login walls, and unexpected pages."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


SIGNALS_SCRIPT = (Path(__file__).parent / "detect_signals.js").read_text(encoding="utf-8")

_CAPTCHA_TEXT = re.compile(
    r"verify (that )?you are (a )?human|i'?m not a robot|are you a robot"
    r"|complete the security check|checking your browser|press (and|&) hold",
    re.IGNORECASE,
)
_LOGIN_TEXT = re.compile(
    r"\b(sign|log) ?in to (apply|continue)\b|\bcreate an account to apply\b",
    re.IGNORECASE,
)
_LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|sso|oauth2?|auth)(/|$|\?)", re.IGNORECASE)
_SSO_HOSTS = (
    "accounts.google.com",
    "login.microsoftonline.com",
    "login.live.com",
    "appleid.apple.com",
)


@dataclass(frozen=True)
class PageSignals:
    captcha: bool = False
    password_fields: int = 0
    text: str = ""


def site_of(url: str) -> str:
    """Approximate registrable domain: the last two host labels."""

    host = (urlsplit(url).hostname or "").lower()
    return ".".join(host.split(".")[-2:])


def find_blocker(
    *, url: str, start_url: str, signals: Iterable[PageSignals]
) -> str | None:
    """The reason to stop, or None when the page looks safe to keep filling."""

    host = (urlsplit(url).hostname or "").lower()
    frames = list(signals)
    if site_of(url) == "linkedin.com":
        return "LinkedIn page: apply manually (no LinkedIn automation)"
    if any(frame.captcha or _CAPTCHA_TEXT.search(frame.text) for frame in frames):
        return "CAPTCHA detected"
    if host in _SSO_HOSTS or _LOGIN_PATH.search(urlsplit(url).path):
        return f"login wall at {host}"
    if any(frame.password_fields for frame in frames):
        return "login wall: the page asks for a password"
    if any(_LOGIN_TEXT.search(frame.text) for frame in frames):
        return "login wall: the page asks to sign in"
    if site_of(url) != site_of(start_url):
        return f"unexpected page: left {site_of(start_url)} for {host}"
    return None
