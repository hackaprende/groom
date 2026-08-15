"""Tunable constants and environment configuration for Groom.

Every threshold the pipeline depends on lives here. Nothing tunable should be
written inline in a stage module.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Anchor to the project root rather than the working directory. `load_dotenv()`
# with no argument searches from wherever the process happens to be started,
# so a script run from another folder would silently see no configuration at
# all and fail on a missing environment variable instead.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env_int(name: str, default: int) -> int:
    """An integer setting that the environment may lower or raise."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from None


# ── Pipeline tuning ───────────────────────────────────────────────────────────

# Hard ceiling — requests above this are rejected, never quietly trimmed.
#
# Overridable by environment so a publicly reachable deployment can run at a
# much lower cap than the design limit. The service is open during judging and
# each image costs a Gemini call and a write to the operator's Drive; 200 is the
# ceiling the pipeline is built for, not the one a stranger should be handed.
MAX_IMAGES_PER_REQUEST = _env_int("MAX_IMAGES_PER_REQUEST", 200)
CROP_PADDING_RATIO = 0.25 # 25% margin around bodybndbox

# Output resolution. `None` means "save the crop at its native size and aspect
# ratio", which is what we want: the TensorFlow training code does its own
# resize, so resizing here would resample twice and bake one target resolution
# into every file. Set to an int to force a square resize instead — nothing
# else needs to change.
OUTPUT_SIZE: int | None = None
MIN_SOURCE_WIDTH = 200  # px — pre-filter threshold
MIN_SOURCE_HEIGHT = 200  # px — pre-filter threshold

# The input size the TensorFlow training code resizes to. A crop whose short
# side falls below this can only be upscaled to reach it, which invents detail
# rather than supplying it — so such crops are rejected instead of filed.
#
# This catches what the source pre-filter above cannot: a large photo of a
# small, distant dog passes a 200px source check easily, yet yields a tiny
# crop. Almost all rejections here are small-dog-in-large-frame rather than
# small photos.
#
# The rate is sensitive to CROP_PADDING_RATIO, since padding is what lifts a
# marginal crop over the threshold. Measured on this corpus at the current
# 0.25 padding, ~12% of otherwise good candidates fall here; at 0.10 it was
# closer to 30%.
TRAINING_INPUT_SIZE = 224  # IMG_SIZE in the training notebook
MIN_CROP_SIZE = TRAINING_INPUT_SIZE
DEDUP_HASH_THRESHOLD = 5  # max Hamming distance to consider duplicates

# JPEG quality for processed output. Cropping forces a re-encode, and since
# these files are now the master copy — nothing downsamples them again before
# training — the quality is set high to keep generational loss negligible.
OUTPUT_JPEG_QUALITY = 95

# ── Throughput ────────────────────────────────────────────────────────────────

# A breed folder can hold well over a thousand images while a request asks for a
# hundred. Rather than download the whole folder, the pipeline works in batches
# and tops up only when rejections leave it short of the requested count.
CANDIDATE_OVERSHOOT_RATIO = 1.5  # examine 50% more than requested, to absorb rejections
MIN_BATCH_SIZE = 25  # never issue a batch smaller than this

# Candidates are examined in a fixed pseudo-random order rather than the
# alphabetical order GCS lists them in. Taking the first N alphabetically means
# only ever seeing the head of a folder — 100 images out of 1160 would always
# come from the same opening slice, and if those files are grouped by source or
# scrape batch, that is a systematic bias the dedup stage cannot see, because
# they are not near-duplicates, just samey.
#
# Changing this seed re-samples every breed from scratch. Keep it stable unless
# you mean to.
SAMPLE_SEED = "groom-v1"
# Parallel GCS reads; these are IO-bound, not CPU-bound. Kept at or below the
# underlying urllib3 connection pool size (10) — going higher makes the pool
# discard and reopen connections, which costs more than the extra threads win.
DOWNLOAD_CONCURRENCY = 8

# ── Corpus layout ─────────────────────────────────────────────────────────────

IMAGE_PREFIX = "low-resolution"
ANNOTATION_PREFIX = "low-annotations"
IMAGE_EXTENSIONS = (".jpg", ".jpeg")

# ── Model ─────────────────────────────────────────────────────────────────────

# Served only from the `global` Vertex location — see GOOGLE_CLOUD_LOCATION below.
BREED_MATCH_MODEL = "gemini-3.5-flash"
AGENT_MODEL = "gemini-3.5-flash"
INSPECTION_MODEL = "gemini-3.5-flash"

# ── Image inspection (stage 4) ────────────────────────────────────────────────

# One model call per candidate, so this stage dominates both the cost and the
# wall-clock time of a run. The three knobs below are the ones that matter.

# Images are sent at low media resolution. The judgements being made — is there
# a dog, how many, is it sharp, does the background look like a phone snapshot —
# do not need fine detail, and resolution is billed.
INSPECTION_MEDIA_RESOLUTION = "MEDIA_RESOLUTION_LOW"

# Longest edge, in px, that an image is downscaled to before being sent. Cuts
# upload bytes; the model resamples to its own tile size regardless.
INSPECTION_MAX_EDGE = 512

# Calls run in parallel. Kept modest to stay clear of Vertex rate limits, which
# reject rather than queue.
INSPECTION_CONCURRENCY = 8

# Reasoning effort for inspection. Thinking tokens are billed as *output*, and
# they dominated the bill before this was set: measured at 247 thinking tokens
# against 49 of actual answer per image, so 83% of output spend went on
# deliberation for a perception task that does not need it.
#
# MINIMAL brings output from ~296 to ~33 tokens per image, an 89% cut, with no
# change in verdicts across the full set of criteria — blurred, too dark, no
# dog, several dogs, wrong breed and a clean keep were all still judged
# correctly. Raise it only with a measured reason.
INSPECTION_THINKING_LEVEL = "MINIMAL"

# Transient failures are retried before the candidate is given up on.
INSPECTION_MAX_RETRIES = 2

# An image whose inspection fails after retries is rejected rather than let
# through. Curation should under-deliver with an explanation before it admits
# something nothing ever looked at.
INSPECTION_REJECT_ON_ERROR = True

# ── Environment ───────────────────────────────────────────────────────────────


def _require_env(name: str) -> str:
    """Read a required environment variable or fail loudly at first use."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Required environment variable {name} is not set. "
            "Copy .env.example to .env for local runs, or set it on the "
            "Cloud Run service for deployments."
        )
    return value


def gcs_bucket_name() -> str:
    return _require_env("GCS_BUCKET_NAME")


def drive_output_folder_id() -> str:
    return _require_env("DRIVE_OUTPUT_FOLDER_ID")


# ── Drive credentials ─────────────────────────────────────────────────────────

# Drive writes run as the *user*, not as the service account. A service account
# has no Drive storage quota of its own, so it can create folders (0 bytes) but
# every file upload fails with "Service Accounts do not have storage quota".
# The alternatives Google suggests — shared drives and OAuth delegation — both
# require Google Workspace, so a personal account has to use user credentials.
#
# Everything else (Cloud Storage, Vertex AI) still uses ambient credentials.
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive"]

# Where the authorized-user token lives. Locally it is a file written by
# `scripts/authorize_drive.py`; on Cloud Run the same JSON is injected as an
# environment variable from Secret Manager.
DRIVE_TOKEN_ENV = "GOOGLE_DRIVE_TOKEN_JSON"
DRIVE_TOKEN_PATH_ENV = "GOOGLE_DRIVE_TOKEN_PATH"
DEFAULT_DRIVE_TOKEN_PATH = "drive_token.json"
DEFAULT_OAUTH_CLIENT_PATH = "oauth_client.json"


def drive_token_path() -> str:
    """Local path to the stored Drive token, resolved against the project root."""
    override = os.environ.get(DRIVE_TOKEN_PATH_ENV)
    if override:
        return override
    return str(PROJECT_ROOT / DEFAULT_DRIVE_TOKEN_PATH)


def drive_token_json() -> str | None:
    """Token JSON supplied directly through the environment, if any."""
    return os.environ.get(DRIVE_TOKEN_ENV) or None


def google_cloud_project() -> str:
    return _require_env("GOOGLE_CLOUD_PROJECT")


def google_cloud_location() -> str:
    """Vertex AI location.

    Must be `global` for Gemini 3.5 models — they are not served from regional
    endpoints. This is unrelated to the physical region Cloud Run runs in.
    """
    return os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
