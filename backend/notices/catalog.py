"""chat 通道 error_class → (blame, user_text) 目录（spec Phase B / B3）。

同源纪律：这 8 类、blame、user_text 一字不差搬自
``tools/chat_resident_consumer.py`` 的 ``_ERROR_CLASS_RULES`` +
``classify_agent_error`` 硬编码分支（turn_timeout / reply_parse_failed /
model_not_found[裸404] / unknown）。两处各自维护（consumer 在 tools/ 不能
import backend），一致性由 tests/test_catalog_consumer_parity.py 锁定。

blame 三分类纪律（照 docs/FRONTEND_ERROR_CONTRACT.md §二）：
- user_provider      → 可以引导用户去充值 / 改 key / 改模型名
- provider_transient → 上游临时问题，等它自己恢复
- system             → 我们的问题，绝不能引导用户改配置（会误导用户，dded 案例的教训）
"""
from __future__ import annotations

ERROR_CLASSES = frozenset({
    "quota_insufficient",
    "auth_invalid",
    "model_not_found",
    "rate_limited",
    "upstream_unavailable",
    "turn_timeout",
    "reply_parse_failed",
    "unknown",
})

# error_class -> (blame, user_text)
_CATALOG: dict[str, tuple[str, str]] = {
    "quota_insufficient": (
        "user_provider", "你的 API 服务额度不足，充值后再发消息即可恢复。"),
    "auth_invalid": (
        "user_provider", "API Key 无效或已过期，请到设置里重新保存。"),
    "model_not_found": (
        "user_provider", "模型名不可用，请检查设置里的模型名。"),
    "rate_limited": (
        "provider_transient", "你的 API 服务限流了，稍等几分钟再试。"),
    "upstream_unavailable": (
        "provider_transient", "你的模型服务暂时不可用，稍后会自动恢复。"),
    "turn_timeout": (
        "system", "这轮回复超时了，稍后再试。"),
    "reply_parse_failed": (
        "system", "系统处理回复时出了问题，我们会尽快排查。"),
    "unknown": (
        "system", "连接模型服务时出了问题。"),
}

_FALLBACK_BLAME = "system"
_FALLBACK_USER_TEXT = "连接模型服务时出了问题。"


def blame_for(error_class: str) -> str:
    """未命中的 error_class 安全兜底到 'system'——绝不 KeyError，也绝不
    误导性地默认成 user_provider（那会让用户白跑一趟改配置）。"""
    entry = _CATALOG.get(error_class)
    return entry[0] if entry is not None else _FALLBACK_BLAME


def user_text_for(error_class: str, **ctx) -> str:
    """``**ctx`` 为未来动态占位（如失败次数）预留——当前 8 类均静态文案，
    ctx 暂被忽略；保留形参使接口稳定，未来加占位不必改调用方签名。"""
    entry = _CATALOG.get(error_class)
    return entry[1] if entry is not None else _FALLBACK_USER_TEXT
