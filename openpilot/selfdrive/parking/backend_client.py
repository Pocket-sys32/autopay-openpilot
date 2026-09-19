from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from typing import Any
from urllib.parse import urlsplit

import requests


MAX_RESPONSE_BYTES = 64 * 1024


class BackendClientError(RuntimeError):
  pass


@dataclass(frozen=True, slots=True)
class BackendAttemptResponse:
  status_code: int
  body: dict[str, Any]


def _validate_base_url(base_url: str, environment: str) -> str:
  parsed = urlsplit(base_url)
  if parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise ValueError("backend URL may not contain credentials, query, or fragment")
  if not parsed.hostname or parsed.path not in ("", "/"):
    raise ValueError("backend URL must contain only a scheme and host")
  if parsed.scheme == "https":
    return base_url.rstrip("/")
  if parsed.scheme != "http" or environment != "demo":
    raise ValueError("non-demo backends require HTTPS")

  hostname = parsed.hostname.lower()
  try:
    loopback = ipaddress.ip_address(hostname).is_loopback
  except ValueError:
    loopback = hostname == "localhost"
  if not loopback:
    raise ValueError("HTTP is allowed only for a loopback demo backend")
  return base_url.rstrip("/")


class ParkingBackendClient:
  """Typed client for the parking backend; contains no provider-specific logic."""

  def __init__(self, base_url: str, environment: str, *, session: Any | None = None,
               auth_header: str | None = None, timeout_seconds: float = 5.0,
               ca_path: str | None = None):
    self.base_url = _validate_base_url(base_url, environment)
    self.environment = environment
    self.session = session or requests.Session()
    self.auth_header = auth_header
    self.timeout_seconds = timeout_seconds
    self.ca_path = ca_path or True

  def put_attempt(self, attempt_id: str, payload: dict[str, Any]) -> BackendAttemptResponse:
    if payload.get("attempt_id") != attempt_id:
      raise ValueError("attempt ID must match the immutable payload")
    if payload.get("environment") != self.environment:
      raise ValueError("payload environment does not match the client")
    return self._request("PUT", f"/v1/attempts/{attempt_id}", json=payload)

  def get_attempt(self, attempt_id: str) -> BackendAttemptResponse:
    return self._request("GET", f"/v1/attempts/{attempt_id}")

  def get_events(self, after_sequence: int) -> BackendAttemptResponse:
    if after_sequence < 0:
      raise ValueError("event sequence must be nonnegative")
    return self._request("GET", f"/v1/events?after={after_sequence}", request_timeout=max(35.0, self.timeout_seconds))

  def _request(self, method: str, path: str, **kwargs) -> BackendAttemptResponse:
    headers = {"Accept": "application/json", "X-Parking-Environment": self.environment}
    if self.auth_header:
      headers["Authorization"] = self.auth_header
    try:
      request_timeout = kwargs.pop("request_timeout", self.timeout_seconds)
      response = self.session.request(
        method,
        f"{self.base_url}{path}",
        headers=headers,
        timeout=request_timeout,
        verify=self.ca_path,
        **kwargs,
      )
    except requests.RequestException as exc:
      raise BackendClientError("parking backend request failed") from exc

    if len(response.content) > MAX_RESPONSE_BYTES:
      raise BackendClientError("parking backend response exceeded size limit")
    try:
      body = response.json()
    except ValueError as exc:
      raise BackendClientError("parking backend returned invalid JSON") from exc
    if not isinstance(body, dict):
      raise BackendClientError("parking backend response must be a JSON object")
    response_attempt_id = body.get("attempt_id")
    if response_attempt_id is not None and response_attempt_id != path.rsplit("/", 1)[-1]:
      raise BackendClientError("parking backend returned a different attempt ID")
    return BackendAttemptResponse(response.status_code, body)
