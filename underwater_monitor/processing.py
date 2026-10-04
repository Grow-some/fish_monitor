from __future__ import annotations

import io
import os
import threading
from dataclasses import asdict, dataclass, fields, replace
from typing import Any, ClassVar, Mapping

from PIL import Image, ImageEnhance, ImageFilter, ImageOps


@dataclass(frozen=True)
class CorrectionSettings:
    enabled: bool = True
    autocontrast: bool = True
    gamma: float = 1.0
    contrast: float = 1.15
    color: float = 1.05
    red_gain: float = 1.20
    sharpness: float = 1.50
    jpeg_quality: int = 85

    RANGES: ClassVar[dict[str, tuple[float, float]]] = {
        "gamma": (0.20, 3.00),
        "contrast": (0.50, 3.00),
        "color": (0.00, 3.00),
        "red_gain": (0.50, 3.00),
        "sharpness": (0.00, 5.00),
        "jpeg_quality": (40, 95),
    }
    BOOLEAN_FIELDS: ClassVar[set[str]] = {"enabled", "autocontrast"}

    @classmethod
    def from_env(cls) -> "CorrectionSettings":
        values: dict[str, Any] = {}
        for field_name in cls.BOOLEAN_FIELDS:
            env_name = f"CORRECTION_{field_name.upper()}"
            raw = os.getenv(env_name)
            if raw is not None:
                normalized = raw.strip().lower()
                if normalized not in {"true", "false", "1", "0", "yes", "no"}:
                    raise ValueError(f"{env_name} must be a boolean")
                values[field_name] = normalized in {"true", "1", "yes"}
        for field_name in cls.RANGES:
            env_name = f"CORRECTION_{field_name.upper()}"
            raw = os.getenv(env_name)
            if raw is not None:
                values[field_name] = float(raw)
        return cls().updated(values)

    def updated(self, values: Mapping[str, Any]) -> "CorrectionSettings":
        allowed = {item.name for item in fields(self)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(sorted(unknown))}")

        updates: dict[str, Any] = {}
        for name, raw_value in values.items():
            if name in self.BOOLEAN_FIELDS:
                if not isinstance(raw_value, bool):
                    raise ValueError(f"{name} must be a boolean")
                updates[name] = raw_value
                continue

            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise ValueError(f"{name} must be a number")
            minimum, maximum = self.RANGES[name]
            numeric = float(raw_value)
            if not minimum <= numeric <= maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")
            updates[name] = int(numeric) if name == "jpeg_quality" else numeric

        return replace(self, **updates)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SettingsStore:
    def __init__(self, initial: CorrectionSettings) -> None:
        self._settings = initial
        self._lock = threading.Lock()

    def get(self) -> CorrectionSettings:
        with self._lock:
            return self._settings

    def update(self, values: Mapping[str, Any]) -> CorrectionSettings:
        with self._lock:
            updated = self._settings.updated(values)
            self._settings = updated
            return updated


class ImageProcessor:
    def process(self, jpeg: bytes, settings: CorrectionSettings) -> bytes:
        if not settings.enabled:
            return jpeg

        with Image.open(io.BytesIO(jpeg)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")

        if settings.red_gain != 1.0:
            red, green, blue = image.split()
            red_lut = [min(255, round(value * settings.red_gain)) for value in range(256)]
            image = Image.merge("RGB", (red.point(red_lut), green, blue))

        if settings.autocontrast:
            image = ImageOps.autocontrast(image, cutoff=1)

        if settings.gamma != 1.0:
            inverse_gamma = 1.0 / settings.gamma
            gamma_lut = [
                min(255, round(255 * ((value / 255) ** inverse_gamma)))
                for value in range(256)
            ]
            image = image.point(gamma_lut * 3)

        image = ImageEnhance.Contrast(image).enhance(settings.contrast)
        image = ImageEnhance.Color(image).enhance(settings.color)

        if settings.sharpness > 0:
            image = image.filter(
                ImageFilter.UnsharpMask(
                    radius=2.0,
                    percent=round(settings.sharpness * 100),
                    threshold=3,
                )
            )

        output = io.BytesIO()
        image.save(output, format="JPEG", quality=settings.jpeg_quality)
        return output.getvalue()
