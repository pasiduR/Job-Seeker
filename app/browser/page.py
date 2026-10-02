"""The browser operations behind the form-filling tools, on a Playwright page."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from app.browser.fields import FormField, extract_fields
from app.browser.stop_conditions import SIGNALS_SCRIPT, PageSignals


BUTTONS_SCRIPT = (Path(__file__).parent / "extract_buttons.js").read_text(encoding="utf-8")
MAX_BUTTON_TEXT = 120
# Buttons whose text looks like a final submit. click_next refuses them: the
# application is submitted only after approval, by separate code.
SUBMIT_TEXT = re.compile(
    r"\b(submit|send|finish|complete|confirm|apply|done)\b", re.IGNORECASE
)


class PageButton(BaseModel):
    model_config = ConfigDict(frozen=True)

    button_id: str
    text: str
    is_submit_type: bool = False
    selector: str
    frame: int = 0

    @property
    def looks_like_submit(self) -> bool:
        return SUBMIT_TEXT.search(self.text) is not None

    def for_llm(self) -> dict[str, Any]:
        return {
            "button_id": self.button_id,
            "text": self.text[:MAX_BUTTON_TEXT],
            "final_submit": self.looks_like_submit,
        }


class FormPage(Protocol):
    """What the form-filling tools may do to a page; nothing else is exposed."""

    @property
    def url(self) -> str: ...

    def text(self) -> str: ...

    def fields(self) -> list[FormField]: ...

    def buttons(self) -> list[PageButton]: ...

    def signals(self) -> list[PageSignals]: ...

    def fill(self, field: FormField, value: str | bool | list[str]) -> None: ...

    def upload(self, field: FormField, path: Path) -> None: ...

    def click(self, button: PageButton) -> None: ...

    def screenshot(self, path: Path) -> None: ...


class PlaywrightFormPage:
    def __init__(self, page: Any, *, timeout_ms: int) -> None:
        self._page = page
        self._timeout_ms = timeout_ms

    @property
    def url(self) -> str:
        return str(self._page.url)

    def text(self) -> str:
        return str(self._page.locator("body").inner_text(timeout=self._timeout_ms))

    def fields(self) -> list[FormField]:
        return extract_fields(self._page)

    def buttons(self) -> list[PageButton]:
        found: list[PageButton] = []
        for index, frame in enumerate(self._page.frames):
            try:
                raw_buttons = frame.evaluate(BUTTONS_SCRIPT, f"b{index}_")
            except Exception:
                continue
            found.extend(PageButton(**raw, frame=index) for raw in raw_buttons)
        return found

    def signals(self) -> list[PageSignals]:
        found: list[PageSignals] = []
        for frame in self._page.frames:
            try:
                found.append(PageSignals(**frame.evaluate(SIGNALS_SCRIPT)))
            except Exception:
                continue
        return found

    def fill(self, field: FormField, value: str | bool | list[str]) -> None:
        locator = self._frame(field.frame).locator(field.selector)
        if field.type == "select":
            locator.select_option(label=str(value), timeout=self._timeout_ms)
        elif field.type == "checkbox":
            locator.set_checked(bool(value), timeout=self._timeout_ms)
        elif field.type in {"radio_group", "checkbox_group"}:
            wanted = set(value) if isinstance(value, list) else {str(value)}
            for member in locator.all():
                label = member.evaluate(
                    "el => ((el.labels && el.labels[0] && el.labels[0].textContent) "
                    "|| el.value || '').replace(/\\s+/g, ' ').trim()"
                )
                if field.type == "radio_group" and label in wanted:
                    member.check(timeout=self._timeout_ms)
                elif field.type == "checkbox_group":
                    member.set_checked(label in wanted, timeout=self._timeout_ms)
        elif field.type == "combobox":
            locator.fill(str(value), timeout=self._timeout_ms)
            locator.press("Enter", timeout=self._timeout_ms)
        else:
            locator.fill(str(value), timeout=self._timeout_ms)

    def upload(self, field: FormField, path: Path) -> None:
        self._frame(field.frame).locator(field.selector).set_input_files(
            str(path), timeout=self._timeout_ms
        )

    def click(self, button: PageButton) -> None:
        self._frame(button.frame).locator(button.selector).click(timeout=self._timeout_ms)
        try:
            self._page.wait_for_load_state("domcontentloaded", timeout=self._timeout_ms)
        except Exception:  # Single-page forms do not navigate.
            pass

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._page.screenshot(path=str(path), full_page=True, timeout=self._timeout_ms)

    def _frame(self, index: int) -> Any:
        return self._page.frames[index]
