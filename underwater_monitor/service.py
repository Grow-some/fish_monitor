from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from .camera import CameraSource
from .processing import ImageProcessor, SettingsStore

LOGGER = logging.getLogger(__name__)
FPS_SAMPLE_SIZE = 30


@dataclass(frozen=True)
class FrameSnapshot:
    frame_id: int
    captured_at: float
    original: bytes
    enhanced: bytes


class FrameStore:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._latest: FrameSnapshot | None = None
        self._frame_timestamps: deque[float] = deque(maxlen=FPS_SAMPLE_SIZE)
        self._error: str | None = None
        self._stopped = False

    def publish(self, original: bytes, enhanced: bytes) -> FrameSnapshot:
        with self._condition:
            frame_id = 1 if self._latest is None else self._latest.frame_id + 1
            snapshot = FrameSnapshot(frame_id, time.time(), original, enhanced)
            self._latest = snapshot
            self._frame_timestamps.append(time.monotonic())
            self._error = None
            self._condition.notify_all()
            return snapshot

    def set_error(self, message: str) -> None:
        with self._condition:
            self._error = message
            self._condition.notify_all()

    def mark_stopped(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()

    def latest(self) -> FrameSnapshot | None:
        with self._condition:
            return self._latest

    def wait_after(self, frame_id: int, timeout: float) -> FrameSnapshot | None:
        with self._condition:
            self._condition.wait_for(
                lambda: self._stopped
                or (self._latest is not None and self._latest.frame_id > frame_id),
                timeout=timeout,
            )
            if self._latest is not None and self._latest.frame_id > frame_id:
                return self._latest
            return None

    def status(self, stale_seconds: float, source_name: str) -> dict[str, Any]:
        with self._condition:
            now = time.time()
            age = None if self._latest is None else max(0.0, now - self._latest.captured_at)
            healthy = (
                not self._stopped
                and self._latest is not None
                and age is not None
                and age <= stale_seconds
                and self._error is None
            )
            if self._stopped:
                state = "STOPPED"
            elif healthy:
                state = "RUNNING"
            elif self._latest is None and self._error is None:
                state = "STARTING"
            else:
                state = "DEGRADED"
            frame_per_second = 0.0
            if len(self._frame_timestamps) >= 2:
                elapsed = self._frame_timestamps[-1] - self._frame_timestamps[0]
                if elapsed > 0:
                    frame_per_second = (len(self._frame_timestamps) - 1) / elapsed
            return {
                "state": state,
                "healthy": healthy,
                "source": source_name,
                "frame_id": None if self._latest is None else self._latest.frame_id,
                "frame_per_second": round(frame_per_second, 3),
                "error": self._error,
            }


class CaptureService:
    def __init__(
        self,
        source: CameraSource,
        settings: SettingsStore,
        processor: ImageProcessor,
        restart_delay_seconds: float,
    ) -> None:
        self.source = source
        self.settings = settings
        self.processor = processor
        self.restart_delay_seconds = restart_delay_seconds
        self.frames = FrameStore()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._capture_loop,
            name="camera-capture",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=6)
        self.frames.mark_stopped()

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _capture_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                received = False
                for original in self.source.frames(self._stop_event):
                    if self._stop_event.is_set():
                        break
                    received = True
                    processing_error: str | None = None
                    try:
                        enhanced = self.processor.process(original, self.settings.get())
                    except Exception as exc:  # keep the original stream observable
                        LOGGER.exception("image correction failed")
                        enhanced = original
                        processing_error = f"image correction failed: {exc}"
                    self.frames.publish(original, enhanced)
                    if processing_error is not None:
                        self.frames.set_error(processing_error)
                if not self._stop_event.is_set():
                    reason = "camera stream ended" if received else "camera produced no frames"
                    raise RuntimeError(reason)
            except Exception as exc:
                LOGGER.exception("camera capture failed")
                self.frames.set_error(str(exc))
                self._stop_event.wait(self.restart_delay_seconds)

        self.frames.mark_stopped()
