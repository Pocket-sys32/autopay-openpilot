"""The Vertex client, against a fake HTTP session. No network, no spend."""
import base64
import json
import unittest

from parking_backend.agent.llm import LLMUnavailable, VertexGeminiClient


class FakeResponse:
  def __init__(self, payload, status=200, text=""):
    self._payload = payload
    self.status_code = status
    self.text = text

  def json(self):
    if self._payload is None:
      raise ValueError("not json")
    return self._payload

  def raise_for_status(self):
    if self.status_code >= 400:
      import requests
      raise requests.HTTPError(f"status {self.status_code}")


class FakeSession:
  def __init__(self, *, answer="{}", post_response=None):
    self.answer = answer
    self.posts = []
    self.post_response = post_response

  def get(self, url, headers=None, timeout=None):
    if url.endswith("/token"):
      return FakeResponse({"access_token": "tok", "expires_in": 3600})
    return FakeResponse(None, text="fieldscout-497018")

  def post(self, url, json=None, timeout=None, headers=None):
    self.posts.append({"url": url, "body": json, "headers": headers})
    if self.post_response is not None:
      return self.post_response
    return FakeResponse({"candidates": [{"content": {"parts": [{"text": self.answer}]}}]})


class TestVertexClient(unittest.TestCase):
  def client(self, session) -> VertexGeminiClient:
    return VertexGeminiClient(project="fieldscout-497018", session=session)

  def test_an_action_comes_back_from_the_model(self):
    session = FakeSession(answer='{"action": "TAP", "nid": "n1"}')
    reply = self.client(session).propose(system="sys", user="obs", screenshot_jpeg=None)
    self.assertEqual(json.loads(reply)["action"], "TAP")

  def test_the_request_is_addressed_and_authenticated(self):
    session = FakeSession()
    self.client(session).propose(system="sys", user="obs", screenshot_jpeg=None)
    post = session.posts[0]
    self.assertIn("fieldscout-497018", post["url"])
    self.assertTrue(post["url"].endswith(":generateContent"))
    self.assertEqual(post["headers"]["Authorization"], "Bearer tok")
    self.assertEqual(post["body"]["systemInstruction"]["parts"][0]["text"], "sys")

  def test_the_model_is_asked_for_one_deterministic_json_action(self):
    session = FakeSession()
    self.client(session).propose(system="sys", user="obs", screenshot_jpeg=None)
    config = session.posts[0]["body"]["generationConfig"]
    self.assertEqual(config["temperature"], 0)
    self.assertEqual(config["responseMimeType"], "application/json")

  def test_a_screenshot_is_sent_inline_when_there_is_one(self):
    session = FakeSession()
    self.client(session).propose(system="sys", user="obs", screenshot_jpeg=b"pixels")
    parts = session.posts[0]["body"]["contents"][0]["parts"]
    self.assertEqual(base64.b64decode(parts[1]["inlineData"]["data"]), b"pixels")

  def test_no_screenshot_means_text_only(self):
    session = FakeSession()
    self.client(session).propose(system="sys", user="obs", screenshot_jpeg=None)
    self.assertEqual(len(session.posts[0]["body"]["contents"][0]["parts"]), 1)

  def test_the_token_is_reused_rather_than_refetched(self):
    session = FakeSession()
    client = self.client(session)
    client.propose(system="s", user="u", screenshot_jpeg=None)
    first = client._token_expires
    client.propose(system="s", user="u", screenshot_jpeg=None)
    self.assertEqual(client._token_expires, first)

  def test_a_blocked_or_empty_response_is_reported_not_guessed(self):
    session = FakeSession(post_response=FakeResponse({"promptFeedback": {"blockReason": "SAFETY"}}))
    with self.assertRaises(LLMUnavailable):
      self.client(session).propose(system="s", user="u", screenshot_jpeg=None)

  def test_an_http_failure_is_reported(self):
    session = FakeSession(post_response=FakeResponse({}, status=503))
    with self.assertRaises(LLMUnavailable):
      self.client(session).propose(system="s", user="u", screenshot_jpeg=None)


if __name__ == "__main__":
  unittest.main()
