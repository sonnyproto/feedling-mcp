"""Pass/fail predicates for a hosted-agent smoke turn."""
import secrets

# Fallback 话术黑名单：agent-runner 接不上真模型时给用户的占位文案。
FALLBACK_MARKERS = [
    "我这会儿有点慢",
    "刚刚没接上",
    "稍后再发一次",
    "我会继续接",
]


def make_token() -> str:
    return "PONG-" + secrets.token_hex(4).upper()


def token_echoed(reply: str, token: str) -> bool:
    return token.lower() in (reply or "").lower()


def is_fallback(reply: str) -> bool:
    r = reply or ""
    return any(marker in r for marker in FALLBACK_MARKERS)


def context_recalled(reply: str, fact: str) -> bool:
    return fact.lower() in (reply or "").lower()
