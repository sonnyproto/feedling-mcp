from tools.provider_smoke import run_smoke


def test_parse_args_defaults():
    ns = run_smoke.parse_args([])
    assert ns.providers == []
    assert ns.reuse is False
    assert ns.base_url == run_smoke.DEFAULT_BASE_URL
    assert ns.turns == 2


def test_parse_args_subset_and_flags():
    ns = run_smoke.parse_args(["deepseek", "gemini", "--reuse", "--timeout", "30"])
    assert ns.providers == ["deepseek", "gemini"]
    assert ns.reuse is True
    assert ns.timeout == 30.0


def test_load_dotenv_parses_and_strips_quotes(tmp_path):
    p = tmp_path / ".env"
    p.write_text('# comment\nFOO="bar"\nBAZ = qux \nBLANK=\n')
    env = run_smoke.load_dotenv(str(p))
    assert env["FOO"] == "bar"
    assert env["BAZ"] == "qux"
    assert env["BLANK"] == ""


def test_format_summary_contains_rows():
    out = run_smoke.format_summary([
        run_smoke._res("anthropic", "PASS", "-", "2 turns OK"),
        run_smoke._res("gemini", "FAIL", "no-reply", "120s 内无回复"),
    ])
    assert "anthropic" in out and "PASS" in out
    assert "gemini" in out and "no-reply" in out
