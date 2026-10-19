"""Run a Temporal worker for the slice workflow: ``python -m bidu_workflows.worker``."""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from .activities import ALL
from .workflow import TASK_QUEUE, SliceWorkflow


def make_worker(client: Client, *, max_concurrent: int = 4) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[SliceWorkflow],
        activities=ALL,
        activity_executor=ThreadPoolExecutor(max_concurrent),
        max_concurrent_activities=max_concurrent,
    )


async def main() -> None:
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"), data_converter=pydantic_data_converter
    )
    await make_worker(client).run()


if __name__ == "__main__":
    asyncio.run(main())
