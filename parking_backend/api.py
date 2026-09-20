from __future__ import annotations

import asyncio
import hmac

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from starlette.concurrency import run_in_threadpool

from parking_backend.config import Settings
from parking_backend.qr_decode import InvalidSnapshot, decode_jpeg
from parking_backend.store import AttemptConflict, InvalidAttempt, ParkingStore


settings = Settings.from_environment()
store = ParkingStore(settings.database_path)
app = FastAPI(title="Comma parking demo", docs_url=None, redoc_url=None, openapi_url=None)
MAX_SNAPSHOT_BYTES = 768 * 1024


def authenticate(authorization: str | None = Header(default=None)) -> str:
  expected = f"Bearer {settings.bearer_token}"
  if authorization is None or not hmac.compare_digest(authorization, expected):
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid bearer token")
  return settings.device_id


@app.get("/healthz")
def healthz() -> dict[str, object]:
  return {"ok": True, "environment": "demo"}


@app.post("/v1/qr/decode")
async def decode_qr(request: Request, device_id: str = Depends(authenticate),
                    content_type: str | None = Header(default=None),
                    x_parking_camera: str | None = Header(default=None)) -> dict[str, object]:
  del device_id
  if content_type != "image/jpeg" or x_parking_camera not in ("narrow", "wide"):
    raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "expected an identified JPEG road-camera snapshot")
  content_length = request.headers.get("content-length")
  if content_length is None or not content_length.isdigit() or not 0 < int(content_length) <= MAX_SNAPSHOT_BYTES:
    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "snapshot size is invalid")
  jpeg = await request.body()
  if len(jpeg) != int(content_length) or len(jpeg) > MAX_SNAPSHOT_BYTES:
    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "snapshot size is invalid")
  try:
    payloads, processing_ms = await run_in_threadpool(decode_jpeg, jpeg)
  except InvalidSnapshot as exc:
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
  return {"payloads": payloads, "camera": x_parking_camera, "processing_ms": processing_ms, "retained": False}


@app.put("/v1/attempts/{attempt_id}", status_code=status.HTTP_202_ACCEPTED)
def put_attempt(attempt_id: str, payload: dict[str, object], device_id: str = Depends(authenticate)) -> dict[str, object]:
  if payload.get("attempt_id") != attempt_id:
    raise HTTPException(status.HTTP_400_BAD_REQUEST, "attempt ID does not match path")
  try:
    result, created = store.put_attempt(device_id, payload)
  except InvalidAttempt as exc:
    raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
  except AttemptConflict as exc:
    raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
  result["created"] = created
  return result


@app.get("/v1/attempts/{attempt_id}")
def get_attempt(attempt_id: str, device_id: str = Depends(authenticate)) -> dict[str, object]:
  result = store.get_attempt(device_id, attempt_id)
  if result is None:
    raise HTTPException(status.HTTP_404_NOT_FOUND, "attempt not found")
  return result


@app.get("/v1/events")
async def get_events(after: int = Query(default=0, ge=0), device_id: str = Depends(authenticate)) -> dict[str, object]:
  for _ in range(25):
    events = store.events_after(device_id, after)
    if events:
      return {"events": events, "next_sequence": events[-1]["sequence"]}
    await asyncio.sleep(1)
  return {"events": [], "next_sequence": after}
