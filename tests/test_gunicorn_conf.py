def test_keepalive_outlives_client_connection_pools():
    """gunicorn's ``keepalive`` default is 2s, and uvicorn_worker maps it straight
    onto ``timeout_keep_alive``. At 2s the server closes an idle connection while
    still omitting ``Connection: close`` — so a pooling client (iOS URLSession)
    reuses a socket the server has already FIN'd and the request dies in transit
    (NSURLErrorNetworkConnectionLost, surfaced as "网络连接失败"). Symptom: the
    first tap on any POST after the user idles in a form fails, the second works.

    The invariant: the server's idle timeout must comfortably outlive a client's
    connection-pool reuse window, not undercut it.
    """
    import importlib

    gconf = importlib.import_module("gunicorn_conf")
    assert getattr(gconf, "keepalive", 2) >= 60


def test_on_starting_calls_assert_hosting_ready(monkeypatch):
    import sys, os, importlib

    # Import gunicorn_conf (backend is on sys.path via PYTHONPATH=. when pytest
    # runs from the backend dir — no manual pre-injection here).
    gconf = importlib.import_module("gunicorn_conf")
    here = os.path.dirname(os.path.abspath(gconf.__file__))

    # Simulate the condition on_starting must handle: backend not yet in sys.path
    # (gunicorn master starts before --chdir has a chance to inject the path).
    saved = sys.path[:]
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != here]
    try:
        called = {}
        monkeypatch.setattr("hosted.agent_runtime_cutover.assert_hosting_ready",
                            lambda: called.setdefault("x", True))
        gconf.on_starting(None)
        assert called.get("x") is True
        # Hardening check: on_starting must have re-inserted backend into sys.path.
        assert here in [os.path.abspath(p) for p in sys.path]
    finally:
        sys.path[:] = saved
