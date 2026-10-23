"""Build a Linux ARM64 ZIP from the lockfile without local credentials or dev extras."""

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from datetime import UTC
from pathlib import Path

from movie_alert.config import load_config

ROOT = Path(__file__).resolve().parents[1]


def build(config_path: Path) -> None:
    config_path = config_path.resolve()
    config = load_config(config_path)
    enabled = [rule for rule in config.watches if rule.enabled]
    if not enabled:
        raise ValueError("활성 감시 규칙이 필요합니다.")
    output = ROOT / "build"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="lambda-", dir=output) as temporary:
        temporary = Path(temporary)
        requirements = temporary / "requirements.txt"
        package = temporary / "package"
        subprocess.run(
            [
                "uv",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--no-editable",
                "--output-file",
                str(requirements),
            ],
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python-version",
                "3.13",
                "--python-platform",
                "aarch64-manylinux2014",
                "--only-binary",
                ":all:",
                "--require-hashes",
                "--no-deps",
                "--target",
                str(package),
                "-r",
                str(requirements),
            ],
            cwd=ROOT,
            check=True,
        )
        shutil.copytree(
            ROOT / "src/movie_alert",
            package / "movie_alert",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        (package / "config").mkdir()
        shutil.copyfile(config_path, package / "config/watch.toml")
        archive = output / "movie-alert-lambda.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for path in sorted(package.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                info = zipfile.ZipInfo(path.relative_to(package).as_posix(), (1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                zip_file.writestr(info, path.read_bytes())
    manifest = {
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "active_from": min(rule.active_from for rule in enabled)
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
        "active_until": max(rule.active_until for rule in enabled)
        .astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    (output / "lambda-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Lambda package: {archive} ({archive.stat().st_size} bytes)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "config/watch.example.toml")
    args = parser.parse_args()
    build(args.config)


if __name__ == "__main__":
    main()
