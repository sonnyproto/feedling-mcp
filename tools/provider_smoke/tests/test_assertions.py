from tools.provider_smoke import assertions


def test_make_token_has_prefix_and_is_unique():
    a, b = assertions.make_token(), assertions.make_token()
    assert a.startswith("PONG-") and len(a) > 5
    assert a != b


def test_token_echoed_case_insensitive():
    assert assertions.token_echoed("好的，PONG-ABCD1234", "pong-abcd1234")
    assert not assertions.token_echoed("我不知道", "PONG-ABCD1234")


def test_is_fallback_detects_known_phrase():
    assert assertions.is_fallback("我这会儿有点慢，刚刚没接上。你稍后再发一次")
    assert not assertions.is_fallback("PONG-ABCD1234")


def test_context_recalled():
    assert assertions.context_recalled("那个词是 PONG-ABCD1234", "PONG-ABCD1234")
    assert not assertions.context_recalled("我忘了", "PONG-ABCD1234")
