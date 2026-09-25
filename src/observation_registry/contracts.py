"""观察记录与分类意见的严格数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


# 隐私级别从宽到严；合并后的可见范围只能取交集（更严），不能因合并而扩大披露。
PRIVACY_LEVELS = ("public", "observers", "curators")
PRIVACY_RANK = {level: index for index, level in enumerate(PRIVACY_LEVELS)}

# 受保护物种名单命中后，位置只对馆员可见。
PROTECTED_SPECIES_TAGS = frozenset({"protected"})

# 分类意见之间的关系。
TAXONOMY_RELATIONS = ("same", "compatible", "conflicting", "unassessed")


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _decimal(value: object, path: str, *, minimum: Decimal | None = None,
             maximum: Decimal | None = None) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationError(f"{path} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationError(f"{path} 不能大于 {maximum}")
    return result


@dataclass(frozen=True, slots=True)
class Coordinates:
    """带误差半径（米）的观测点；误差半径越大，坐标越不精确。"""

    latitude: Decimal
    longitude: Decimal
    accuracy_m: Decimal

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "Coordinates":
        data = _require_mapping(raw, path)
        latitude = _decimal(data.get("latitude"), f"{path}.latitude", minimum=Decimal("-90"), maximum=Decimal("90"))
        longitude = _decimal(data.get("longitude"), f"{path}.longitude", minimum=Decimal("-180"), maximum=Decimal("180"))
        accuracy_m = _decimal(data.get("accuracy_m"), f"{path}.accuracy_m", minimum=Decimal(0))
        return cls(latitude=latitude, longitude=longitude, accuracy_m=accuracy_m)

    def as_dict(self) -> dict[str, str]:
        return {
            "latitude": format(self.latitude, "f"),
            "longitude": format(self.longitude, "f"),
            "accuracy_m": format(self.accuracy_m, "f"),
        }


@dataclass(frozen=True, slots=True)
class TaxonomyOpinion:
    """对观察记录给出的一条分类鉴定意见。"""

    opinion_id: str
    taxon_id: str
    taxon_name: str
    confidence: Decimal
    reviewer_id: str
    created_at: str
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "TaxonomyOpinion":
        data = _require_mapping(raw, path)
        tags_value = data.get("tags", ())
        if not isinstance(tags_value, Sequence) or isinstance(tags_value, (str, bytes)):
            raise ValidationError(f"{path}.tags 必须是数组")
        tags = tuple(_required_text(tag, f"{path}.tags[]") for tag in tags_value)
        return cls(
            opinion_id=_required_text(data.get("opinion_id"), f"{path}.opinion_id"),
            taxon_id=_required_text(data.get("taxon_id"), f"{path}.taxon_id"),
            taxon_name=_required_text(data.get("taxon_name"), f"{path}.taxon_name"),
            confidence=_decimal(data.get("confidence"), f"{path}.confidence",
                                minimum=Decimal(0), maximum=Decimal(1)),
            reviewer_id=_required_text(data.get("reviewer_id"), f"{path}.reviewer_id"),
            created_at=_required_text(data.get("created_at"), f"{path}.created_at"),
            tags=tags,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "opinion_id": self.opinion_id,
            "taxon_id": self.taxon_id,
            "taxon_name": self.taxon_name,
            "confidence": format(self.confidence, "f"),
            "reviewer_id": self.reviewer_id,
            "created_at": self.created_at,
            "tags": list(self.tags),
        }


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    """一条由贡献者上报的原始观察记录；原始内容不可变、永不删除。"""

    record_id: str
    contributor_id: str
    observer_group: str
    coordinates: Coordinates
    observed_at: str
    privacy_level: str
    protected: bool
    materials: tuple[str, ...]
    taxonomy_opinions: tuple[TaxonomyOpinion, ...]
    note: str | None

    @classmethod
    def from_dict(cls, raw: object) -> "ObservationRecord":
        data = _require_mapping(raw, "observation_record")
        privacy_level = _required_text(data.get("privacy_level"), "observation_record.privacy_level")
        if privacy_level not in PRIVACY_RANK:
            raise ValidationError(f"未知隐私级别: {privacy_level}")
        materials_value = data.get("materials", ())
        if not isinstance(materials_value, Sequence) or isinstance(materials_value, (str, bytes)):
            raise ValidationError("observation_record.materials 必须是数组")
        materials = tuple(_required_text(item, "observation_record.materials[]") for item in materials_value)
        opinions_value = data.get("taxonomy_opinions", ())
        if not isinstance(opinions_value, Sequence) or isinstance(opinions_value, (str, bytes)):
            raise ValidationError("observation_record.taxonomy_opinions 必须是数组")
        opinions = tuple(
            TaxonomyOpinion.from_dict(item, f"observation_record.taxonomy_opinions[{index}]")
            for index, item in enumerate(opinions_value)
        )
        opinion_ids = [item.opinion_id for item in opinions]
        if len(set(opinion_ids)) != len(opinion_ids):
            raise ValidationError("分类意见编号不能重复")
        return cls(
            record_id=_required_text(data.get("record_id"), "observation_record.record_id"),
            contributor_id=_required_text(data.get("contributor_id"), "observation_record.contributor_id"),
            observer_group=_required_text(data.get("observer_group"), "observation_record.observer_group"),
            coordinates=Coordinates.from_dict(data.get("coordinates"), "observation_record.coordinates"),
            observed_at=_required_text(data.get("observed_at"), "observation_record.observed_at"),
            privacy_level=privacy_level,
            protected=bool(data.get("protected", False)),
            materials=materials,
            taxonomy_opinions=opinions,
            note=_optional_text(data.get("note"), "observation_record.note"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "contributor_id": self.contributor_id,
            "observer_group": self.observer_group,
            "coordinates": self.coordinates.as_dict(),
            "observed_at": self.observed_at,
            "privacy_level": self.privacy_level,
            "protected": self.protected,
            "materials": list(self.materials),
            "taxonomy_opinions": [item.as_dict() for item in self.taxonomy_opinions],
            "note": self.note,
        }
