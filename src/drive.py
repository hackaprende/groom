"""Stage 6 — file the processed images into Google Drive.

Unlike every other Google call in this project, Drive runs as the **user**
rather than as the service account. A service account has no Drive storage
quota of its own: it can create folders, which are zero bytes, but every file
upload fails with "Service Accounts do not have storage quota". Google's two
suggested fixes — shared drives and OAuth delegation — both require Google
Workspace, so a personal account has no service-account path to Drive at all.

Credentials therefore come from a stored authorized-user token, produced once
by `scripts/authorize_drive.py`. Locally that is a gitignored file; on Cloud
Run the same JSON arrives in an environment variable from Secret Manager. It
holds a refresh token, so it renews itself without further interaction — but
see the note in that script about tokens expiring while the OAuth consent
screen is in Testing mode.

The destination folder given by `DRIVE_OUTPUT_FOLDER_ID` must be reachable by
that user, which it is by definition when the user owns it.
"""

from __future__ import annotations

import io
import json
import logging
import os
from functools import lru_cache

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from . import config
from .models import ProcessedImage

logger = logging.getLogger(__name__)

_FOLDER_MIME = "application/vnd.google-apps.folder"
_JPEG_MIME = "image/jpeg"

# Shared drives need this on every call; it is harmless on My Drive.
_SHARED_DRIVE_ARGS = {"supportsAllDrives": True}


class DriveError(RuntimeError):
    """A Drive operation failed in a way the caller should report."""


def _stale_token(action: str, exc: RefreshError) -> DriveError:
    """A refresh failure, which surfaces on the first call rather than at build."""
    return DriveError(
        f"{action}: the stored Drive token could not be refreshed ({exc}). "
        "Tokens are revoked when the OAuth consent screen sits in Testing mode "
        "for too long. Re-run `python scripts/authorize_drive.py`."
    )


def _describe(action: str, exc: HttpError) -> DriveError:
    """Turn a Drive HttpError into something that names the likely fix.

    The two failures worth calling out both arrive looking like something
    else: a missing Drive scope reports as 403 "insufficient authentication
    scopes", and a folder that was never shared with the service account
    reports as 404 "not found" rather than as a permission problem.
    """
    status = exc.resp.status if exc.resp is not None else None
    message = str(exc)
    hint = ""

    if "storage quota" in message.lower():
        hint = (
            " This is a service account writing to Drive, which cannot work: "
            "service accounts have no Drive storage quota. Drive must run as a "
            f"user. {_SETUP_HINT}"
        )
    elif status == 403 and "scope" in message.lower():
        hint = f" The stored token lacks the Drive scope. {_SETUP_HINT}"
    elif status in (403, 404):
        hint = (
            " Check that DRIVE_OUTPUT_FOLDER_ID is correct and that the "
            "authorized user can reach that folder — a folder they cannot see "
            "reports as 'not found' rather than as a permission error."
        )

    return DriveError(f"{action}: {exc}.{hint}")


_SETUP_HINT = (
    "Run `python scripts/authorize_drive.py` to authorize Drive access once, "
    "or set "
    f"{config.DRIVE_TOKEN_ENV} to the contents of the token it produces."
)


def _load_credentials() -> Credentials:
    """The stored authorized-user credentials for Drive.

    Prefers the environment variable, so a Cloud Run deployment can be handed
    the token from Secret Manager without a file ever touching the image.
    """
    raw = config.drive_token_json()
    source = f"${config.DRIVE_TOKEN_ENV}"

    if raw is None:
        path = config.drive_token_path()
        if not os.path.exists(path):
            raise DriveError(f"No Drive credentials found at '{path}'. {_SETUP_HINT}")
        source = path
        with open(path, encoding="utf-8") as handle:
            raw = handle.read()

    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DriveError(f"Drive credentials in {source} are not valid JSON: {exc}") from exc

    try:
        credentials = Credentials.from_authorized_user_info(info, config.DRIVE_SCOPES)
    except (ValueError, KeyError) as exc:
        raise DriveError(
            f"Drive credentials in {source} are missing required fields "
            f"({exc}). {_SETUP_HINT}"
        ) from exc

    if not credentials.refresh_token:
        raise DriveError(
            f"The Drive token in {source} has no refresh token, so it cannot "
            f"renew itself. {_SETUP_HINT}"
        )
    return credentials


@lru_cache(maxsize=1)
def _service():  # type: ignore[no-untyped-def]  # googleapiclient builds this dynamically
    """The Drive v3 client, built once per process from the stored user token."""
    try:
        credentials = _load_credentials()
        return build("drive", "v3", credentials=credentials, cache_discovery=False)
    except DriveError:
        raise
    except RefreshError as exc:
        raise DriveError(
            f"The stored Drive token could not be refreshed: {exc}. This happens "
            "when the token is revoked, or when it expires because the OAuth "
            f"consent screen is still in Testing mode. {_SETUP_HINT}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — auth failures vary by environment
        raise DriveError(f"Could not authenticate to Google Drive: {exc}") from exc


def normalize_folder_name(breed: str) -> str:
    """Folder-name form of a breed: lowercase, spaces to underscores."""
    return "_".join(breed.lower().split())


def ensure_breed_folder(breed: str, parent_id: str | None = None) -> tuple[str, str]:
    """Find or create the breed subfolder. Returns `(folder_id, folder_name)`.

    Reusing an existing folder is what makes a repeated request safe to run.
    """
    parent_id = parent_id or config.drive_output_folder_id()
    name = normalize_folder_name(breed)

    existing = _find_child(name, parent_id, mime_type=_FOLDER_MIME)
    if existing is not None:
        logger.info("Reusing existing Drive folder '%s' (%s)", name, existing)
        return existing, name

    try:
        created = (
            _service()
            .files()
            .create(
                body={"name": name, "mimeType": _FOLDER_MIME, "parents": [parent_id]},
                fields="id",
                **_SHARED_DRIVE_ARGS,
            )
            .execute()
        )
    except RefreshError as exc:
        raise _stale_token(f"Could not create Drive folder '{name}'", exc) from exc
    except HttpError as exc:
        raise _describe(f"Could not create Drive folder '{name}' under {parent_id}", exc) from exc

    logger.info("Created Drive folder '%s' (%s)", name, created["id"])
    return created["id"], name


def _find_child(name: str, parent_id: str, mime_type: str | None = None) -> str | None:
    """The id of a child of `parent_id` with this exact name, if one exists."""
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = f"name = '{escaped}' and '{parent_id}' in parents and trashed = false"
    if mime_type:
        query += f" and mimeType = '{mime_type}'"

    try:
        response = (
            _service()
            .files()
            .list(
                q=query,
                fields="files(id)",
                pageSize=1,
                includeItemsFromAllDrives=True,
                **_SHARED_DRIVE_ARGS,
            )
            .execute()
        )
    except RefreshError as exc:
        raise _stale_token(f"Could not search Drive folder {parent_id}", exc) from exc
    except HttpError as exc:
        raise _describe(f"Could not search Drive folder {parent_id}", exc) from exc

    files = response.get("files", [])
    return files[0]["id"] if files else None


def upload_image(image: ProcessedImage, folder_id: str) -> str:
    """Upload one processed image, replacing any file of the same name.

    Replacing rather than appending keeps a re-run of the same request from
    accumulating duplicate copies in the folder.
    """
    media = MediaIoBaseUpload(
        io.BytesIO(image.image_bytes), mimetype=_JPEG_MIME, resumable=False
    )

    try:
        existing_id = _find_child(image.filename, folder_id)
        if existing_id is not None:
            result = (
                _service()
                .files()
                .update(fileId=existing_id, media_body=media, fields="id", **_SHARED_DRIVE_ARGS)
                .execute()
            )
        else:
            result = (
                _service()
                .files()
                .create(
                    body={"name": image.filename, "parents": [folder_id]},
                    media_body=media,
                    fields="id",
                    **_SHARED_DRIVE_ARGS,
                )
                .execute()
            )
    except RefreshError as exc:
        raise _stale_token(f"Could not upload {image.filename}", exc) from exc
    except HttpError as exc:
        raise _describe(f"Could not upload {image.filename}", exc) from exc

    return result["id"]


def upload_all(
    images: list[ProcessedImage], folder_id: str
) -> tuple[int, list[tuple[str, str]]]:
    """Upload a batch. Returns the count uploaded and `(filename, reason)` failures.

    A failure partway through does not abort the rest: the run reports what it
    managed to file rather than losing the work already done.
    """
    uploaded = 0
    failures: list[tuple[str, str]] = []

    for image in images:
        try:
            upload_image(image, folder_id)
            uploaded += 1
        except DriveError as exc:
            logger.warning("Upload failed for %s: %s", image.filename, exc)
            failures.append((image.filename, str(exc)))

    logger.info("Uploaded %d/%d images", uploaded, len(images))
    return uploaded, failures
