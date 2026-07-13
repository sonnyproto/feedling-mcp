"""TEE 影子库尽力而为镜像（spec §5.1）。

影子期铁律：任何失败只 log+计数，绝不传染主路径；漏写由 reconciler 补偿。
"""
from __future__ import annotations

import logging
import os
import threading
import time

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


def _pool_int(env: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(env, "") or default))
    except (TypeError, ValueError):
        return default


def _pool_min() -> int:
    # 热连接数。保持得多一些,突发时更少现场做网关 direct-TLS 冷握手(那条慢链路正是
    # 当年把 pool_timeout 放宽到 15s 的理由)。
    return _pool_int("FEEDLING_TEE_POOL_MIN", 8)


def _pool_max() -> int:
    # 影子写并发上限——2026-07-13 实测这是整条影子链路上唯一的约束:TEE PG
    # max_connections=200 而当时只用了 11 条,其中 app 用户恰好 4 条 = 池被自己的
    # max_size=4 顶死;41 次镜像失败全是 "couldn't get a connection",零 SSL/链路错误,
    # TEE CVM healthy。即瓶颈是我们自己设的上限,不是 DB、也不是网关。
    #
    # 定 32 的依据是 WORKERS × max_size(池是 per-worker 的),不是拍脑袋:
    #   TEE PG 200 上限(3 条 superuser 保留),owner/replicator/monitoring 等非 app
    #   角色常驻约 7 条。
    #   - 32 → 单 worker 32;即便日后跟 prod 一样开 4 worker 也才 128,尚余约 70。
    #   - 64 → 4 worker 就是 256 > 200,会把 TEE PG 打满。
    # 故 32 是「在安全前提下尽量大」的取值。内存不是约束:work_mem=4MB → 32 条最坏
    # 约 128MB,而 TEE CVM 尚有 3.2GB 空闲(PG 当前仅用 142MB)。
    #
    # 注意:prod 目前不跑双写(compose 无 TEE_DATABASE_URL/FEEDLING_TEE_DUAL_WRITE,
    # enabled() 为假 → 整条镜像是 no-op),所以这里的默认值当前只作用于 test。
    return _pool_int("FEEDLING_TEE_POOL_MAX", 32)


def get_tee_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                from psycopg_pool import ConnectionPool
                _pool = ConnectionPool(
                    os.environ["TEE_DATABASE_URL"],
                    min_size=_pool_min(),
                    max_size=_pool_max(),
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


def probe() -> dict:
    """TEE 影子库健康探活（``SELECT 1`` + 往返延迟）。绝不抛：TEE 未接/连不上都
    返回 ``ok=False`` + 简短 error,给可观测端点当结构化 health 字段用（否则连不上
    会一路 500/503,拿不到"TEE 不可达"这个本身就是信号的数据点）。

    走 ``get_tee_pool()`` 的既有池（受 ``_pool_timeout`` 上限约束),所以探活也享受
    2s 短超时——TEE 卡住时探活自己不会把调用方拖住。"""
    if not os.environ.get("TEE_DATABASE_URL"):
        return {"ok": False, "latency_ms": None, "error": "unconfigured"}
    t0 = time.monotonic()
    try:
        with get_tee_pool().connection() as conn:
            conn.execute("SELECT 1")
        return {"ok": True, "latency_ms": round((time.monotonic() - t0) * 1000, 1), "error": None}
    except Exception as exc:  # noqa: BLE001 — 探活绝不上抛
        return {"ok": False, "latency_ms": None, "error": str(exc)[:200]}


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
