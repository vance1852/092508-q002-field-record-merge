"""野外观察上报与鉴定意见的数据契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import parse_utc
from .errors import ValidationFailed
from .permissions import VISIBILITY_LEVELS


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
IDENTIFICATION_CONFIDENCES = {"low", "medium", "high"}
MAX_COORDINATE_UNCERTAINTY_M = Decimal("100000")


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def optional_text(value: object, field: str, maximum: int = 500) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValidationFailed(f"{field} 必须是字符串")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def utc_timestamp(value: object, field: str) -> str:
    result = required_text(value, field, 40)
    try:
        parse_utc(result, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return result


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationFailed(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationFailed(f"{path} 必须是数组")
    return value


@dataclass(frozen=True, slots=True)
class MediaItem:
    """一份观察材料（照片、录音等）的内容摘要与拍摄时间。"""

    sha256: str
    captured_at: str | None

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "MediaItem":
        data = _require_mapping(raw, path)
        digest = required_text(data.get("sha256"), f"{path}.sha256", 64).lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValidationFailed(f"{path}.sha256 必须是 64 位十六进制摘要")
        captured_at = data.get("captured_at")
        if captured_at is not None:
            captured_at = utc_timestamp(captured_at, f"{path}.captured_at")
        return cls(sha256=digest, captured_at=captured_at)


@dataclass(frozen=True, slots=True)
class ObservationReport:
    """一条不可变的野外观察上报记录。"""

    report_id: str
    taxon_name: str
    latitude: Decimal
    longitude: Decimal
    coordinate_uncertainty_m: Decimal
    observed_at: str
    visibility: str
    protected: bool
    media: tuple[MediaItem, ...]
    note: str

    @classmethod
    def from_dict(cls, raw: object) -> "ObservationReport":
        data = _require_mapping(raw, "observation_report")
        visibility = required_text(data.get("visibility"), "observation_report.visibility", 16)
        if visibility not in VISIBILITY_LEVELS:
            raise ValidationFailed("observation_report.visibility 必须是 public、restricted 或 sensitive")
        protected = data.get("protected", False)
        if not isinstance(protected, bool):
            raise ValidationFailed("observation_report.protected 必须是布尔值")
        media = tuple(
            MediaItem.from_dict(item, f"observation_report.media[{index}]")
            for index, item in enumerate(_require_sequence(data.get("media", []), "observation_report.media"))
        )
        return cls(
            report_id=identifier(data.get("report_id"), "observation_report.report_id"),
            taxon_name=required_text(data.get("taxon_name"), "observation_report.taxon_name", 200),
            latitude=decimal_value(
                data.get("latitude"), "observation_report.latitude",
                minimum=Decimal("-90"), maximum=Decimal("90"),
            ),
            longitude=decimal_value(
                data.get("longitude"), "observation_report.longitude",
                minimum=Decimal("-180"), maximum=Decimal("180"),
            ),
            coordinate_uncertainty_m=decimal_value(
                data.get("coordinate_uncertainty_m"), "observation_report.coordinate_uncertainty_m",
                minimum=Decimal("0"), maximum=MAX_COORDINATE_UNCERTAINTY_M,
            ),
            observed_at=utc_timestamp(data.get("observed_at"), "observation_report.observed_at"),
            visibility=visibility,
            protected=protected,
            media=media,
            note=optional_text(data.get("note"), "observation_report.note"),
        )


@dataclass(frozen=True, slots=True)
class Identification:
    """针对一条上报记录的后续鉴定意见。"""

    taxon_name: str
    confidence: str
    note: str

    @classmethod
    def from_dict(cls, raw: object) -> "Identification":
        data = _require_mapping(raw, "identification")
        confidence = required_text(data.get("confidence"), "identification.confidence", 16)
        if confidence not in IDENTIFICATION_CONFIDENCES:
            raise ValidationFailed("identification.confidence 必须是 low、medium 或 high")
        return cls(
            taxon_name=required_text(data.get("taxon_name"), "identification.taxon_name", 200),
            confidence=confidence,
            note=optional_text(data.get("note"), "identification.note"),
        )
