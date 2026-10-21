import json
from pathlib import Path

import httpx
import pytest

from movie_alert.config import load_config
from movie_alert.providers.cgv import parse_schedule

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def block_real_http(monkeypatch):
    def fail(*args, **kwargs):
        pytest.fail("테스트에서 실제 HTTP 요청은 허용하지 않습니다.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", fail)


@pytest.fixture
def payload():
    return json.loads((ROOT / "tests/fixtures/cgv_gangnam_20261024.json").read_text())


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "watch.toml"
    path.write_text(
        (ROOT / "config/watch.example.toml")
        .read_text()
        .replace(
            'dates = ["2026-10-28", "2026-10-31", "2026-11-04", "2026-11-07"]',
            'dates = ["2026-10-24"]',
        )
    )
    return load_config(path)


@pytest.fixture
def rule(config):
    return config.watches[0]


@pytest.fixture
def showtimes(payload):
    return parse_schedule(payload)


@pytest.fixture
def sungshin_showtimes():
    path = ROOT / "tests/fixtures/cgv_sungshin_20261024.json"
    return parse_schedule(json.loads(path.read_text(encoding="utf-8")))
