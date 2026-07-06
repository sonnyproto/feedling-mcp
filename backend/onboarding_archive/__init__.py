"""Onboarding 原始档归档 — 自包含后端功能模块。

用户 onboarding 上传的原始文件经 POST /v1/onboarding/archive 代理存到
``io-user-logs`` R2 桶的 ``onboarding/`` 前缀（明文；见 storage.py 的隐私
说明），一条索引落 Postgres ``onboarding_archive`` 日志流。

装配：asgi_app.py 的域包注册表里列了 ``onboarding_archive.routes_asgi``，
由其 ``register_asgi(app)`` 挂路由。
"""
