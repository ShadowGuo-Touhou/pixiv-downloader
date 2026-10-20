"""通过真实 Chrome 完成 Pixiv 登录（CDP 方案）。

为什么不用 iframe：``pixiv.net`` 与 ``accounts.google.com`` 都返回
``X-Frame-Options: DENY``，网页里嵌不进去，必须开独立窗口。

为什么不用 playwright：它要下载上百 MB 的 driver，而且在受限环境里解包容易
失败。这里直接用「启动 Chrome + CDP 取 cookie」，只需要标准的 ``websockets``。

做法：用独立的 ``user-data-dir`` 启动 Chrome，打开调试端口，用户在窗口里
自由登录（Google 或账号密码都行），程序每 2 秒通过 ``Storage.getCookies``
读一次 cookie，一旦能通过 ``/ajax/user/self`` 验证就收工并关掉这个浏览器实例。

因为 profile 是持久化的，登录过一次之后，下次启动窗口里通常已是登录态。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx

from .api import PixivClient

LOGIN_TIMEOUT = 600.0  # 10 分钟没完成就放弃
POLL_INTERVAL = 2.0
START_URL = "https://www.pixiv.net/"

_BROWSER_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

# 注意：不要加 --disable-blink-features=AutomationControlled。
# Chromium 会为它弹出「unsupported command-line flag」黄色警告条，很难看；
# 而且我们本来就不是自动化启动（没有 --enable-automation），
# navigator.webdriver 默认就是 false，不需要再遮掩。
_CHROME_ARGS = [
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-features=Translate",
]


def _default_browser() -> str | None:
    """从注册表读系统默认浏览器，保证登录窗口和 GUI 页面用同一个浏览器。"""
    try:
        import winreg
    except ImportError:  # 非 Windows
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
        ) as key:
            prog_id = str(winreg.QueryValueEx(key, "ProgId")[0])
        with winreg.OpenKey(
            winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command"
        ) as key:
            command = str(winreg.QueryValueEx(key, "")[0])
    except OSError:
        return None

    command = command.strip()
    if command.startswith('"'):
        candidate = command[1:].split('"', 1)[0]
    else:
        candidate = command.split(" ", 1)[0]

    path = Path(candidate)
    # 只认 Chromium 系（Chrome / Edge），别的浏览器开不了调试端口
    name = path.name.lower()
    if path.exists() and ("chrome" in name or "msedge" in name):
        return str(path)
    return None


def find_browser() -> str | None:
    """优先系统默认浏览器，其次 Chrome，最后 Edge。"""
    default = _default_browser()
    if default:
        return default
    for candidate in _BROWSER_CANDIDATES:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(f'{c["name"]}={c["value"]}' for c in cookies if c.get("name"))


def _phpsessid(cookies: list[dict[str, Any]]) -> str:
    for cookie in cookies:
        if cookie.get("name") == "PHPSESSID" and cookie.get("value"):
            return str(cookie["value"])
    return ""


class LoginSession:
    """一次登录窗口的生命周期。"""

    def __init__(self, profile_dir: Path) -> None:
        self.profile_dir = profile_dir
        self.status = "idle"  # idle|opening|waiting|success|error|cancelled
        self.message = ""
        self.cookie = ""
        self.user_name = ""
        self.user_id: int | None = None
        self.browser_path = find_browser()
        self._task: asyncio.Task | None = None
        self._cancel = asyncio.Event()

    # -- 对外 -----------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "message": self.message,
            "userName": self.user_name,
            "userId": self.user_id,
            "cookiePresent": bool(self.cookie),
            "browserFound": bool(self.browser_path),
            "browser": Path(self.browser_path).stem if self.browser_path else "",
        }

    def is_running(self) -> bool:
        if self.status not in ("opening", "waiting"):
            return False
        # 后台任务已经结束了就不能再卡在 waiting 上，否则第二次点击会被忽略
        if self._task is not None and self._task.done():
            return False
        return True

    async def start(self) -> dict[str, Any]:
        if self.is_running():
            return self.snapshot()
        # 上一轮的收尾还没走完就先等它结束，避免两个会话抢同一个 profile
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._cancel = asyncio.Event()
        self.status = "opening"
        self.user_name = ""
        self.user_id = None
        if not self.browser_path:
            self.status = "error"
            self.message = "没找到 Chrome 或 Edge，请改用「用 Cookie 登录」"
            return self.snapshot()
        self.message = f"正在用 {Path(self.browser_path).stem} 打开登录页…"
        self._task = asyncio.create_task(self._run())
        return self.snapshot()

    async def cancel(self) -> dict[str, Any]:
        self._cancel.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.status = "cancelled"
        self.message = "已取消"
        return self.snapshot()

    async def shutdown(self) -> None:
        self._cancel.set()
        if self._task and not self._task.done():
            self._task.cancel()

    # -- CDP ------------------------------------------------------------

    async def _wait_debugger(self, port: int, timeout: float = 30.0) -> str:
        """等 Chrome 的调试端口起来，返回浏览器的 WebSocket 地址。"""
        deadline = time.time() + timeout
        url = f"http://127.0.0.1:{port}/json/version"
        while time.time() < deadline and not self._cancel.is_set():
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    response = await client.get(url)
                if response.status_code == 200:
                    data = response.json()
                    ws = data.get("webSocketDebuggerUrl")
                    if ws:
                        return str(ws)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return ""

    async def _cdp(self, ws_url: str, method: str, params: dict | None = None) -> dict:
        try:
            from websockets.asyncio.client import connect
        except ImportError:  # 老版本回退
            from websockets import connect  # type: ignore

        async with connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=20) as ws:
            await ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
                message = json.loads(raw)
                if message.get("id") == 1:
                    if "error" in message:
                        raise RuntimeError(str(message["error"]))
                    return message.get("result", {})

    async def _pixiv_cookies(self, ws_url: str) -> list[dict[str, Any]]:
        result = await self._cdp(ws_url, "Storage.getCookies")
        cookies = result.get("cookies") or []
        return [
            c
            for c in cookies
            if "pixiv" in str(c.get("domain") or "")
        ]

    # -- 主流程 ---------------------------------------------------------

    async def _run(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port = free_port()
        process: subprocess.Popen | None = None
        ws_url = ""
        try:
            process = subprocess.Popen(
                [
                    str(self.browser_path),
                    f"--user-data-dir={self.profile_dir}",
                    f"--remote-debugging-port={port}",
                    *_CHROME_ARGS,
                    START_URL,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            ws_url = await self._wait_debugger(port)
            if not ws_url:
                self.status = "error"
                self.message = "浏览器调试端口没起来（可能被杀软拦了）"
                return

            self.status = "waiting"
            self.message = "请在弹出的浏览器窗口里完成登录（Google 或账号密码都可以）"

            deadline = time.time() + LOGIN_TIMEOUT
            read_failures = 0
            while time.time() < deadline and not self._cancel.is_set():
                # 用户自己把窗口关了：立刻结束，别让状态卡在「等待登录」
                if process.poll() is not None:
                    self.status = "error"
                    self.message = "浏览器窗口已关闭，登录中止"
                    return

                try:
                    cookies = await self._pixiv_cookies(ws_url)
                except Exception:  # noqa: BLE001
                    cookies = []
                    read_failures += 1
                    if read_failures >= 3:
                        self.status = "error"
                        self.message = "浏览器调试连接中断，登录中止"
                        return
                else:
                    read_failures = 0

                if _phpsessid(cookies):
                    cookie_header = _cookie_header(cookies)
                    try:
                        async with PixivClient(cookie_header, min_interval=0.2) as client:
                            me = await client.me()
                    except Exception:  # noqa: BLE001
                        me = None
                    if me and me.get("name"):
                        self.cookie = cookie_header
                        self.user_name = str(me.get("name"))
                        raw_id = me.get("id") or me.get("userId")
                        try:
                            self.user_id = int(raw_id) if raw_id else None
                        except (TypeError, ValueError):
                            self.user_id = None
                        self.status = "success"
                        self.message = f"登录成功：{self.user_name}"
                        return
                await asyncio.sleep(POLL_INTERVAL)

            if self._cancel.is_set():
                self.status = "cancelled"
                self.message = "已取消"
            else:
                self.status = "error"
                self.message = "等待登录超时（10 分钟）"
        except asyncio.CancelledError:
            self.status = "cancelled"
            self.message = "已取消"
            raise
        except Exception as exc:  # noqa: BLE001
            self.status = "error"
            self.message = f"启动浏览器失败：{exc}"
        finally:
            if ws_url:
                try:
                    await self._cdp(ws_url, "Browser.close")
                except Exception:  # noqa: BLE001
                    pass
            if process is not None:
                try:
                    process.wait(timeout=5)
                except Exception:  # noqa: BLE001
                    try:
                        process.terminate()
                    except Exception:  # noqa: BLE001
                        pass
