"""tools/gen_url_map.py route accounting vs the live ASGI app.

fastapi 0.139 / starlette 1.3 made ``include_router`` lazy: ``app.routes`` holds
``_IncludedRouter`` entries instead of flattened ``starlette.routing.Route``s, so
the old isinstance walk silently yielded ZERO rows — the post-cutover route-drift
gate (plan §6.1) produced an empty snapshot instead of failing loudly. These
tests pin that ``_iter_routes`` actually flattens the whole surface.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO / "backend"))
import asgi_app  # noqa: E402

_spec = importlib.util.spec_from_file_location("gen_url_map", REPO / "tools" / "gen_url_map.py")
gen_url_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_url_map)


def _rows():
    return list(gen_url_map._iter_routes(asgi_app.app))


def test_snapshot_covers_full_route_surface():
    rows = _rows()
    # The cutover ledger accounted 133 Flask rules; the ASGI surface has grown
    # slightly since. Anything materially below that means the walker is blind
    # to lazily-included routers again.
    assert len(rows) >= 130, f"route walker only saw {len(rows)} routes"


def test_snapshot_contains_known_routes_with_owner():
    by_path = {}
    for r in _rows():
        by_path.setdefault(r["path"], []).append(r)

    healthz = by_path.get("/healthz")
    assert healthz and healthz[0]["methods"] == "GET"

    whoami = by_path.get("/v1/users/whoami")
    assert whoami and whoami[0]["methods"] == "GET"
    assert whoami[0]["owner"] == "accounts"


def test_no_duplicate_path_method_pairs():
    seen = set()
    for r in _rows():
        key = (r["path"], r["methods"])
        assert key not in seen, f"duplicate route emitted: {key}"
        seen.add(key)
