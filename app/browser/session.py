"""Headed Chromium with a persistent profile, plus bounded navigation."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import sleep
from typing import Any, Protocol
from urllib.parse import urlsplit


PROFILE_ROOT = Path("data/browser-profiles")
_PROFILE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


class NavigationFailed(RuntimeError):
    """The page did not load after the allowed retries."""


@dataclass(frozen=True)
class BrowserOptions:
    profile: str = "default"
    headless: bool = False
    timeout_seconds: float = 30.0
    profile_root: Path = PROFILE_ROOT

    def __post_init__(self) -> None:
        if _PROFILE_NAME.fullmatch(self.profile) is None:
            raise ValueError(f"Invalid browser profile name: {self.profile!r}")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def profile_dir(self) -> Path:
        return self.profile_root / self.profile

    @property
    def timeout_ms(self) -> int:
        return round(self.timeout_seconds * 1000)


@contextmanager
def open_browser(options: BrowserOptions) -> Iterator[Any]:
    """Yield a page in a persistent Chromium context; cookies survive between runs."""

    from playwright.sync_api import sync_playwright

    options.profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(options.profile_dir),
            headless=options.headless,
            accept_downloads=False,
            permissions=[],
        )
        try:
            context.set_default_timeout(options.timeout_ms)
            context.set_default_navigation_timeout(options.timeout_ms)
            page = context.pages[0] if context.pages else context.new_page()
            yield page
        finally:
            context.close()


@contextmanager
def open_form_page(options: BrowserOptions, url: str) -> Iterator[Any]:
    """Open ``url`` in the persistent browser and yield a fill-ready page."""

    from app.browser.page import PlaywrightFormPage

    with open_browser(options) as page:
        goto(page, url, timeout_ms=options.timeout_ms)
        yield PlaywrightFormPage(page, timeout_ms=options.timeout_ms)


class NavigablePage(Protocol):
    def goto(self, url: str, **kwargs: Any) -> Any: ...


def goto(
    page: NavigablePage,
    url: str,
    *,
    timeout_ms: int,
    sleeper: Callable[[float], None] = sleep,
) -> None:
    """Open an http(s) URL with up to three attempts and exponential backoff."""

    if urlsplit(url).scheme not in {"http", "https"}:
        raise ValueError(f"Refusing to open non-http(s) URL: {url!r}")
    for attempt in range(1, 4):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except Exception as exc:
            if attempt == 3:
                raise NavigationFailed(f"{type(exc).__name__}: {exc}") from exc
            sleeper(float(2 ** (attempt - 1)))
