"""Cloud Storage access for the source corpus.

The corpus is two parallel trees in one bucket::

    low-resolution/{number}-{id}-{breed}/n123456.jpg
    low-annotations/{number}-{id}-{breed}/n123456.jpg.xml

Network calls fail. Every public function here raises `StorageError` with a
message a human can act on, so the orchestration can report a partial run
instead of surfacing a raw client traceback.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from google.api_core import exceptions as gcp_exceptions
from google.cloud import storage

from src import config
from src.models import Candidate

logger = logging.getLogger(__name__)


class StorageError(RuntimeError):
    """A Cloud Storage operation failed in a way the caller should report."""


@lru_cache(maxsize=1)
def _client() -> storage.Client:
    """The GCS client, built once per process from ambient credentials."""
    try:
        return storage.Client(project=config.google_cloud_project())
    except Exception as exc:  # noqa: BLE001 — surface any auth/config failure uniformly
        raise StorageError(f"Could not create a Cloud Storage client: {exc}") from exc


def warm_client() -> None:
    """Build the client on the calling thread, before any pool is started.

    `lru_cache` protects its cache but not the call that populates it, so
    concurrent first-callers each build a client and all but one is discarded
    and closed — leaving whichever thread held a discarded one to fail.
    """
    _client()


@lru_cache(maxsize=1)
def list_breed_folders() -> tuple[str, ...]:
    """Every breed folder name under `low-resolution/`.

    Cached for the process lifetime: the corpus does not change between
    requests, and this is on the path of every single run.

    Returns bare folder names, e.g. ``1324-n000004-malamute``.
    """
    prefix = f"{config.IMAGE_PREFIX}/"
    try:
        # delimiter="/" makes GCS return common prefixes (i.e. folders) instead
        # of walking every object in the corpus.
        iterator = _client().list_blobs(
            config.gcs_bucket_name(), prefix=prefix, delimiter="/"
        )
        # `prefixes` is only populated once the iterator has been consumed.
        for _ in iterator:
            pass
        folders = sorted(p[len(prefix) :].rstrip("/") for p in iterator.prefixes)
    except gcp_exceptions.GoogleAPIError as exc:
        raise StorageError(f"Could not list breed folders: {exc}") from exc

    if not folders:
        raise StorageError(
            f"No breed folders found under gs://{config.gcs_bucket_name()}/{prefix}"
        )
    logger.info("Found %d breed folders in the corpus", len(folders))
    return tuple(folders)


def list_candidates(folder: str) -> list[Candidate]:
    """Every image in one breed folder, as unpopulated `Candidate`s."""
    prefix = f"{config.IMAGE_PREFIX}/{folder}/"
    try:
        blobs = _client().list_blobs(config.gcs_bucket_name(), prefix=prefix)
        candidates = [
            Candidate(blob_name=blob.name, filename=blob.name.rsplit("/", 1)[-1])
            for blob in blobs
            if blob.name.lower().endswith(config.IMAGE_EXTENSIONS)
        ]
    except gcp_exceptions.GoogleAPIError as exc:
        raise StorageError(f"Could not list images in '{folder}': {exc}") from exc

    logger.info("Folder '%s' holds %d images", folder, len(candidates))
    return candidates


def download_bytes(blob_name: str) -> bytes:
    """Download one object. Raises `StorageError` if it is missing or unreadable."""
    try:
        bucket = _client().bucket(config.gcs_bucket_name())
        return bucket.blob(blob_name).download_as_bytes()
    except gcp_exceptions.NotFound as exc:
        raise StorageError(f"Object not found: {blob_name}") from exc
    except gcp_exceptions.GoogleAPIError as exc:
        raise StorageError(f"Could not download {blob_name}: {exc}") from exc


def download_annotation(folder: str, image_filename: str) -> bytes | None:
    """The annotation XML for an image, or None when it has none.

    A missing annotation is normal in this corpus and must not be an error —
    the caller falls back to a centre crop.
    """
    blob_name = f"{config.ANNOTATION_PREFIX}/{folder}/{image_filename}.xml"
    try:
        bucket = _client().bucket(config.gcs_bucket_name())
        blob = bucket.blob(blob_name)
        return blob.download_as_bytes()
    except gcp_exceptions.NotFound:
        logger.debug("No annotation for %s", image_filename)
        return None
    except gcp_exceptions.GoogleAPIError as exc:
        # Treat a transient read failure the same as a missing annotation: the
        # image is still usable via fallback crop, and the report notes it.
        logger.warning("Could not read annotation for %s: %s", image_filename, exc)
        return None
