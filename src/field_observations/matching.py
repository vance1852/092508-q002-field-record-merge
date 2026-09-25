"""疑似重复上报的可解释匹配算法。

每条候选由四个可解释因子组成：地点误差、时间窗口、分类意见和观察材料。
算法输出 0..1 的置信得分、置信等级以及逐因子的匹配理由，供馆员复核，
算法本身不做合并决定。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


ALGORITHM_VERSION = "field-merge-v1"

FACTOR_WEIGHTS = {
    "distance": Decimal("0.40"),
    "time": Decimal("0.20"),
    "taxonomy": Decimal("0.25"),
    "media": Decimal("0.15"),
}
CANDIDATE_THRESHOLD = Decimal("0.35")
HIGH_CONFIDENCE_THRESHOLD = Decimal("0.75")
MEDIUM_CONFIDENCE_THRESHOLD = Decimal("0.50")
TIME_WINDOW_HOURS = Decimal(48)
EARTH_RADIUS_M = 6371008.8

_SCORE_PLACES = Decimal("0.0001")
_DISTANCE_PLACES = Decimal("0.001")
_HOUR_PLACES = Decimal("0.01")


def _quantize(value: Decimal, places: Decimal) -> Decimal:
    return value.quantize(places, rounding=ROUND_HALF_UP)


def haversine_m(
    latitude_a: Decimal, longitude_a: Decimal, latitude_b: Decimal, longitude_b: Decimal
) -> Decimal:
    """两坐标间的球面距离（米），结果量化到毫米以保证跨平台一致。"""

    phi_a = math.radians(float(latitude_a))
    phi_b = math.radians(float(latitude_b))
    delta_phi = phi_b - phi_a
    delta_lambda = math.radians(float(longitude_b) - float(longitude_a))
    hav = math.sin(delta_phi / 2) ** 2 + math.cos(phi_a) * math.cos(phi_b) * math.sin(delta_lambda / 2) ** 2
    distance = 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(hav)))
    return _quantize(Decimal(str(distance)), _DISTANCE_PLACES)


@dataclass(frozen=True, slots=True)
class ReportView:
    """参与匹配的一边上报记录快照（含最新鉴定意见）。"""

    report_id: str
    effective_taxon: str
    latitude: Decimal
    longitude: Decimal
    coordinate_uncertainty_m: Decimal
    observed_at: datetime
    media_hashes: frozenset[str]


@dataclass(frozen=True, slots=True)
class Evaluation:
    """一次成对评估的得分、置信等级与逐因子理由。"""

    score: Decimal
    confidence: str
    reasons: tuple[dict[str, Any], ...]


def _normalize_taxon(name: str) -> str:
    return " ".join(name.strip().lower().split())


def _genus(name: str) -> str:
    parts = _normalize_taxon(name).split(" ")
    return parts[0] if parts else ""


def _distance_factor(left: ReportView, right: ReportView) -> tuple[Decimal, str]:
    distance = haversine_m(left.latitude, left.longitude, right.latitude, right.longitude)
    combined = left.coordinate_uncertainty_m + right.coordinate_uncertainty_m
    if combined <= 0:
        component = Decimal(1) if distance == 0 else Decimal(0)
    else:
        component = max(Decimal(0), Decimal(1) - distance / combined)
    if distance <= combined:
        explanation = f"坐标相距 {format(distance, 'f')} 米，双方定位误差半径合计 {format(combined, 'f')} 米，误差圆相交"
    else:
        explanation = f"坐标相距 {format(distance, 'f')} 米，超出双方定位误差半径合计 {format(combined, 'f')} 米"
    return _quantize(component, _SCORE_PLACES), explanation


def _time_factor(left: ReportView, right: ReportView) -> tuple[Decimal, str]:
    seconds = abs((left.observed_at - right.observed_at).total_seconds())
    hours = _quantize(Decimal(str(seconds)) / Decimal(3600), _HOUR_PLACES)
    component = max(Decimal(0), Decimal(1) - hours / TIME_WINDOW_HOURS)
    if hours <= TIME_WINDOW_HOURS:
        explanation = f"观察时间相差 {format(hours, 'f')} 小时，处于 {format(TIME_WINDOW_HOURS, 'f')} 小时窗口内"
    else:
        explanation = f"观察时间相差 {format(hours, 'f')} 小时，超出 {format(TIME_WINDOW_HOURS, 'f')} 小时窗口"
    return _quantize(component, _SCORE_PLACES), explanation


def _taxonomy_factor(left: ReportView, right: ReportView) -> tuple[Decimal, str]:
    left_taxon = _normalize_taxon(left.effective_taxon)
    right_taxon = _normalize_taxon(right.effective_taxon)
    if left_taxon == right_taxon:
        return Decimal(1), f"分类意见一致：{left.effective_taxon}"
    if _genus(left.effective_taxon) and _genus(left.effective_taxon) == _genus(right.effective_taxon):
        return (
            Decimal("0.6"),
            f"同属于 {_genus(left.effective_taxon)}，种级意见不一致（{left.effective_taxon} 与 {right.effective_taxon}）",
        )
    return Decimal(0), f"分类意见不一致：{left.effective_taxon} 与 {right.effective_taxon}"


def _media_factor(left: ReportView, right: ReportView) -> tuple[Decimal, str]:
    shared = left.media_hashes & right.media_hashes
    if shared:
        return Decimal(1), f"双方有 {len(shared)} 份内容摘要一致的观察材料"
    if left.media_hashes and right.media_hashes:
        return Decimal("0.2"), "双方均有观察材料，但内容摘要不一致"
    return Decimal(0), "至少一方缺少可比对的观察材料"


def evaluate_pair(left: ReportView, right: ReportView) -> Evaluation:
    """对两条上报记录给出可解释的匹配得分。"""

    factors = {
        "distance": _distance_factor(left, right),
        "time": _time_factor(left, right),
        "taxonomy": _taxonomy_factor(left, right),
        "media": _media_factor(left, right),
    }
    score = Decimal(0)
    reasons: list[dict[str, Any]] = []
    for name in ("distance", "time", "taxonomy", "media"):
        component, explanation = factors[name]
        weight = FACTOR_WEIGHTS[name]
        score += weight * component
        reasons.append({
            "factor": name,
            "component": format(component, "f"),
            "weight": format(weight, "f"),
            "explanation": explanation,
        })
    score = _quantize(score, _SCORE_PLACES)
    if score >= HIGH_CONFIDENCE_THRESHOLD:
        confidence = "high"
    elif score >= MEDIUM_CONFIDENCE_THRESHOLD:
        confidence = "medium"
    else:
        confidence = "low"
    return Evaluation(score=score, confidence=confidence, reasons=tuple(reasons))
