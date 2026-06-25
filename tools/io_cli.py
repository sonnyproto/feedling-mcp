#!/usr/bin/env python3
"""io_cli — thin Feedling tool client for resident (VPS) agents.

A resident autonomous agent (OpenClaw / Hermes / Claude Code) registers this as
a NATIVE tool so it can pull Feedling perception during chat (true agentic pull),
instead of the prompt-"emit tool_calls JSON" hack that does not work with
autonomous agents. See docs/PERCEPTION_CLI_DESIGN.md.

Design notes:
  - Stdlib only (urllib) — runs in any agent venv, no httpx/requests/psycopg.
  - Output is JSON on stdout (the agent parses it). Errors are JSON too.
  - Two-head routing:
      perception.*   -> main backend (FEEDLING_API_URL)   [coarse, no decrypt]
      photo/memory   -> enclave (FEEDLING_ENCLAVE_URL)     [decrypt; phase 2]
  - Auth: X-API-Key = FEEDLING_API_KEY (both backend and enclave accept it).

Config via env (same as the resident consumer): FEEDLING_API_URL,
FEEDLING_API_KEY, FEEDLING_ENCLAVE_URL.

MVP = `perception`. send / wait-for-wake / schedule-wake / photo are phase 2 and
currently return a clean "not implemented" JSON so the agent degrades gracefully.
"""
import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

FAST_SIGNALS = ("now", "location", "weather", "motion", "calendar")
SLOW_SIGNALS = ("steps", "sleep", "workout", "vitals")
# pull-only context signals (focus = are-you-in-a-focus-mode, audio_route =
# headphones/car). Valid + pullable, but kept out of the default fast set.
EXTRA_SIGNALS = ("focus", "audio_route")
PERCEPTION_SIGNALS = FAST_SIGNALS + SLOW_SIGNALS + EXTRA_SIGNALS

PHASE2_VERBS = ("send", "wait-for-wake", "schedule-wake", "photo")


def _emit(obj, code=0):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.exit(code)


def _env(name):
    return os.environ.get(name, "").strip()


def _http_json(method, url, api_key, *, payload=None, insecure=False, timeout=30):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-API-Key": api_key, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    # insecure: the enclave presents a dstack-gateway TEE cert the local httpx
    # client does not verify today (consumer uses verify=False); mirror that for
    # enclave calls only. Backend calls use normal TLS verification.
    ctx = ssl._create_unverified_context() if insecure else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
        except Exception:
            detail = {"error": "http_error"}
        return e.code, detail
    except Exception as e:  # noqa: BLE001 — return a JSON error, never crash the agent
        return -1, {"error": f"{type(e).__name__}: {e}"}


def cmd_perception(args):
    api_url = _env("FEEDLING_API_URL")
    api_key = _env("FEEDLING_API_KEY")
    if not api_url or not api_key:
        _emit({"ok": False, "error": "missing FEEDLING_API_URL / FEEDLING_API_KEY in env"}, 2)
    signals = list(args.signals) or list(FAST_SIGNALS)
    unknown = [s for s in signals if s not in PERCEPTION_SIGNALS]
    if unknown:
        _emit({"ok": False, "error": f"unknown signals: {unknown}",
               "available": list(PERCEPTION_SIGNALS)}, 2)
    qs = urllib.parse.urlencode({"signals": ",".join(signals)})
    url = f"{api_url.rstrip('/')}/v1/agent/perception?{qs}"
    status, body = _http_json("GET", url, api_key)
    if status == 200:
        _emit({"ok": True, **body})
    # Surface the backend's shape verbatim so the agent (and we, during
    # acceptance) can see disabled/switch_off/not_permitted reasons + 404 before
    # the backend verb ships.
    _emit({"ok": False, "http_status": status, "error": body}, 1)


def cmd_phase2(args):
    _emit({"ok": False,
           "error": f"'{args.verb}' is not implemented yet (phase 2)",
           "see": "docs/PERCEPTION_CLI_DESIGN.md"}, 3)


def main():
    p = argparse.ArgumentParser(
        prog="io_cli",
        description="Feedling resident-agent tool client. Outputs JSON.",
    )
    sub = p.add_subparsers(dest="verb", required=True)

    pp = sub.add_parser("perception", help="Pull current coarse perception signals (JSON).")
    pp.add_argument(
        "signals", nargs="*",
        help="one or more of: " + ", ".join(PERCEPTION_SIGNALS) + " (default: fast set)",
    )
    pp.set_defaults(func=cmd_perception)

    for verb in PHASE2_VERBS:
        sp = sub.add_parser(verb, help="(phase 2 — not implemented yet)")
        sp.add_argument("rest", nargs="*")
        sp.set_defaults(func=cmd_phase2)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
