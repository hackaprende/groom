"""Data passed between pipeline stages.

Explicit dataclasses rather than dicts: each stage's contract is readable from
the type, and a missing field fails at construction instead of at use.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Rejection stages. Deliberately plain strings rather than an enum — Block 3
# adds STAGE_INSPECTION without touching any existing signature.
STAGE_PREFILTER = "prefilter"
STAGE_DEDUP = "dedup"
STAGE_PROCESSING = "processing"
STAGE_UPLOAD = "upload"


@dataclass
class BoundingBox:
    """An axis-aligned box in pixel coordinates, `xmax`/`ymax` exclusive."""

    xmin: int
    ymin: int
    xmax: int
    ymax: int

    @property
    def width(self) -> int:
        return self.xmax - self.xmin

    @property
    def height(self) -> int:
        return self.ymax - self.ymin


@dataclass
class Candidate:
    """One source image as it moves through the pipeline.

    Fields are filled in progressively: `width`/`height` by the pre-filter,
    `image_bytes` when first downloaded, `bbox` at crop time.
    """

    blob_name: str
    filename: str
    width: int | None = None
    height: int | None = None
    image_bytes: bytes | None = None
    bbox: BoundingBox | None = None


@dataclass
class Rejection:
    """A candidate that did not survive, and why."""

    filename: str
    stage: str  # "prefilter" | "dedup" | ... (see STAGE_* above)
    reason: str  # human-readable


@dataclass
class ProcessedImage:
    """A survivor, cropped to its dog, ready to upload."""

    filename: str
    image_bytes: bytes
    width: int
    height: int
    # True when the image had no usable annotation, so the full frame was kept
    # instead of a crop around the dog.
    used_fallback_crop: bool = False


@dataclass
class BreedMatch:
    """Result of Gemini breed-name matching (Stage 1)."""

    matched_folder: str | None
    confidence: str  # "high" | "medium" | "low" | "none"
    alternatives: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def is_match(self) -> bool:
        return bool(self.matched_folder)

    @property
    def is_ambiguous(self) -> bool:
        """Ambiguous when the model hedged or offered other plausible folders.

        The agent must stop and ask rather than pick — a wrong breed match
        silently poisons the training set.
        """
        return bool(self.alternatives) or self.confidence.lower() != "high"


@dataclass
class RunReport:
    """Everything the user needs to judge the run."""

    breed_requested: str
    folder_matched: str
    requested_count: int
    candidates_examined: int
    kept: int
    rejections: list[Rejection] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    drive_folder_name: str = ""
    drive_folder_id: str = ""
    # Candidates that passed every stage but were not needed once the requested
    # count was reached. Tracked so that
    # `candidates_examined == kept + len(rejections) + unused_surplus` always
    # holds — the report's counts have to reconcile to be worth anything.
    unused_surplus: int = 0
