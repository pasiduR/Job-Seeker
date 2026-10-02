import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.llm.schemas import (
    FormMapperOutput,
    ListingExtractorOutput,
    ScorerOutput,
    TailorOutput,
)


@pytest.fixture
def schema_outputs(project_root: Path) -> dict[str, object]:
    return json.loads(
        (project_root / "tests/fixtures/schema_outputs.json").read_text(
            encoding="utf-8"
        )
    )


def test_all_llm_output_contracts_accept_their_fixtures(
    schema_outputs: dict[str, object],
) -> None:
    assert ScorerOutput.model_validate(schema_outputs["scorer"]).score == 8
    assert TailorOutput.model_validate(schema_outputs["tailor"]).added_skills[0].est_days == 5
    assert FormMapperOutput.model_validate(schema_outputs["form_mapper"]).answers[1].value is None
    assert len(
        ListingExtractorOutput.model_validate(schema_outputs["listing_extractor"]).listings
    ) == 1


def test_scorer_contract_rejects_out_of_range_score(
    schema_outputs: dict[str, object],
) -> None:
    scorer = dict(schema_outputs["scorer"])
    scorer["score"] = 11

    with pytest.raises(ValidationError):
        ScorerOutput.model_validate(scorer)


def test_contracts_reject_unexpected_fields(
    schema_outputs: dict[str, object],
) -> None:
    scorer = dict(schema_outputs["scorer"])
    scorer["model_commentary"] = "not part of the contract"

    with pytest.raises(ValidationError):
        ScorerOutput.model_validate(scorer)
