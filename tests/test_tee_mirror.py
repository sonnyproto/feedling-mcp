import os, psycopg
from tee_shadow import mirror


def _tee(sql):
    with psycopg.connect(os.environ["TEE_DATABASE_URL"], autocommit=True) as c:
        return c.execute(sql).fetchall()


def test_mirror_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("FEEDLING_TEE_DUAL_WRITE", raising=False)
    mirror.execute("INSERT INTO server_config (key, value) VALUES (%s, %s)", ("k1", b"v"))
    assert _tee("SELECT count(*) FROM server_config WHERE key='k1'")[0][0] == 0


def test_mirror_writes_when_enabled(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    mirror.execute(
        "INSERT INTO server_config (key, value) VALUES (%s, %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k2", b"v"))
    assert _tee("SELECT count(*) FROM server_config WHERE key='k2'")[0][0] == 1


def test_mirror_swallows_failure_and_counts(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")
    before = mirror.failure_count()
    mirror.execute("INSERT INTO no_such_table VALUES (1)")  # 必须不 raise
    assert mirror.failure_count() == before + 1


def test_mirror_execute_many_atomic_and_counts_failure(monkeypatch):
    monkeypatch.setenv("FEEDLING_TEE_DUAL_WRITE", "1")

    # Group applied atomically when enabled.
    mirror.execute_many([
        ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k3", b"v")),
        ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k4", b"v")),
    ])
    assert _tee("SELECT count(*) FROM server_config WHERE key IN ('k3', 'k4')")[0][0] == 2

    # A failing group leaves no partial writes and increments failure_count by 1.
    before = mirror.failure_count()
    mirror.execute_many([
        ("INSERT INTO server_config (key, value) VALUES (%s, %s) "
         "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", ("k5", b"v")),
        ("INSERT INTO no_such_table VALUES (1)", ()),
    ])
    assert mirror.failure_count() == before + 1
    assert _tee("SELECT count(*) FROM server_config WHERE key='k5'")[0][0] == 0


def test_pool_sizing_defaults(monkeypatch):
    """Pool size is the ONLY thing that limited the shadow write path.

    Measured on the live TEE PG (2026-07-13): max_connections=200 with just 11 in
    use — and exactly 4 of them held by the app user, i.e. the pool was pegged at
    its own max_size=4 while Postgres had ~190 free slots. Every mirror failure was
    "couldn't get a connection" (zero SSL/link errors, TEE CVM healthy), so the
    constraint was self-inflicted, not the DB or the gateway.

    The ceiling is set by WORKERS × max_size, because the pool is per-worker. TEE PG
    allows 200 (3 reserved) and non-app roles (owner/replicator/monitoring) hold ~7:
      - 32 → 1 worker: 32; even at 4 workers (what prod runs): 128, leaving ~70 free.
      - 64 → 4 workers: 256 > 200. Would wedge the DB. So 32 is the "as large as
        safely possible" answer, not an arbitrary bump.
    Memory is not the constraint: work_mem=4MB → 128MB worst case at 32 (TEE CVM has
    3.2GB free, PG currently uses 142MB). min_size=8 keeps enough warm to avoid the
    cold gateway TLS handshake that motivated the old 15s timeout."""
    from tee_shadow import mirror
    monkeypatch.delenv("FEEDLING_TEE_POOL_MIN", raising=False)
    monkeypatch.delenv("FEEDLING_TEE_POOL_MAX", raising=False)
    assert mirror._pool_min() == 8
    assert mirror._pool_max() == 32
    monkeypatch.setenv("FEEDLING_TEE_POOL_MAX", "64")
    assert mirror._pool_max() == 64


def test_pool_timeout_env_configurable(monkeypatch):
    """Default is 2s: the shadow write is best-effort (failures are swallowed and
    the reconciler backfills), so it must never hold a user-facing request hostage.
    The old 15s was chosen assuming min_size=2 kept the pool warm enough that the
    tail "rarely hits" — live test 2026-07-13 disproved that (18 pool timeouts in
    13 min), because the pool is max_size=4 while every poll drove a mirror write.
    A cold gateway handshake can exceed 2s, so a burst may now fail its mirror
    write instead of stalling the request — the correct trade for a shadow that
    the reconciler converges anyway."""
    from tee_shadow import mirror
    monkeypatch.delenv("FEEDLING_TEE_POOL_TIMEOUT", raising=False)
    assert mirror._pool_timeout() == 2.0
    monkeypatch.setenv("FEEDLING_TEE_POOL_TIMEOUT", "30")
    assert mirror._pool_timeout() == 30.0
    monkeypatch.setenv("FEEDLING_TEE_POOL_TIMEOUT", "garbage")
    assert mirror._pool_timeout() == 2.0  # bad value falls back to default


def test_statement_timeout_env_configurable(monkeypatch):
    """服务端语句执行上限。补 connect_timeout/keepalives 盖不住的挂死:连接已建好、
    query 已到 pg,但网关黑洞回包 → 客户端无限等 recv。有了它 pg 到点自 cancel,
    reconcile 干净失败并释放 run-lock,而非永久 wedge 住 scheduler(2026-07-14 事故)。
    默认 120s 够 prod user_logs(376MB)reconcile 的全 PK prune/count 跑完。"""
    from tee_shadow import mirror
    monkeypatch.delenv("FEEDLING_TEE_STATEMENT_TIMEOUT_MS", raising=False)
    assert mirror._statement_timeout_ms() == 120000
    monkeypatch.setenv("FEEDLING_TEE_STATEMENT_TIMEOUT_MS", "45000")
    assert mirror._statement_timeout_ms() == 45000
    monkeypatch.setenv("FEEDLING_TEE_STATEMENT_TIMEOUT_MS", "garbage")
    assert mirror._statement_timeout_ms() == 120000  # bad value → default
    monkeypatch.setenv("FEEDLING_TEE_STATEMENT_TIMEOUT_MS", "10")
    assert mirror._statement_timeout_ms() == 1000  # floored at 1s


def test_tcp_user_timeout_env_configurable(monkeypatch):
    """出站未 ACK 上限(Linux 内核强杀连接)。补反方向挂死:出站被网关黑洞、根本没
    送达 pg,服务端没 query 可 statement_timeout cancel,keepalives 又被网关 TCP 层
    回探测骗过。默认 30s;0 = 关闭(交系统默认),此时不下发该 kwarg。"""
    from tee_shadow import mirror
    monkeypatch.delenv("FEEDLING_TEE_TCP_USER_TIMEOUT_MS", raising=False)
    assert mirror._tcp_user_timeout_ms() == 30000
    monkeypatch.setenv("FEEDLING_TEE_TCP_USER_TIMEOUT_MS", "15000")
    assert mirror._tcp_user_timeout_ms() == 15000
    monkeypatch.setenv("FEEDLING_TEE_TCP_USER_TIMEOUT_MS", "0")
    assert mirror._tcp_user_timeout_ms() == 0  # 显式关闭
    monkeypatch.setenv("FEEDLING_TEE_TCP_USER_TIMEOUT_MS", "garbage")
    assert mirror._tcp_user_timeout_ms() == 30000  # bad value → default


def test_pool_connection_enforces_statement_timeout(monkeypatch):
    """接线验证(不只测 helper):从 get_tee_pool() 借出的连接必须真的带上
    statement_timeout —— options='-c statement_timeout=' 已下发到服务端。"""
    from tee_shadow import mirror
    monkeypatch.setenv("FEEDLING_TEE_STATEMENT_TIMEOUT_MS", "7000")
    # 重建全局池,让新 kwargs 生效(其它测试可能已建过旧池)。
    if mirror._pool is not None:
        mirror._pool.close()
        mirror._pool = None
    try:
        with mirror.get_tee_pool().connection() as conn:
            got = conn.execute("SHOW statement_timeout").fetchone()[0]
        assert got == "7s"  # 7000ms 服务端归一化为 '7s'
    finally:
        # 别把这条 7s 上限的池留给后续测试:关掉,下次 get_tee_pool 用默认重建。
        if mirror._pool is not None:
            mirror._pool.close()
            mirror._pool = None
