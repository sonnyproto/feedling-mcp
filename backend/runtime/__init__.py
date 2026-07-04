"""ASGI runtime primitives (ASGI-migration plan §5.1).

Home for the async long-poll machinery — the asyncio waiter registry and its
wake bridge — that replaces the Flask ``threading.Event`` waiters. This is the
heart of the migration's payoff: idle long-polls park an asyncio future instead
of pinning an OS thread (plan §9).
"""
