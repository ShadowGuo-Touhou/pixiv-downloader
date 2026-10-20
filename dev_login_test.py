"""验证：登录窗口被关掉之后，再点一次登录能重新拉起来。

    python dev_login_test.py [base_url]

会真实启动一次 Chrome，然后结束它，模拟「用户自己把窗口关了」。
"""

from __future__ import annotations

import subprocess
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"


def status(client: httpx.Client) -> dict:
    return client.get(BASE + "/api/login/status", timeout=20).json()


def kill_profile_browser() -> str:
    """只杀掉用本项目 profile 启起来的浏览器，不碰用户自己的 Chrome。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
        "Where-Object { $_.CommandLine -like '*profile\\pixiv*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
        "Write-Output $_.ProcessId }"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    return result.stdout.strip()


def wait_status(client: httpx.Client, wanted: set[str], timeout: float = 25.0) -> dict:
    deadline = time.time() + timeout
    snapshot = status(client)
    while time.time() < deadline:
        snapshot = status(client)
        if snapshot.get("status") in wanted:
            return snapshot
        time.sleep(1.0)
    return snapshot


def main() -> None:
    with httpx.Client(timeout=30) as client:
        print("== 1. 第一次点「登录 Pixiv」 ==")
        opened = client.post(BASE + "/api/login/open").json()
        print("   ->", opened.get("status"), "|", opened.get("message"))
        snapshot = wait_status(client, {"waiting", "error", "success"})
        print("   状态:", snapshot.get("status"), "|", snapshot.get("message"))

        print("== 2. 模拟用户关掉浏览器窗口 ==")
        killed = kill_profile_browser()
        print("   已结束进程:", killed or "(无)")
        snapshot = wait_status(client, {"error", "cancelled", "success"}, timeout=20)
        print("   状态:", snapshot.get("status"), "|", snapshot.get("message"))
        verdict = "OK" if snapshot.get("status") == "error" else "FAIL（状态没退出）"
        print("   判定:", verdict)

        print("== 3. 第二次点「登录 Pixiv」（修复前这里会没反应）==")
        again = client.post(BASE + "/api/login/open").json()
        print("   ->", again.get("status"), "|", again.get("message"))
        snapshot = wait_status(client, {"waiting", "error", "success"}, timeout=25)
        reopened = snapshot.get("status") in ("waiting", "success")
        print("   状态:", snapshot.get("status"), "|", snapshot.get("message"))
        print("   判定:", "OK（重新打开了）" if reopened else "FAIL（没重新打开）")

        print("== 4. 清理 ==")
        client.post(BASE + "/api/login/cancel")
        time.sleep(1)
        kill_profile_browser()
        print("   已清理")


if __name__ == "__main__":
    main()
