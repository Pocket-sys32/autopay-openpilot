from __future__ import annotations

import argparse
import json
import secrets
from urllib.parse import urlencode

import requests


AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/gmail.send"


def main() -> None:
  parser = argparse.ArgumentParser(description="Exchange a Gmail OAuth authorization code for an offline refresh token")
  parser.add_argument("--client-json", required=True, help="OAuth desktop client JSON downloaded from Google Cloud")
  args = parser.parse_args()
  data = json.loads(open(args.client_json, encoding="utf-8").read())
  client = data.get("installed") or data.get("web")
  if not isinstance(client, dict):
    raise RuntimeError("OAuth JSON has no installed or web client")
  redirect_uri = "http://localhost:8765"
  state = secrets.token_urlsafe(24)
  query = urlencode({
    "client_id": client["client_id"],
    "redirect_uri": redirect_uri,
    "response_type": "code",
    "scope": SCOPE,
    "access_type": "offline",
    "prompt": "consent",
    "state": state,
  })
  print(f"Open this URL and authorize pocketsfast@gmail.com:\n\n{AUTH_ENDPOINT}?{query}\n")
  print("After Google redirects to localhost, copy the code query parameter from the browser address bar.")
  code = input("Authorization code: ").strip()
  response = requests.post(TOKEN_ENDPOINT, data={
    "client_id": client["client_id"],
    "client_secret": client["client_secret"],
    "code": code,
    "grant_type": "authorization_code",
    "redirect_uri": redirect_uri,
  }, timeout=30)
  response.raise_for_status()
  refresh_token = response.json().get("refresh_token")
  if not refresh_token:
    raise RuntimeError("Google did not return a refresh token; revoke the prior grant and retry with prompt=consent")
  print("\nStore this value as PARKING_GMAIL_REFRESH_TOKEN in Secret Manager or backend.env:")
  print(refresh_token)


if __name__ == "__main__":
  main()
