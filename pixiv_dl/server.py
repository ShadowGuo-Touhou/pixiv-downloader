"""本地 HTTP 服务 + GUI 后端。"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .api import PixivClient, PixivError, diagnose
from .downloader import Job, Options
from .login import LoginSession
from .naming import parse_user_id

ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
STATE_DIR = ROOT / ".state"
PROFILE_DIR = ROOT / ".profile" / "pixiv"
CONFIG_PATH = STATE_DIR / "config.json"

app = FastAPI(title="Pixiv Batch Downloader")


# ------------------------------------------------------------------ 应用状态


class AppState:
    def __init__(self) -> None:
        self.cookie = ""
        self.user_name = ""
        self.user_id: int | None = None
        self.jobs: dict[str, Job] = {}
        self.login = LoginSession(PROFILE_DIR)
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        self.cookie = str(data.get("cookie") or "")
        self.user_name = str(data.get("userName") or "")
        raw = data.get("userId")
        try:
            self.user_id = int(raw) if raw else None
        except (TypeError, ValueError):
            self.user_id = None

    def save(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "cookie": self.cookie,
            "userName": self.user_name,
            "userId": self.user_id,
        }
        CONFIG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def make_client(self, min_interval: float = 0.6, concurrency: int = 3) -> PixivClient:
        return PixivClient(
            self.cookie, min_interval=min_interval, image_concurrency=concurrency
        )


state = AppState()


def _get_job(job_id: str) -> Job:
    job = state.jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job


# ------------------------------------------------------------------ 基础信息


class LoginCookieRequest(BaseModel):
    cookie: str


class PlanRequest(BaseModel):
    url: str | None = None
    userId: int | None = None
    options: dict[str, Any] = {}


class InspectDirRequest(BaseModel):
    dir: str


@app.post("/api/inspect-dir")
async def api_inspect_dir(req: InspectDirRequest) -> dict[str, Any]:
    """看一眼用户填的这个目录里已经有什么。

    不涉及任何预设路径：程序在哪台机器、哪个盘上跑都只看用户自己选的目录。
    """
    path = Path(req.dir).expanduser()
    if not path.is_dir():
        return {"exists": False, "files": 0, "bytes": 0}
    files = 0
    total = 0
    for entry in path.rglob("*"):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        files += 1
        try:
            total += entry.stat().st_size
        except OSError:
            continue
    return {"exists": True, "files": files, "bytes": total}


@app.get("/api/state")
async def api_state() -> dict[str, Any]:
    return {
        "loggedIn": bool(state.cookie and state.user_name),
        "userName": state.user_name,
        "userId": state.user_id,
        "cookiePresent": bool(state.cookie),
        "login": state.login.snapshot(),
        "jobs": [
            job.snapshot() for job in state.jobs.values()
        ],
    }


# -------------------------------------------------------------------- 登录


@app.post("/api/login/open")
async def api_login_open() -> dict[str, Any]:
    return await state.login.start()


@app.get("/api/login/status")
async def api_login_status() -> dict[str, Any]:
    snapshot = state.login.snapshot()
    if snapshot["status"] == "success" and state.login.cookie:
        state.cookie = state.login.cookie
        state.user_name = state.login.user_name
        state.user_id = state.login.user_id
        state.save()
    return snapshot


@app.post("/api/login/cancel")
async def api_login_cancel() -> dict[str, Any]:
    return await state.login.cancel()


def normalize_cookie(raw: str) -> str:
    """把用户粘进来的各种形态统一成一条 Cookie 头。

    能处理：完整 Cookie 头、带 ``cookie:`` 前缀、DevTools 里多行复制、
    以及只有 PHPSESSID 的值这一种最省事的粘法。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"(?i)\bcookie\s*:\s*", "", text)
    if "=" not in text:
        return f"PHPSESSID={text.strip()}"

    pairs = re.findall(r"([A-Za-z0-9_\-.]+)=([^;\s\"']+)", text)
    if not pairs:
        return text

    seen: set[str] = set()
    parts: list[str] = []
    for key, value in pairs:
        if key in seen:
            continue
        seen.add(key)
        parts.append(f"{key}={value}")
    return "; ".join(parts)


@app.post("/api/login/cookie")
async def api_login_cookie(req: LoginCookieRequest) -> dict[str, Any]:
    cookie = normalize_cookie(req.cookie)
    if not cookie:
        raise HTTPException(status_code=400, detail="Cookie 不能为空")
    if "PHPSESSID=" not in cookie:
        raise HTTPException(
            status_code=400, detail="没有找到 PHPSESSID，请确认复制的是 pixiv.net 的 Cookie"
        )
    async with PixivClient(cookie, min_interval=0.2) as client:
        me = await client.me()
    if not me or not me.get("name"):
        raise HTTPException(
            status_code=400,
            detail="Cookie 没通过验证：可能已过期，或复制时漏了 PHPSESSID",
        )
    state.cookie = cookie
    state.user_name = str(me.get("name"))
    raw = me.get("id") or me.get("userId")
    try:
        state.user_id = int(raw) if raw else None
    except (TypeError, ValueError):
        state.user_id = None
    state.save()
    return {"ok": True, "userName": state.user_name, "userId": state.user_id}


@app.post("/api/logout")
async def api_logout() -> dict[str, Any]:
    state.cookie = ""
    state.user_name = ""
    state.user_id = None
    state.save()
    return {"ok": True}


# -------------------------------------------------------------------- 作者


@app.post("/api/parse-user")
async def api_parse_user(payload: dict[str, Any]) -> dict[str, Any]:
    raw = str(payload.get("url") or "")
    user_id = parse_user_id(raw)
    if user_id is None:
        raise HTTPException(status_code=400, detail="没能从输入里解析出作者 id")
    name = ""
    async with state.make_client(min_interval=0.3) as client:
        try:
            body = await client._json(f"/ajax/user/{user_id}", params={"lang": "en"})
            if isinstance(body, dict):
                name = str(body.get("name") or "")
        except PixivError:
            name = ""
    return {"userId": user_id, "name": name}


@app.post("/api/diagnose")
async def api_diagnose(payload: dict[str, Any]) -> dict[str, Any]:
    user_id = payload.get("userId")
    try:
        user_id = int(user_id) if user_id else None
    except (TypeError, ValueError):
        user_id = None
    return await diagnose(state.cookie, user_id)


@app.post("/api/pick-folder")
async def api_pick_folder() -> dict[str, Any]:
    return await asyncio.to_thread(_ask_directory)


def _ask_directory() -> dict[str, Any]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"tkinter 不可用：{exc}"}
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        path = filedialog.askdirectory(title="选择保存目录")
        root.destroy()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": bool(path), "path": path or ""}


# -------------------------------------------------------------------- 任务


@app.post("/api/plan")
async def api_plan(req: PlanRequest) -> dict[str, Any]:
    user_id = req.userId or parse_user_id(req.url or "")
    if not user_id:
        raise HTTPException(status_code=400, detail="缺少作者 id 或主页链接")
    # 公开作品不登录也能抓（实测确认），登录主要用于 R-18 与受限内容

    options = Options.from_dict(req.options or {})
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=int(user_id), options=options)
    state.jobs[job.job_id] = job
    job._task = asyncio.create_task(_plan_task(job))
    return {"jobId": job.job_id}


async def _plan_task(job: Job) -> None:
    # 计划阶段只抓元数据，间隔可以比下载阶段更短，省掉等待
    async with state.make_client(
        min_interval=min(0.3, job.options.min_interval), concurrency=job.options.concurrency
    ) as client:
        try:
            await job.plan(client)
        except Exception:  # noqa: BLE001
            pass  # 错误已经写进 job.error 并广播


@app.get("/api/job/{job_id}")
async def api_job(job_id: str, offset: int = 0, limit: int = 200) -> dict[str, Any]:
    job = _get_job(job_id)
    snapshot = job.snapshot()
    snapshot["items"] = [item.to_dict() for item in job.items[offset : offset + limit]]
    snapshot["itemsLength"] = len(job.items)
    return snapshot


@app.get("/api/job/{job_id}/events")
async def api_job_events(job_id: str) -> StreamingResponse:
    job = _get_job(job_id)
    queue = job.subscribe()

    async def generator():
        try:
            yield "data: " + json.dumps(
                {"type": "hello", "snapshot": job.snapshot()}, ensure_ascii=False
            ) + "\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if event.get("type") == "finished":
                    break
        finally:
            job.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/job/{job_id}/start")
async def api_job_start(job_id: str) -> dict[str, Any]:
    job = _get_job(job_id)
    if job.status == "paused":
        job.resume()
        return job.snapshot()
    if job.status != "ready":
        raise HTTPException(status_code=409, detail=f"当前状态不能开始：{job.status}")
    job._task = asyncio.create_task(_run_task(job))
    return job.snapshot()


async def _run_task(job: Job) -> None:
    async with state.make_client(
        min_interval=job.options.min_interval, concurrency=job.options.concurrency
    ) as client:
        try:
            await job.run(client)
        except Exception as exc:  # noqa: BLE001
            job.status = "failed"
            job.error = str(exc)
            job.emit({"type": "error", "message": str(exc)})


@app.post("/api/job/{job_id}/pause")
async def api_job_pause(job_id: str) -> dict[str, Any]:
    job = _get_job(job_id)
    job.pause()
    return job.snapshot()


@app.post("/api/job/{job_id}/cancel")
async def api_job_cancel(job_id: str) -> dict[str, Any]:
    job = _get_job(job_id)
    job.cancel()
    return job.snapshot()


@app.post("/api/job/{job_id}/retry-failed")
async def api_job_retry(job_id: str) -> dict[str, Any]:
    job = _get_job(job_id)
    pending = [item for item in job.items if item.status == "failed"]
    for item in pending:
        item.status = "pending"
        item.error = ""
    if not pending:
        return job.snapshot()
    if job.status in ("done", "failed", "cancelled"):
        job.status = "ready"
    job._task = asyncio.create_task(_run_task(job))
    return job.snapshot()


# -------------------------------------------------------------------- 静态


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


def main() -> None:  # pragma: no cover - 入口
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="info")


if __name__ == "__main__":  # pragma: no cover
    main()
