from __future__ import annotations

import io
import logging
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from PIL import Image, ImageDraw

from .config import CameraConfig
from .v4l2_capture import (
    BYTES_PER_LINE,
    FRAME_BYTES,
    FRAME_HEIGHT,
    FRAME_WIDTH,
    V4L2_DEVICE,
    V4L2_SUBDEVICE,
    MMapV4L2Capture,
    set_sensor_controls,
)

LOGGER = logging.getLogger(__name__)

PIXEL_FORMAT = "GB10"


class CameraSource(Protocol):
    name: str

    def frames(self, stop_event: threading.Event) -> Iterator[bytes]: ...


class RawBayerDecoder:
    def __init__(self, config: CameraConfig) -> None:
        if not config.raw_auto_levels and config.raw_black_level >= config.raw_white_level:
            raise ValueError("RAW_BLACK_LEVEL must be less than RAW_WHITE_LEVEL")
        self.config = config
        self.frame_bytes = FRAME_BYTES

    def decode(self, raw: bytes) -> bytes:
        if len(raw) != self.frame_bytes:
            raise ValueError(f"raw frame size must be {self.frame_bytes}, got {len(raw)}")

        words_per_line = BYTES_PER_LINE // 2
        bayer = np.frombuffer(raw, dtype="<u2").reshape(
            FRAME_HEIGHT, words_per_line
        )[:, :FRAME_WIDTH]

        if self.config.raw_auto_levels:
            black = float(np.percentile(bayer, 0.5))
            white = float(np.percentile(bayer, 99.9))
        else:
            black = float(self.config.raw_black_level)
            white = float(self.config.raw_white_level)
        if white <= black + 1.0:
            white = black + 1.0

        if black == 0.0 and white == 1023.0:
            # The web output is 8-bit JPEG. Converting GB10 directly to 8-bit
            # avoids a float image and a slower 16-bit demosaic on the Pi.
            bayer8 = (bayer >> 2).astype(np.uint8)
        else:
            bayer8 = np.clip(
                (bayer.astype(np.float32) - black) * (255.0 / (white - black)),
                0,
                255,
            ).astype(np.uint8)
        # v4l2 reports GB10 as rows GBGB... / RGRG... (GBRG).
        # OpenCV names the matching BGR conversion COLOR_BAYER_GR2BGR.
        bgr = cv2.cvtColor(bayer8, cv2.COLOR_BAYER_GR2BGR)

        if self.config.raw_auto_white_balance:
            channel_means = bgr.reshape(-1, 3).mean(axis=0)
            target = float(channel_means.mean())
            gains = np.clip(target / np.maximum(channel_means, 1.0), 0.5, 3.0)
            bgr = np.clip(bgr.astype(np.float32) * gains, 0, 255).astype(np.uint8)

        if self.config.rotation == 90:
            bgr = cv2.rotate(bgr, cv2.ROTATE_90_CLOCKWISE)
        elif self.config.rotation == 180:
            bgr = cv2.rotate(bgr, cv2.ROTATE_180)
        elif self.config.rotation == 270:
            bgr = cv2.rotate(bgr, cv2.ROTATE_90_COUNTERCLOCKWISE)

        encoded, jpeg = cv2.imencode(
            ".jpg",
            bgr,
            [cv2.IMWRITE_JPEG_QUALITY, self.config.raw_jpeg_quality],
        )
        if not encoded:
            raise RuntimeError("failed to encode the V4L2 frame as JPEG")
        return jpeg.tobytes()


@dataclass
class V4L2RawSource:
    config: CameraConfig
    name: str = "v4l2-gb10"

    def frames(self, stop_event: threading.Event) -> Iterator[bytes]:
        if not Path(V4L2_DEVICE).exists():
            raise RuntimeError(f"camera device not found: {V4L2_DEVICE}")
        if not Path(V4L2_SUBDEVICE).exists():
            raise RuntimeError(f"camera subdevice not found: {V4L2_SUBDEVICE}")

        decoder = RawBayerDecoder(self.config)
        # Leave a small tolerance so a sensor running nominally at the configured
        # rate is not accidentally reduced to half-rate by scheduling jitter.
        minimum_interval = 0.95 / self.config.framerate
        last_emitted_at = 0.0
        warmup_remaining = self.config.warmup_frames

        LOGGER.info(
            "starting direct V4L2 mmap capture: %s (%dx%d %s)",
            V4L2_DEVICE,
            FRAME_WIDTH,
            FRAME_HEIGHT,
            PIXEL_FORMAT,
        )
        with MMapV4L2Capture(V4L2_DEVICE) as capture:
            capture.configure_format()
            set_sensor_controls(
                vertical_blanking=self.config.vertical_blanking,
                exposure=self.config.exposure,
                analogue_gain=self.config.analogue_gain,
            )
            capture.start()

            while not stop_event.is_set():
                raw = capture.read_frame(timeout=0.5)
                if raw is None:
                    continue
                if warmup_remaining:
                    warmup_remaining -= 1
                    continue
                now = time.monotonic()
                if now - last_emitted_at < minimum_interval:
                    continue
                last_emitted_at = now
                yield decoder.decode(raw)


@dataclass
class SyntheticSource:
    config: CameraConfig
    name: str = "synthetic"

    def frames(self, stop_event: threading.Event) -> Iterator[bytes]:
        frame_number = 0
        interval = 1.0 / self.config.framerate
        while not stop_event.is_set():
            image = Image.new("RGB", (FRAME_WIDTH, FRAME_HEIGHT), "#06233b")
            draw = ImageDraw.Draw(image)
            band_width = max(40, FRAME_WIDTH // 8)
            offset = (frame_number * 7) % (FRAME_WIDTH + band_width)
            draw.rectangle(
                (offset - band_width, 0, offset, FRAME_HEIGHT),
                fill="#087f8c",
            )
            draw.ellipse(
                (
                    FRAME_WIDTH * 0.25,
                    FRAME_HEIGHT * 0.22,
                    FRAME_WIDTH * 0.75,
                    FRAME_HEIGHT * 0.78,
                ),
                outline="#f0b67f",
                width=max(2, FRAME_WIDTH // 240),
            )
            draw.text((24, 24), f"Synthetic camera  frame {frame_number}", fill="white")
            draw.text((24, 48), time.strftime("%Y-%m-%d %H:%M:%S"), fill="#b9e6ff")

            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88)
            yield output.getvalue()
            frame_number += 1
            stop_event.wait(interval)


def build_source(config: CameraConfig) -> CameraSource:
    if config.backend == "synthetic":
        return SyntheticSource(config)
    return V4L2RawSource(config)
