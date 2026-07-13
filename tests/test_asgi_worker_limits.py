"""uvicorn 的 limit_concurrency 是最后兜底闸，正常负载下永不应触发。

它有两个容易被忽略的性质，这里各守一条：

1. uvicorn 是按 **打开的连接数** 判的（``len(self.connections) >= limit``），
   不只是在途请求。keep-alive 空闲连接（gunicorn_conf.keepalive=75s）会计入，
   而这些连接既不占线程也不占 DB 连接。
2. 它必须高于 poll-waiter 上限（FEEDLING_POLLER_MAX_ACTIVE），否则长轮询本身
   就能把这道闸顶爆。这条以前只写在注释里，而实际值 2048 < 5000，不变量是破的。
"""


def _limit_concurrency() -> int:
    from asgi.worker import FeedlingUvicornWorker

    return FeedlingUvicornWorker.CONFIG_KWARGS["limit_concurrency"]


def test_limit_concurrency_clears_poll_waiter_cap():
    from asgi.settings import settings

    assert _limit_concurrency() > settings.poller_max_active


def test_limit_concurrency_leaves_room_for_pooled_keepalive_sockets():
    """Idle pooled sockets count toward the same limit, so the guard needs room
    above the poll-waiter cap — not merely one more than it."""
    from asgi.settings import settings

    assert _limit_concurrency() - settings.poller_max_active >= 2000
