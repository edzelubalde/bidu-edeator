"""Command line for local runs.

bidu slice ./my-shoot --seconds 60            # run every stage in this process
bidu slice ./my-shoot --temporal localhost:7233  # run through a Temporal worker
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from . import stages
from .store import ProjectStore


def _print(obj) -> None:
    print(json.dumps(obj.model_dump() if hasattr(obj, "model_dump") else obj, indent=2, default=str))


def run_local(store: ProjectStore, asset_ids: list[str], seconds: int, profile: str) -> int:
    ingested = []
    for a in asset_ids:
        r = stages.ingest(store, a)
        ingested.append(r)
        label = store.load_asset(a).label
        print(
            f"ingest  {label}: {'ok' if r.ok else r.error['message']}{' (reused)' if r.reused else ''}",
            file=sys.stderr,
        )
    ready = [r.asset_id for r in ingested if r.ok]
    if not ready:
        print("No usable files.", file=sys.stderr)
        return 1
    for a in ready:
        r = stages.analyze(store, a)
        print(
            f"analyze {store.load_asset(a).label}: {r.moments} moments, "
            f"{r.transcript_segments} transcript segments{' (reused)' if r.reused else ''}",
            file=sys.stderr,
        )
    plan = stages.plan(store, ready, seconds)
    for note in plan.gaps + plan.conflicts:
        print(f"note    {note}", file=sys.stderr)
    print(f"plan    {plan.shots} shots, {plan.total_ms / 1000:.1f}s -> {plan.edit_id}", file=sys.stderr)
    out = stages.render_edit(store, plan.edit_id, profile)
    print(
        f"render  {out.output} ({out.segments_rendered} new segments, {out.segments_reused} reused)",
        file=sys.stderr,
    )
    _print({"plan": plan.model_dump(), "render": out.model_dump()})
    return 0 if out.ok else 2


async def run_temporal(
    address: str, store: ProjectStore, asset_ids: list[str], seconds: int, profile: str
) -> int:
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter

    from .workflow import TASK_QUEUE, SliceInput, SliceResult, SliceWorkflow

    client = await Client.connect(address, data_converter=pydantic_data_converter)
    result: SliceResult = await client.execute_workflow(
        SliceWorkflow.run,
        SliceInput(
            project_dir=str(store.root.resolve()),
            asset_ids=asset_ids,
            target_seconds=seconds,
            profile=profile,
        ),
        id=f"slice-{uuid.uuid4().hex[:8]}",
        task_queue=TASK_QUEUE,
    )
    _print(result)
    return 0 if result.status == "completed" else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bidu")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("slice", help="Turn a folder of clips into a rough cut")
    s.add_argument("folder", type=Path)
    s.add_argument("--seconds", type=int, default=60, help="Target length (default 60)")
    s.add_argument("--project", type=Path, help="Project directory (default: <folder>/.bidu)")
    s.add_argument("--profile", choices=["preview", "delivery"], default="preview")
    s.add_argument("--temporal", metavar="ADDRESS", help="Run through Temporal at this address")
    args = parser.parse_args(argv)

    store = ProjectStore(args.project or args.folder / ".bidu")
    assets = stages.register_folder(store, args.folder)
    ids = [a.id for a in assets]
    if args.temporal:
        return asyncio.run(run_temporal(args.temporal, store, ids, args.seconds, args.profile))
    return run_local(store, ids, args.seconds, args.profile)


if __name__ == "__main__":
    sys.exit(main())
