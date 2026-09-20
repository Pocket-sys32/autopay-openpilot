from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3

from openpilot.selfdrive.parking.models import (AttemptRequest, AttemptState, OperationResult, TERMINAL_ATTEMPT_STATES,
                                                canonical_json)


class JournalError(RuntimeError):
  pass


class AttemptConflict(JournalError):
  pass


class InvalidTransition(JournalError):
  pass


@dataclass(frozen=True, slots=True)
class StoredAttempt:
  attempt_id: str
  episode_id: str
  quote_id: str
  payload_sha256: str
  state: AttemptState
  dispatch_deadline_unix_ms: int
  canonical_request: dict[str, object]
  operation_result: OperationResult | None


_ALLOWED_TRANSITIONS: dict[AttemptState, frozenset[AttemptState]] = {
  AttemptState.AUTHORIZED: frozenset({AttemptState.DISPATCHING, AttemptState.CANCELLED, AttemptState.EXPIRED}),
  AttemptState.DISPATCHING: frozenset({AttemptState.PENDING, AttemptState.ACTION_REQUIRED, AttemptState.UNKNOWN,
                                      AttemptState.ACTIVE, AttemptState.DECLINED, AttemptState.FAILED_DEFINITIVELY}),
  AttemptState.PENDING: frozenset({AttemptState.AWAITING_CONFIRMATION, AttemptState.ACTION_REQUIRED,
                                  AttemptState.UNKNOWN, AttemptState.ACTIVE, AttemptState.DECLINED,
                                  AttemptState.CANCELLED, AttemptState.FAILED_DEFINITIVELY}),
  # The agent is parked at a checkout it has not paid for, waiting on the driver.
  AttemptState.AWAITING_CONFIRMATION: frozenset({AttemptState.PENDING, AttemptState.ACTION_REQUIRED,
                                                AttemptState.UNKNOWN, AttemptState.ACTIVE, AttemptState.DECLINED,
                                                AttemptState.CANCELLED, AttemptState.EXPIRED,
                                                AttemptState.FAILED_DEFINITIVELY}),
  AttemptState.ACTION_REQUIRED: frozenset({AttemptState.PENDING, AttemptState.UNKNOWN, AttemptState.ACTIVE,
                                          AttemptState.DECLINED, AttemptState.CANCELLED, AttemptState.EXPIRED}),
  AttemptState.UNKNOWN: frozenset({AttemptState.PENDING, AttemptState.ACTION_REQUIRED, AttemptState.ACTIVE,
                                  AttemptState.DECLINED, AttemptState.FAILED_DEFINITIVELY}),
  AttemptState.ACTIVE: frozenset(),
  AttemptState.DECLINED: frozenset(),
  AttemptState.CANCELLED: frozenset(),
  AttemptState.EXPIRED: frozenset(),
  AttemptState.FAILED_DEFINITIVELY: frozenset(),
}


class ParkingJournal:
  def __init__(self, path: str | Path):
    self.path = str(path)
    if self.path != ":memory:":
      parent = Path(self.path).parent
      parent_created = not parent.exists()
      parent.mkdir(mode=0o700, parents=True, exist_ok=True)
      if parent_created:
        os.chmod(parent, 0o700)
    self.connection = sqlite3.connect(self.path, timeout=2.0, isolation_level=None)
    self.connection.row_factory = sqlite3.Row
    self.connection.execute("PRAGMA foreign_keys=ON")
    self.connection.execute("PRAGMA busy_timeout=2000")
    self.connection.execute("PRAGMA synchronous=FULL")
    if self.path != ":memory:":
      self.connection.execute("PRAGMA journal_mode=WAL")
    self._create_schema()
    if self.path != ":memory:":
      os.chmod(self.path, 0o600)

  def close(self) -> None:
    self.connection.close()

  def __enter__(self) -> ParkingJournal:
    return self

  def __exit__(self, *_args: object) -> None:
    self.close()

  @contextmanager
  def _transaction(self) -> Iterator[None]:
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
      CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
      );
      INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1');
      CREATE TABLE IF NOT EXISTS parking_episode (
        episode_id TEXT PRIMARY KEY,
        created_wall_ms INTEGER NOT NULL,
        created_mono_ns INTEGER NOT NULL,
        state TEXT NOT NULL,
        version INTEGER NOT NULL
      );
      CREATE TABLE IF NOT EXISTS local_attempt (
        attempt_id TEXT PRIMARY KEY,
        episode_id TEXT NOT NULL REFERENCES parking_episode(episode_id),
        quote_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        state TEXT NOT NULL,
        dispatch_deadline_unix_ms INTEGER NOT NULL,
        canonical_json TEXT NOT NULL,
        result_json TEXT
      );
      CREATE TABLE IF NOT EXISTS transition_event (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        episode_id TEXT,
        attempt_id TEXT,
        mono_ns INTEGER NOT NULL,
        wall_ms INTEGER,
        event_type TEXT NOT NULL,
        reason_code TEXT NOT NULL,
        redacted_json TEXT NOT NULL
      );
      CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_per_episode ON local_attempt(episode_id);
    """)

  def create_episode(self, episode_id: str, *, created_wall_ms: int, created_mono_ns: int) -> None:
    if not episode_id:
      raise ValueError("episode_id must be nonempty")
    with self._transaction():
      existing = self.connection.execute("SELECT * FROM parking_episode WHERE episode_id=?", (episode_id,)).fetchone()
      if existing is not None:
        return
      self.connection.execute(
        "INSERT INTO parking_episode VALUES (?, ?, ?, 'candidate', 1)",
        (episode_id, created_wall_ms, created_mono_ns),
      )
      self._insert_event(episode_id, None, created_mono_ns, created_wall_ms, "episode_created", "CANDIDATE_CREATED", {})

  def create_attempt(self, request: AttemptRequest, *, mono_ns: int, wall_ms: int) -> StoredAttempt:
    request_json = canonical_json(request.to_dict())
    request_hash = request.payload_sha256
    with self._transaction():
      existing = self.connection.execute("SELECT * FROM local_attempt WHERE attempt_id=?", (request.attempt_id,)).fetchone()
      if existing is not None:
        if existing["payload_sha256"] != request_hash or existing["canonical_json"] != request_json:
          raise AttemptConflict("attempt ID already identifies a different immutable payload")
        return self._stored_attempt(existing)
      try:
        self.connection.execute(
          "INSERT INTO local_attempt VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
          (request.attempt_id, request.episode_id, request.quote.quote_id, request_hash,
           AttemptState.AUTHORIZED.value, request.dispatch_deadline_unix_ms, request_json),
        )
      except sqlite3.IntegrityError as error:
        existing_attempt = self.connection.execute(
          "SELECT attempt_id FROM local_attempt WHERE episode_id=?", (request.episode_id,),
        ).fetchone()
        if existing_attempt is not None:
          raise AttemptConflict(f"episode already has attempt {existing_attempt['attempt_id']}") from error
        raise JournalError("attempt references an unknown episode") from error
      self._insert_event(request.episode_id, request.attempt_id, mono_ns, wall_ms, "attempt_created", "ATTEMPT_AUTHORIZED",
                         {"payload_sha256": request_hash, "quote_id": request.quote.quote_id})
      row = self.connection.execute("SELECT * FROM local_attempt WHERE attempt_id=?", (request.attempt_id,)).fetchone()
      assert row is not None
      return self._stored_attempt(row)

  def transition_attempt(self, attempt_id: str, state: AttemptState, *, reason_code: str, mono_ns: int,
                         wall_ms: int) -> StoredAttempt:
    state = AttemptState(state)
    with self._transaction():
      row = self.connection.execute("SELECT * FROM local_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      if row is None:
        raise JournalError("attempt does not exist")
      previous = AttemptState(row["state"])
      if state == previous:
        return self._stored_attempt(row)
      if state in (AttemptState.ACTIVE, AttemptState.DECLINED):
        raise InvalidTransition("active and declined outcomes require complete_attempt with a canonical result")
      if state not in _ALLOWED_TRANSITIONS[previous]:
        raise InvalidTransition(f"cannot transition attempt from {previous.value} to {state.value}")
      self.connection.execute("UPDATE local_attempt SET state=? WHERE attempt_id=?", (state.value, attempt_id))
      self._insert_event(row["episode_id"], attempt_id, mono_ns, wall_ms, "attempt_transition", reason_code,
                         {"from": previous.value, "to": state.value})
      updated = self.connection.execute("SELECT * FROM local_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      assert updated is not None
      return self._stored_attempt(updated)

  def complete_attempt(self, attempt_id: str, result: OperationResult, *, mono_ns: int, wall_ms: int) -> StoredAttempt:
    """Atomically store the terminal state and its canonical mock receipt/outcome."""
    if result.attempt_state not in TERMINAL_ATTEMPT_STATES:
      raise ValueError("completion result must be terminal")
    result_json = canonical_json(result.to_dict())
    with self._transaction():
      row = self.connection.execute("SELECT * FROM local_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      if row is None:
        raise JournalError("attempt does not exist")
      previous = AttemptState(row["state"])
      if previous in TERMINAL_ATTEMPT_STATES:
        if previous != result.attempt_state or row["result_json"] != result_json:
          raise AttemptConflict("terminal attempt already has a different canonical result")
        return self._stored_attempt(row)
      if result.attempt_state not in _ALLOWED_TRANSITIONS[previous]:
        raise InvalidTransition(f"cannot transition attempt from {previous.value} to {result.attempt_state.value}")
      self.connection.execute(
        "UPDATE local_attempt SET state=?, result_json=? WHERE attempt_id=?",
        (result.attempt_state.value, result_json, attempt_id),
      )
      self._insert_event(row["episode_id"], attempt_id, mono_ns, wall_ms, "attempt_completed", result.reason_code,
                         {"from": previous.value, "to": result.attempt_state.value})
      updated = self.connection.execute("SELECT * FROM local_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
      assert updated is not None
      return self._stored_attempt(updated)

  def get_attempt(self, attempt_id: str) -> StoredAttempt | None:
    row = self.connection.execute("SELECT * FROM local_attempt WHERE attempt_id=?", (attempt_id,)).fetchone()
    return None if row is None else self._stored_attempt(row)

  def unresolved_attempts(self) -> tuple[StoredAttempt, ...]:
    terminal = tuple(state.value for state in TERMINAL_ATTEMPT_STATES)
    placeholders = ",".join("?" for _ in terminal)
    rows = self.connection.execute(f"SELECT * FROM local_attempt WHERE state NOT IN ({placeholders}) ORDER BY rowid", terminal).fetchall()
    return tuple(self._stored_attempt(row) for row in rows)

  def event_count(self, attempt_id: str | None = None) -> int:
    if attempt_id is None:
      row = self.connection.execute("SELECT COUNT(*) AS count FROM transition_event").fetchone()
    else:
      row = self.connection.execute("SELECT COUNT(*) AS count FROM transition_event WHERE attempt_id=?", (attempt_id,)).fetchone()
    assert row is not None
    return int(row["count"])

  def _insert_event(self, episode_id: str | None, attempt_id: str | None, mono_ns: int, wall_ms: int | None,
                    event_type: str, reason_code: str, redacted: dict[str, object]) -> None:
    self.connection.execute(
      "INSERT INTO transition_event(episode_id, attempt_id, mono_ns, wall_ms, event_type, reason_code, redacted_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
      (episode_id, attempt_id, mono_ns, wall_ms, event_type, reason_code, canonical_json(redacted)),
    )

  @staticmethod
  def _stored_attempt(row: sqlite3.Row) -> StoredAttempt:
    result_value = json.loads(row["result_json"]) if row["result_json"] is not None else None
    return StoredAttempt(
      attempt_id=row["attempt_id"],
      episode_id=row["episode_id"],
      quote_id=row["quote_id"],
      payload_sha256=row["payload_sha256"],
      state=AttemptState(row["state"]),
      dispatch_deadline_unix_ms=row["dispatch_deadline_unix_ms"],
      canonical_request=json.loads(row["canonical_json"]),
      operation_result=None if result_value is None else OperationResult.from_dict(result_value),
    )
