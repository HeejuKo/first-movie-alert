import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "review_plan", Path(__file__).parents[1] / "scripts/review_plan.py"
)
review = importlib.util.module_from_spec(spec)
spec.loader.exec_module(review)


def plan_with(actions):
    return {
        "prior_state": {
            "values": {
                "root_module": {"resources": [{"address": name} for name in review.REQUIRED]}
            }
        },
        "resource_changes": [
            {"address": "aws_lambda_function.checker[0]", "change": {"actions": actions}}
        ],
    }


def test_allows_existing_function_update():
    assert review.validate_plan(plan_with(["update"])) == ["aws_lambda_function.checker[0]"]


@pytest.mark.parametrize(
    "actions", [["create"], ["delete"], ["delete", "create"], ["create", "delete"]]
)
def test_new_resources_deletion_and_replacement_require_manual_review(actions):
    with pytest.raises(ValueError):
        review.validate_plan(plan_with(actions))


def test_empty_remote_state_cannot_recreate_existing_resources():
    with pytest.raises(ValueError):
        review.validate_plan({"resource_changes": []})


def test_missing_notification_table_state_cannot_pass():
    plan = plan_with(["update"])
    resources = plan["prior_state"]["values"]["root_module"]["resources"]
    resources[:] = [r for r in resources if r["address"] != "aws_dynamodb_table.notifications"]
    with pytest.raises(ValueError):
        review.validate_plan(plan)


def test_plan_is_read_with_its_initialized_provider_directory(monkeypatch, tmp_path):
    plan = tmp_path / "infra" / "lambda.tfplan"
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(plan_with(["update"])))

    monkeypatch.setattr(sys, "argv", ["review_plan.py", "--plan", str(plan)])
    monkeypatch.setattr(review.subprocess, "run", run)
    review.main()
    assert commands == [["terraform", f"-chdir={plan.parent}", "show", "-json", "lambda.tfplan"]]
