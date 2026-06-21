"""Per-user fail-closed flag for screen.read VLM captioning.

Mirrors perception_ingress_runtime_v2_enabled: default OFF, any load error
falls back to OFF so a config/db hiccup can never silently enable screen
egress to a third-party VLM.
"""
from __future__ import annotations

from hosted import config_store as hosted_config_store

SCREEN_CAPTION_FLAG = "screen_caption_enabled"


def screen_caption_enabled(store) -> bool:
    try:
        config = hosted_config_store._load_model_api_config(store) or {}
        profile = hosted_config_store._ensure_model_api_runtime_profile(store, config) or {}
        if SCREEN_CAPTION_FLAG in profile:
            return bool(profile.get(SCREEN_CAPTION_FLAG))
        return bool(config.get(SCREEN_CAPTION_FLAG))
    except Exception:
        return False
