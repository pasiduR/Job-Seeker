"""Conservative removal of instruction-like lines from scraped text."""

from __future__ import annotations

import re


_REMOVED_MARKER = "[removed untrusted instruction]"
_INSTRUCTION_PATTERNS = tuple(
    re.compile(pattern, flags=re.IGNORECASE)
    for pattern in (
        r"^\s*(ignore|disregard|forget)\b.*\b(instructions?|prompts?|rules?)\b",
        r"^\s*(system|developer|assistant|user)\s*(message|prompt)?\s*:",
        r"^\s*you\s+are\s+(now|an?\b|the\b)",
        r"^\s*(your\s+)?(new\s+)?(task|instruction)\s+is\b",
        r"^\s*(output|return|respond\s+with)\s+(only|exactly)\b",
        r"^\s*(prompt\s+injection|jailbreak)\b",
        r"^\s*<\s*/?\s*(system|developer|assistant|user)\b",
        r"^\s*-{0,3}\s*(begin|end)\s+(system|developer|assistant|user|untrusted)\b",
    )
)


def sanitize_scraped_text(text: str) -> str:
    """Replace likely prompt instructions while retaining ordinary page content."""

    cleaned_lines: list[str] = []
    for raw_line in text.splitlines():
        line = _strip_unsafe_control_characters(raw_line)
        if any(pattern.search(line) for pattern in _INSTRUCTION_PATTERNS):
            cleaned_lines.append(_REMOVED_MARKER)
        else:
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines)


def _strip_unsafe_control_characters(value: str) -> str:
    return "".join(
        character
        for character in value
        if character == "\t" or ord(character) >= 32
    )
