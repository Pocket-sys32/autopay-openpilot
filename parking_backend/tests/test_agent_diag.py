import json
import os
from pathlib import Path
import tempfile
import unittest

from parking_backend.agent.diag import DiagnosticWriter
from parking_backend.agent.secrets import SecretVault
from parking_backend.agent.types import StepLog


class TestAgentDiagnostics(unittest.TestCase):
  def setUp(self):
    self.directory = tempfile.TemporaryDirectory()
    self.path = Path(self.directory.name)

  def tearDown(self):
    self.directory.cleanup()

  def test_writes_bounded_metadata_without_secrets(self):
    vault = SecretVault(card_number="4242424242424242")
    writer = DiagnosticWriter(self.path, vault=vault)
    steps = [StepLog(1, "navigating", "TAP(n1)", model_ms=125, prompt_tokens=100,
                     candidate_tokens=8, total_tokens=108, screenshot_sent=True)]
    writer.write("attempt/unsafe", "checkout_ready", steps, outcome="4242424242424242")
    files = list(self.path.glob("*.json"))
    self.assertEqual(len(files), 1)
    self.assertEqual(os.stat(files[0]).st_mode & 0o777, 0o600)
    raw = files[0].read_text()
    self.assertNotIn("4242424242424242", raw)
    payload = json.loads(raw)
    self.assertEqual(payload["steps"][0]["total_tokens"], 108)
    self.assertEqual(payload["attempt_id"], "attempt_unsafe")

  def test_retention_prunes_the_oldest_file(self):
    writer = DiagnosticWriter(self.path, max_files=2)
    for index in range(3):
      writer.write(f"attempt-{index}", "done", [])
    files = list(self.path.glob("*.json"))
    self.assertEqual(len(files), 2)
    self.assertFalse(any("attempt-0" in path.name for path in files))

  def test_a_diagnostic_failure_never_escapes(self):
    blocked = self.path / "not-a-directory"
    blocked.write_text("file")
    DiagnosticWriter(blocked).write("attempt", "failed", [])


if __name__ == "__main__":
  unittest.main()
