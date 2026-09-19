from __future__ import annotations

import base64
import json
import os
import sys

import requests


def main() -> None:
  if len(sys.argv) < 2:
    raise SystemExit("usage: python -m parking_backend.secret_exec COMMAND [ARGS...]")
  secret_name = os.environ.get("PARKING_SECRET_NAME", "")
  project_id = os.environ.get("GOOGLE_CLOUD_PROJECT", "")
  if secret_name:
    if not project_id:
      project_response = requests.get(
        "http://metadata.google.internal/computeMetadata/v1/project/project-id",
        headers={"Metadata-Flavor": "Google"}, timeout=5,
      )
      project_response.raise_for_status()
      project_id = project_response.text
    token_response = requests.get(
      "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
      headers={"Metadata-Flavor": "Google"}, timeout=5,
    )
    token_response.raise_for_status()
    token = token_response.json()["access_token"]
    secret_response = requests.get(
      f"https://secretmanager.googleapis.com/v1/projects/{project_id}/secrets/{secret_name}/versions/latest:access",
      headers={"Authorization": f"Bearer {token}"}, timeout=10,
    )
    secret_response.raise_for_status()
    decoded = base64.b64decode(secret_response.json()["payload"]["data"])
    secrets = json.loads(decoded)
    if not isinstance(secrets, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in secrets.items()):
      raise RuntimeError("Secret Manager payload must be a JSON object of string environment values")
    os.environ.update(secrets)
  os.execvpe(sys.argv[1], sys.argv[1:], os.environ)


if __name__ == "__main__":
  main()
