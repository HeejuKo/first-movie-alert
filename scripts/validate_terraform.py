"""Expose Terraform validation diagnostics as GitHub annotations without AWS access."""

import argparse
import json
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", default="infra")
    parser.add_argument("--terraform", default="terraform")
    args = parser.parse_args()
    result = subprocess.run(
        [args.terraform, f"-chdir={args.directory}", "validate", "-json"],
        capture_output=True,
        text=True,
    )
    try:
        data = json.loads(result.stdout)
    except ValueError:
        parser.exit(1, "Terraform validate JSON을 읽을 수 없습니다.\n")
    for diagnostic in data.get("diagnostics", []):
        message = f"{diagnostic['summary']}: {diagnostic['detail']}"
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        level = "error" if diagnostic["severity"] == "error" else "warning"
        print(f"::{level} title=Terraform validate::{escaped}")
    if result.returncode or data.get("valid") is not True:
        message = (
            f"exit={result.returncode}, valid={data.get('valid')}, "
            f"diagnostics={len(data.get('diagnostics', []))}; {result.stderr.strip()}"
        )
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Terraform validation process::{escaped}")
        raise SystemExit(result.returncode or 1)
    print("Terraform validation passed.")


if __name__ == "__main__":
    main()
