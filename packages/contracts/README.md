# Contracts

JSON Schemas shared by the Python services and the web app. They are generated from the
Pydantic models (the timeline package and the analysis records), never edited by hand:

    uv run python scripts/gen_contracts.py

CI runs the same script with `--check` and fails if a model changed without regenerating.
The web app will generate its TypeScript types from these files with `json-schema-to-typescript`.
