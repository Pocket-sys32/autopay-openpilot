"""Submit one LAZ attempt through the backend's real authenticated API, run from the comma.

The token is read on the device and never printed. The comma's own daemon cannot request this provider
(its candidate parser is hardcoded to the demo Google Form), so the attempt is built here instead.
"""
import datetime
import json
import ssl
import urllib.error
import urllib.request
import uuid

BASE = "https://136.69.249.114"
CA = "/data/parking/parking-ca.crt"
TOKEN_PATH = "/data/parking/device-token"

token = open(TOKEN_PATH).read().strip()
now = int(datetime.datetime.now(datetime.UTC).timestamp() * 1000)
attempt_id = str(uuid.uuid4())
payload = {
    "schema_version": 1,
    "environment": "demo",
    "attempt_id": attempt_id,
    "episode_id": str(uuid.uuid4()),
    "provider_id": "laz_ttp",
    "form_id": "143245",
    "qr_payload_sha256": "b" * 64,
    "plate": "AQUAM4",
    "plate_country": "US",
    "plate_region": "CA",
    "duration_seconds": 10800,
    "evidence_age_ms": 100,
    "dispatch_deadline_unix_ms": now + 55_000,
    "payer_first_name": "Manraj",
    "payer_last_name": "Thandi",
    "name_on_card": "Manraj Thandi",
}

context = ssl.create_default_context(cafile=CA)
request = urllib.request.Request(
    f"{BASE}/v1/attempts/{attempt_id}",
    data=json.dumps(payload).encode(),
    method="PUT",
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
)
try:
    response = urllib.request.urlopen(request, context=context, timeout=30)
    print(response.status, response.read().decode())
except urllib.error.HTTPError as exc:
    print("HTTP", exc.code, exc.read().decode())
except Exception as exc:
    print(f"{type(exc).__name__}: {exc}")
print("attempt_id", attempt_id)
