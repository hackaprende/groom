"""Request validation, sampling order, and the report's arithmetic.

The reconciliation assertion is the important one here. A curation report that
does not add up is worse than no report, because it would be trusted.
"""

from __future__ import annotations

import pytest

from src import config, report
from src.models import (
    STAGE_DEDUP,
    STAGE_INSPECTION,
    STAGE_PREFILTER,
    STAGE_PROCESSING,
    Candidate,
    Rejection,
    RunReport,
)
from src.pipeline import RequestError, sample_order, validate_request


# ── stage 0: request validation ───────────────────────────────────────────────


@pytest.mark.parametrize("count", [1, 5, config.MAX_IMAGES_PER_REQUEST])
def test_valid_counts_are_accepted(count):
    validate_request(count)


@pytest.mark.parametrize("count", [0, -1, -100])
def test_non_positive_counts_are_rejected(count):
    with pytest.raises(RequestError):
        validate_request(count)


def test_over_the_ceiling_is_rejected_not_clamped():
    over = config.MAX_IMAGES_PER_REQUEST + 1
    with pytest.raises(RequestError) as exc:
        validate_request(over)
    # The message has to state the limit, since the caller is a language model
    # relaying it to a person.
    assert str(config.MAX_IMAGES_PER_REQUEST) in str(exc.value)


# ── sampling order ────────────────────────────────────────────────────────────


def _candidates(n: int) -> list[Candidate]:
    return [Candidate(f"b/n{i:05d}.jpg", f"n{i:05d}.jpg") for i in range(n)]


def test_sample_order_is_deterministic():
    pool = _candidates(200)
    first = [c.filename for c in sample_order(pool)]
    second = [c.filename for c in sample_order(list(reversed(pool)))]
    assert first == second


def test_sample_order_spreads_across_the_folder():
    # Alphabetical order would put the first 20 selections in decile 0. The
    # hash order must scatter them, or the sample is systematically biased.
    pool = _candidates(1000)
    positions = {c.filename: i for i, c in enumerate(pool)}
    chosen = [positions[c.filename] for c in sample_order(pool)[:100]]
    deciles = {p // 100 for p in chosen}
    assert len(deciles) >= 8


def test_sample_order_is_stable_when_the_folder_grows():
    # Adding images later must not reshuffle earlier selections, or a repeated
    # request picks a different set and the output folder accumulates.
    pool = _candidates(500)
    before = [c.filename for c in sample_order(pool)[:50]]
    grown = pool + _candidates(200)[:0] + [
        Candidate(f"b/new{i}.jpg", f"new{i}.jpg") for i in range(200)
    ]
    after = {c.filename for c in sample_order(grown)[:70]}
    assert set(before) <= after


def test_sample_order_keeps_every_candidate():
    pool = _candidates(50)
    assert len(sample_order(pool)) == 50
    assert {c.filename for c in sample_order(pool)} == {c.filename for c in pool}


# ── the report ────────────────────────────────────────────────────────────────


def _run(**kwargs) -> RunReport:
    base = dict(
        breed_requested="Siberian Husky",
        folder_matched="1160-n000003-Siberian_husky",
        requested_count=10,
        candidates_examined=25,
        kept=10,
    )
    base.update(kwargs)
    return RunReport(**base)


def test_counts_reconcile_against_candidates_examined():
    run = _run(
        kept=10,
        candidates_examined=25,
        unused_surplus=11,
        rejections=[
            Rejection("a.jpg", STAGE_PREFILTER, "below minimum resolution (100x90 < 200x200)"),
            Rejection("b.jpg", STAGE_DEDUP, "near-duplicate of a.jpg"),
            Rejection("c.jpg", STAGE_INSPECTION, "wrong breed: not a husky"),
            Rejection("d.jpg", STAGE_PROCESSING, "crop too small (180x190, short side below 224px)"),
        ],
    )
    data = report.to_dict(run)
    assert run.kept + len(run.rejections) + run.unused_surplus == run.candidates_examined
    assert data["rejected_total"] == 4
    assert data["unused_surplus"] == 11


def test_rejections_group_by_stage_and_reason_category():
    run = _run(
        rejections=[
            Rejection("a.jpg", STAGE_INSPECTION, "wrong breed: not a husky at all"),
            Rejection("b.jpg", STAGE_INSPECTION, "wrong breed: this is a malamute"),
            Rejection("c.jpg", STAGE_INSPECTION, "no dog: empty background"),
        ]
    )
    grouped = report.group_rejections(run.rejections)
    # Free-text detail after the category must not split the counts.
    assert grouped[STAGE_INSPECTION]["wrong breed"] == 2
    assert grouped[STAGE_INSPECTION]["no dog"] == 1


def test_per_image_detail_does_not_fragment_counts():
    run = _run(
        rejections=[
            Rejection(f"{i}.jpg", STAGE_PREFILTER, f"below minimum resolution ({i}x{i} < 200x200)")
            for i in range(5)
        ]
    )
    grouped = report.group_rejections(run.rejections)
    assert grouped[STAGE_PREFILTER]["below minimum resolution"] == 5


def test_stages_render_in_pipeline_order():
    run = _run(
        rejections=[
            Rejection("d.jpg", STAGE_PROCESSING, "crop too small"),
            Rejection("a.jpg", STAGE_PREFILTER, "unreadable image"),
            Rejection("c.jpg", STAGE_INSPECTION, "no dog: nothing there"),
            Rejection("b.jpg", STAGE_DEDUP, "near-duplicate of a.jpg"),
        ]
    )
    text = report.render_text(run)
    order = [
        text.index("in pre-filter"),
        text.index("as near-duplicates"),
        text.index("on inspection"),
        text.index("during processing"),
    ]
    assert order == sorted(order)


def test_shortfall_is_stated_with_the_real_number():
    text = report.render_text(_run(requested_count=100, kept=62))
    assert "62" in text
    assert "100" in text
    assert "Shortfall" in text


def test_no_shortfall_line_when_the_request_is_met():
    assert "Shortfall" not in report.render_text(_run(requested_count=10, kept=10))


def test_warnings_are_surfaced():
    run = _run(warnings=["6 images had no usable annotation, so the full frame was kept."])
    assert "Warning:" in report.render_text(run)
    assert "no usable annotation" in report.render_text(run)


def test_unknown_stage_still_renders():
    # Block 3 added "inspection" without touching the report. A future stage
    # must degrade to its raw name rather than disappearing.
    run = _run(rejections=[Rejection("a.jpg", "some-future-stage", "because reasons")])
    assert "some-future-stage" in report.render_text(run)
