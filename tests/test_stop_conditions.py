import pytest

from app.browser.stop_conditions import PageSignals, find_blocker


START = "https://boards.greenhouse.io/acme/jobs/123"


@pytest.mark.parametrize(
    ("url", "signals", "reason"),
    [
        (START, [PageSignals()], None),
        (START + "#step-2", [PageSignals(text="Tell us about yourself")], None),
        (START, [PageSignals(), PageSignals(captcha=True)], "CAPTCHA detected"),
        (START, [PageSignals(text="Please verify you are a human")], "CAPTCHA detected"),
        (START, [PageSignals(text="Checking your browser before accessing")], "CAPTCHA detected"),
        ("https://accounts.google.com/o/oauth2/auth", [PageSignals()], "login wall at accounts.google.com"),
        ("https://boards.greenhouse.io/users/sign-in", [PageSignals()], "login wall at boards.greenhouse.io"),
        (START, [PageSignals(password_fields=1)], "login wall: the page asks for a password"),
        (START, [PageSignals(text="Sign in to apply for this role")], "login wall: the page asks to sign in"),
        ("https://www.linkedin.com/jobs/view/1", [PageSignals()], "LinkedIn page: apply manually (no LinkedIn automation)"),
        ("https://evil.example.net/form", [PageSignals()], "unexpected page: left greenhouse.io for evil.example.net"),
    ],
)
def test_find_blocker(url: str, signals: list[PageSignals], reason: str | None) -> None:
    assert find_blocker(url=url, start_url=START, signals=signals) == reason
