"""Skills subpackage.

Lane A owns the loader and frontmatter parsing; Lane D owns the scanner and confusability.
Both lanes add only their own imports here, so the two blocks merge without conflict.
"""

from __future__ import annotations

from pikachu.skills.confusability import (
    ConfusabilityReport,
    SimilarityPair,
    check_new_skill,
    cosine_similarity,
    max_pairwise_similarity,
)
from pikachu.skills.frontmatter import parse_frontmatter, split_frontmatter
from pikachu.skills.loader import SkillMeta, load_bundle, load_metadata, load_skill
from pikachu.skills.scanner import (
    Finding,
    PatternFamily,
    ScanReport,
    Severity,
    reject_or_raise,
    scan,
)

__all__ = [
    # loader (Lane A)
    "SkillMeta",
    "load_bundle",
    "load_metadata",
    "load_skill",
    "parse_frontmatter",
    "split_frontmatter",
    # scanner
    "Finding",
    "PatternFamily",
    "ScanReport",
    "Severity",
    "reject_or_raise",
    "scan",
    # confusability
    "ConfusabilityReport",
    "SimilarityPair",
    "check_new_skill",
    "cosine_similarity",
    "max_pairwise_similarity",
]
