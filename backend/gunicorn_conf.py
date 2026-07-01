"""gunicorn server config — 生产 WSGI 启动钩子。

``on_starting`` 在 gunicorn master 进程启动时执行一次（worker fork 之前）。用于
fail-fast：gateway-only codex 用户依赖 in-CVM LiteLLM gateway，未开则拒绝启动而非让
请求在运行期 hang。校验放这里而非 app.py 模块 import 顶层——后者会让单测 ``import app``
也触发校验。仅 backend gunicorn（app:app，注册了 chat_routes）需要；enclave_app 不路由
chat send，不加载本 config。"""

import os


def _worker_count() -> int:
    """gunicorn worker (process) count, env-driven (``FEEDLING_BACKEND_WORKERS``,
    default 1, clamped ≥1). The backend already supports ``-w N`` — a cross-worker
    LISTEN/NOTIFY wake bus (``core.wake_bus``) so a write in one worker wakes
    long-poll waiters in the others, and advisory-lock leader election
    (``core.leader.run_singleton``) so only ONE worker binds the :9998 WS server.
    So raising this is a pure config change. Sizing is bounded by Postgres
    max_connections: each worker holds ~16 pool + 1 LISTEN + 1 election ≈ 18
    connections, so -w2 ≈ 36, -w3 ≈ 54 — check the RDS instance's max_connections
    before raising (test t4g-micro = 79 → -w2 safe / -w3 edge)."""
    # `or "1"` guards the empty string: CI passes `-e FEEDLING_BACKEND_WORKERS=$VAR`
    # and an unset GitHub var expands to "", so the key is SET-but-empty — int("")
    # would crash gunicorn config load and the backend would fail to boot.
    return max(1, int((os.environ.get("FEEDLING_BACKEND_WORKERS") or "").strip() or "1"))


# gunicorn reads this module-level name for the worker count (no `-w` on the CLI,
# so the config file wins). Default 1 = unchanged behavior until the env is set.
workers = _worker_count()


def on_starting(server):
    # on_starting 跑在 gunicorn master 进程、worker fork 之前。--chdir backend 或
    # WorkingDirectory=backend 的 path 注入时序不保证在此时完成，故自插 backend 目录
    # 到 sys.path，使 hosted 包在任何启动方式（容器 WORKDIR /app + --chdir backend、
    # systemd WorkingDirectory=backend）下均可解析。
    import os, sys
    here = os.path.dirname(os.path.abspath(__file__))  # .../backend
    if here not in sys.path:
        sys.path.insert(0, here)
    from hosted import agent_runtime_cutover
    agent_runtime_cutover.assert_hosting_ready()
