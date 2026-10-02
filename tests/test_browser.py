from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.browser.fields import FormField, extract_fields, normalize


@pytest.fixture(scope="module")
def page() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def by_label(fields: list[FormField]) -> dict[str, FormField]:
    return {field.label: field for field in fields}


def test_extracts_labels_types_options_and_required(page: Any, project_root: Path) -> None:
    page.set_content(
        (project_root / "tests/fixtures/forms/greenhouse_like.html").read_text(encoding="utf-8")
    )

    fields = by_label(extract_fields(page))

    assert list(fields) == [
        "First Name *",
        "Email",
        "Phone",
        "Resume/CV",
        "Why do you want to work here?",
        "Country *",
        "Are you legally authorized to work in the UK? *",
        "Which stacks have you used?",
        "I agree to the privacy notice",
        "Location (city)",
    ]
    assert fields["First Name *"].required and fields["First Name *"].type == "text"
    assert fields["Email"].required and fields["Email"].type == "email"
    assert not fields["Phone"].required and fields["Phone"].placeholder == "+1 555 0100"
    resume = fields["Resume/CV"]
    assert (resume.type, resume.required, resume.accept) == ("file", True, ".pdf,.doc,.docx")
    assert fields["Why do you want to work here?"].type == "textarea"
    assert fields["Country *"].options == ["Sri Lanka", "United Kingdom"]
    assert fields["Country *"].required
    work_auth = fields["Are you legally authorized to work in the UK? *"]
    assert (work_auth.type, work_auth.options, work_auth.required) == (
        "radio_group",
        ["Yes", "No"],
        True,
    )
    stacks = fields["Which stacks have you used?"]
    assert (stacks.type, stacks.options, stacks.multiple) == ("checkbox_group", ["Python", "Go"], True)
    assert fields["I agree to the privacy notice"].type == "checkbox"
    assert fields["I agree to the privacy notice"].required
    assert fields["Location (city)"].type == "combobox"


def test_selectors_locate_each_field_and_whole_groups(page: Any, project_root: Path) -> None:
    page.set_content(
        (project_root / "tests/fixtures/forms/greenhouse_like.html").read_text(encoding="utf-8")
    )

    fields = by_label(extract_fields(page))

    assert page.locator(fields["Email"].selector).get_attribute("id") == "email"
    work_auth = fields["Are you legally authorized to work in the UK? *"]
    assert page.locator(work_auth.selector).count() == 2
    assert len({field.field_id for field in fields.values()}) == len(fields)


def test_fields_inside_iframes_carry_their_frame_index(page: Any) -> None:
    page.set_content(
        '<label for="a">Top</label><input id="a">'
        '<iframe srcdoc="<label for=b>Embedded *</label><input id=b>"></iframe>'
    )
    page.wait_for_function("document.querySelector('iframe').contentDocument.getElementById('b')")

    fields = by_label(extract_fields(page))

    assert fields["Top"].frame == 0
    embedded = fields["Embedded *"]
    assert embedded.frame == 1 and embedded.required
    assert embedded.field_id != fields["Top"].field_id
    assert page.frames[1].locator(embedded.selector).count() == 1


def test_for_llm_omits_selectors_and_unknown_types_become_other() -> None:
    field = normalize(
        {"field_id": "f0_1", "label": "x" * 400, "type": "color", "required": False,
         "options": [], "selector": '[data-jobseeker-field="f0_1"]'},
        frame=0,
    )

    assert field.type == "other"
    assert len(field.label) == 300
    assert field.for_llm() == {
        "field_id": "f0_1", "label": "x" * 300, "type": "other", "required": False, "options": []
    }
