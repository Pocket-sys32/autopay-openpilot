"""Bounded, redacted diagnostics for agent dry runs and failures."""
from __future__ import annotations

from dataclasses import asdict
import json
import os
from pathlib import Path
import re
import time

from parking_backend.agent.secrets import SecretVault, redact
from parking_backend.agent.types import StepLog


MAX_FILES = 50
MAX_STEPS = 40
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]")


class DiagnosticWriter:
  """Writes metadata only: no DOM text, prompts, model replies, screenshots, or profile values."""

  def __init__(self, directory: Path | str, *, vault: SecretVault | None = None, max_files: int = MAX_FILES):
    self.directory = Path(directory)
    self.vault = vault
    self.max_files = max(1, max_files)

  def write(self, attempt_id: str, stage: str, transcript: list[StepLog], *, outcome: str = "") -> None:
    try:
      self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
      safe_id = _SAFE_ID.sub("_", attempt_id)[:80] or "attempt"
      timestamp = time.time_ns() // 1_000_000
      target = self.directory / f"{timestamp}-{safe_id}-{_SAFE_ID.sub('_', stage)[:32]}.json"
      payload = {
        "schema_version": 1,
        "attempt_id": safe_id,
        "stage": stage[:32],
        "outcome": outcome[:96],
        "written_unix_ms": timestamp,
        "steps": [asdict(step) for step in transcript[-MAX_STEPS:]],
      }
      encoded = json.dumps(self._scrub(payload), sort_keys=True, separators=(",", ":")).encode()
      descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
      with os.fdopen(descriptor, "wb") as output:
        output.write(encoded)
      self._prune()
    except Exception:
      pass  # diagnostics must never alter the parking outcome

  def _scrub(self, value):
    """Redact string leaves without touching numeric JSON syntax such as timestamps."""
    if isinstance(value, str):
      return redact(value, self.vault)
    if isinstance(value, list):
      return [self._scrub(item) for item in value]
    if isinstance(value, dict):
      return {key: self._scrub(item) for key, item in value.items()}
    return value

  def _prune(self) -> None:
    files = sorted(self.directory.glob("*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    for stale in files[self.max_files:]:
      try:
        stale.unlink()
      except OSError:
        pass
