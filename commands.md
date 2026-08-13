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
export AGENT_PATH="./my_agent"
export SERVICE_NAME="adk-tutorial"
export APP_NAME="adk_tutorial_app"

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

Local `.env` values do **not** travel into the container. Set them explicitly:

```bash
gcloud run services update adk-tutorial \
  --region=us-central1 \
  --set-env-vars=GOOGLE_GENAI_USE_ENTERPRISE=1,GOOGLE_CLOUD_PROJECT=groom-todogs,GOOGLE_CLOUD_LOCATION=global
```

This creates a new revision and routes traffic to it. No redeploy needed.

---

## Cloud Run inspection

```bash
gcloud run services list
gcloud run services describe <service-name> --region=us-central1
gcloud run revisions list --service=<service-name> --region=us-central1
gcloud run services logs read <service-name> --region=us-central1 --limit=50
```

---

## Notes worth remembering

**Cloud Run region ≠ Vertex AI location.** Cloud Run requires a real physical region and rejects `global` outright. The newest Gemini models are served *only* from `global`. Every 404 during setup traced back to conflating these two.

**A Vertex `404 NOT_FOUND` is ambiguous.** The same message covers a wrong model name, missing project access, and a model unavailable in that region. Test with `curl` before chasing model identifiers.

**Secret Manager is for the AI Studio API key path.** On the Vertex + service account path there is no key to store. Skip those steps in the ADK docs.

**`GOOGLE_GENAI_USE_VERTEXAI` was renamed to `GOOGLE_GENAI_USE_ENTERPRISE`.** ADK moves fast — trust the current docs over anything remembered, including from an AI assistant.

**Drive folders must be shared with the service account's email** (as editor) before the agent can write to them. Enabling the Drive API is not enough.

---

## How to find commands you don't know

```bash
gcloud <service> --help
gcloud <service> <resource> --help
gcloud <service> <resource> list      # the safest command in existence — read-only
```

The shape is always `gcloud <service> <resource> <verb> <flags>`. Tab completion walks you through what exists at each level.
