"""Event-driven structured extraction."""

from .collector import ExtractionCollector, ExtractionResult
from .parser import parse_rendered_page
from .scoring import ObservationScorer, classify_observations

__all__ = [
    "ExtractionCollector",
    "ExtractionResult",
    "ObservationScorer",
    "classify_observations",
    "parse_rendered_page",
]
