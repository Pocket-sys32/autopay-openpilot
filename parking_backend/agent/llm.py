"""The model boundary.

Kept behind a Protocol so the whole loop can be exercised with a scripted stand-in: no network, no
emulator, no spend.
"""
from __future__ import annotations

from typing import Protocol


class LLMClient(Protocol):
  def propose(self, *, system: str, user: str, screenshot_jpeg: bytes | None) -> str: ...


class ScriptedLLM:
  """Replays a fixed list of responses. Anything the loop asks beyond the script is a test bug, not a
  silent pass, so it raises."""

  def __init__(self, responses: list[str | dict[str, object]]):
    self.responses = list(responses)
    self.prompts: list[tuple[str, str]] = []
    self.screenshots: list[bytes | None] = []

  def propose(self, *, system: str, user: str, screenshot_jpeg: bytes | None) -> str:
    import json
    self.prompts.append((system, user))
    self.screenshots.append(screenshot_jpeg)
    if not self.responses:
      raise AssertionError("the agent asked for more actions than the script provides")
    response = self.responses.pop(0)
    return response if isinstance(response, str) else json.dumps(response)
