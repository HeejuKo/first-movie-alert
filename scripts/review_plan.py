"""Reject missing production state and resource creation, deletion or replacement."""

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

REQUIRED = {
    "aws_dynamodb_table.notifications",
    "aws_lambda_function.checker[0]",
    "aws_scheduler_schedule.checker[0]",
}


def resource_addresses(module: dict[str, Any]) -> set[str]:
    addresses = {resource["address"] for resource in module.get("resources", [])}
    for child in module.get("child_modules", []):
        addresses.update(resource_addresses(child))
    return addresses


def validate_plan(plan: dict[str, Any]) -> list[str]:
    prior = plan.get("prior_state", {}).get("values", {}).get("root_module", {})
    if REQUIRED - resource_addresses(prior):
        raise ValueError("기존 운영 state가 없습니다. 마이그레이션을 확인하세요.")
    changes = []
    for resource in plan.get("resource_changes", []):
        actions = resource["change"]["actions"]
        if "delete" in actions or "create" in actions:
            raise ValueError(
                "리소스 생성·삭제·교체는 자동 배포하지 않습니다. 별도 검토가 필요합니다."
            )
        if actions != ["no-op"]:
            changes.append(resource["address"])
    return changes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform", default="terraform")
    parser.add_argument("--plan", type=Path, default=Path("infra/lambda.tfplan"))
    args = parser.parse_args()
    # Plan JSON can contain secrets. Capture only in memory and print addresses, never values.
    result = subprocess.run(
        [
            args.terraform,
            f"-chdir={args.plan.resolve().parent}",
            "show",
            "-json",
            args.plan.name,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        parser.exit(1, "Terraform plan을 읽지 못했습니다. 원문은 민감값 보호를 위해 생략합니다.\n")
    try:
        changes = validate_plan(json.loads(result.stdout))
    except (ValueError, KeyError, TypeError):
        parser.exit(
            1, "운영 state 누락·생성·삭제·교체 또는 잘못된 계획입니다. 수동 검토가 필요합니다.\n"
        )
    print(json.dumps({"changed_resources": changes}, ensure_ascii=False))


if __name__ == "__main__":
    main()
