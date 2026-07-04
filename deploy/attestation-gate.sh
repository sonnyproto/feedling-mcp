#!/usr/bin/env bash
# §2 incident guardrail: post-deploy attestation gate.
#
# Fetches <ATTEST_BASE>/attestation from the freshly deployed enclave and fails
# (exit 1) unless its live enclave_content_pk_hex matches the recorded baseline.
# A mismatch means a real key event (KMS/OS/config) OR a stale baseline — either
# way the deploy should go RED here, in minutes, not via a user weeks later.
#
# DEFENSIVE: if either ATTEST_BASE or BASELINE_PK is empty the gate SKIPS (exit 0)
# with a warning, so merging it can never break an existing deploy until the repo
# vars are deliberately set. A legitimate planned key change = update the
# baseline repo var in the same PR.
#
# Usage: attestation-gate.sh <env-name> <out-json-path>
#   env: ATTEST_BASE (e.g. https://<app-id>-5003s.dstack-pha-prod9.phala.network)
#        BASELINE_PK (64-hex enclave content pk)
set -euo pipefail

ENV_NAME="${1:-env}"
OUT_JSON="${2:-attestation.json}"

if [ -z "${ATTEST_BASE:-}" ] || [ -z "${BASELINE_PK:-}" ]; then
  echo "::warning::[$ENV_NAME] attestation gate SKIPPED — set repo vars for the"\
       "enclave URL + ENCLAVE_CONTENT_PK baseline to enable it (see deploy/attestation-gate.sh)."
  exit 0
fi

url="${ATTEST_BASE%/}/attestation"
echo ">>> [$ENV_NAME] attestation gate: $url"

# The enclave takes ~2min to come up; retry and tolerate transient 000/5xx.
body=""
for i in $(seq 1 12); do
  body="$(curl -sk --max-time 20 "$url" || true)"
  pk="$(printf '%s' "$body" \
    | python3 -c 'import sys,json;print(json.load(sys.stdin).get("enclave_content_pk_hex",""))' 2>/dev/null || true)"
  if [ -n "$pk" ]; then break; fi
  echo "    attestation not ready yet (attempt $i/12); sleeping 15s"
  sleep 15
  body=""
done

if [ -z "$body" ]; then
  echo "::error::[$ENV_NAME] /attestation never returned a usable enclave_content_pk_hex after ~3min"
  exit 1
fi

printf '%s' "$body" > "$OUT_JSON"  # archived as a build artifact by the caller
live_pk="$(printf '%s' "$body" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["enclave_content_pk_hex"])')"

echo "    live     = $live_pk"
echo "    baseline = $BASELINE_PK"
if [ "$live_pk" != "$BASELINE_PK" ]; then
  echo "::error::[$ENV_NAME] enclave_content_pk_hex != baseline — key drift or stale baseline."\
       "If this was a PLANNED key change, update the ENCLAVE_CONTENT_PK baseline repo var in the same PR."
  exit 1
fi
echo ">>> [$ENV_NAME] attestation gate PASS (live pk == baseline)"
