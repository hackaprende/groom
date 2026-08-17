# Commands reference

Google Cloud commands used to set up and operate Groom.

**Project:** `groom-todogs`
**Bucket:** `groom-todogs-tsinghua`
**Service account:** `groom-agent@groom-todogs.iam.gserviceaccount.com`
**Cloud Run region:** `us-central1`
**Vertex AI location:** `global` ← *not the same thing, see notes*

---

## Authentication & project

```bash
# Authenticate the gcloud CLI itself
gcloud auth login

# Authenticate application code running locally (separate from the above — you need both)
gcloud auth application-default login

# Avoid "quota project" warnings from local code
gcloud auth application-default set-quota-project groom-todogs

# Set / check the active project
gcloud config set project groom-todogs
gcloud config get-value project
```

### Drive access — the service account cannot do it

**A service account can never write files to Drive from a personal Google
account.** It has no Drive storage quota, so it creates folders fine (0 bytes)
and then fails every upload with:

```
Service Accounts do not have storage quota. Leverage shared drives, or use OAuth delegation
```

Both fixes Google names there need Google Workspace. Two dead ends worth
recording so they are not retried:

```bash
# DEAD END 1 — you cannot add the Drive scope to a user ADC login.
# Drive is a *restricted* scope and gcloud's OAuth client is not verified for
# it, so Google blocks the login outright: "Se bloqueó esta app".
gcloud auth application-default login --scopes=...auth/drive

# DEAD END 2 — impersonating the service account authenticates fine, but hits
# the same quota wall, because the identity doing the upload is still the SA.
gcloud auth application-default login \
  --impersonate-service-account=groom-agent@groom-todogs.iam.gserviceaccount.com
```

Drive therefore runs as **your user account**, via a stored OAuth token.
Everything else — Cloud Storage, Vertex AI — still uses the service account.

Console setup, once:

1. *APIs & Services > OAuth consent screen* — External, publishing status
   **Testing**, and add your own account under **Test users**
2. *APIs & Services > Credentials > Create credentials > OAuth client ID* —
   **Desktop app**, download the JSON as `oauth_client.json` in the project root

Then:

```bash
python scripts/authorize_drive.py      # opens a browser, writes drive_token.json
```

`oauth_client.json` and `drive_token.json` are both gitignored. Neither is a
service account key.

**Tokens expire after 7 days while the consent screen is in Testing mode.** If
uploads start failing with a refresh error, re-run the script. Publishing the
app removes the limit but triggers Google's verification review for the Drive
scope.

### Drive token on Cloud Run

The token file does not travel into the container. Put it in Secret Manager and
hand it to the service as an environment variable:

```bash
gcloud secrets create groom-drive-token --data-file=drive_token.json
gcloud secrets add-iam-policy-binding groom-drive-token \
  --member="serviceAccount:groom-agent@groom-todogs.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud run services update groom --region=us-central1 \
  --set-secrets=GOOGLE_DRIVE_TOKEN_JSON=groom-drive-token:latest

# After re-running authorize_drive.py, push the new token as a new version
gcloud secrets versions add groom-drive-token --data-file=drive_token.json
```

### Switching between projects

```bash
# Named configurations bundle project + account + region
gcloud config configurations create groom
gcloud config configurations activate groom
gcloud config configurations list

# Or override per-command without touching global state
gcloud <command> --project=other-project
```

---

## APIs

```bash
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  storage.googleapis.com \
  drive.googleapis.com \
  cloudbuild.googleapis.com

gcloud services list --enabled
```

---

## Service accounts & IAM

```bash
# Create the service account
gcloud iam service-accounts create groom-agent \
  --display-name="Groom Agent"

gcloud iam service-accounts list

# Grant Vertex AI access (the agent calls Gemini)
gcloud projects add-iam-policy-binding groom-todogs \
  --member="serviceAccount:groom-agent@groom-todogs.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"

# Grant read access to the source bucket
gcloud projects add-iam-policy-binding groom-todogs \
  --member="serviceAccount:groom-agent@groom-todogs.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

# Allow Cloud Build to build images (uses the DEFAULT compute account, not groom-agent)
gcloud projects add-iam-policy-binding groom-todogs \
  --member="serviceAccount:717368070959-compute@developer.gserviceaccount.com" \
  --role="roles/cloudbuild.builds.builder"
```

**Do not create service account key files.** On Cloud Run, credentials are provided by the environment. A key JSON on disk is a credential waiting to be committed by accident.

---

## Cloud Storage

```bash
# Create the bucket
gcloud storage buckets create gs://groom-todogs-tsinghua \
  --project=groom-todogs \
  --location=us-central1 \
  --uniform-bucket-level-access

# Upload a breed folder (images AND annotations — both trees are needed)
gcloud storage cp -r "low-resolution/<breed-folder>"  gs://groom-todogs-tsinghua/low-resolution/
gcloud storage cp -r "low-annotations/<breed-folder>" gs://groom-todogs-tsinghua/low-annotations/

# List
gcloud storage ls gs://groom-todogs-tsinghua/low-resolution/
gcloud storage ls gs://groom-todogs-tsinghua/low-resolution/<breed-folder>/

# Count files in a folder
gcloud storage ls gs://groom-todogs-tsinghua/low-resolution/<breed-folder>/ | wc -l
```

---

## Vertex AI / Gemini

```bash
# Call a model directly, bypassing any framework.
# The fastest way to tell whether a problem is your code or your configuration.
curl -X POST \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  "https://aiplatform.googleapis.com/v1/projects/groom-todogs/locations/global/publishers/google/models/gemini-3.5-flash:generateContent" \
  -d '{"contents":[{"role":"user","parts":[{"text":"hola"}]}]}'
```

Note the host has **no region prefix** when using `locations/global`. Regional endpoints look like `https://us-central1-aiplatform.googleapis.com/...`.

`gcloud ai models list` does **not** list Gemini — it lists models you uploaded to your own project. Zero results there says nothing about Gemini availability.

---

## ADK deployment

```bash
# Convenience variables (session-scoped — they vanish when the terminal closes)
export GOOGLE_CLOUD_PROJECT="groom-todogs"
export GOOGLE_CLOUD_LOCATION="global"        # Vertex AI — where the model lives
export CLOUD_RUN_REGION="us-central1"        # Cloud Run — where the service lives
export AGENT_PATH="./src"
export SERVICE_NAME="groom"
export APP_NAME="groom"

# Deploy
adk deploy cloud_run \
  --project=$GOOGLE_CLOUD_PROJECT \
  --region=$CLOUD_RUN_REGION \
  --service_name=$SERVICE_NAME \
  --app_name=$APP_NAME \
  --with_ui \
  $AGENT_PATH
```

`--with_ui` serves a browser chat interface — useful for demos and screen recordings.

### Environment variables on the deployed service

**`adk deploy` wipes every environment variable, the secret and the service
account.** Every redeploy of the image needs this block reapplied, or the service
comes up with no configuration and no Drive token and fails at the first
request. Run it as one command so it lands in a single revision:

```bash
gcloud run services update groom --region=us-central1 \
  --service-account=groom-agent@groom-todogs.iam.gserviceaccount.com \
  --max-instances=2 \
  --set-env-vars=GOOGLE_GENAI_USE_ENTERPRISE=1,GOOGLE_CLOUD_PROJECT=groom-todogs,GOOGLE_CLOUD_LOCATION=global,GCS_BUCKET_NAME=groom-todogs-tsinghua,DRIVE_OUTPUT_FOLDER_ID=1SqHiDKAWYOmtXP78QMDj_h-ixpugm6k8,MAX_IMAGES_PER_REQUEST=15 \
  --set-secrets=GOOGLE_DRIVE_TOKEN_JSON=groom-drive-token:latest
```

This creates a new revision and routes traffic to it. No redeploy needed.

`MAX_IMAGES_PER_REQUEST` overrides the 200 in `config.py`. The code keeps the
design ceiling; the deployed service runs lower because it is publicly reachable
and every image costs a Gemini call and a write to a personal Drive.

Check what is actually set:

```bash
gcloud run services describe groom --region=us-central1 \
  --format="value(spec.template.spec.containers[0].env)" | tr ';' '\n'
```

### Public access, on and off

Private by default — the browser gets `403 Forbidden`. Open it only while it has
to be reachable (recording, judging) and close it afterwards.

```bash
# Open
gcloud run services add-iam-policy-binding groom --region=us-central1 \
  --member="allUsers" --role="roles/run.invoker"

# Close
gcloud run services remove-iam-policy-binding groom --region=us-central1 \
  --member="allUsers" --role="roles/run.invoker"

# Which is it right now?
gcloud run services get-iam-policy groom --region=us-central1 | grep -q allUsers \
  && echo public || echo private
```

Verify from outside, unauthenticated — `200` means open, `403` means not:

```bash
curl -s -o /dev/null -w "%{http_code}\n" -L https://groom-717368070959.us-central1.run.app/
```

To check the service is alive **without spending anything**, ask for more images
than the ceiling. It is rejected at validation and never reaches Gemini.

---

## Cloud Run inspection

```bash
gcloud run services list
gcloud run services describe <service-name> --region=us-central1
gcloud run revisions list --service=<service-name> --region=us-central1
gcloud run services logs read <service-name> --region=us-central1 --limit=50
```

---

## Git remote

This repo pushes through an SSH alias, not `github.com` directly:

```bash
git remote -v            # git@github-hackaprende:hackaprende/groom.git
```

Two GitHub accounts share this machine and an SSH key can only belong to one.
The default `github.com` host resolves to the other account's key, and the push
is denied with *"Permission to hackaprende/groom.git denied to ElHanSolo"*. The
alias in `~/.ssh/config` picks the right key.

```bash
# Check which account a host authenticates as
ssh -T git@github-hackaprende          # -> Hi hackaprende!
ssh -T git@github.com                  # -> Hi ElHanSolo!

# Clone another hackaprende repo
git clone git@github-hackaprende:hackaprende/<repo>.git
```

---

## Notes worth remembering

**Cloud Run region ≠ Vertex AI location.** Cloud Run requires a real physical region and rejects `global` outright. The newest Gemini models are served *only* from `global`. Every 404 during setup traced back to conflating these two.

**A Vertex `404 NOT_FOUND` is ambiguous.** The same message covers a wrong model name, missing project access, and a model unavailable in that region. Test with `curl` before chasing model identifiers.

**`adk deploy` resets the service configuration.** Environment variables, the
mounted secret and the service account are all wiped on every image redeploy.
Reapply the block under *Environment variables* or the agent comes up unable to
reach anything.

**`adk deploy` only installs a `requirements.txt` found inside the agent
directory**, not the project root — `os.path.join(agent_src_path, 'requirements.txt')`.
The canonical list lives in `src/requirements.txt`; the root file points at it
with `-r`. A container built without it fails at import with *"cannot import name
'storage' from 'google.cloud'"*.

**Intra-package imports must be relative.** `adk deploy` copies the agent folder
into the image under the app name, so `src` does not exist there and
`from src.agent import ...` raises `ModuleNotFoundError`. Absolute imports only
ever worked locally because the project root happened to be on `sys.path`.

**Secret Manager is for the AI Studio API key path.** On the Vertex + service account path there is no key to store. Skip those steps in the ADK docs.

**`GOOGLE_GENAI_USE_VERTEXAI` was renamed to `GOOGLE_GENAI_USE_ENTERPRISE`.** ADK moves fast — trust the current docs over anything remembered, including from an AI assistant.

**Drive is the one API the service account cannot use.** Service accounts have no Drive storage quota, so uploads fail no matter how the folder is shared. Drive runs as your user account through a stored OAuth token; everything else uses the service account. See *Drive access* above — it also lists the two approaches that look like they should work and don't.

**A Drive folder you cannot see reports as `404 not found`**, not as a permission error. The id looks wrong when access is what's missing.

**Secret Manager is needed after all** — not for an AI Studio API key, but to carry the Drive OAuth token into Cloud Run.

---

## How to find commands you don't know

```bash
gcloud <service> --help
gcloud <service> <resource> --help
gcloud <service> <resource> list      # the safest command in existence — read-only
```

The shape is always `gcloud <service> <resource> <verb> <flags>`. Tab completion walks you through what exists at each level.
