"""Onboarding 原始档归档 — 自包含后端功能模块。

用户 onboarding 上传的原始文件经 POST /v1/onboarding/archive 代理存到
``io-user-logs`` R2 桶的 ``onboarding/`` 前缀（明文；见 storage.py 的隐私
说明），一条索引落 Postgres ``onboarding_archive`` 日志流。

装配（app.py 两行）：
    from onboarding_archive import register as register_onboarding_archive
    register_onboarding_archive(app)
"""

from __future__ import annotations

from .routes import bp

__all__ = ["register"]


def register(app) -> None:
    """Mount the onboarding-archive blueprint (POST /v1/onboarding/archive)."""
    app.register_blueprint(bp)
