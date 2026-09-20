"""Poll one attempt through the backend's authenticated API until it reaches a terminal state."""
import json
import ssl
import sys
import time
import urllib.error
import urllib.request

BASE = "https://136.69.249.114"
CA = "/data/parking/parking-ca.crt"
TOKEN_PATH = "/data/parking/device-token"
ATTEMPT = sys.argv[1]
TERMINAL = {"succeeded", "failed", "action_required", "unknown", "expired"}

token = open(TOKEN_PATH).read().strip()
context = ssl.create_default_context(cafile=CA)
last = None
for _ in range(80):
    request = urllib.request.Request(
        f"{BASE}/v1/attempts/{ATTEMPT}", headers={"Authorization": f"Bearer {token}"})
    try:
        body = json.loads(urllib.request.urlopen(request, context=context, timeout=20).read().decode())
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}")
        time.sleep(3)
        continue
    state = body.get("state")
    if state != last:
        print(f"{time.strftime('%H:%M:%S')} state={state} reason={body.get('reason_code')}")
        last = state
    if state in TERMINAL:
        print(json.dumps(body.get("result"), indent=2))
        print(f"email_status={body.get('email_status')}")
        break
    time.sleep(3)
else:
    print("still not terminal after the polling window")
