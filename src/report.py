"""Stage 7 — turn a finished run into a structured summary and readable text.

The report is the product as much as the images are. A curation run that
quietly returns 60 images when 100 were asked for is worse than one that
returns 60 and says so, because only the second lets the human decide what to
do about it. Nothing here rounds up, pads, or omits a shortfall.
"""

from __future__ import annotations

from collections import Counter

from .models import (
    STAGE_DEDUP,
    STAGE_INSPECTION,
    STAGE_PREFILTER,
    STAGE_PROCESSING,
    STAGE_UPLOAD,
    Rejection,
    RunReport,
)

# How each stage is phrased in the rendered text. Unknown stages — such as the
# "inspection" stage added in the next block — fall back to their raw name.
_STAGE_LABELS = {
    STAGE_PREFILTER: "in pre-filter",
    STAGE_DEDUP: "as near-duplicates",
    STAGE_INSPECTION: "on inspection",
    STAGE_PROCESSING: "during processing",
    STAGE_UPLOAD: "during upload",
}

_STAGE_ORDER = [
    STAGE_PREFILTER,
    STAGE_DEDUP,
    STAGE_INSPECTION,
    STAGE_PROCESSING,
    STAGE_UPLOAD,
]


def _reason_key(reason: str) -> str:
    """Collapse a specific reason into the category it belongs to.

    Reasons carry per-image detail — "below minimum resolution (150x180 < ...)",
    "near-duplicate of n103339.jpg" — which is worth keeping on the individual
    rejection but would make every count come out as 1. Trimming at the detail
    marker groups them without losing the human-readable original.
    """
    for marker in (" (", " of ", ": "):
        index = reason.find(marker)
        if index != -1:
            reason = reason[:index]
    return reason.strip()


def group_rejections(rejections: list[Rejection]) -> dict[str, Counter[str]]:
    """Rejections counted by stage, then by reason category."""
    grouped: dict[str, Counter[str]] = {}
    for rejection in rejections:
        grouped.setdefault(rejection.stage, Counter())[_reason_key(rejection.reason)] += 1
    return grouped


def _sorted_stages(grouped: dict[str, Counter[str]]) -> list[str]:
    """Known stages in pipeline order, then any others alphabetically."""
    known = [stage for stage in _STAGE_ORDER if stage in grouped]
    unknown = sorted(stage for stage in grouped if stage not in _STAGE_ORDER)
    return known + unknown


def render_text(report: RunReport) -> str:
    """The report as the plain-language summary a human reads."""
    lines: list[str] = [
        f"Requested {report.requested_count} {report.breed_requested} images.",
        f"Matched folder: {report.folder_matched}",
        f"Examined {report.candidates_examined} candidates.",
    ]

    grouped = group_rejections(report.rejections)
    for stage in _sorted_stages(grouped):
        reasons = grouped[stage]
        total = sum(reasons.values())
        label = _STAGE_LABELS.get(stage, f"at stage '{stage}'")
        breakdown = ", ".join(
            f"{count} {reason}" for reason, count in reasons.most_common()
        )
        lines.append(f"  Rejected {total} {label}: {breakdown}.")

    if report.kept:
        destination = f' to Drive folder "{report.drive_folder_name}"' if report.drive_folder_name else ""
        lines.append(f"Processed and filed {report.kept} images{destination}.")
    else:
        lines.append("No images were filed.")

    if report.kept < report.requested_count:
        lines.append(
            f"Shortfall: {report.kept} of {report.requested_count} requested images "
            f"were delivered. The rest did not survive the pipeline."
        )

    lines.extend(f"Warning: {warning}" for warning in report.warnings)

    return "\n".join(lines)


def to_dict(report: RunReport) -> dict[str, object]:
    """The report as structured data, for the agent to return alongside the text."""
    grouped = group_rejections(report.rejections)
    return {
        "breed_requested": report.breed_requested,
        "folder_matched": report.folder_matched,
        "requested_count": report.requested_count,
        "candidates_examined": report.candidates_examined,
        "kept": report.kept,
        "shortfall": max(0, report.requested_count - report.kept),
        "rejections_by_stage": {
            stage: {
                "total": sum(grouped[stage].values()),
                "reasons": dict(grouped[stage].most_common()),
            }
            for stage in _sorted_stages(grouped)
        },
        "rejected_total": len(report.rejections),
        "unused_surplus": report.unused_surplus,
        "warnings": list(report.warnings),
        "drive_folder_name": report.drive_folder_name,
        "drive_folder_id": report.drive_folder_id,
    }
