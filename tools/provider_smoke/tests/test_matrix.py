from tools.provider_smoke import matrix


def test_all_providers_covers_the_six():
    assert set(matrix.all_providers()) == {
        "anthropic", "openai", "deepseek", "gemini", "openrouter", "openai_compatible",
    }


def test_load_matrix_includes_only_providers_with_keys():
    env = {"ANTHROPIC_API_KEY": "sk-ant-xxx", "DEEPSEEK_API_KEY": "  "}
    loaded = matrix.load_matrix(env)
    assert set(loaded) == {"anthropic"}            # deepseek blank -> skipped
    assert loaded["anthropic"]["api_key"] == "sk-ant-xxx"
    assert loaded["anthropic"]["models"] == ["claude-haiku-4-5"]


def test_load_matrix_openai_compatible_carries_base_url():
    loaded = matrix.load_matrix({"KIMI_API_KEY": "sk-kimi"})
    assert "openai_compatible" in loaded
    assert loaded["openai_compatible"]["base_url"].startswith("https://")


def test_load_matrix_empty_env_is_empty():
    assert matrix.load_matrix({}) == {}
