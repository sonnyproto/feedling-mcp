"""Per-user flag for screen.read VLM captioning.

Default follows the env-gated baseline (core/util.runtime_v2_default_on): OFF in
prod-without-env, ON where FEEDLING_RUNTIME_V2_DEFAULT_ON=true. An explicit
per-user value still wins. NOTE: this gates screen egress to a third-party VLM,
so it stays fail-closed on errors — any config/db hiccup falls back to OFF and
never silently enables egress.
"""
from __future__ import annotations

from core import util as core_util
from hosted import config_store as hosted_config_store

SCREEN_CAPTION_FLAG = "screen_caption_enabled"


def screen_caption_enabled(store) -> bool:
    try:
        config = hosted_config_store._load_model_api_config(store) or {}
        profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
        if SCREEN_CAPTION_FLAG in profile:
            return bool(profile.get(SCREEN_CAPTION_FLAG))
        if SCREEN_CAPTION_FLAG in config:
            return bool(config.get(SCREEN_CAPTION_FLAG))
        return core_util.runtime_v2_default_on()
    except Exception:
        return False
