"""catalog 覆盖 consumer 分类器的全部 error_class（spec Phase B / B3 一致性纪律）。

consumer 在 tools/ 不能 import backend，catalog 在 backend/——两处各自维护，
用本测试锁一致性：catalog.ERROR_CLASSES ⊇ consumer 的全部 error_class，且
同一 error_class 在两处的 blame 一字不差（同源纪律的真正锁）。

Run:  python -m pytest tests/test_catalog_consumer_parity.py -q
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

# tools/chat_resident_consumer.py 在 import 时需要一批环境变量默认值（照抄
# tests/test_consumer_error_classify.py 的既有写法，保持两处环境一致）。
_ENV_DEFAULTS = {
    "FEEDLING_API_URL": "http://localhost:5001",
    "FEEDLING_API_KEY": "test_key_00000000",
    "AGENT_MODE": "http",
    "AGENT_HTTP_URL": "http://localhost:8080/chat",
    "CHECKPOINT_FILE": "/tmp/feedling_test_catalog_parity_checkpoint.json",
}
for k, v in _ENV_DEFAULTS.items():
    os.environ.setdefault(k, v)

sys.path.insert(0, str(Path(__file__).parent.parent))              # 让 tools 可 import
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))  # 让 notices 可 import

try:
    import content_encryption  # noqa: F401
except ModuleNotFoundError:
    _fake_enc = types.ModuleType("content_encryption")
    _fake_enc.build_envelope = lambda **kw: {"v": 1, "stub": True}
    sys.modules["content_encryption"] = _fake_enc

from notices import catalog  # noqa: E402
from notices import core  # noqa: E402
import tools.chat_resident_consumer as crc  # noqa: E402


def test_catalog_covers_all_consumer_error_classes():
    missing = set(crc.CONSUMER_ERROR_CLASSES) - set(catalog.ERROR_CLASSES)
    assert not missing, f"catalog 缺 error_class: {sorted(missing)}"


def test_every_catalog_blame_is_valid():
    for ec in catalog.ERROR_CLASSES:
        assert catalog.blame_for(ec) in core.VALID_BLAME


def _consumer_blame_map() -> dict[str, str]:
    """从 consumer 的分类规则表 + classify_agent_error 硬编码分支推导
    error_class -> blame 全集（同源纪律：不重新发明，只是把已知代码路径的
    结果收集成 dict）。"""
    out = {klass: blame for klass, blame, _text, _pat in crc._ERROR_CLASS_RULES}
    # classify_agent_error 里硬编码（非规则表）的三类：
    out.setdefault("turn_timeout", "system")
    out.setdefault("reply_parse_failed", "system")
    out.setdefault("model_not_found", "user_provider")  # 裸 404+model 分支，和规则表一致
    out.setdefault("unknown", "system")
    return out


def test_catalog_blame_matches_consumer_blame_for_every_class():
    consumer_blame = _consumer_blame_map()
    assert set(consumer_blame) == set(crc.CONSUMER_ERROR_CLASSES)
    for ec, blame in consumer_blame.items():
        assert catalog.blame_for(ec) == blame, (
            f"blame mismatch for {ec}: catalog={catalog.blame_for(ec)!r} "
            f"consumer={blame!r}")
