"""Write the shared JSON Schemas that the web app's TypeScript types are generated from.

uv run python scripts/gen_contracts.py          # rewrite packages/contracts/schemas
uv run python scripts/gen_contracts.py --check  # fail if they are stale (CI)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import TypeAdapter

from bidu_analysis.models import Moment, TranscriptSegment
from bidu_analysis.planning import Proposal, ShotPlan
from bidu_timeline import EditOp, Timeline

OUT = Path(__file__).resolve().parents[1] / "packages" / "contracts" / "schemas"

SCHEMAS = {
    "timeline.schema.json": TypeAdapter(Timeline),
    "edit-ops.schema.json": TypeAdapter(list[EditOp]),
    "moment.schema.json": TypeAdapter(Moment),
    "transcript-segment.schema.json": TypeAdapter(TranscriptSegment),
    "proposal.schema.json": TypeAdapter(Proposal),
    "shot-plan.schema.json": TypeAdapter(ShotPlan),
}


def render() -> dict[str, str]:
    out = {}
    for name, adapter in SCHEMAS.items():
        schema = adapter.json_schema(by_alias=True)
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://bidu.dev/schemas/{name}"
        out[name] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return out


def main() -> int:
    check = "--check" in sys.argv
    stale = []
    OUT.mkdir(parents=True, exist_ok=True)
    for name, text in render().items():
        path = OUT / name
        if path.exists() and path.read_text() == text:
            continue
        stale.append(name)
        if not check:
            path.write_text(text)
    if check and stale:
        print(f"Stale contracts: {', '.join(stale)}. Run scripts/gen_contracts.py.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
