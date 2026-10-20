"""端到端测试：走完整的 HTTP 接口，模拟前端的操作顺序。

    python dev_e2e.py [userId] [最多下载几个文件]

覆盖：plan -> 轮询 -> start -> 轮询 -> 落盘检查。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8899"
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "_e2e_out"


def wait_job(client: httpx.Client, job_id: str, want: set[str], timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        snapshot = client.get(f"{BASE}/api/job/{job_id}", params={"limit": 5000}).json()
        status = snapshot.get("status")
        if status != last:
            print(f"  status -> {status}")
            last = status
        if status in want:
            return snapshot
        if status == "failed":
            print("  job failed:", snapshot.get("error"))
            return snapshot
        time.sleep(1.0)
    raise TimeoutError(f"等待 {want} 超时，最后状态 {last}")


def main(user_id: int, limit: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30.0) as client:
        print("== 1. 健康检查 ==")
        state = client.get(f"{BASE}/api/state").json()
        print("  loggedIn:", state["loggedIn"])

        print("== 2. 创建计划 ==")
        response = client.post(
            f"{BASE}/api/plan",
            json={
                "userId": user_id,
                "options": {
                    "outputDir": str(OUT),
                    "includeNovel": True,
                    "concurrency": 2,
                    "minIntervalMs": 300,
                    "r18": "all",
                    "maxFiles": limit,
                },
            },
        )
        response.raise_for_status()
        job_id = response.json()["jobId"]
        print("  jobId:", job_id)

        snapshot = wait_job(client, job_id, {"ready"})
        counts = snapshot["counts"]
        print(f"  作品 {snapshot['works']}，文件 {counts['total']}")

        items = snapshot.get("items") or []
        if not items:
            print("  没有可下载的文件，结束")
            return

        print("== 3. 只保留前几个文件，其余标记跳过 ==")
        # 通过接口没法改单项状态，这里直接限制下载数量：先全部标跳过，
        # 再用 retry 机制不适用，所以改为只在一个小集合上验证。
        # 简单做法：把所有 item 状态视为 pending，但下载阶段由服务端并发控制，
        # 这里直接开始下载并在目标目录里检查结果。
        print("== 4. 开始下载 ==")
        client.post(f"{BASE}/api/job/{job_id}/start").raise_for_status()
        snapshot = wait_job(client, job_id, {"done", "failed", "cancelled"}, timeout=600.0)
        counts = snapshot["counts"]
        print(
            f"  完成 {counts['done']}，跳过 {counts['skipped']}，失败 {counts['failed']}，"
            f"共 {counts['total']} 个"
        )
        print("  字节:", snapshot["bytes"])

        print("== 5. 检查落盘 ==")
        files = sorted(p for p in OUT.rglob("*") if p.is_file())
        print(f"  实际文件数 {len(files)}")
        for path in files[:12]:
            rel = path.relative_to(OUT)
            print(f"   {rel}  ({path.stat().st_size} B)")

        failed = [i for i in (snapshot.get("items") or []) if i["status"] == "failed"]
        if failed:
            print("== 失败明细 ==")
            for item in failed[:10]:
                print("  ", item["path"], "|", item["error"])


if __name__ == "__main__":
    uid = int(sys.argv[1]) if len(sys.argv) > 1 else 114430563
    lim = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    main(uid, lim)
