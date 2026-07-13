"""In-process TEE 影子库自动同步（后端原生，不依赖外部 cron）。

双写（mirror）只自动搬「明文表的新写入」；存量 + 全部密文表（chat/memory/
identity/world_book/frames，经 enclave 解密成明文）不会自己进影子库。本调度器
在后端进程里定时把它们也同步进来，达到「设完即忘」。

只在**一个** worker 上跑：由 asgi/lifespan.py 经 ``core.leader.run_singleton``
（pg advisory-lock 选主）拉起，故 N 个 gunicorn worker 不会同时复制。每个 tick
走的是手动 run 走的同一入口 ``tee_replication.run_action``（复用它的单-run 锁 +
校验 + confirm 门），所以手动 admin 触发和本循环永不重叠。

节奏（env 可调）：
  - 每 ``FEEDLING_TEE_SYNC_INTERVAL_SEC``（默认 300s）：增量 replicate 每张密文表
    （游标扫描，无新行时是空 SELECT，极廉价）。
  - 每 ``FEEDLING_TEE_RECONCILE_INTERVAL_SEC``（默认 86400s）+ **首个 tick**：
    全量 reconcile（明文表漂移补偿/首次回填）+ verify（对账观测）。

故障隔离：每个操作都兜异常，失败只 log、循环继续，绝不拖垮进程。仅当
``mirror.enabled()``（FEEDLING_TEE_DUAL_WRITE=1 且 TEE_DATABASE_URL 非空）时干活。
"""
from __future__ import annotations

import logging
import os
import threading
import time

from tee_shadow import mirror

log = logging.getLogger("feedling.tee_sync")

# 密文表 —— 经 enclave 解密成明文。与 tee_replicator.worker._TABLES 对齐。
_CIPHERTEXT_TABLES = (
    "chat_messages",
    "memory_moments",
    "world_book_entries",
    "identity",
    "frame_envelopes",
)

# 首个 tick 的启动延迟（秒）：短于常规间隔，让父表回填尽快发生（见 _loop）。
_FIRST_DELAY = 30.0


def _interval() -> float:
    try:
        return max(30.0, float(os.environ.get("FEEDLING_TEE_SYNC_INTERVAL_SEC", "300") or 300))
    except (TypeError, ValueError):
        return 300.0


def _reconcile_interval() -> float:
    try:
        return max(300.0, float(os.environ.get("FEEDLING_TEE_RECONCILE_INTERVAL_SEC", "86400") or 86400))
    except (TypeError, ValueError):
        return 86400.0


def _sync_tick(*, do_reconcile: bool) -> bool:
    """一轮同步。复用 ``tee_replication.run_action``（校验 + 单-run 锁 + confirm 门）。
    ``AlreadyRunning`` = 有手动 run 持锁 → 本 tick 跳过；``Unconfigured`` = TEE 未接 →
    跳过；其余单表错误只 log、继续下一张表。

    顺序铁律：**reconcile 必须在 replicate 之前**。密文子表（chat/memory/identity/
    world_book/frames）都有指向 ``users`` 的外键；父表没先回填，子表 replicate 会
    ``violates foreign key`` 全灭。所以先 reconcile 灌明文父表（users 等），再 replicate
    密文子表。（reconcile 走 direct-TLS 网关批量拷贝，大表可能数分钟，但本循环在专用
    后台线程、不碰请求路径。）"""
    from admin import tee_replication as tr

    # reconcile_ok 决定调用方要不要推进「下次 reconcile」计时器：reconcile 失败
    # （网关瞬时断连、SSL eof 等）时返回 False → 调用方不推进 → 下个 tick(5min)就
    # 重试，而不是傻等到一天后的日常周期。not do_reconcile 时视为 True（本 tick
    # 本就无需 reconcile，不制造重试压力）。
    reconcile_ok = not do_reconcile

    # (1) reconcile 明文表在前 —— 回填/修复父表，子表才有 FK 父行。
    if do_reconcile:
        try:
            rep = tr.run_action(action="reconcile", dry_run=False, confirm="MIGRATE")
            tbls = rep.get("tables") or []
            copied = sum(t.get("copied", 0) for t in tbls if isinstance(t, dict))
            unconv = [t.get("table") for t in tbls
                      if isinstance(t, dict) and t.get("rds_rows") != t.get("tee_rows")]
            log.info("[tee-sync] reconcile done: copied=%s unconverged=%s", copied, unconv or "none")
            reconcile_ok = True
        except tr.AlreadyRunning:
            reconcile_ok = True  # 别人（手动 run）在跑 → 不重试风暴
        except tr.Unconfigured:
            return True  # TEE 未接，无事可做
        except Exception as e:  # noqa: BLE001
            log.warning("[tee-sync] reconcile 失败: %s", e)  # reconcile_ok 保持 False → 尽快重试

    # (2) replicate 密文子表在后 —— 父表已在，不再 FK 失败。
    for table in _CIPHERTEXT_TABLES:
        try:
            rep = tr.run_action(action="replicate", table=table, dry_run=False, confirm="MIGRATE")
            if rep.get("copied") or rep.get("pending") or rep.get("errors"):
                log.info("[tee-sync] replicate %s: copied=%s pending=%s errors=%s",
                         table, rep.get("copied"), rep.get("pending"), rep.get("errors"))
        except tr.AlreadyRunning:
            log.info("[tee-sync] 手动复制 run 持锁中 — 跳过本 tick")
            return reconcile_ok
        except tr.Unconfigured:
            return reconcile_ok
        except Exception as e:  # noqa: BLE001
            log.warning("[tee-sync] replicate %s 失败: %s", table, e)

    # (3) verify 观测（只读）—— reconcile 成功才有对账意义。
    if do_reconcile and reconcile_ok:
        try:
            rep = tr.run_action(action="verify", dry_run=False)
            log.info("[tee-sync] verify ok=%s", rep.get("ok"))
        except Exception as e:  # noqa: BLE001
            log.warning("[tee-sync] verify 失败: %s", e)

    return reconcile_ok


def _loop() -> None:
    # monotonic()，起点非 0 → 首个 tick 的 (now - 0) 必然 >= 间隔 → 首轮就 reconcile
    # （初始回填明文父表 + 游标从头搬密文）。
    last_reconcile = 0.0
    first = True
    while True:
        # 首个 tick 只等一小会儿就跑 —— 尽快把明文父表回填上，缩短「父表未回填 →
        # 子表双写 FK 失败」的启动窗口；之后按整间隔。
        time.sleep(_FIRST_DELAY if first else _interval())
        first = False
        if not mirror.enabled():
            continue
        now = time.monotonic()
        do_reconcile = (now - last_reconcile) >= _reconcile_interval()
        try:
            reconcile_ok = _sync_tick(do_reconcile=do_reconcile)
            # 仅在 reconcile 真成功时推进计时器；失败(断连等)则保持不动，下个 tick
            # 就重试，而不是等一整个 reconcile 周期。
            if do_reconcile and reconcile_ok:
                last_reconcile = now
        except Exception as e:  # noqa: BLE001 — 循环绝不能死
            log.warning("[tee-sync] tick 错误: %s", e)


def start() -> None:
    """Spawn 同步循环线程并立即返回（照 screen.ws.start）。由 assembly 层经
    ``core.leader.run_singleton("tee-sync", ...)`` 调用，保证只一个 worker 跑。"""
    threading.Thread(target=_loop, daemon=True, name="tee-sync").start()
