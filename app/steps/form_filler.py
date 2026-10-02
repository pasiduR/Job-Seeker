"""The one agentic step: the LLM picks form-filling tools, code runs them.

The model only chooses which tool to call next. Values always come from the
code-checked form mapper, click_next refuses final-submit buttons, and step
and page limits end the run as ``needs_manual``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from app.browser.fields import FormField
from app.config import RuntimeSettings
from app.browser.page import FormPage, PageButton
from app.browser.stop_conditions import find_blocker
from app.llm.schemas import AnswerSource, FormAgentAction
from app.steps.form_mapper import RESUME_UPLOAD, FieldAnswer, FormMapper


PROMPT_VERSION = "form_fill_v1"
PROMPT_PATH = Path(__file__).parents[1] / "llm" / "prompts" / f"{PROMPT_VERSION}.md"
PAGE_TEXT_CHARS = 3000
HISTORY_LINES = 10
MAX_REPEATS = 3


class FormAgentClient(Protocol):
    def generate(
        self,
        *,
        schema: type[FormAgentAction],
        job_id: int | None,
        step: str,
        prompt_version: str,
        model: str,
        prompt: str,
        untrusted_data: Mapping[str, str],
    ) -> FormAgentAction: ...


class FillOutcome(StrEnum):
    FILLED = "filled"
    NEEDS_MANUAL = "needs_manual"


@dataclass(frozen=True)
class FillLimits:
    max_steps: int = 25
    max_pages: int = 6

    @classmethod
    def from_settings(cls, settings: RuntimeSettings) -> FillLimits:
        return cls(max_steps=settings.form_max_steps, max_pages=settings.form_max_pages)


@dataclass(frozen=True)
class TraceEntry:
    sequence: int
    tool: str
    input: dict[str, Any]
    result: dict[str, Any]
    screenshot_path: str | None = None


@dataclass(frozen=True)
class FillResult:
    outcome: FillOutcome
    reason: str
    trace: tuple[TraceEntry, ...]
    answers: tuple[FieldAnswer, ...]
    screenshot_path: str | None


class StopFilling(Exception):
    """A stop condition: the job goes to ``needs_manual`` with this reason."""


def _signature(form_field: FormField) -> tuple[Any, ...]:
    return (form_field.frame, form_field.label, form_field.type, tuple(form_field.options))


@dataclass
class _FillState:
    job_id: int
    page: FormPage
    profile: Mapping[str, Any]
    cv_text: str
    cv_pdf: Path
    screenshot_dir: Path
    start_url: str = ""
    pages_visited: int = 1
    fields: dict[str, FormField] = field(default_factory=dict)
    answers: dict[str, FieldAnswer] = field(default_factory=dict)
    filled: set[str] = field(default_factory=set)
    buttons: dict[str, PageButton] = field(default_factory=dict)
    trace: list[TraceEntry] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    last_screenshot: str | None = None
    last_action: tuple[Any, ...] | None = None
    repeats: int = 0

    def answer_for(self, form_field: FormField) -> FieldAnswer | None:
        answer = self.answers.get(form_field.field_id)
        if answer is None or _signature(answer.field) != _signature(form_field):
            return None
        return answer

    def all_answers(self) -> tuple[FieldAnswer, ...]:
        return tuple(self.answers.values())


class FormFiller:
    def __init__(
        self,
        *,
        llm: FormAgentClient,
        mapper: FormMapper,
        model: str,
        limits: FillLimits = FillLimits(),
    ) -> None:
        self._llm = llm
        self._mapper = mapper
        self._model = model
        self._limits = limits

    def run(
        self,
        *,
        job_id: int,
        page: FormPage,
        profile: Mapping[str, Any],
        cv_text: str,
        cv_pdf: Path,
        screenshot_dir: Path,
    ) -> FillResult:
        state = _FillState(
            job_id, page, profile, cv_text, cv_pdf, screenshot_dir, start_url=page.url
        )
        try:
            self._check_page(state)
            self._call_tool(state, "extract_fields", {})
            for step in range(1, self._limits.max_steps + 1):
                self._check_page(state)
                action = self._next_action(state, steps_left=self._limits.max_steps - step + 1)
                self._check_repeats(state, action)
                if action.tool == "stop":
                    raise StopFilling(f"agent stopped: {action.reason or 'no reason given'}")
                if action.tool == "done":
                    missing = self._unfilled_required(state)
                    if not missing:
                        self._call_tool(state, "screenshot", {"final": True})
                        return self._result(state, FillOutcome.FILLED, "form filled; ready for review")
                    state.history.append(
                        f"done -> refused: required fields not filled: {', '.join(missing)}"
                    )
                    continue
                self._call_tool(state, action.tool, _tool_input(action))
            raise StopFilling(f"step limit of {self._limits.max_steps} reached")
        except StopFilling as stop:
            return self._result(state, FillOutcome.NEEDS_MANUAL, str(stop))

    # --- stop conditions --------------------------------------------------------

    def _check_page(self, state: _FillState) -> None:
        blocker = find_blocker(
            url=state.page.url, start_url=state.start_url, signals=state.page.signals()
        )
        if blocker is not None:
            raise StopFilling(blocker)

    def _check_repeats(self, state: _FillState, action: FormAgentAction) -> None:
        key = (action.tool, action.field_id, action.button_id)
        state.repeats = state.repeats + 1 if key == state.last_action else 1
        state.last_action = key
        if state.repeats >= MAX_REPEATS:
            raise StopFilling(f"same action repeated {MAX_REPEATS} times: {action.tool}")

    # --- agent ----------------------------------------------------------------

    def _next_action(self, state: _FillState, *, steps_left: int) -> FormAgentAction:
        state.buttons = {button.button_id: button for button in state.page.buttons()}
        fields = "\n".join(
            json.dumps(
                {
                    **form_field.for_llm(),
                    "answer": _answer_status(state.answer_for(form_field)),
                    "filled": form_field.field_id in state.filled,
                },
                ensure_ascii=False,
            )
            for form_field in state.fields.values()
        )
        status = {
            "url": state.page.url,
            "pages_visited": state.pages_visited,
            "max_pages": self._limits.max_pages,
            "steps_left": steps_left,
        }
        return self._llm.generate(
            schema=FormAgentAction,
            job_id=state.job_id,
            step="form_filler",
            prompt_version=PROMPT_VERSION,
            model=self._model,
            prompt=PROMPT_PATH.read_text(encoding="utf-8"),
            untrusted_data={
                "status": json.dumps(status),
                "fields": fields,
                "buttons": "\n".join(
                    json.dumps(button.for_llm(), ensure_ascii=False)
                    for button in state.buttons.values()
                ),
                "page_text": state.page.text()[:PAGE_TEXT_CHARS],
                "history": "\n".join(state.history[-HISTORY_LINES:]),
            },
        )

    # --- tools ----------------------------------------------------------------

    def _call_tool(self, state: _FillState, tool: str, tool_input: dict[str, Any]) -> None:
        handlers = {
            "extract_fields": self._extract_fields,
            "fill_field": self._fill_field,
            "upload_file": self._upload_file,
            "click_next": self._click_next,
            "screenshot": self._screenshot,
        }
        screenshot_before = state.last_screenshot
        try:
            result = handlers[tool](state, tool_input)
        except StopFilling as stop:
            result = {"error": str(stop), "stopped": True}
            self._record(state, tool, tool_input, result, screenshot_before)
            raise
        except Exception as exc:  # Browser errors go back to the agent as results.
            result = {"error": f"{type(exc).__name__}: {exc}"[:500]}
        self._record(state, tool, tool_input, result, screenshot_before)

    def _record(
        self,
        state: _FillState,
        tool: str,
        tool_input: dict[str, Any],
        result: dict[str, Any],
        screenshot_before: str | None,
    ) -> None:
        new_screenshot = state.last_screenshot if state.last_screenshot != screenshot_before else None
        state.trace.append(
            TraceEntry(
                sequence=len(state.trace) + 1,
                tool=tool,
                input=tool_input,
                result=result,
                screenshot_path=new_screenshot,
            )
        )
        state.history.append(
            f"{tool} {json.dumps(tool_input, sort_keys=True)} -> {json.dumps(result, sort_keys=True)}"
        )

    def _extract_fields(self, state: _FillState, _: dict[str, Any]) -> dict[str, Any]:
        current = state.page.fields()
        if not current:
            raise StopFilling("unexpected page: no form fields found")
        state.fields = {form_field.field_id: form_field for form_field in current}
        state.filled &= set(state.fields)
        unmapped = [form_field for form_field in current if state.answer_for(form_field) is None]
        if unmapped:
            mapped = self._mapper.run(
                job_id=state.job_id,
                fields=unmapped,
                profile=state.profile,
                cv_text=state.cv_text,
            )
            for answer in mapped.answers:
                state.answers[answer.field.field_id] = answer
            if mapped.needs_manual_reasons:
                raise StopFilling("; ".join(mapped.needs_manual_reasons))
        return {"fields": len(current), "newly_mapped": len(unmapped)}

    def _field(self, state: _FillState, tool_input: dict[str, Any]) -> tuple[FormField, FieldAnswer]:
        form_field = state.fields.get(str(tool_input.get("field_id")))
        if form_field is None:
            raise ValueError("unknown field_id; call extract_fields")
        answer = state.answer_for(form_field)
        if answer is None or answer.source == AnswerSource.UNKNOWN:
            raise ValueError("no approved answer for this field")
        return form_field, answer

    def _fill_field(self, state: _FillState, tool_input: dict[str, Any]) -> dict[str, Any]:
        form_field, answer = self._field(state, tool_input)
        if form_field.type == "file" or answer.value is None:
            raise ValueError("use upload_file for file fields")
        state.page.fill(form_field, answer.value)
        state.filled.add(form_field.field_id)
        return {"ok": True}

    def _upload_file(self, state: _FillState, tool_input: dict[str, Any]) -> dict[str, Any]:
        form_field, answer = self._field(state, tool_input)
        if form_field.type != "file" or answer.value != RESUME_UPLOAD:
            raise ValueError("only the tailored CV can be uploaded, to a file field")
        state.page.upload(form_field, state.cv_pdf)
        state.filled.add(form_field.field_id)
        return {"ok": True, "file": state.cv_pdf.name}

    def _click_next(self, state: _FillState, tool_input: dict[str, Any]) -> dict[str, Any]:
        button = state.buttons.get(str(tool_input.get("button_id")))
        if button is None:
            raise ValueError("unknown button_id")
        if button.looks_like_submit:
            raise ValueError("refused: this looks like the final submit button")
        if state.pages_visited >= self._limits.max_pages:
            raise StopFilling(f"page limit of {self._limits.max_pages} reached")
        state.page.click(button)
        state.pages_visited += 1
        # Element tags may not survive navigation; the agent re-extracts.
        state.fields = {}
        state.filled = set()
        return {"ok": True, "url": state.page.url, "next": "call extract_fields"}

    def _screenshot(self, state: _FillState, tool_input: dict[str, Any]) -> dict[str, Any]:
        name = "final.png" if tool_input.get("final") else f"step_{len(state.trace) + 1:02d}.png"
        path = state.screenshot_dir / name
        state.page.screenshot(path)
        state.last_screenshot = str(path)
        return {"ok": True, "path": str(path)}

    # --- results --------------------------------------------------------------

    def _unfilled_required(self, state: _FillState) -> list[str]:
        return [
            form_field.label or form_field.field_id
            for form_field in state.fields.values()
            if form_field.required and form_field.field_id not in state.filled
        ]

    def _result(self, state: _FillState, outcome: FillOutcome, reason: str) -> FillResult:
        final = state.last_screenshot if outcome == FillOutcome.FILLED else None
        return FillResult(
            outcome=outcome,
            reason=reason,
            trace=tuple(state.trace),
            answers=state.all_answers(),
            screenshot_path=final,
        )


def _tool_input(action: FormAgentAction) -> dict[str, Any]:
    if action.tool in {"fill_field", "upload_file"}:
        return {"field_id": action.field_id}
    if action.tool == "click_next":
        return {"button_id": action.button_id}
    return {}


def _answer_status(answer: FieldAnswer | None) -> str:
    if answer is None:
        return "not_mapped"
    if answer.source == AnswerSource.UNKNOWN:
        return "unknown"
    return "upload_cv" if answer.value == RESUME_UPLOAD else "ready"
