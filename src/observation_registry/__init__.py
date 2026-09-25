"""观察记录候选合并登记：可解释匹配、馆员决策与隐私安全的统一观察追溯。"""

from .contracts import ObservationRecord, TaxonomyOpinion, ValidationError
from .matching import ALGORITHM_VERSION, RecordEvidence, evaluate_pair, haversine_m
from .service import ObservationRegistryService

__all__ = [
    "ObservationRecord",
    "TaxonomyOpinion",
    "ValidationError",
    "ALGORITHM_VERSION",
    "RecordEvidence",
    "ObservationRegistryService",
    "evaluate_pair",
    "haversine_m",
]

__version__ = "0.1.0"
