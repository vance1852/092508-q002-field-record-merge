"""候选合并的确定性匹配打分：地点误差、时间窗口、分类意见、观察材料。

打分只依赖输入证据与固定参数，结果可复算、可回放；每个因子都同时返回
数值、权重和中文理由，馆员可据此解释“为什么建议合并 / 为什么不合并”。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .jsonio import canonical_json, content_digest


ALGORITHM_VERSION = "observation-match/1"

# 四个因子的固定权重，之和为 1。
WEIGHTS = {
    "spatial": Decimal("0.35"),
    "temporal": Decimal("0.20"),
    "taxonomy": Decimal("0.25"),
    "materials": Decimal("0.20"),
}

# 默认时间窗口（小时）：雨后同次调查的上报通常落在三天内。
DEFAULT_TIME_WINDOW_HOURS = Decimal("72")

# 置信度分档阈值。
MERGE_THRESHOLD = Decimal("0.70")
REVIEW_THRESHOLD = Decimal("0.45")

EARTH_RADIUS_M = Decimal("6371000")


@dataclass(frozen=True, slots=True)
class OpinionEvidence:
    taxon_id: str
    confidence: Decimal
    protected: bool = False


@dataclass(frozen=True, slots=True)
class RecordEvidence:
    """匹配时使用的证据视图：原始记录 + 后续补充证据。"""

    record_id: str
    latitude: Decimal
    longitude: Decimal
    accuracy_m: Decimal
    observed_at: datetime
    materials: tuple[str, ...]
    opinions: tuple[OpinionEvidence, ...]
    protected: bool
    opinion_count: int = 0
    evidence_count: int = 0

    @property
    def effective_protected(self) -> bool:
        return self.protected or any(opinion.protected for opinion in self.opinions)


def _quantize3(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.001"))


def haversine_m(
    latitude_a: Decimal, longitude_a: Decimal, latitude_b: Decimal, longitude_b: Decimal
) -> Decimal:
    """两点之间的大圆距离（米）。"""

    from math import asin, cos, radians, sin, sqrt

    lat1 = radians(float(latitude_a))
    lat2 = radians(float(latitude_b))
    dlat = radians(float(latitude_b - latitude_a))
    dlon = radians(float(longitude_b - longitude_a))
    arc = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return Decimal(str(2 * EARTH_RADIUS_M * Decimal(str(asin(sqrt(arc))))))


def _spatial_factor(a: RecordEvidence, b: RecordEvidence) -> dict[str, object]:
    distance = haversine_m(a.latitude, a.longitude, b.latitude, b.longitude)
    radius_sum = a.accuracy_m + b.accuracy_m
    radius_diff = abs(a.accuracy_m - b.accuracy_m)
    vetoed = distance > radius_sum
    if distance == 0 and radius_sum == 0:
        score = Decimal("1")
        reason = "两个坐标完全重合，且都声明为精确点位"
    elif distance <= radius_diff:
        score = Decimal("1")
        reason = "一个误差圆完全包含另一个误差圆，点位相容"
    elif distance <= radius_sum:
        denominator = radius_sum - radius_diff
        score = (radius_sum - distance) / denominator if denominator > 0 else Decimal("1")
        reason = "误差圆相交，点位可能为同一处"
    else:
        score = Decimal("0")
        reason = "两个误差圆不相交，按现有坐标精度不可能是同一地点"
    return {
        "score": format(_quantize3(score), "f"),
        "weight": format(WEIGHTS["spatial"], "f"),
        "distance_m": format(distance.quantize(Decimal("0.1")), "f"),
        "accuracy_m": {a.record_id: format(a.accuracy_m, "f"), b.record_id: format(b.accuracy_m, "f")},
        "overlap": not vetoed,
        "reason": reason,
    }


def _temporal_factor(
    a: RecordEvidence, b: RecordEvidence, window_hours: Decimal
) -> dict[str, object]:
    gap_seconds = abs((a.observed_at - b.observed_at).total_seconds())
    gap_hours = Decimal(str(gap_seconds)) / Decimal(3600)
    vetoed = gap_hours > window_hours
    if not vetoed:
        score = Decimal(1) - gap_hours / window_hours
        reason = "拍摄时间落在同一调查时间窗口内"
    else:
        score = Decimal("0")
        reason = "拍摄时间相差超过时间窗口，不属于同次可合并观察"
    return {
        "score": format(_quantize3(score), "f"),
        "weight": format(WEIGHTS["temporal"], "f"),
        "gap_hours": format(gap_hours.quantize(Decimal("0.01")), "f"),
        "window_hours": format(window_hours, "f"),
        "reason": reason,
    }


def _taxonomy_factor(a: RecordEvidence, b: RecordEvidence) -> dict[str, object]:
    by_taxon_a: dict[str, Decimal] = {}
    for opinion in a.opinions:
        by_taxon_a[opinion.taxon_id] = max(by_taxon_a.get(opinion.taxon_id, Decimal(0)), opinion.confidence)
    by_taxon_b: dict[str, Decimal] = {}
    for opinion in b.opinions:
        by_taxon_b[opinion.taxon_id] = max(by_taxon_b.get(opinion.taxon_id, Decimal(0)), opinion.confidence)
    shared = sorted(set(by_taxon_a) & set(by_taxon_b))
    if shared:
        best = max(min(by_taxon_a[taxon], by_taxon_b[taxon]) for taxon in shared)
        relation = "same"
        reason = f"双方鉴定意见指向相同类群 {shared}，最低一致置信度 {best}"
        result = {
            "score": format(_quantize3(best), "f"),
            "relation": relation,
            "shared_taxa": shared,
            "reason": reason,
        }
    elif by_taxon_a and by_taxon_b:
        relation = "conflicting"
        result = {
            "score": "0.150",
            "relation": relation,
            "shared_taxa": [],
            "reason": "双方都给出了鉴定意见但类群不一致，需要馆员复核，不能仅凭意见合并",
        }
    else:
        relation = "unassessed"
        result = {
            "score": "0.500",
            "relation": relation,
            "shared_taxa": [],
            "reason": "至少一方尚无鉴定意见，分类维度保持中性",
        }
    result["weight"] = format(WEIGHTS["taxonomy"], "f")
    return result


def _materials_factor(a: RecordEvidence, b: RecordEvidence) -> dict[str, object]:
    set_a = set(a.materials)
    set_b = set(b.materials)
    union = set_a | set_b
    intersection = set_a & set_b
    if not union:
        score = Decimal("0.5")
        reason = "双方都未登记观察材料类型，材料维度保持中性"
    else:
        score = Decimal(len(intersection)) / Decimal(len(union))
        reason = (
            f"共有材料 {sorted(intersection) or '无'}，并集 {sorted(union)}，Jaccard 重合度 {_quantize3(score)}"
        )
    return {
        "score": format(_quantize3(score), "f"),
        "weight": format(WEIGHTS["materials"], "f"),
        "shared_materials": sorted(intersection),
        "all_materials": sorted(union),
        "reason": reason,
    }


def evaluate_pair(
    a: RecordEvidence,
    b: RecordEvidence,
    *,
    time_window_hours: Decimal | float | int | str = DEFAULT_TIME_WINDOW_HOURS,
) -> dict[str, object]:
    """对两条记录计算四维分数、总置信度、建议与可否决原因。"""

    window = Decimal(str(time_window_hours))
    if window <= 0:
        raise ValueError("时间窗口必须大于零")
    spatial = _spatial_factor(a, b)
    temporal = _temporal_factor(a, b, window)
    taxonomy = _taxonomy_factor(a, b)
    materials = _materials_factor(a, b)
    factors = {
        "spatial": spatial,
        "temporal": temporal,
        "taxonomy": taxonomy,
        "materials": materials,
    }
    confidence = sum(
        WEIGHTS[name] * Decimal(str(factor["score"])) for name, factor in factors.items()
    )
    gap_hours = Decimal(str(abs((a.observed_at - b.observed_at).total_seconds()))) / Decimal(3600)
    temporal_vetoed = gap_hours > window
    veto_reasons: list[str] = []
    if not spatial["overlap"]:
        veto_reasons.append("spatial_disjoint")
    if temporal_vetoed:
        veto_reasons.append("outside_time_window")
    vetoed = bool(veto_reasons)
    taxonomy_conflict = taxonomy["relation"] == "conflicting"
    if vetoed:
        recommendation = "no_match"
    elif taxonomy_conflict and confidence >= MERGE_THRESHOLD:
        # 业务规则：双方高置信度鉴定相互冲突时，不允许仅凭其他维度自动合并。
        recommendation = "review"
    elif confidence >= MERGE_THRESHOLD:
        recommendation = "merge"
    elif confidence >= REVIEW_THRESHOLD:
        recommendation = "review"
    else:
        recommendation = "no_match"
    explanation = {
        "algorithm_version": ALGORITHM_VERSION,
        "record_ids": [a.record_id, b.record_id],
        "evidence_revision": sorted([
            {"record_id": a.record_id, "opinions": a.opinion_count, "evidence": a.evidence_count},
            {"record_id": b.record_id, "opinions": b.opinion_count, "evidence": b.evidence_count},
        ], key=lambda item: item["record_id"]),
        "time_window_hours": format(window, "f"),
        "factors": factors,
        "confidence": format(_quantize3(confidence), "f"),
        "recommendation": recommendation,
        "vetoed": vetoed,
        "veto_reasons": veto_reasons,
        "taxonomy_relation": taxonomy["relation"],
        "protected_any": a.effective_protected or b.effective_protected,
    }
    explanation["fingerprint"] = content_digest([
        canonical_json({key: value for key, value in explanation.items() if key != "fingerprint"})
    ])
    return explanation
