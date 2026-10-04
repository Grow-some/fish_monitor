from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable


V4L2_REQUIRED_ENVIRONMENT = (
    "CAMERA_BACKEND",
    "CAMERA_FRAMERATE",
    "CAMERA_RESTART_DELAY_SECONDS",
    "CAMERA_WARMUP_FRAMES",
    "CAMERA_VERTICAL_BLANKING",
    "CAMERA_EXPOSURE",
    "CAMERA_ANALOGUE_GAIN",
    "CAMERA_ROTATION",
    "RAW_AUTO_LEVELS",
    "RAW_BLACK_LEVEL",
    "RAW_WHITE_LEVEL",
    "RAW_AUTO_WHITE_BALANCE",
    "RAW_JPEG_QUALITY",
)


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} must be a boolean")
    return normalized in {"true", "1", "yes"}


def _configuration_errors() -> list[str]:
    errors: list[str] = []

    def validate(check: Callable[[], object]) -> object | None:
        try:
            return check()
        except ValueError as exc:
            errors.append(str(exc))
            return None

    backend = os.getenv("CAMERA_BACKEND", "v4l2").strip().lower()
    if backend not in {"v4l2", "synthetic"}:
        errors.append("CAMERA_BACKEND must be v4l2 or synthetic")

    if backend != "synthetic":
        for name in V4L2_REQUIRED_ENVIRONMENT:
            if os.getenv(name) is None:
                errors.append(
                    f"{name} is required when CAMERA_BACKEND is v4l2"
                )

    rotation = validate(lambda: _integer("CAMERA_ROTATION", 180, 0, 270))
    if rotation is not None and rotation not in {0, 90, 180, 270}:
        errors.append("CAMERA_ROTATION must be 0, 90, 180, or 270")

    validate(lambda: _integer("CAMERA_FRAMERATE", 30, 1, 120))
    validate(lambda: _number("CAMERA_RESTART_DELAY_SECONDS", 3.0, 0.1, 300.0))
    validate(lambda: _integer("CAMERA_WARMUP_FRAMES", 10, 0, 300))
    vertical_blanking = validate(
        lambda: _integer("CAMERA_VERTICAL_BLANKING", 510, 24, 32287)
    )
    exposure = validate(lambda: _integer("CAMERA_EXPOSURE", 980, 4, 32763))
    validate(lambda: _integer("CAMERA_ANALOGUE_GAIN", 306, 16, 1023))

    raw_auto_levels = validate(lambda: _boolean("RAW_AUTO_LEVELS", False))
    raw_black_level = validate(lambda: _integer("RAW_BLACK_LEVEL", 0, 0, 1022))
    raw_white_level = validate(lambda: _integer("RAW_WHITE_LEVEL", 1023, 1, 1023))
    validate(lambda: _boolean("RAW_AUTO_WHITE_BALANCE", False))
    validate(lambda: _integer("RAW_JPEG_QUALITY", 88, 40, 95))

    if (
        raw_auto_levels is False
        and raw_black_level is not None
        and raw_white_level is not None
        and raw_black_level >= raw_white_level
    ):
        errors.append("RAW_BLACK_LEVEL must be less than RAW_WHITE_LEVEL")

    if vertical_blanking is not None and exposure is not None:
        exposure_max = 480 + int(vertical_blanking) - 4
        if exposure > exposure_max:
            errors.append(
                f"CAMERA_EXPOSURE must not exceed {exposure_max} "
                "for CAMERA_VERTICAL_BLANKING"
            )

    username = os.getenv("WEB_USERNAME") or None
    password = os.getenv("WEB_PASSWORD") or None
    if (username is None) != (password is None):
        errors.append("WEB_USERNAME and WEB_PASSWORD must be set together")

    validate(lambda: _integer("WEB_PORT", 8080, 1, 65535))
    validate(lambda: _number("FRAME_STALE_SECONDS", 5.0, 0.5, 300.0))
    return errors


@dataclass(frozen=True)
class CameraConfig:
    backend: str
    framerate: int
    restart_delay_seconds: float
    warmup_frames: int
    vertical_blanking: int
    exposure: int
    analogue_gain: int
    rotation: int
    raw_auto_levels: bool
    raw_black_level: int
    raw_white_level: int
    raw_auto_white_balance: bool
    raw_jpeg_quality: int


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int
    frame_stale_seconds: float
    username: str | None
    password: str | None


@dataclass(frozen=True)
class RuntimeConfig:
    camera: CameraConfig
    server: ServerConfig

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        errors = _configuration_errors()
        if errors:
            details = "\n".join(f"- {message}" for message in errors)
            raise ValueError(f"invalid environment configuration:\n{details}")

        backend = os.getenv("CAMERA_BACKEND", "v4l2").strip().lower()

        rotation = _integer("CAMERA_ROTATION", 180, 0, 270)

        username = os.getenv("WEB_USERNAME") or None
        password = os.getenv("WEB_PASSWORD") or None

        raw_auto_levels = _boolean("RAW_AUTO_LEVELS", False)
        raw_black_level = _integer("RAW_BLACK_LEVEL", 0, 0, 1022)
        raw_white_level = _integer("RAW_WHITE_LEVEL", 1023, 1, 1023)

        vertical_blanking = _integer("CAMERA_VERTICAL_BLANKING", 510, 24, 32287)
        exposure = _integer("CAMERA_EXPOSURE", 980, 4, 32763)

        return cls(
            camera=CameraConfig(
                backend=backend,
                framerate=_integer("CAMERA_FRAMERATE", 30, 1, 120),
                restart_delay_seconds=_number(
                    "CAMERA_RESTART_DELAY_SECONDS", 3.0, 0.1, 300.0
                ),
                warmup_frames=_integer("CAMERA_WARMUP_FRAMES", 10, 0, 300),
                vertical_blanking=vertical_blanking,
                exposure=exposure,
                analogue_gain=_integer("CAMERA_ANALOGUE_GAIN", 306, 16, 1023),
                rotation=rotation,
                raw_auto_levels=raw_auto_levels,
                raw_black_level=raw_black_level,
                raw_white_level=raw_white_level,
                raw_auto_white_balance=_boolean("RAW_AUTO_WHITE_BALANCE", False),
                raw_jpeg_quality=_integer("RAW_JPEG_QUALITY", 88, 40, 95),
            ),
            server=ServerConfig(
                host=os.getenv("WEB_HOST", "0.0.0.0"),
                port=_integer("WEB_PORT", 8080, 1, 65535),
                frame_stale_seconds=_number(
                    "FRAME_STALE_SECONDS", 5.0, 0.5, 300.0
                ),
                username=username,
                password=password,
            ),
        )
