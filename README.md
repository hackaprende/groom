# Groom
An autonomous agent that curates ML training datasets — sources images, inspects each one with Gemini, deduplicates, preprocesses, and files the keepers.

> Curating a training dataset means reviewing hundreds of images by hand. This agent looks at every photo, rejects what won't help, and files the rest.

Built for the [All Things Agentic Hackathon](https://allthingsagentichackathon.devpost.com/) — **Taskmaster** track.

## The problem

I maintain a dog breed recognition app with an on-device model trained on 117 breeds. Expanding it means more training data, and preparing a batch looks like this: download a few hundred images per breed, open them, delete the ones with no dog, delete the ones with five dogs, delete the blurry ones and the studio shots that look nothing like what users actually photograph, find the near-duplicates, crop and resize what survived, move it into the right folder. Then repeat for the next breed.

Hours of clicking, none of it engineering — and mistakes are invisible. A mislabeled image doesn't crash anything, it just quietly makes the model worse.

## What Groom does

Given a request like `100 Dalmatian images`, the agent runs a multi-stage pipeline without supervision:

1. **Source** — pulls candidates for the requested breed from the Tsinghua Dogs corpus in Cloud Storage
2. **Pre-filter** — drops images below resolution and format thresholds
3. **Deduplicate** — perceptual hashing catches near-identical shots, not just byte-identical files
4. **Inspect** — Gemini evaluates each surviving image against training-quality criteria
5. **Process** — crops and resizes to training specification
6. **File** — writes to the correct breed folder in Google Drive
7. **Report** — summarizes what was kept, what was rejected, and why

### Inspection criteria

- Is there actually a dog in frame?
- Does it match the requested breed? (public datasets contain mislabeled images)
- Is there a single subject? (multiple dogs introduce label ambiguity)
- Is it usable — sharp enough, lit well enough, dog large enough in frame?
- **Does it match deployment conditions?** Users photograph dogs with phones, outdoors, in poor conditions. Studio shots with clean backgrounds are technically good photos and practically bad training data. Groom rejects them on purpose.

The agent decides what gets rejected. The human decides what gets trained — every rejection is logged with its reason.

## Stack

- **Gemini** via Vertex AI — multimodal image inspection
- **Google ADK** — agent orchestration
- **Cloud Run** — autonomous background execution
- **Cloud Storage** — source image corpus
- **Google Drive API** — output destination
- **Python**

## Status

🚧 In active development for the hackathon submission period (August 2026).

## Setup

_Coming soon._

## License

MIT
