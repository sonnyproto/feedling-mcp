"""TEE 影子库尽力而为镜像（spec §5.1）。

影子期铁律：任何失败只 log+计数，绝不传染主路径；漏写由 reconciler 补偿。
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("feedling.tee_shadow")
_pool = None
_pool_lock = threading.Lock()
_failures = 0
_failures_lock = threading.Lock()


def enabled() -> bool:
    return os.environ.get("FEEDLING_TEE_DUAL_WRITE", "") == "1" and bool(
        os.environ.get("TEE_DATABASE_URL"))


def _pool_timeout() -> float:
    # 拿连接的等待上限。影子写是 best-effort(失败被吞、reconciler 后续补齐),所以
    # 它绝不能把用户请求扣在这里——这个上限就是每次主写在 TEE 不可用时白等的时间。
    #
    # 曾放宽到 15s(16320c2),理由是网关 direct-TLS 冷握手可能 >5s,并假设 min_size=2
    # 的热连接让这条尾延迟"很少真正命中"。2026-07-13 test 实测推翻了该假设:13 分钟
    # 内 18 次 "couldn't get a connection after 15.00 sec"——瓶颈不是冷握手而是池容量
    # (max_size=4),因为当时每次 /v1/chat/poll 都驱动一次 consumer_state 影子写。
    # 那个热源已被摘除(db.set_blob 不再镜像 consumer_state),这里再把上限收回 2s 作为
    # 第二道闸:即使池再被打满,主请求最多让路 2s 而不是 15s。
    #
    # 代价(有意接受):冷握手 >2s 时该次影子写会失败而不是阻塞请求——对一个 reconciler
    # 本就会收敛的影子库,这是正确的取舍。
    try:
        return max(1.0, float(os.environ.get("FEEDLING_TEE_POOL_TIMEOUT", "2") or 2))
    except (TypeError, ValueError):
        return 2.0


def get_tee_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(
                    os.environ["TEE_DATABASE_URL"],
                    # min_size=2:保持 2 条热连接,热路径(双写)几乎不必现场做网关
                    # TLS 握手 → 规避那条慢链路,pool_timeout 尾延迟很少真正命中。
                    min_size=int(os.environ.get("FEEDLING_TEE_POOL_MIN", "2")),
                    max_size=int(os.environ.get("FEEDLING_TEE_POOL_MAX", "4")),
                    timeout=_pool_timeout(),
                    max_idle=300,
                    # connect_timeout:单条连接的建立上限(libpq 参数),防止一次
                    # 网关握手无限期挂住占着 pool 的补给名额。
                    kwargs={"autocommit": True, "connect_timeout": 10},
                    open=True,
                )
    return _pool


def failure_count() -> int:
    return _failures


def _record_failure(exc: Exception, sql: str) -> None:
    global _failures
    with _failures_lock:
        _failures += 1
    log.warning("[tee-mirror] shadow write failed (#%d): %s | sql=%.80s",
                _failures, exc, sql)


def execute(sql: str, params: tuple = ()) -> None:
    if not enabled():
        return
    try:
        with get_tee_pool().connection() as conn:
            conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001 — 影子期吞掉一切
        _record_failure(exc, sql)


# tee_pending_device_migration 的 upsert：同时服务两种用途——
#   1. requeue lane（reason LIKE 'requeue%'）：标记「同 PK 原地改写」的行，让
#      cursor 永不回头的 replicator 在下一趟 run_table 开头重新拉取转换（见
#      tee_replicator.worker 的 requeue 消费步）。
#   2. visibility_local_only：内容被 swap 成 local_only 后的终态标记（TEE 明文行
#      已被删，这行占位使 verify 的 rds == tee + pending 仍然平衡）。
# ON CONFLICT 覆盖 reason/marked_at，故一次 requeue 会盖掉旧的 local_only 标记、
# 反之亦然（controller 定案）。与 worker._PENDING_UPSERT 同一套语义。
_PENDING_UPSERT_SQL = (
    "INSERT INTO tee_pending_device_migration "
    "(user_id, table_name, item_id, reason, marked_at) VALUES (%s,%s,%s,%s, now()) "
    "ON CONFLICT (user_id, table_name, item_id) DO UPDATE SET "
    "reason = EXCLUDED.reason, marked_at = now()"
)


def mark_pending(user_id: str, table_name: str, item_id: str, reason: str) -> None:
    """尽力而为地写/覆盖一条 pending_device_migration 行（影子期吞掉失败）。"""
    execute(_PENDING_UPSERT_SQL, (user_id, table_name, item_id, reason))


def execute_many(statements: list[tuple[str, tuple]]) -> None:
    """尽力而为地把一组语句作为单个事务镜像到 TEE 影子库。

    与 `execute` 同样的 enabled() 门禁与失败吞掉语义：整组要么原子生效，
    要么任一语句失败就整组回滚且只计一次失败（不逐条计数），因为它们在
    主路径上本就属于同一次逻辑写入。
    """
    if not enabled():
        return
    try:
        with get_tee_pool().connection() as conn:
            with conn.transaction():
                for sql, params in statements:
                    conn.execute(sql, params)
    except Exception as exc:  # noqa: BLE001 — 影子期吞掉一切
        _record_failure(exc, "; ".join(sql for sql, _ in statements))
