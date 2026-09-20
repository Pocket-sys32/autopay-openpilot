"""The model boundary.

Kept behind a Protocol so the whole loop can be exercised with a scripted stand-in: no network, no
emulator, no spend.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
from typing import Protocol

import requests


# The VM already reaches the metadata server for Secret Manager, so ADC works here with no extra key.
_METADATA_ROOT = "http://metadata.google.internal/computeMetadata/v1"
METADATA_TOKEN_URL = f"{_METADATA_ROOT}/instance/service-accounts/default/token"
METADATA_PROJECT_URL = f"{_METADATA_ROOT}/project/project-id"
METADATA_HEADERS = {"Metadata-Flavor": "Google"}
DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_LOCATION = "us-west1"


class LLMClient(Protocol):
  def propose(self, *, system: str, user: str, screenshot_jpeg: bytes | None) -> str: ...


class LLMUnavailable(RuntimeError):
  """The model could not be reached or did not answer."""


@dataclass(frozen=True, slots=True)
class ModelTelemetry:
  elapsed_ms: int = 0
  prompt_tokens: int = 0
  candidate_tokens: int = 0
  total_tokens: int = 0


class VertexGeminiClient:
  """Gemini through Vertex AI, authenticated as the VM's own service account.

  Deliberately a plain REST call rather than the aiplatform SDK: the metadata-server token flow is the
  same one secret_exec already relies on, and it keeps a large dependency off the worker."""

  def __init__(self, *, project: str = "", location: str = DEFAULT_LOCATION, model: str = DEFAULT_MODEL,
               timeout_seconds: float = 25.0, session=None):
    self.project = project or os.getenv("PARKING_AGENT_PROJECT", "")
    self.location = location
    self.model = model
    self.timeout_seconds = timeout_seconds
    self.session = session or requests.Session()
    self._token = ""
    self._token_expires = 0.0
    self.last_telemetry = ModelTelemetry()

  def _metadata(self, url: str) -> dict | str:
    response = self.session.get(url, headers=METADATA_HEADERS, timeout=5)
    response.raise_for_status()
    return response.json() if url.endswith("/token") else response.text.strip()

  def _access_token(self) -> str:
    import time as _time
    if self._token and _time.monotonic() < self._token_expires:
      return self._token
    try:
      payload = self._metadata(METADATA_TOKEN_URL)
    except requests.RequestException as exc:
      raise LLMUnavailable("could not obtain a service-account token") from exc
    assert isinstance(payload, dict)
    self._token = str(payload["access_token"])
    # Refresh a minute early rather than discovering expiry mid-checkout.
    self._token_expires = _time.monotonic() + max(60, int(payload.get("expires_in", 3600)) - 60)
    return self._token

  def _project_id(self) -> str:
    if not self.project:
      try:
        self.project = str(self._metadata(METADATA_PROJECT_URL))
      except requests.RequestException as exc:
        raise LLMUnavailable("could not determine the GCP project") from exc
    return self.project

  def propose(self, *, system: str, user: str, screenshot_jpeg: bytes | None) -> str:
    import time as _time
    started = _time.monotonic_ns()
    project, token = self._project_id(), self._access_token()
    host = f"https://{self.location}-aiplatform.googleapis.com/v1"
    endpoint = f"{host}/projects/{project}/locations/{self.location}/publishers/google/models/{self.model}:generateContent"
    parts: list[dict[str, object]] = [{"text": user}]
    if screenshot_jpeg:
      parts.append({"inlineData": {"mimeType": "image/png",
                                   "data": base64.b64encode(screenshot_jpeg).decode()}})
    body = {
      "systemInstruction": {"parts": [{"text": system}]},
      "contents": [{"role": "user", "parts": parts}],
      # One action per turn, as JSON. Temperature 0 so the same screen gives the same move.
      "generationConfig": {"temperature": 0, "maxOutputTokens": 512, "responseMimeType": "application/json"},
    }
    try:
      response = self.session.post(endpoint, json=body, timeout=self.timeout_seconds,
                                   headers={"Authorization": f"Bearer {token}"})
      response.raise_for_status()
      payload = response.json()
    except (requests.RequestException, ValueError) as exc:
      raise LLMUnavailable(f"vertex request failed: {type(exc).__name__}") from exc
    usage = payload.get("usageMetadata") or {}
    self.last_telemetry = ModelTelemetry(
      elapsed_ms=(_time.monotonic_ns() - started) // 1_000_000,
      prompt_tokens=int(usage.get("promptTokenCount") or 0),
      candidate_tokens=int(usage.get("candidatesTokenCount") or 0),
      total_tokens=int(usage.get("totalTokenCount") or 0),
    )
    try:
      return payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, TypeError) as exc:
      blocked = json.dumps(payload)[:200]
      raise LLMUnavailable(f"vertex returned no usable candidate: {blocked}") from exc


class ScriptedLLM:
  """Replays a fixed list of responses. Anything the loop asks beyond the script is a test bug, not a
  silent pass, so it raises."""

  def __init__(self, responses: list[str | dict[str, object]]):
    self.responses = list(responses)
    self.prompts: list[tuple[str, str]] = []
    self.screenshots: list[bytes | None] = []
    self.last_telemetry = ModelTelemetry()

  def propose(self, *, system: str, user: str, screenshot_jpeg: bytes | None) -> str:
    self.prompts.append((system, user))
    self.screenshots.append(screenshot_jpeg)
    if not self.responses:
      raise AssertionError("the agent asked for more actions than the script provides")
    response = self.responses.pop(0)
    return response if isinstance(response, str) else json.dumps(response)
