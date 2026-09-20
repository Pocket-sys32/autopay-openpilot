"""Submit one generic-agent dry run through the backend's authenticated local API.

The helper refuses to run unless PARKING_AGENT_DRY_RUN=1. It stops when checkout confirmation would be
required and never confirms or clicks PAY. Identity values are runtime overrides so personal data is not
stored in this file.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid

import requests


TERMINAL_STATES = frozenset({"succeeded", "failed", "expired", "action_required", "unknown"})


def setting(name: str, default: str) -> str:
  value = os.getenv(name, default).strip()
  if not value:
    raise SystemExit(f"{name} must not be empty")
  return value


def main() -> None:
  if os.environ.get("PARKING_AGENT_DRY_RUN") != "1":
    raise SystemExit("refusing to submit unless PARKING_AGENT_DRY_RUN=1")

  now_ms = time.time_ns() // 1_000_000
  attempt_id = f"agent-dry-{uuid.uuid4().hex[:12]}"
  url = setting("PARKING_DRY_RUN_URL", "https://clip.lazparking.com/p/143245")
  first_name = setting("PARKING_DRY_RUN_FIRST_NAME", "Demo")
  last_name = setting("PARKING_DRY_RUN_LAST_NAME", "Driver")
  payload = {
    "schema_version": 2,
    "environment": "demo",
    "attempt_id": attempt_id,
    "episode_id": f"episode-{uuid.uuid4().hex[:12]}",
    "provider_id": "generic_agent",
    "form_id": "clip.lazparking.com",
    "qr_url": url,
    "qr_payload_sha256": hashlib.sha256(url.encode()).hexdigest(),
    "plate": setting("PARKING_DRY_RUN_PLATE", "DEMO123"),
    "plate_country": setting("PARKING_DRY_RUN_PLATE_COUNTRY", "US"),
    "plate_region": setting("PARKING_DRY_RUN_PLATE_REGION", "CA"),
    "duration_seconds": int(setting("PARKING_DRY_RUN_DURATION_SECONDS", "10800")),
    "max_total_minor": int(setting("PARKING_DRY_RUN_MAX_TOTAL_MINOR", "3000")),
    "payer_first_name": first_name,
    "payer_last_name": last_name,
    "name_on_card": setting("PARKING_DRY_RUN_NAME_ON_CARD", f"{first_name} {last_name}"),
    "evidence_age_ms": 0,
    "dispatch_deadline_unix_ms": now_ms + 30_000,
  }
  headers = {"Authorization": f"Bearer {setting('PARKING_BEARER_TOKEN', '')}"}
  base_url = setting("PARKING_DRY_RUN_BACKEND_URL", "http://127.0.0.1:8080")
  response = requests.put(f"{base_url}/v1/attempts/{attempt_id}", json=payload, headers=headers, timeout=10)
  response.raise_for_status()
  print(json.dumps({"attempt_id": attempt_id, "submitted": True}, sort_keys=True), flush=True)

  deadline = time.monotonic() + int(setting("PARKING_DRY_RUN_POLL_TIMEOUT_S", "360"))
  last_state = None
  cancelled = False
  while time.monotonic() < deadline:
    response = requests.get(f"{base_url}/v1/attempts/{attempt_id}", headers=headers, timeout=10)
    response.raise_for_status()
    body = response.json()
    state = body.get("state")
    if state != last_state:
      print(json.dumps({"state": state, "reason_code": body.get("reason_code")}, sort_keys=True), flush=True)
      last_state = state
    if state == "confirmation_required":
      confirmation = body.get("confirmation")
      if not isinstance(confirmation, dict) or not isinstance(confirmation.get("quote_hash"), str):
        raise SystemExit("confirmation is missing its quote hash; refusing to continue")
      if not cancelled:
        public_confirmation = {key: confirmation.get(key) for key in
                               ("quote_hash", "merchant", "duration_seconds", "total_minor", "currency")}
        print(json.dumps({"attempt_id": attempt_id, "confirmation": public_confirmation}, sort_keys=True),
              flush=True)
        decision = {"schema_version": 1, "attempt_id": attempt_id, "decision": "cancel",
                    "quote_hash": confirmation["quote_hash"]}
        response = requests.post(f"{base_url}/v1/attempts/{attempt_id}/decision", json=decision,
                                 headers=headers, timeout=10)
        response.raise_for_status()
        print(json.dumps({"attempt_id": attempt_id, "decision": "cancel",
                          "decision_outcome": response.json().get("decision_outcome")}, sort_keys=True), flush=True)
        cancelled = True
    if state in TERMINAL_STATES:
      print(json.dumps({"attempt_id": attempt_id, "terminal": body}, sort_keys=True), flush=True)
      return
    time.sleep(2)
  raise SystemExit("timed out waiting for the agent")


if __name__ == "__main__":
  main()
