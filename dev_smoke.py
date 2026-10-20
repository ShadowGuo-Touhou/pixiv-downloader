"""开发用冒烟脚本：不经过 GUI，直接验证 plan 流程。

    python dev_smoke.py [userId]

只抓清单，不下载，用来核对命名规则与接口字段。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pixiv_dl.api import PixivClient  # noqa: E402
from pixiv_dl.downloader import Job, Options  # noqa: E402

DEFAULT_USER = 114430563


async def main(user_id: int) -> None:
    out = ROOT / "_smoke_out"
    options = Options(output_dir=out, include_novel=True, concurrency=2, min_interval=0.5)
    job = Job(job_id="smoke", user_id=user_id, options=options)

    events: list[dict] = []
    queue = job.subscribe()

    async def drain() -> None:
        while True:
            event = await queue.get()
            events.append(event)
            if event.get("type") in ("planned", "error"):
                return

    drainer = asyncio.create_task(drain())

    async with PixivClient("", min_interval=0.5) as client:
        try:
            await job.plan(client)
        except Exception as exc:  # noqa: BLE001
            print("PLAN FAILED:", exc)

    try:
        await asyncio.wait_for(drainer, timeout=5)
    except asyncio.TimeoutError:
        drainer.cancel()

    print(f"status={job.status} works={job.works_total} items={len(job.items)}")
    if job.error:
        print("error:", job.error)

    kinds: dict[str, int] = {}
    for item in job.items:
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    print("kinds:", kinds)

    for item in job.items[:24]:
        print(
            f"  {item.date_str} | {item.title[:22]:22s} | {item.kind:6s} | "
            f"p{item.page_index + 1}/{item.page_count} | {item.path.name}"
        )

    for event in events:
        if event.get("type") == "error":
            print("EVENT ERROR:", event.get("message"))


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_USER
    asyncio.run(main(target))
