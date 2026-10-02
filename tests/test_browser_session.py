from pathlib import Path
from typing import Any

import pytest

from app.browser.fields import extract_fields
from app.browser.session import BrowserOptions, NavigationFailed, goto, open_browser


def test_persistent_profile_is_created_under_the_profile_root(tmp_path: Path) -> None:
    options = BrowserOptions(profile="tester", headless=True, timeout_seconds=10, profile_root=tmp_path)

    with open_browser(options) as browser_page:
        browser_page.set_content("<input aria-label='Name'>")
        assert [field.label for field in extract_fields(browser_page)] == ["Name"]

    assert (tmp_path / "tester").is_dir()
    assert any((tmp_path / "tester").iterdir())


@pytest.mark.parametrize("name", ["../escape", "", "a b"])
def test_profile_names_cannot_escape_the_profile_root(name: str) -> None:
    with pytest.raises(ValueError):
        BrowserOptions(profile=name)


class FlakyPage:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[str] = []

    def goto(self, url: str, **kwargs: Any) -> None:
        self.calls.append(url)
        if len(self.calls) <= self.failures:
            raise TimeoutError("navigation timed out")


def test_goto_retries_with_backoff_then_fails() -> None:
    delays: list[float] = []
    flaky = FlakyPage(failures=2)
    goto(flaky, "https://jobs.example.com/apply", timeout_ms=1000, sleeper=delays.append)
    assert (len(flaky.calls), delays) == (3, [1.0, 2.0])

    with pytest.raises(NavigationFailed):
        goto(FlakyPage(failures=3), "https://jobs.example.com/apply", timeout_ms=1000, sleeper=delays.append)


def test_goto_refuses_non_http_urls() -> None:
    with pytest.raises(ValueError):
        goto(FlakyPage(0), "file:///etc/passwd", timeout_ms=1000)
