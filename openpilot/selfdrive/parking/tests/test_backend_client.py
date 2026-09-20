import json

import requests

from openpilot.common.test import OpenpilotTestCase
from openpilot.selfdrive.parking.backend_client import BackendClientError, ParkingBackendClient


class FakeResponse:
  def __init__(self, body, status_code=200):
    self.content = json.dumps(body).encode()
    self.status_code = status_code

  def json(self):
    return json.loads(self.content)


class FakeSession:
  def __init__(self, response=None, error=None):
    self.response = response
    self.error = error
    self.calls = []

  def request(self, method, url, **kwargs):
    self.calls.append((method, url, kwargs))
    if self.error:
      raise self.error
    return self.response


class TestBackendClient(OpenpilotTestCase):
  def test_rejects_insecure_non_loopback_backend(self):
    with self.assertRaises(ValueError):
      ParkingBackendClient("http://parking.example", "demo")
    with self.assertRaises(ValueError):
      ParkingBackendClient("http://127.0.0.1:8080", "live")

  def test_put_is_bound_to_attempt_and_environment(self):
    session = FakeSession(FakeResponse({"attempt_id": "a", "state": "accepted"}, 202))
    client = ParkingBackendClient("http://127.0.0.1:8080", "demo", session=session)
    response = client.put_attempt("a", {"attempt_id": "a", "environment": "demo"})
    self.assertEqual(response.status_code, 202)
    self.assertEqual(session.calls[0][0], "PUT")
    self.assertEqual(session.calls[0][2]["headers"]["X-Parking-Environment"], "demo")

  def test_rejects_changed_attempt_identity(self):
    client = ParkingBackendClient("https://parking.example", "live", session=FakeSession())
    with self.assertRaises(ValueError):
      client.put_attempt("a", {"attempt_id": "b", "environment": "live"})

  def test_wraps_network_error_without_secrets(self):
    session = FakeSession(error=requests.ConnectionError("private endpoint details"))
    client = ParkingBackendClient("https://parking.example", "live", session=session)
    with self.assertRaisesRegex(BackendClientError, "request failed"):
      client.get_attempt("a")

  def test_events_use_long_poll_timeout(self):
    session = FakeSession(FakeResponse({"events": [], "next_sequence": 3}))
    client = ParkingBackendClient("http://127.0.0.1:8080", "demo", session=session, timeout_seconds=5)
    response = client.get_events(3)
    self.assertEqual(response.body["next_sequence"], 3)
    self.assertIn("after=3", session.calls[0][1])
    self.assertEqual(session.calls[0][2]["timeout"], 35.0)

  def test_snapshot_upload_is_raw_authenticated_jpeg(self):
    session = FakeSession(FakeResponse({"payloads": ["comma:park:demo"], "retained": False}))
    client = ParkingBackendClient("http://127.0.0.1:8080", "demo", session=session, auth_header="Bearer token")
    response = client.decode_snapshot(b"jpeg", "narrow")
    self.assertEqual(response.body["payloads"], ["comma:park:demo"])
    _method, _url, kwargs = session.calls[0]
    self.assertEqual(kwargs["data"], b"jpeg")
    self.assertEqual(kwargs["headers"]["Content-Type"], "image/jpeg")
    self.assertEqual(kwargs["headers"]["X-Parking-Camera"], "narrow")
    self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token")

  def test_snapshot_size_and_stream_are_bounded(self):
    client = ParkingBackendClient("http://127.0.0.1:8080", "demo", session=FakeSession())
    with self.assertRaises(ValueError):
      client.decode_snapshot(b"", "narrow")
    with self.assertRaises(ValueError):
      client.decode_snapshot(b"jpeg", "driver")
