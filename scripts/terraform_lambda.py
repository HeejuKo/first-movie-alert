"""Pass the local webhook to Terraform through the environment, never CLI arguments."""

import argparse
import os
import subprocess
from pathlib import Path

from build_lambda import build

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["plan", "apply"])
    parser.add_argument("--terraform", default="terraform")
    parser.add_argument("--enable-schedule", action="store_true")
    parser.add_argument("--config", type=Path, default=ROOT / "config/watch.example.toml")
    args = parser.parse_args()
    if args.action == "apply" and args.enable_schedule:
        parser.error(
            "--enable-schedule은 plan 단계에서 지정하세요. apply는 저장한 계획을 적용합니다."
        )
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        parser.error("DISCORD_WEBHOOK_URL이 필요합니다. uv run --env-file .env로 주입하세요.")
    environment = dict(os.environ)
    environment["TF_VAR_discord_webhook_url"] = webhook
    environment["TF_VAR_deploy_lambda"] = "true"
    environment["TF_VAR_schedule_enabled"] = str(args.enable_schedule).lower()
    environment.setdefault("AWS_PROFILE", "movie-alert")
    os.umask(0o077)
    if args.action == "plan":
        build(args.config)
        # Prevent a later bare Terraform plan from treating the function as undeployed.
        (ROOT / "infra/deployment.auto.tfvars.json").write_text(
            '{"deploy_lambda": true, "schedule_enabled": '
            + str(args.enable_schedule).lower()
            + "}\n"
        )
    command = [args.terraform, "-chdir=infra", args.action, "-input=false", "-no-color"]
    if args.action == "plan":
        command.append("-out=lambda.tfplan")
    else:
        command.append("lambda.tfplan")
    result = subprocess.run(command, cwd=ROOT, env=environment)
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
