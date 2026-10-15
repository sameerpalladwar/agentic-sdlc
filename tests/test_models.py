import pytest
from pydantic import ValidationError

from models.requirements import RequirementsOutput


def test_model_round_trip(payload):
    result = RequirementsOutput.model_validate(payload)
    assert RequirementsOutput.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("change", [
    "priority", "criteria", "blank", "duplicate", "clarification", "empty_stories", "extra",
])
def test_invalid_models(payload, change):
    if change == "priority":
        payload["user_stories"][0]["priority"] = "urgent"
    elif change == "criteria":
        payload["user_stories"][0]["acceptance_criteria"] = []
    elif change == "blank":
        payload["user_stories"][0]["acceptance_criteria"][0]["then"] = " "
    elif change == "duplicate":
        payload["user_stories"] *= 2
    elif change == "clarification":
        payload["requires_human_clarification"] = True
    elif change == "empty_stories":
        payload["user_stories"] = []
    else:
        payload["unexpected"] = True
    with pytest.raises(ValidationError):
        RequirementsOutput.model_validate(payload)
