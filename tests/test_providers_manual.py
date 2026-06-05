#!/usr/bin/env python3
"""Manual provider key smoke test.

Reuses backend/provider_client.test_provider_key to verify that each
configured model-API provider key actually works (sends a "Say ok." probe).

Keys are read from environment variables ONLY — nothing is hardcoded and
keys are never printed. Set only the providers you want to test:

  OPENAI_API_KEY        [OPENAI_MODEL=gpt-4o-mini]
  ANTHROPIC_API_KEY     [ANTHROPIC_MODEL=claude-haiku-4-5]
  GEMINI_API_KEY        [GEMINI_MODEL=gemini-2.5-flash]
  DEEPSEEK_API_KEY      [DEEPSEEK_MODEL=deepseek-v4-flash]
  OPENROUTER_API_KEY    [OPENROUTER_MODEL=openai/gpt-4o-mini]
  COMPAT_API_KEY        COMPAT_BASE_URL=https://...  [COMPAT_MODEL=...]

Run from the repo root:

  OPENAI_API_KEY=sk-... ANTHROPIC_API_KEY=sk-ant-... python3 tests/test_providers_manual.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from provider_client import ProviderConfig, ProviderError, mask_api_key  # noqa: E402
from provider_client import test_provider_key as _probe_provider_key  # noqa: E402

# Runnable smoke script, not a pytest module: this stops pytest from collecting
# the imported `test_provider_key` (a test_-prefixed function) as a test case.
__test__ = False

# provider, env key var, env model var, default model, base_url (or env var for compat)
PROVIDERS = [
    ("openai", "OPENAI_API_KEY", "OPENAI_MODEL", "gpt-4o-mini", ""),
    ("anthropic", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "claude-haiku-4-5", ""),
    ("gemini", "GEMINI_API_KEY", "GEMINI_MODEL", "gemini-2.5-flash", ""),
    ("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "deepseek-v4-flash", ""),
    ("openrouter", "OPENROUTER_API_KEY", "OPENROUTER_MODEL", "openai/gpt-4o-mini", ""),
    ("openai_compatible", "COMPAT_API_KEY", "COMPAT_MODEL", "", "COMPAT_BASE_URL"),
]


def main() -> None:
    tested = 0
    for provider, key_var, model_var, default_model, base_var in PROVIDERS:
        key = (os.environ.get(key_var) or "").strip()
        if not key:
            continue
        tested += 1
        model = (os.environ.get(model_var) or default_model).strip()
        base_url = (os.environ.get(base_var) or "").strip() if base_var else ""

        label = f"{provider:18s} key={mask_api_key(key):14s} model={model or '(none)'}"
        if not model:
            print(f"[SKIP] {label}  -> set {model_var}")
            continue

        t0 = time.monotonic()
        try:
            result = _probe_provider_key(
                ProviderConfig(provider=provider, model=model, api_key=key, base_url=base_url)
            )
            dt = (time.monotonic() - t0) * 1000
            reply = (result.get("reply") or "").replace("\n", " ")[:60]
            usage = result.get("usage") or {}
            print(f"[ OK ] {label}  {dt:6.0f}ms  reply={reply!r}  usage={usage}")
        except ProviderError as e:
            dt = (time.monotonic() - t0) * 1000
            code = f" http={e.status_code}" if e.status_code else ""
            print(f"[FAIL] {label}  {dt:6.0f}ms{code}  {e}")
        except Exception as e:  # noqa: BLE001
            print(f"[ERR ] {label}  {type(e).__name__}: {e}")

    if tested == 0:
        print("No provider keys found in environment. Set e.g. OPENAI_API_KEY=... and re-run.")


if __name__ == "__main__":
    main()
