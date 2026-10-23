"""Write private S3 backend metadata; this does not initialize or migrate state."""

import argparse
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", default="ap-northeast-2")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", args.bucket):
        parser.error("유효한 S3 bucket 이름이 필요합니다.")
    if not re.fullmatch(r"\d{12}", args.account):
        parser.error("12자리 AWS 계정 ID가 필요합니다.")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", args.region):
        parser.error("유효한 AWS 리전이 필요합니다.")
    os.umask(0o077)
    infra = ROOT / "infra"
    template = (infra / "backend.tf.example").read_text()
    destination = infra / "backend.local.tf"
    if destination.exists() and destination.read_text() != template:
        parser.error("기존 backend.local.tf가 다릅니다. 수동 마이그레이션을 검토하세요.")
    metadata = infra / ".terraform/terraform.tfstate"
    if metadata.exists():
        backend = json.loads(metadata.read_text()).get("backend", {})
        if backend.get("type") == "s3":
            current = backend.get("config", {})
            if (
                current.get("bucket") != args.bucket
                or current.get("key") != "state/movie-alert.tfstate"
                or current.get("region") != args.region
            ):
                parser.error("이미 다른 S3 backend를 사용 중입니다. 자동으로 변경하지 않습니다.")
    destination.write_text(template)
    (infra / "backend.local.hcl").write_text(
        f"bucket = {json.dumps(args.bucket)}\n"
        'key = "state/movie-alert.tfstate"\n'
        f"region = {json.dumps(args.region)}\n"
        f"allowed_account_ids = [{json.dumps(args.account)}]\n"
        "encrypt = true\nuse_lockfile = true\n"
    )
    print("S3 backend 설정 파일만 준비했습니다. 기존 state 마이그레이션은 별도로 확인하세요.")


if __name__ == "__main__":
    main()
