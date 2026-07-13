"""io_cli onboarding/chat verb payload builders (pure).

Covers the thin io_cli verbs added for onboarding acceptance tooling:
``onboarding-validate`` (GET /v1/onboarding/validate) and ``chat-verify-loop``
(POST /v1/chat/verify_loop).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))

import io_cli  # noqa: E402
