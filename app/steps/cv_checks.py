"""Deterministic guards applied to tailored CVs after the LLM returns."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from app.llm.schemas import AddedSkill
from app.steps.latex import latex_to_text


SkillPlacement = Literal["skills_section", "currently_learning"]

_SHORT_SEGMENT_WORDS = 6
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#]*(?:[.\-][A-Za-z0-9+#]+)*")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_MONTH_DATE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{4})\b",
    re.IGNORECASE,
)
_DEGREE = re.compile(
    r"\b(B\.?\s?Sc|M\.?\s?Sc|B\.?\s?Eng|M\.?\s?Eng|B\.?\s?Tech|M\.?\s?Tech|"
    r"B\.?A|M\.?A|B\.?S|M\.?S|Ph\.?\s?D|MBA|Bachelor|Master|Doctor(?:ate)?|"
    r"Diploma|Associate|Certificat(?:e|ion))\b"
)
_SEGMENT_SPLIT = re.compile(r"\n|(?<=[.!?:;])\s+|\s+--+\s+|\s+\|\s+|\s+·\s+")
_UNSAFE_COMMANDS = re.compile(
    r"\\(write18|immediate|openout|openin|input|include|InputIfFileExists|read|"
    r"directlua|catcode|ShellEscape)\b"
)
_SKILLS_SECTION = re.compile(r"\\section\*?\{[^}]*skills[^}]*\}", re.IGNORECASE)
_SECTION_END = re.compile(r"\\section\*?\{|\\end\{document\}")
_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_PLACEMENT_LABELS = {
    "currently_learning": "Currently learning:",
    "skills_section": "Additional skills:",
}


@dataclass(frozen=True)
class SkillLimitResult:
    kept: list[AddedSkill]
    dropped: list[str]


def find_unsafe_commands(base_tex: str, tailored_tex: str) -> list[str]:
    """LaTeX commands that read/write files or escape to a shell, new in the output."""

    allowed = set(_UNSAFE_COMMANDS.findall(base_tex))
    return sorted(set(_UNSAFE_COMMANDS.findall(tailored_tex)) - allowed)


def find_invented_entities(
    base_tex: str, tailored_tex: str, *, allowed_terms: Iterable[str] = ()
) -> list[str]:
    """Names, degrees, dates, and numbers in the output that the base CV lacks."""

    allowed_terms = list(allowed_terms)
    base_text = latex_to_text(base_tex)
    tailored_text = latex_to_text(tailored_tex)
    vocabulary = {word.lower() for word in _WORD.findall(base_text)}
    vocabulary.update(
        word.lower() for term in allowed_terms for word in _WORD.findall(term)
    )

    violations: list[str] = []
    for date in sorted(_month_dates(tailored_text) - _month_dates(base_text)):
        violations.append(f"date: {date}")
    allowed_numbers = _numbers(base_text) | _numbers(" ".join(allowed_terms))
    for number in sorted(_numbers(tailored_text) - allowed_numbers):
        violations.append(f"number: {number}")
    for degree in sorted(_degrees(tailored_text) - _degrees(base_text)):
        violations.append(f"degree: {degree}")
    for name in sorted(_proper_nouns(tailored_text)):
        if name.lower() not in vocabulary:
            violations.append(f"name: {name}")
    return violations


def _month_dates(text: str) -> set[str]:
    return {
        f"{month[:3].lower()} {year}" for month, year in _MONTH_DATE.findall(text)
    }


def _numbers(text: str) -> set[str]:
    return {number.replace(",", "") for number in _NUMBER.findall(text)}


def _degrees(text: str) -> set[str]:
    return {re.sub(r"[.\s]", "", degree).lower() for degree in _DEGREE.findall(text)}


def _proper_nouns(text: str) -> set[str]:
    """Capitalised words, ignoring the first word of sentence-like segments."""

    names: set[str] = set()
    for segment in _SEGMENT_SPLIT.split(text):
        words = _WORD.findall(segment)
        candidates = words if len(words) <= _SHORT_SEGMENT_WORDS else words[1:]
        names.update(word for word in candidates if word[0].isupper())
    return names


def limit_added_skills(
    skills: Sequence[AddedSkill],
    *,
    base_tex: str,
    max_days: int,
    max_count: int,
) -> SkillLimitResult:
    base_text = latex_to_text(base_tex).lower()
    kept: list[AddedSkill] = []
    dropped: list[str] = []
    seen: set[str] = set()
    for skill in skills:
        name = skill.skill.strip()
        key = name.lower()
        if not name or key in seen:
            dropped.append(f"{name or '(blank)'}: duplicate or blank")
        elif re.search(rf"(?<!\w){re.escape(key)}(?!\w)", base_text):
            dropped.append(f"{name}: already in base CV")
        elif skill.est_days > max_days:
            dropped.append(f"{name}: {skill.est_days} days exceeds limit {max_days}")
        elif len(kept) >= max_count:
            dropped.append(f"{name}: exceeds limit of {max_count} skills")
        else:
            kept.append(skill.model_copy(update={"skill": name}))
        seen.add(key)
    return SkillLimitResult(kept=kept, dropped=dropped)


def escape_latex(text: str) -> str:
    return "".join(_LATEX_SPECIALS.get(character, character) for character in text)


def placement_terms(skills: Sequence[AddedSkill], placement: SkillPlacement) -> list[str]:
    """Words that the placement step itself may introduce into the CV."""

    return [_PLACEMENT_LABELS[placement], *(skill.skill for skill in skills)]


def apply_skill_placement(
    tex: str, skills: Sequence[AddedSkill], placement: SkillPlacement
) -> str:
    if not skills:
        return tex
    names = ", ".join(escape_latex(skill.skill) for skill in skills)
    section = _SKILLS_SECTION.search(tex)
    if section is None:
        heading = "Currently Learning" if placement == "currently_learning" else "Skills"
        return _insert_before_end_document(tex, f"\\section{{{heading}}}\n{names}\n\n")

    end_match = _SECTION_END.search(tex, section.end())
    end = end_match.start() if end_match else len(tex)
    body = tex[section.end() : end]

    if placement == "skills_section":
        appended = _append_to_list_line(body, names)
        if appended is not None:
            return tex[: section.end()] + appended + tex[end:]

    label = escape_latex(_PLACEMENT_LABELS[placement])
    line = f"\\textbf{{{label}}} {names}\n\n"
    new_body = body.rstrip() + "\n\n" + line
    return tex[: section.end()] + new_body + tex[end:]


def _append_to_list_line(body: str, names: str) -> str | None:
    """Extend a plain comma-separated skills line, if the section ends with one."""

    lines = body.rstrip().split("\n")
    last = lines[-1].rstrip()
    if "," not in last or "\\" in last or "{" in last:
        return None
    lines[-1] = f"{last}, {names}"
    return "\n".join(lines) + body[len(body.rstrip()) :]


def _insert_before_end_document(tex: str, snippet: str) -> str:
    marker = "\\end{document}"
    index = tex.rfind(marker)
    if index == -1:
        return tex + "\n" + snippet
    return tex[:index] + snippet + tex[index:]
