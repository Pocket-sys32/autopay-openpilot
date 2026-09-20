from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import threading


FORM_ID = "1FAIpQLSfsn3xRdGLVcJXyoSBccSGiPYMi_fCqja-0Iay87If5Ncmu_Q"
ALLOWED_DURATIONS = frozenset({3600, 7200})
TERMINAL_STATES = frozenset({"succeeded", "failed", "expired", "action_required", "unknown"})
VALID_TRANSITIONS = {
  "accepted": frozenset({"preparing", "expired", "failed"}),
  "preparing": frozenset({"submitting", "failed", "action_required", "expired"}),
  # "failed" from submitting is only used for a card decline the provider confirmed.
  "submitting": frozenset({"succeeded", "unknown", "failed"}),
}
LAZ_PROVIDER_ID = "laz_ttp"
LAZ_LOCATION_ID = "143245"
LAZ_DURATION_SECONDS = 10800
LAZ_PAYER_FIELDS = ("payer_first_name", "payer_last_name", "name_on_card")


class AttemptConflict(RuntimeError):
  pass


class InvalidAttempt(ValueError):
  pass


def canonical_json(value: object) -> str:
  return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def now_unix_ms() -> int:
  return int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)


def validate_attempt(payload: dict[str, object], *, now_ms: int) -> dict[str, object]:
  required = {
    "schema_version", "environment", "attempt_id", "episode_id", "provider_id", "form_id",
    "qr_payload_sha256", "plate", "plate_country", "plate_region", "duration_seconds",
    "evidence_age_ms", "dispatch_deadline_unix_ms",
  }
  is_laz = payload.get("provider_id") == LAZ_PROVIDER_ID
  if set(payload) != (required | set(LAZ_PAYER_FIELDS) if is_laz else required):
    raise InvalidAttempt("attempt fields do not match schema version 1")
  if payload["schema_version"] != 1 or payload["environment"] != "demo":
    raise InvalidAttempt("unsupported schema or environment")
  if is_laz:
    if payload["form_id"] != LAZ_LOCATION_ID:
      raise InvalidAttempt("unsupported LAZ location")
    for field in LAZ_PAYER_FIELDS:
      value = payload[field]
      if not isinstance(value, str) or not 1 <= len(value) <= 40 or not all(c.isascii() and (c.isalpha() or c in " -'") for c in value):
        raise InvalidAttempt(f"invalid {field}")
  elif payload["provider_id"] != "demo_google_form" or payload["form_id"] != FORM_ID:
    raise InvalidAttempt("unsupported provider or form")
  for field in ("attempt_id", "episode_id"):
    value = payload[field]
    if not isinstance(value, str) or not value or len(value) > 64:
      raise InvalidAttempt(f"invalid {field}")
  digest = payload["qr_payload_sha256"]
  if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
    raise InvalidAttempt("invalid QR digest")
  plate = payload["plate"]
  if not isinstance(plate, str) or not plate.isascii() or not plate.isalnum() or not 1 <= len(plate) <= 12:
    raise InvalidAttempt("invalid plate")
  if payload["duration_seconds"] != LAZ_DURATION_SECONDS if is_laz else payload["duration_seconds"] not in ALLOWED_DURATIONS:
    raise InvalidAttempt("unsupported duration")
  evidence_age = payload["evidence_age_ms"]
  if isinstance(evidence_age, bool) or not isinstance(evidence_age, int) or not 0 <= evidence_age <= 1_000:
    raise InvalidAttempt("vehicle evidence is stale")
  deadline = payload["dispatch_deadline_unix_ms"]
  if isinstance(deadline, bool) or not isinstance(deadline, int) or deadline < now_ms or deadline > now_ms + 60_000:
    raise InvalidAttempt("invalid dispatch deadline")
  return payload


class ParkingStore:
  def __init__(self, path: str | Path):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    self.connection = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
    self.connection.row_factory = sqlite3.Row
    self.connection.execute("PRAGMA journal_mode=WAL")
    self.connection.execute("PRAGMA synchronous=FULL")
    self.connection.execute("PRAGMA busy_timeout=5000")
    self._lock = threading.RLock()
    self._create_schema()

  def close(self) -> None:
    self.connection.close()

  @contextmanager
  def transaction(self) -> Iterator[None]:
    with self._lock:
      self.connection.execute("BEGIN IMMEDIATE")
      try:
        yield
      except BaseException:
        self.connection.execute("ROLLBACK")
        raise
      else:
        self.connection.execute("COMMIT")

  def _create_schema(self) -> None:
    self.connection.executescript("""
      CREATE TABLE IF NOT EXISTS attempt (
        attempt_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        episode_id TEXT NOT NULL,
        payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        state TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        accepted_ms INTEGER NOT NULL,
        updated_ms INTEGER NOT NULL,
        result_version INTEGER NOT NULL DEFAULT 1,
        email_status TEXT NOT NULL DEFAULT 'pending',
        result_json TEXT,
        UNIQUE(device_id, episode_id)
      );
      CREATE TABLE IF NOT EXISTS event (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        state TEXT NOT NULL,
        created_ms INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS email_outbox (
        attempt_id TEXT PRIMARY KEY REFERENCES attempt(attempt_id),
        status TEXT NOT NULL,
        tries INTEGER NOT NULL DEFAULT 0,
        next_try_ms INTEGER NOT NULL,
        last_error TEXT
      );
    """)

  def put_attempt(self, device_id: str, payload: dict[str, object], *, now_ms: int | None = None) -> tuple[dict[str, object], bool]:
    now_ms = now_unix_ms() if now_ms is None else now_ms
    validate_attempt(payload, now_ms=now_ms)
    serialized = canonical_json(payload)
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    with self.transaction():
      existing = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (payload["attempt_id"],)).fetchone()
      if existing is not None:
        if existing["device_id"] != device_id or existing["payload_hash"] != digest:
          raise AttemptConflict("attempt ID already identifies different content")
        return self._public(existing), False
      episode = self.connection.execute(
        "SELECT attempt_id FROM attempt WHERE device_id=? AND episode_id=?", (device_id, payload["episode_id"]),
      ).fetchone()
      if episode is not None:
        raise AttemptConflict(f"episode already has attempt {episode['attempt_id']}")
      insert_sql = " ".join([
        "INSERT INTO attempt(attempt_id,device_id,episode_id,payload_hash,payload_json,state,reason_code,accepted_ms,updated_ms)",
        "VALUES(?,?,?,?,?,'accepted','ATTEMPT_ACCEPTED',?,?)",
      ])
      self.connection.execute(
        insert_sql,
        (payload["attempt_id"], device_id, payload["episode_id"], digest, serialized, now_ms, now_ms),
      )
      self._event(device_id, str(payload["attempt_id"]), "accepted", now_ms)
      row = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (payload["attempt_id"],)).fetchone()
      assert row is not None
      return self._public(row), True

  def get_attempt(self, device_id: str, attempt_id: str) -> dict[str, object] | None:
    row = self.connection.execute(
      "SELECT * FROM attempt WHERE device_id=? AND attempt_id=?", (device_id, attempt_id),
    ).fetchone()
    return None if row is None else self._public(row)

  def claim_next(self, *, now_ms: int | None = None) -> dict[str, object] | None:
    now_ms = now_unix_ms() if now_ms is None else now_ms
    with self.transaction():
      row = self.connection.execute("SELECT * FROM attempt WHERE state='accepted' ORDER BY accepted_ms LIMIT 1").fetchone()
      if row is None:
        return None
      payload = json.loads(row["payload_json"])
      if payload["dispatch_deadline_unix_ms"] < now_ms:
        result = canonical_json({"demo": True, "message": "Demo request expired before submission."})
        expire_sql = " ".join([
          "UPDATE attempt SET state='expired',reason_code='DISPATCH_DEADLINE_EXPIRED',updated_ms=?,",
          "result_version=result_version+1,result_json=?,email_status='pending' WHERE attempt_id=?",
        ])
        self.connection.execute(expire_sql, (now_ms, result, row["attempt_id"]))
        self.connection.execute(
          "INSERT OR IGNORE INTO email_outbox(attempt_id,status,next_try_ms) VALUES(?,'pending',?)",
          (row["attempt_id"], now_ms),
        )
        self._event(row["device_id"], row["attempt_id"], "expired", now_ms)
        return None
      self._transition_row(row, "preparing", "AUTOMATION_PREPARING", now_ms)
      updated = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (row["attempt_id"],)).fetchone()
      assert updated is not None
      return self._private(updated)

  def transition(self, attempt_id: str, state: str, reason_code: str, *, now_ms: int | None = None) -> dict[str, object]:
    now_ms = now_unix_ms() if now_ms is None else now_ms
    with self.transaction():
      row = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      if row is None:
        raise KeyError(attempt_id)
      if state == row["state"]:
        return self._public(row)
      if state not in VALID_TRANSITIONS.get(row["state"], frozenset()):
        raise RuntimeError(f"invalid transition {row['state']} -> {state}")
      self._transition_row(row, state, reason_code, now_ms)
      updated = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      assert updated is not None
      return self._public(updated)

  def complete(self, attempt_id: str, state: str, reason_code: str, result: dict[str, object], *, now_ms: int | None = None) -> dict[str, object]:
    if state not in TERMINAL_STATES:
      raise ValueError("completion must be terminal")
    now_ms = now_unix_ms() if now_ms is None else now_ms
    with self.transaction():
      row = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      if row is None:
        raise KeyError(attempt_id)
      if row["state"] in TERMINAL_STATES:
        return self._public(row)
      if state not in VALID_TRANSITIONS.get(row["state"], frozenset()):
        raise RuntimeError(f"invalid transition {row['state']} -> {state}")
      result_json = canonical_json(result)
      self.connection.execute(
        "UPDATE attempt SET state=?,reason_code=?,updated_ms=?,result_version=result_version+1,result_json=?,email_status='pending' WHERE attempt_id=?",
        (state, reason_code, now_ms, result_json, attempt_id),
      )
      self.connection.execute(
        "INSERT OR IGNORE INTO email_outbox(attempt_id,status,next_try_ms) VALUES(?,'pending',?)", (attempt_id, now_ms),
      )
      self._event(row["device_id"], attempt_id, state, now_ms)
      updated = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      assert updated is not None
      return self._public(updated)

  def recover_interrupted(self, *, now_ms: int | None = None) -> None:
    now_ms = now_unix_ms() if now_ms is None else now_ms
    with self.transaction():
      for row in self.connection.execute("SELECT * FROM attempt WHERE state IN ('preparing','submitting')").fetchall():
        if row["state"] == "preparing":
          self.connection.execute(
            "UPDATE attempt SET state='accepted',reason_code='WORKER_RESTARTED',updated_ms=? WHERE attempt_id=?",
            (now_ms, row["attempt_id"]),
          )
          self._event(row["device_id"], row["attempt_id"], "accepted", now_ms)
        else:
          result = {"message": "Submission result is unknown; the form was not submitted again.", "demo": True}
          self.connection.execute(
            "UPDATE attempt SET state='unknown',reason_code='INTERRUPTED_AFTER_SUBMIT',updated_ms=?,result_json=?,email_status='pending' WHERE attempt_id=?",
            (now_ms, canonical_json(result), row["attempt_id"]),
          )
          self.connection.execute("INSERT OR IGNORE INTO email_outbox(attempt_id,status,next_try_ms) VALUES(?,'pending',?)", (row["attempt_id"], now_ms))
          self._event(row["device_id"], row["attempt_id"], "unknown", now_ms)

  def events_after(self, device_id: str, after: int) -> list[dict[str, object]]:
    rows = self.connection.execute(
      "SELECT sequence,attempt_id,state,created_ms FROM event WHERE device_id=? AND sequence>? ORDER BY sequence LIMIT 100",
      (device_id, after),
    ).fetchall()
    return [dict(row) for row in rows]

  def next_email(self, *, now_ms: int | None = None) -> dict[str, object] | None:
    now_ms = now_unix_ms() if now_ms is None else now_ms
    query = " ".join([
      "SELECT a.*,o.tries FROM email_outbox o JOIN attempt a USING(attempt_id)",
      "WHERE o.status='pending' AND o.next_try_ms<=? ORDER BY o.next_try_ms LIMIT 1",
    ])
    row = self.connection.execute(query, (now_ms,)).fetchone()
    return None if row is None else self._private(row)

  def finish_email(self, attempt_id: str, *, sent: bool, error: str = "", now_ms: int | None = None) -> None:
    now_ms = now_unix_ms() if now_ms is None else now_ms
    with self.transaction():
      if sent:
        self.connection.execute("UPDATE email_outbox SET status='sent',tries=tries+1,last_error=NULL WHERE attempt_id=?", (attempt_id,))
        self.connection.execute("UPDATE attempt SET email_status='sent' WHERE attempt_id=?", (attempt_id,))
      else:
        row = self.connection.execute("SELECT tries FROM email_outbox WHERE attempt_id=?", (attempt_id,)).fetchone()
        tries = 1 + (0 if row is None else int(row["tries"]))
        delay_ms = min(3_600_000, 30_000 * (2 ** min(tries - 1, 7)))
        status = "failed" if tries >= 10 else "pending"
        self.connection.execute(
          "UPDATE email_outbox SET status=?,tries=?,next_try_ms=?,last_error=? WHERE attempt_id=?",
          (status, tries, now_ms + delay_ms, error[:256], attempt_id),
        )
        self.connection.execute("UPDATE attempt SET email_status=? WHERE attempt_id=?", (status, attempt_id))

  def mark_email_unknown(self, attempt_id: str, error: str) -> None:
    with self.transaction():
      self.connection.execute(
        "UPDATE email_outbox SET status='unknown',tries=tries+1,last_error=? WHERE attempt_id=?", (error[:256], attempt_id),
      )
      self.connection.execute("UPDATE attempt SET email_status='unknown' WHERE attempt_id=?", (attempt_id,))

  def _transition_row(self, row: sqlite3.Row, state: str, reason: str, now_ms: int) -> None:
    self.connection.execute(
      "UPDATE attempt SET state=?,reason_code=?,updated_ms=?,result_version=result_version+1 WHERE attempt_id=?",
      (state, reason, now_ms, row["attempt_id"]),
    )
    self._event(row["device_id"], row["attempt_id"], state, now_ms)

  def _event(self, device_id: str, attempt_id: str, state: str, now_ms: int) -> None:
    self.connection.execute("INSERT INTO event(device_id,attempt_id,state,created_ms) VALUES(?,?,?,?)", (device_id, attempt_id, state, now_ms))

  @staticmethod
  def _private(row: sqlite3.Row) -> dict[str, object]:
    value = ParkingStore._public(row)
    value["request"] = json.loads(row["payload_json"])
    return value

  @staticmethod
  def _public(row: sqlite3.Row) -> dict[str, object]:
    payload = json.loads(row["payload_json"])
    result = None if row["result_json"] is None else json.loads(row["result_json"])
    return {
      "schema_version": 1,
      "environment": "demo",
      "demo": payload.get("provider_id") != LAZ_PROVIDER_ID,
      "attempt_id": row["attempt_id"],
      "episode_id": row["episode_id"],
      "state": row["state"],
      "reason_code": row["reason_code"],
      "duration_seconds": payload["duration_seconds"],
      "updated_unix_ms": row["updated_ms"],
      "result_version": row["result_version"],
      "email_status": row["email_status"],
      "result": result,
    }
