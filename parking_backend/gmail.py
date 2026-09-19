from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import cast

import requests


class EmailDeliveryUnknown(RuntimeError):
  pass


class GmailSender:
  def __init__(self, *, sender: str, recipient: str, client_id: str, client_secret: str, refresh_token: str):
    self.sender = sender
    self.recipient = recipient
    self.client_id = client_id
    self.client_secret = client_secret
    self.refresh_token = refresh_token

  @property
  def configured(self) -> bool:
    return all((self.sender, self.recipient, self.client_id, self.client_secret, self.refresh_token))

  def send_result(self, attempt: dict[str, object]) -> None:
    if not self.configured:
      raise RuntimeError("Gmail OAuth is not configured")
    token_response = requests.post(
      "https://oauth2.googleapis.com/token",
      data={
        "client_id": self.client_id,
        "client_secret": self.client_secret,
        "refresh_token": self.refresh_token,
        "grant_type": "refresh_token",
      },
      timeout=15,
    )
    token_response.raise_for_status()
    access_token = token_response.json()["access_token"]

    request = cast(dict[str, object], attempt["request"])
    duration_value = request["duration_seconds"]
    if not isinstance(duration_value, int) or isinstance(duration_value, bool):
      raise ValueError("stored duration is invalid")
    masked_plate = "*" * max(0, len(str(request["plate"])) - 2) + str(request["plate"])[-2:]
    message = EmailMessage()
    message["To"] = self.recipient
    message["From"] = self.sender
    message["Subject"] = f"Parking demo: {attempt['state']}"
    message.set_content("\n".join([
      "Comma parking demo result",
      "",
      "This was a controlled demo. No parking was purchased.",
      f"Outcome: {attempt['state']}",
      f"Reason: {attempt['reason_code']}",
      f"Plate: {masked_plate}",
      f"Duration: {duration_value // 3600} hour(s)",
      f"Attempt: {attempt['attempt_id']}",
      "",
    ]))
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip("=")
    try:
      response = requests.post(
        "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json={"raw": raw},
        timeout=15,
      )
    except requests.Timeout as exc:
      raise EmailDeliveryUnknown("Gmail send timed out after dispatch") from exc
    response.raise_for_status()
