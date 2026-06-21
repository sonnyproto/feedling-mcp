import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

from proactive import screen_flag_v2


class _Store:
    user_id = "u1"


def test_flag_defaults_off(monkeypatch):
    monkeypatch.setattr(screen_flag_v2.hosted_config_store,
                        "_load_model_api_config", lambda s: {})
    monkeypatch.setattr(screen_flag_v2.hosted_config_store,
                        "_ensure_model_api_runtime_profile", lambda s, c: {})
    assert screen_flag_v2.screen_caption_enabled(_Store()) is False


def test_flag_on_when_profile_sets_it(monkeypatch):
    monkeypatch.setattr(screen_flag_v2.hosted_config_store,
                        "_load_model_api_config", lambda s: {})
    monkeypatch.setattr(screen_flag_v2.hosted_config_store,
                        "_ensure_model_api_runtime_profile",
                        lambda s, c: {"screen_caption_enabled": True})
    assert screen_flag_v2.screen_caption_enabled(_Store()) is True


def test_flag_fail_closed_on_error(monkeypatch):
    def boom(s):
        raise RuntimeError("db down")
    monkeypatch.setattr(screen_flag_v2.hosted_config_store,
                        "_load_model_api_config", boom)
    assert screen_flag_v2.screen_caption_enabled(_Store()) is False
