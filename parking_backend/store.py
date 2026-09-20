from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import datetime
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import threading
from urllib.parse import urlsplit


FORM_ID = "1FAIpQLSfsn3xRdGLVcJXyoSBccSGiPYMi_fCqja-0Iay87If5Ncmu_Q"
ALLOWED_DURATIONS = frozenset({3600, 7200})
TERMINAL_STATES = frozenset({"succeeded", "failed", "expired", "action_required", "unknown"})
VALID_TRANSITIONS = {
  "accepted": frozenset({"preparing", "expired", "failed"}),
  "preparing": frozenset({"submitting", "confirmation_required", "failed", "action_required", "expired"}),
  # The agent has parked at a checkout it has not paid for; only a user decision moves it on.
  "confirmation_required": frozenset({"committing", "expired", "failed"}),
  # The user confirmed, but nothing has been clicked yet: mark_submitting() runs immediately before PAY.
  "committing": frozenset({"submitting", "failed", "action_required", "unknown", "expired"}),
  # "failed" from submitting is only used for a card decline the provider confirmed.
  "submitting": frozenset({"succeeded", "unknown", "failed"}),
}
DECISIONS = frozenset({"confirm", "cancel"})
LAZ_PROVIDER_ID = "laz_ttp"
LAZ_LOCATION_ID = "143245"
LAZ_DURATION_SECONDS = 10800
LAZ_PAYER_FIELDS = ("payer_first_name", "payer_last_name", "name_on_card")


class AttemptConflict(RuntimeError):
  pass


class InvalidAttempt(ValueError):
  pass


class DecisionConflict(RuntimeError):
  """The decision does not apply to this attempt in its current state, or names a different quote."""


class DecisionExpired(RuntimeError):
  """The confirmation window closed before the decision arrived; the attempt is now expired."""


def canonical_json(value: object) -> str:
  return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def now_unix_ms() -> int:
  return int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)


GENERIC_PROVIDER_ID = "generic_agent"
GENERIC_MIN_DURATION = 300
GENERIC_MAX_DURATION = 24 * 3600
GENERIC_MAX_TOTAL_MINOR = 20_000
BASE_REQUIRED = frozenset({
  "schema_version", "environment", "attempt_id", "episode_id", "provider_id", "form_id",
  "qr_payload_sha256", "plate", "plate_country", "plate_region", "duration_seconds",
  "evidence_age_ms", "dispatch_deadline_unix_ms",
})


def _check_demo_form(payload: dict[str, object]) -> None:
  if payload["form_id"] != FORM_ID:
    raise InvalidAttempt("unsupported provider or form")
  if payload["duration_seconds"] not in ALLOWED_DURATIONS:
    raise InvalidAttempt("unsupported duration")


def _check_laz(payload: dict[str, object]) -> None:
  if payload["form_id"] != LAZ_LOCATION_ID:
    raise InvalidAttempt("unsupported LAZ location")
  for field in LAZ_PAYER_FIELDS:
    value = payload[field]
    if not isinstance(value, str) or not 1 <= len(value) <= 40 or not all(c.isascii() and (c.isalpha() or c in " -'") for c in value):
      raise InvalidAttempt(f"invalid {field}")
  if payload["duration_seconds"] != LAZ_DURATION_SECONDS:
    raise InvalidAttempt("unsupported duration")


def _check_generic(payload: dict[str, object]) -> None:
  """The device already applied its own URL policy; this is the backend's independent pass over the same
  payload. The VM re-derives the host from qr_url rather than trusting form_id."""
  url = payload["qr_url"]
  if not isinstance(url, str) or not url.startswith("https://") or len(url) > 512 or url != url.strip():
    raise InvalidAttempt("invalid parking URL")
  host = payload["form_id"]
  if not isinstance(host, str) or not host or host.lower() != urlsplit(url).hostname:
    raise InvalidAttempt("form_id must be the parking URL host")
  duration = payload["duration_seconds"]
  if not isinstance(duration, int) or isinstance(duration, bool):
    raise InvalidAttempt("unsupported duration")
  if not GENERIC_MIN_DURATION <= duration <= GENERIC_MAX_DURATION or duration % 60:
    raise InvalidAttempt("unsupported duration")
  cap = payload["max_total_minor"]
  if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= GENERIC_MAX_TOTAL_MINOR:
    raise InvalidAttempt("invalid spend cap")


@dataclass(frozen=True, slots=True)
class ProviderSpec:
  schema_versions: frozenset[int]
  required: frozenset[str] = frozenset()
  optional: frozenset[str] = frozenset()
  check: Callable[[dict[str, object]], None] = lambda _payload: None


PROVIDER_SPECS: dict[str, ProviderSpec] = {
  "demo_google_form": ProviderSpec(frozenset({1, 2}), check=_check_demo_form),
  LAZ_PROVIDER_ID: ProviderSpec(frozenset({1, 2}), required=frozenset(LAZ_PAYER_FIELDS), check=_check_laz),
  GENERIC_PROVIDER_ID: ProviderSpec(frozenset({2}), required=frozenset({"qr_url", "max_total_minor"}),
                                    check=_check_generic),
}


def validate_attempt(payload: dict[str, object], *, now_ms: int) -> dict[str, object]:
  spec = PROVIDER_SPECS.get(str(payload.get("provider_id", "")))
  if spec is None:
    raise InvalidAttempt("unsupported provider")
  allowed = BASE_REQUIRED | spec.required | spec.optional
  missing = (BASE_REQUIRED | spec.required) - set(payload)
  extra = set(payload) - allowed
  if missing or extra:
    raise InvalidAttempt("attempt fields do not match the provider schema")
  if payload["schema_version"] not in spec.schema_versions or payload["environment"] != "demo":
    raise InvalidAttempt("unsupported schema or environment")
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
  spec.check(payload)
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
    self._migrate()

  def _migrate(self) -> None:
    """Add columns introduced after the first deployment. The tables above use IF NOT EXISTS, so an existing
    database keeps its original shape until this runs."""
    existing = {row["name"] for row in self.connection.execute("PRAGMA table_info(attempt)").fetchall()}
    for column, definition in (("quote_hash", "TEXT"), ("confirmation_json", "TEXT"),
                               ("confirmation_expires_ms", "INTEGER"), ("decision", "TEXT"),
                               ("decided_ms", "INTEGER")):
      if column not in existing:
        self.connection.execute(f"ALTER TABLE attempt ADD COLUMN {column} {definition}")

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

  def await_confirmation(self, attempt_id: str, summary: dict[str, object], *, ttl_ms: int,
                         now_ms: int | None = None) -> dict[str, object]:
    """Park an attempt at a checkout the agent reached but has not paid for.

    The quote hash covers exactly the fields rendered on the device, so a summary the user never saw cannot
    be confirmed, and neither can one whose price moved between render and tap."""
    now_ms = now_unix_ms() if now_ms is None else now_ms
    quote_hash = hashlib.sha256(canonical_json(summary).encode()).hexdigest()
    with self.transaction():
      row = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      if row is None:
        raise KeyError(attempt_id)
      if "confirmation_required" not in VALID_TRANSITIONS.get(row["state"], frozenset()):
        raise RuntimeError(f"invalid transition {row['state']} -> confirmation_required")
      self.connection.execute(
        " ".join(["UPDATE attempt SET state='confirmation_required',reason_code='CHECKOUT_READY',updated_ms=?,",
                  "result_version=result_version+1,quote_hash=?,confirmation_json=?,confirmation_expires_ms=?",
                  "WHERE attempt_id=?"]),
        (now_ms, quote_hash, canonical_json(summary), now_ms + ttl_ms, attempt_id),
      )
      self._event(row["device_id"], attempt_id, "confirmation_required", now_ms)
      updated = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      assert updated is not None
      return self._public(updated)

  def record_decision(self, device_id: str, attempt_id: str, decision: str, quote_hash: str,
                      *, now_ms: int | None = None) -> tuple[dict[str, object], str]:
    """Apply a user's confirm or cancel. Returns (public row, "accepted" | "replayed").

    The decision write and the state transition commit together, so a retried request can never produce a
    second transition."""
    if decision not in DECISIONS:
      raise DecisionConflict("unsupported decision")
    now_ms = now_unix_ms() if now_ms is None else now_ms
    expired = False
    with self.transaction():
      row = self.connection.execute(
        "SELECT * FROM attempt WHERE device_id=? AND attempt_id=?", (device_id, attempt_id)).fetchone()
      if row is None:
        raise KeyError(attempt_id)
      if row["decision"] is not None:
        if row["decision"] == decision and hmac.compare_digest(str(row["quote_hash"] or ""), quote_hash):
          return self._public(row), "replayed"
        raise DecisionConflict("attempt already carries a different decision")
      if row["state"] != "confirmation_required":
        raise DecisionConflict(f"attempt is {row['state']}, not awaiting confirmation")
      if not hmac.compare_digest(str(row["quote_hash"] or ""), quote_hash):
        raise DecisionConflict("QUOTE_HASH_MISMATCH")
      if now_ms > int(row["confirmation_expires_ms"] or 0):
        # Commit the expiry before raising: an exception inside transaction() rolls back.
        self._finish(row, "expired", "CONFIRMATION_TIMEOUT",
                     {"demo": False, "message": "The confirmation window closed. Nothing was purchased."}, now_ms)
        expired = True
      else:
        self.connection.execute("UPDATE attempt SET decision=?,decided_ms=? WHERE attempt_id=?",
                                (decision, now_ms, attempt_id))
        if decision == "confirm":
          self._transition_row(row, "committing", "USER_CONFIRMED", now_ms)
        else:
          self._finish(row, "failed", "USER_DECLINED",
                       {"demo": False, "message": "You cancelled. Nothing was purchased."}, now_ms)
      updated = self.connection.execute("SELECT * FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      assert updated is not None
      public = self._public(updated)
    if expired:
      raise DecisionExpired("the confirmation window closed")
    return public, "accepted"

  def pending_decision(self, attempt_id: str) -> str | None:
    row = self.connection.execute("SELECT decision FROM attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
    return None if row is None else row["decision"]

  def _finish(self, row: sqlite3.Row, state: str, reason: str, result: dict[str, object], now_ms: int) -> None:
    """Terminal completion inside an open transaction (complete() opens its own)."""
    self.connection.execute(
      " ".join(["UPDATE attempt SET state=?,reason_code=?,updated_ms=?,result_version=result_version+1,",
                "result_json=?,email_status='pending' WHERE attempt_id=?"]),
      (state, reason, now_ms, canonical_json(result), row["attempt_id"]),
    )
    self.connection.execute("INSERT OR IGNORE INTO email_outbox(attempt_id,status,next_try_ms) VALUES(?,'pending',?)",
                            (row["attempt_id"], now_ms))
    self._event(row["device_id"], row["attempt_id"], state, now_ms)

  def recover_interrupted(self, *, now_ms: int | None = None) -> None:
    now_ms = now_unix_ms() if now_ms is None else now_ms
    with self.transaction():
      query = "SELECT * FROM attempt WHERE state IN ('preparing','confirmation_required','committing','submitting')"
      for row in self.connection.execute(query).fetchall():
        if row["state"] == "confirmation_required":
          # The summary on screen is bound to a browser session this restart destroyed. Honouring the confirm
          # would mean re-navigating and re-pricing, so the attempt ends and the device starts a new episode.
          self._finish(row, "expired", "SESSION_LOST_BEFORE_PAYMENT",
                       {"demo": False, "message": "The parking session was lost before payment. Nothing was purchased."},
                       now_ms)
        elif row["state"] == "committing":
          # mark_submitting() runs immediately before PAY, so "committing" provably means nothing was clicked.
          self._finish(row, "failed", "AUTOMATION_INTERRUPTED_BEFORE_SUBMIT",
                       {"demo": False, "message": "Interrupted before payment. Nothing was purchased."}, now_ms)
        elif row["state"] == "preparing":
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
  def _confirmation_public(row: sqlite3.Row) -> dict[str, object] | None:
    """The checkout summary for the device, hard-truncated. The device rejects any response over 64 KiB, and
    this is the only field whose size a merchant page can influence."""
    raw = row["confirmation_json"]
    if raw is None:
      return None
    summary = json.loads(raw)
    text = lambda key: str(summary.get(key, ""))[:64]  # noqa: E731
    items = [[str(name)[:32], int(minor)] for name, minor in list(summary.get("line_items") or [])[:6]]
    return {
      "quote_hash": row["quote_hash"],
      "expires_at_unix_ms": row["confirmation_expires_ms"],
      "merchant": text("merchant"), "merchant_host": text("merchant_host"),
      "location_label": text("location_label"), "plate": text("plate"),
      "duration_seconds": int(summary.get("duration_seconds") or 0),
      "total_minor": int(summary.get("total_minor") or 0),
      "currency": text("currency") or "USD",
      "line_items": items,
    }

  @staticmethod
  def _public(row: sqlite3.Row) -> dict[str, object]:
    payload = json.loads(row["payload_json"])
    result = None if row["result_json"] is None else json.loads(row["result_json"])
    return {
      "schema_version": 1,
      "environment": "demo",
      "demo": payload.get("provider_id") == "demo_google_form",
      "attempt_id": row["attempt_id"],
      "episode_id": row["episode_id"],
      "state": row["state"],
      "reason_code": row["reason_code"],
      "duration_seconds": payload["duration_seconds"],
      "updated_unix_ms": row["updated_ms"],
      "result_version": row["result_version"],
      "email_status": row["email_status"],
      "result": result,
      "confirmation": ParkingStore._confirmation_public(row) if row["state"] == "confirmation_required" else None,
    }
