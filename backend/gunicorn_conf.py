"""gunicorn server config — 生产 WSGI 启动钩子。

``on_starting`` 在 gunicorn master 进程启动时执行一次（worker fork 之前）。用于
fail-fast：gateway-only codex 用户依赖 in-CVM LiteLLM gateway，未开则拒绝启动而非让
请求在运行期 hang。校验放这里而非 app.py 模块 import 顶层——后者会让单测 ``import app``
也触发校验。仅 backend gunicorn（app:app，注册了 chat_routes）需要；enclave_app 不路由
chat send，不加载本 config。"""


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
