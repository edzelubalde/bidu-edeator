# bidu-edeator

AI-assisted rough-cut editor for vlogs: a folder of raw clips goes in, an evidence-linked story and a
playable rough cut come out, and the creator stays in control of every shot.

This repository follows the build spec (v1.0, 19 Oct 2026). It currently holds the **timeline core and
the first end-to-end slice**, run from the command line. The API and web app come next.

## Run it

Needs Python 3.12+, [uv](https://docs.astral.sh/uv/) and ffmpeg/ffprobe on `PATH`.

```sh
uv sync
uv run bidu slice path/to/shoot --seconds 60          # every stage in this process
uv run bidu slice path/to/shoot --seconds 60 --profile delivery
```

The project state lives in `path/to/shoot/.bidu/` (assets, proxies, analysis runs, edits, renders).
Re-running reuses every finished stage.

To run through Temporal instead, start a dev server (`temporal server start-dev`), then a worker
(`uv run python -m bidu_workflows.worker`), and pass `--temporal localhost:7233`.

### Models

| Stage | Default | To use the real model |
|---|---|---|
| Transcription | off unless installed | `uv sync --extra whisper` (faster-whisper, `BIDU_WHISPER_MODEL`, default `large-v3`) |
| Video understanding | offline heuristic (cuts + transcript, no visual descriptions) | `uv sync --extra gemini` and set `GEMINI_API_KEY` (`BIDU_GEMINI_MODEL` to pin the model) |
| Story planning | offline chronological outline | reasoning-model planner is the next step |

The offline paths exist so the pipeline runs end to end without keys. They never describe what they
cannot see.

## Layout

```
packages/timeline/   exact rational time, the v1 timeline schema, invariants, storyboard edit ops
packages/contracts/  JSON Schemas generated from the models (for the web app's TS types)
workers/media/       probe + stream validation, SDR proxy + time map, segment-cached renderer, output checks
workers/analysis/    windows, scene cuts, transcription and video-model adapters, moments, planner, shot solver
workflows/           Temporal workflow + activities, local project store, `bidu` CLI
evals/fixtures/      synthetic fixtures are generated here (git-ignored)
```

## Tests

```sh
uv run pytest
```

Synthetic fixtures (landscape, portrait with display rotation, variable frame rate, HEVC 10-bit HLG,
no audio, an unsupported codec) are generated on first run. Every frame carries its own index as a
binary block code, so tests decode rendered output and check that each frame came from exactly the
right source frame.
