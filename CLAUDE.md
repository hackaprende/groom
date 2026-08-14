# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Groom is an ADK agent that curates dog-image training datasets: it matches a
requested breed to a folder in a Cloud Storage corpus, filters and deduplicates
candidates, has Gemini inspect each image for training quality, crops to the
annotated dog, and files the results to Google Drive.

Its output feeds **Todogs**, a separate Android/iOS breed-recognition app with an
on-device TFLite model. Several decisions below only make sense in light of that
consumer.

## Commands

```bash
source .venv/bin/activate
adk web                  # chat UI on :8000 — pick the "groom" agent
adk run ./src            # same agent, terminal

python scripts/authorize_drive.py    # re-authorize Drive (see token expiry below)
```

Run the pipeline without the agent, which is how most work gets verified:

```bash
PYTHONPATH=. .venv/bin/python -c "
from src.pipeline import run_pipeline
from src import report
r = run_pipeline('Siberian Husky', 10, '1160-n000003-Siberian_husky')
print(report.render_text(r))
"
```

Every stage is importable and runnable on its own — `prefilter.prefilter()`,
`dedup.deduplicate()`, `inspection.inspect()`, `processing.process_candidate()`,
`breed_match.match_breed(breed, folders)` (pass `folders` to skip GCS).

**There is no test suite.** Verification has been done by running stages against
the live bucket and inspecting the output. When changing image handling, check
the result visually and against constructed failure cases — see "Verification"
below.

## Architecture

`agent.py` defines the ADK agent and its tools; `pipeline.py` holds the
orchestration, deliberately separate so the pipeline runs without a model
session. One module per stage, with explicit dataclasses in `models.py` rather
than dicts.

Stage order, and why it is this order: pre-filter and dedup are cheap and run
first; **inspection (stage 4) runs on the full image before cropping**, because
the annotation's bounding box frames one dog and cropping first would hide the
second and third dog in frame; cropping and upload come last.

The pipeline fetches candidates in **batches with a carry-over pool**. Survivors
a batch does not need stay in `ready` for the next iteration instead of being
discarded — without that, top-up rounds re-download replacements for images just
thrown away (measured: 113 downloads to file 25, versus 38 with the pool).
Inspection runs *after* the batch is trimmed to what is still needed, so surplus
candidates are downloaded but never sent to Gemini.

**Two identities, and this is not incidental.** Cloud Storage and Vertex AI use
the service account from ambient credentials. Drive runs as the *user* via a
stored OAuth token, because a service account has no Drive storage quota and
cannot upload files at all — it creates folders (zero bytes) and then fails
every upload. No service account key file exists anywhere; do not introduce one.

## Decisions that were measured — do not silently revert

Each of these overturned an earlier design. Reverting one on plausible-sounding
reasoning would undo real work.

- **No resize, native aspect ratio** (`OUTPUT_SIZE = None`). The TensorFlow
  training code resizes to 224 itself; resizing here would resample twice. It
  also previously forced non-square crops into a square, which letterboxed
  44–63% of images with black bars.
- **`CROP_PADDING_RATIO = 0.25` is deliberately loose.** Todogs users photograph
  dogs with background around them, and the model must see the same. Do not
  tighten it because crops "contain too much background" — background is the
  point.
- **`MIN_CROP_SIZE` gates the crop, not the source.** A large photo of a small,
  distant dog clears a 200px source check and still yields a 180px crop. Keep it
  tied to `TRAINING_INPUT_SIZE` (224, the training code's `IMG_SIZE`).
- **Sampling is by hash of the filename, not `random.shuffle`.** A shuffle
  depends on the whole list, so adding images to a folder later reorders
  everything and a repeated request selects a different set. Alphabetical order
  is worse still — it only ever sees the head of a folder.
- **The agent stops rather than guesses.** No match means stop; ambiguous means
  ask. A wrong breed silently poisons a training set and nothing downstream
  detects it. Requests over `MAX_IMAGES_PER_REQUEST` are rejected, never clamped.
- **Report arithmetic must reconcile**: `kept + len(rejections) + unused_surplus
  == candidates_examined`. `unused_surplus` exists for exactly this.
- **`tf.image.resize` squashes**, and both Todogs apps squash identically, so
  training and inference agree. Do not propose aspect-preserving preprocessing
  without a plan to change the training code and both apps together.

## Gotchas that have cost time

- **`lru_cache` guards its cache but not the call that fills it.** Concurrent
  first-callers each build a client, all but one is discarded and closed, and a
  thread holding a discarded one fails with "client has been closed". Build
  clients before starting a pool — `storage.warm_client()`, `inspection._client()`.
- **The Drive OAuth token expires every 7 days** while the consent screen is in
  Testing mode. Refresh errors here mean re-running `scripts/authorize_drive.py`,
  not a code bug. On Cloud Run, push the new token as a Secret Manager version.
- **`GOOGLE_CLOUD_LOCATION` must be `global`** — Gemini 3.5 is not served from
  regional endpoints. This is unrelated to the Cloud Run region (`us-central1`).
- **`load_dotenv` is anchored to `PROJECT_ROOT`**, not the working directory, so
  scripts run from elsewhere still find `.env`. Keep it that way.
- **The git remote uses an SSH alias**, `git@github-hackaprende:...`. The default
  `github.com` host authenticates as a different account and the push is denied.
- `adk web` writes session state to `src/.adk/` (gitignored).

## Verification

Image-handling changes need visual checks and constructed failure cases, because
a clean corpus and a broken filter look identical from the counts alone. When the
inspection prompt was first written, only "wrong breed" ever fired on real
images; the other criteria were confirmed by feeding it a heavily blurred image,
a near-black one, a two-dog composite, and a crop of empty background.

The same prompt also rejected a good photo — a dog against a plain painted wall —
as a "studio shot". Vague criteria in a prompt do not fail loudly; they quietly
discard good data. Check what a filter rejects, not just that it rejects.

## Language

Code, comments, commit messages and documentation are in English. Jesus writes in
Spanish; when replying in Spanish, keep UI labels and console navigation paths in
English, since his Google Cloud console is in English.
