from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from io import BytesIO
import time
from typing import Protocol

import numpy as np

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common import qrcode
from openpilot.selfdrive.parking.backend_client import BackendAttemptResponse


QRDecoder = Callable[[np.ndarray], str | None]


@dataclass(frozen=True, slots=True)
class QRObservation:
  payload: str
  crop_id: str
  observed_mono_ns: int


@dataclass(frozen=True, slots=True)
class QRScan:
  observations: tuple[QRObservation, ...]

  @property
  def payloads(self) -> tuple[str, ...]:
    return tuple(sorted({observation.payload for observation in self.observations}))

  @property
  def ambiguous(self) -> bool:
    return len(self.payloads) > 1


def crop_schedule(gray: np.ndarray) -> tuple[tuple[str, np.ndarray], ...]:
  """Return a small deterministic crop set biased toward roadside signs."""
  if gray.dtype != np.uint8 or gray.ndim != 2:
    raise ValueError("QR input must be a two-dimensional uint8 grayscale image")
  height, width = gray.shape
  if height < 21 or width < 21:
    return ()

  x_overlap = max(int(width * 0.65), 21)
  y_upper = max(int(height * 0.75), 21)
  x0, x1 = max(int(width * 0.18), 0), max(int(width * 0.82), 21)
  y0, y1 = max(int(height * 0.12), 0), max(int(height * 0.88), 21)
  center = gray[y0:y1, x0:x1]
  half = gray[::2, ::2]
  crops = [
    ("full", gray),
    ("center", center if center.shape[0] >= 21 and center.shape[1] >= 21 else gray),
    ("left", gray[:, :x_overlap]),
    ("right", gray[:, width - x_overlap:]),
    ("upper", gray[:y_upper, :]),
  ]
  if half.shape[0] >= 21 and half.shape[1] >= 21:
    crops.append(("half", half))
  return tuple(crops)


def scan_gray(gray: np.ndarray, *, decoder: QRDecoder = qrcode.decode,
              observed_mono_ns: int | None = None) -> QRScan:
  observed_mono_ns = time.monotonic_ns() if observed_mono_ns is None else observed_mono_ns
  observations: list[QRObservation] = []
  seen: set[str] = set()
  for crop_id, crop in crop_schedule(gray):
    payload = decoder(crop)
    if payload is None or payload in seen:
      continue
    seen.add(payload)
    observations.append(QRObservation(payload, crop_id, observed_mono_ns))
  return QRScan(tuple(observations))


class CandidateConsensus:
  """Promote a repeated payload with a strict majority of recent scans."""

  def __init__(self, minimum_observations: int = 2, window_seconds: float = 3.0):
    if minimum_observations < 2:
      raise ValueError("minimum_observations must be at least two")
    if window_seconds <= 0:
      raise ValueError("window_seconds must be positive")
    self.minimum_observations = minimum_observations
    self.window_ns = int(window_seconds * 1e9)
    self._history: deque[tuple[int, tuple[str, ...]]] = deque()

  def observe(self, scan: QRScan, now_mono_ns: int | None = None) -> str | None:
    now_mono_ns = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
    while self._history and now_mono_ns - self._history[0][0] > self.window_ns:
      self._history.popleft()

    payloads = scan.payloads
    if payloads:
      self._history.append((now_mono_ns, payloads))
    if len(payloads) != 1:
      return None

    payload = payloads[0]
    count = sum(previous == (payload,) for _, previous in self._history)
    # Ambiguous scans count against the majority without blocking the window.
    return payload if count >= self.minimum_observations and count * 2 > len(self._history) else None

  def clear(self) -> None:
    self._history.clear()


class VisionQRScanner:
  """Capture snapshots locally and ask the authenticated backend to decode them."""

  # parkingd runs at 2 Hz in production, so this permits one snapshot per tick.
  SCAN_INTERVAL_NS = int(0.45e9)
  MAX_JPEG_BYTES = 768 * 1024

  def __init__(self, *, backend_provider: Callable[[], SnapshotBackend | None], prefer_wide: bool = False):
    self._clients: dict = {}
    self._last_scan_mono_ns = 0
    self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="parking-snapshot")
    self._future: Future[BackendAttemptResponse] | None = None
    self._future_mono_ns = 0
    self._backend_provider = backend_provider
    self._preferred_streams = (
      VisionStreamType.VISION_STREAM_WIDE_ROAD if prefer_wide else VisionStreamType.VISION_STREAM_NARROW_ROAD,
    )

  def _connect(self) -> bool:
    from msgq.visionipc import VisionIpcClient

    available = VisionIpcClient.available_streams("camerad", block=False)
    connected = False
    for stream_type in self._preferred_streams:
      if stream_type not in available:
        continue
      client = self._clients.get(stream_type)
      if client is not None and client.is_connected():
        connected = True
        continue
      client = VisionIpcClient("camerad", stream_type, conflate=True)
      if client.connect(False):
        self._clients[stream_type] = client
        connected = True
    return connected

  @staticmethod
  def _encode(gray: np.ndarray) -> bytes:
    from PIL import Image

    output = BytesIO()
    Image.fromarray(gray, mode="L").save(output, format="JPEG", quality=72, optimize=False)
    jpeg = output.getvalue()
    if len(jpeg) > VisionQRScanner.MAX_JPEG_BYTES:
      raise ValueError("encoded parking snapshot exceeded size limit")
    return jpeg

  def _consume(self) -> QRScan | None:
    if self._future is None or not self._future.done():
      return None
    future, observed_ns = self._future, self._future_mono_ns
    self._future = None
    try:
      response = future.result()
    except Exception:
      return QRScan(())
    if response.status_code != 200:
      return QRScan(())
    raw_payloads = response.body.get("payloads", [])
    if not isinstance(raw_payloads, list):
      return QRScan(())
    payloads = tuple(value for value in raw_payloads if isinstance(value, str) and len(value) <= 2048)
    return QRScan(tuple(QRObservation(value, "vm", observed_ns) for value in sorted(set(payloads))))

  def poll(self, now_mono_ns: int | None = None) -> QRScan | None:
    now_mono_ns = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
    completed = self._consume()
    if self._future is not None or now_mono_ns - self._last_scan_mono_ns < self.SCAN_INTERVAL_NS:
      return completed
    backend = self._backend_provider()
    if backend is None or not self._connect():
      return completed
    for stream_type in self._preferred_streams:
      client = self._clients.get(stream_type)
      if client is None:
        continue
      frame = client.recv(timeout_ms=0)
      if frame is None:
        continue
      y = np.frombuffer(frame.data, dtype=np.uint8, count=frame.height * frame.stride).reshape(frame.height, frame.stride)
      jpeg = self._encode(y[:, :frame.width])
      stream_id = "wide" if stream_type == VisionStreamType.VISION_STREAM_WIDE_ROAD else "narrow"
      self._future = self._executor.submit(backend.decode_snapshot, jpeg, stream_id)
      self._future_mono_ns = now_mono_ns
      self._last_scan_mono_ns = now_mono_ns
      break
    return completed


class SnapshotBackend(Protocol):
  def decode_snapshot(self, jpeg: bytes, stream_id: str) -> BackendAttemptResponse: ...
