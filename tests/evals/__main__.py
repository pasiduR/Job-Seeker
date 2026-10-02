"""Run the scorer and tailor evals against the real LLM.

``python -m tests.evals [scorer|tailor|form_mapper|all] [--model M] [--baseline FILE]``

This spends API credit (each run is well under $1 at Sonnet prices). Results
go to ``tests/evals/results/<prompt_version>__<model>.json`` so a new prompt
version can be compared against the saved baseline with ``--baseline``.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config import RuntimeSettings, SecretSettings
from app.llm.client import AnthropicTransport, LLMCallLogger, LLMClient, LLMTransport
from app.steps.latex import LatexCompiler
from tests.evals.harness import (
    Compiler,
    EvalReport,
    evaluate_form_mapper,
    evaluate_scorer,
    evaluate_tailor,
    offline_field_extractor,
    regressions,
)


RESULTS_DIR = Path(__file__).parent / "results"
# Column order of the llm_calls INSERT in LLMCallLogger.
_INPUT_TOKENS, _OUTPUT_TOKENS, _COST, _VALID = 4, 5, 6, 8


class MemoryCallLog:
    """Stands in for the llm_calls table so evals need no database."""

    def __init__(self) -> None:
        self.rows: list[tuple[object, ...]] = []

    def transaction(self) -> nullcontext[None]:
        return nullcontext()

    def execute(self, query: str, params: tuple[object, ...] | None = None) -> list[Any]:
        if params is not None:
            self.rows.append(params)
        return []

    def totals(self) -> dict[str, Any]:
        def total(index: int) -> Any:
            return sum((row[index] or 0 for row in self.rows), start=0)

        return {
            "calls": len(self.rows),
            "invalid_calls": sum(1 for row in self.rows if row[_VALID] is False),
            "input_tokens": total(_INPUT_TOKENS),
            "output_tokens": total(_OUTPUT_TOKENS),
            "cost_usd": str(sum((Decimal(row[_COST] or 0) for row in self.rows), Decimal(0))),
        }


def run_eval(
    step: str,
    transport: LLMTransport,
    compiler: Compiler,
    *,
    model: str,
) -> dict[str, Any]:
    log = MemoryCallLog()
    client: LLMClient = LLMClient(transport, LLMCallLogger(log))
    if step == "scorer":
        report = evaluate_scorer(client, model=model)
    elif step == "form_mapper":
        with offline_field_extractor() as extract:
            report = evaluate_form_mapper(client, extract, model=model)
    else:
        report = evaluate_tailor(client, compiler, model=model)
    return {
        "step": step,
        "model": model,
        "summary": report.summary(),
        "failures": report.failures,
        "usage": log.totals(),
    }


def save_result(result: dict[str, Any], results_dir: Path = RESULTS_DIR) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    version = result["summary"]["prompt_version"]
    path = results_dir / f"{version}__{result['model']}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return path


def compare(baseline_path: Path, result: dict[str, Any]) -> list[str]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    return regressions(EvalReport(**baseline["summary"]), EvalReport(**result["summary"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "step", choices=["scorer", "tailor", "form_mapper", "all"], nargs="?", default="all"
    )
    parser.add_argument("--model", help="defaults to the llm_model setting")
    parser.add_argument("--baseline", type=Path, help="earlier result JSON to compare against")
    args = parser.parse_args(argv)

    settings = RuntimeSettings()
    model = args.model or settings.llm_model
    transport = AnthropicTransport.from_config(SecretSettings(), settings)
    steps = ["scorer", "tailor", "form_mapper"] if args.step == "all" else [args.step]

    exit_code = 0
    for step in steps:
        result = run_eval(step, transport, LatexCompiler(), model=model)
        path = save_result(result)
        print(json.dumps({"saved": str(path), **result}, indent=2))
        if args.baseline is not None and len(steps) == 1:
            problems = compare(args.baseline, result)
            print("regressions:", problems or "none")
            exit_code = 1 if problems else exit_code
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
