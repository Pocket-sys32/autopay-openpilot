from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
import time

import numpy as np

from openpilot.cereal.visionipc import VisionStreamType
from openpilot.common import qrcode


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
  return (
    ("full", gray),
    ("left", gray[:, :x_overlap]),
    ("right", gray[:, width - x_overlap:]),
    ("upper", gray[:y_upper, :]),
  )


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
  """Promote a payload only after repeated, recent, non-conflicting observations."""

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
    if any(len(previous) != 1 or previous[0] != payload for _, previous in self._history):
      return None
    count = sum(previous == (payload,) for _, previous in self._history)
    return payload if count >= self.minimum_observations else None

  def clear(self) -> None:
    self._history.clear()


class VisionQRScanner:
  """Nonblocking VisionIPC sampler. It owns no payment or policy behavior."""

  SCAN_INTERVAL_NS = int(1e9)

  def __init__(self, *, prefer_wide: bool = False):
    self._client = None
    self._stream_type = None
    self._last_scan_mono_ns = 0
    self._preferred_streams = (
      (VisionStreamType.VISION_STREAM_WIDE_ROAD, VisionStreamType.VISION_STREAM_NARROW_ROAD)
      if prefer_wide else
      (VisionStreamType.VISION_STREAM_NARROW_ROAD, VisionStreamType.VISION_STREAM_WIDE_ROAD)
    )

  def _connect(self) -> bool:
    from msgq.visionipc import VisionIpcClient

    available = VisionIpcClient.available_streams("camerad", block=False)
    stream_type = next((stream for stream in self._preferred_streams if stream in available), None)
    if stream_type is None:
      return False
    if self._client is not None and self._stream_type == stream_type and self._client.is_connected():
      return True

    client = VisionIpcClient("camerad", stream_type, conflate=True)
    if not client.connect(False):
      return False
    self._client = client
    self._stream_type = stream_type
    return True

  def poll(self, now_mono_ns: int | None = None) -> QRScan | None:
    now_mono_ns = time.monotonic_ns() if now_mono_ns is None else now_mono_ns
    if now_mono_ns - self._last_scan_mono_ns < self.SCAN_INTERVAL_NS:
      return None
    if not self._connect():
      return None
    frame = self._client.recv(timeout_ms=0)
    if frame is None:
      return None

    self._last_scan_mono_ns = now_mono_ns
    y = np.frombuffer(frame.data, dtype=np.uint8, count=frame.height * frame.stride).reshape(frame.height, frame.stride)
    gray = y[:, :frame.width].copy()
    return scan_gray(gray, observed_mono_ns=now_mono_ns)
