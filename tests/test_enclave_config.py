"""enclave.config 单元测试：常量存在性 + 两个纯函数的解析语义。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from enclave import config  # noqa: E402


def test_constants_exist_and_typed():
    assert isinstance(config.ENCLAVE_PORT, int)
    assert isinstance(config.ENCLAVE_TLS, bool)
    assert config.FLASK_URL.startswith("http")
    assert isinstance(config.RUNTIME_TOKEN_SECRET, bytes)
    assert isinstance(config.RELEASE, dict) and "git_commit" in config.RELEASE
    assert isinstance(config.APP_AUTH, dict) and "contract" in config.APP_AUTH
    assert config.ENCLAVE_THREADS >= 1


def test_env_flag_enabled(monkeypatch):
    monkeypatch.setenv("X_FLAG", "TRUE")
    assert config.env_flag_enabled("X_FLAG") is True
    monkeypatch.setenv("X_FLAG", "off")
    assert config.env_flag_enabled("X_FLAG") is False
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config.env_flag_enabled("X_FLAG") is False
    assert config.env_flag_enabled("X_FLAG", default="true") is True


def test_enclave_worker_count(monkeypatch):
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "")
    assert config.enclave_worker_count() == 1  # CI 注入空串不能崩
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "4")
    assert config.enclave_worker_count() == 4
    monkeypatch.setenv("FEEDLING_ENCLAVE_WORKERS", "0")
    assert config.enclave_worker_count() == 1  # clamp ≥1
