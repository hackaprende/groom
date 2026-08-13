"""One-time Drive authorization for Groom.

Run this once locally. It opens a browser, asks you to grant Drive access, and
writes an authorized-user token that the agent reuses from then on — including
after a redeploy, because the token carries a refresh token.

    python scripts/authorize_drive.py

Why this exists at all: Drive is the one Google API here that cannot run as the
service account. Service accounts have no Drive storage quota, so uploads fail
even when every permission is correct. See `src/drive.py` for the full note.

Setup in the Cloud console, once, before running this:

  1. APIs & Services > OAuth consent screen
       - User type: External, publishing status: Testing
       - Add your own Google account under "Test users"
  2. APIs & Services > Credentials > Create credentials > OAuth client ID
       - Application type: Desktop app
       - Download the JSON as `oauth_client.json` in the project root

Both files this touches are gitignored. Neither is a service account key.

NOTE ON EXPIRY: while the consent screen is in Testing mode, Google expires
refresh tokens after seven days. If Drive uploads start failing with a refresh
error, re-run this script. Publishing the app removes the limit but requires
Google's verification review for the Drive scope, which is not worth it for a
private tool.
"""

from __future__ import annotations

import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import config  # noqa: E402  — needs the path above


def main() -> int:
    client_path = os.environ.get("GOOGLE_OAUTH_CLIENT_PATH", config.DEFAULT_OAUTH_CLIENT_PATH)
    token_path = config.drive_token_path()

    if not os.path.exists(client_path):
        print(
            f"Missing OAuth client file '{client_path}'.\n\n"
            "Create one in the Cloud console under APIs & Services > Credentials\n"
            "> Create credentials > OAuth client ID > Desktop app, then download\n"
            f"it to '{client_path}'. See the docstring at the top of this file.",
            file=sys.stderr,
        )
        return 1

    flow = InstalledAppFlow.from_client_secrets_file(client_path, config.DRIVE_SCOPES)
    # `offline` is what makes Google return a refresh token; without it the
    # credential dies in an hour and nothing unattended can work.
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    with open(token_path, "w", encoding="utf-8") as handle:
        handle.write(credentials.to_json())
    os.chmod(token_path, 0o600)

    print(f"\nAuthorized. Token written to '{token_path}'.")
    print("This file is gitignored — do not commit it.\n")
    print("For Cloud Run, store the same JSON in Secret Manager and expose it as")
    print(f"the {config.DRIVE_TOKEN_ENV} environment variable. See commands.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
