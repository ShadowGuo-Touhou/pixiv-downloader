"""验证启动出来的浏览器不是「自动化」状态，并确认没有命令行警告。

    python dev_webdriver_check.py

会短暂启动一个浏览器窗口，检查完自动关掉。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from pixiv_dl.login import _CHROME_ARGS, find_browser, free_port  # noqa: E402


async def cdp_eval(ws_url: str, expression: str):
    from websockets.asyncio.client import connect

    async with connect(ws_url, max_size=8 * 1024 * 1024, open_timeout=20) as ws:
        await ws.send(
            json.dumps(
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "returnByValue": True},
                }
            )
        )
        while True:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            if message.get("id") == 1:
                return message.get("result", {}).get("result", {}).get("value")


async def main() -> None:
    browser = find_browser()
    if not browser:
        print("没找到浏览器")
        return
    port = free_port()
    profile = ROOT / "_probe_profile"
    browser_ws = ""
    process = subprocess.Popen(
        [
            browser,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            *_CHROME_ARGS,
            "https://www.pixiv.net/",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=3)
                if response.status_code == 200:
                    browser_ws = response.json()["webSocketDebuggerUrl"]
                    break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)

        if not browser_ws:
            print("调试端口没起来")
            return

        pages = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=5).json()
        page_ws = next(
            (p.get("webSocketDebuggerUrl") for p in pages if p.get("type") == "page"), ""
        )
        if not page_ws:
            print("没有可用页面")
            return

        webdriver = await cdp_eval(page_ws, "navigator.webdriver")
        user_agent = await cdp_eval(page_ws, "navigator.userAgent")
        print("navigator.webdriver =", webdriver)
        print("判定:", "OK（非自动化）" if not webdriver else "FAIL（仍被识别为自动化）")
        print("UA:", user_agent)
    finally:
        if browser_ws:
            try:
                from websockets.asyncio.client import connect

                async with connect(browser_ws, open_timeout=10) as ws:
                    await ws.send(json.dumps({"id": 99, "method": "Browser.close"}))
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1.5)
        try:
            process.terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
