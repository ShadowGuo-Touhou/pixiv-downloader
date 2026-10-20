"""Pixiv 网页 API 客户端。

设计要点
--------

1. **防盗链**：``i.pximg.net`` 的图片必须带 ``Referer: https://www.pixiv.net/``，
   否则 403。所有请求统一走 :meth:`PixivClient._headers`。
2. **限速**：ajax 接口串行限速（默认 0.6 秒/次），图片单独一个并发池。
3. **重试**：429/5xx 指数退避；403 直接报错不重试（通常是 cookie 失效或防盗链）。
4. **端点兜底**：Pixiv 前后端改版频繁，小说列表/正文这类端点准备了多个候选，
   按顺序试到能取出数据为止。取不到时把原始响应结构暴露给诊断接口。
"""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import httpx

BASE = "https://www.pixiv.net"

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class PixivError(RuntimeError):
    """Pixiv 请求失败。"""


class NotLoggedIn(PixivError):
    """需要登录态的接口被匿名访问。"""


# --------------------------------------------------------------------- 限速器


class RateLimiter:
    """保证相邻两次请求至少间隔 ``min_interval`` 秒。"""

    def __init__(self, min_interval: float = 0.6) -> None:
        self.min_interval = max(0.0, float(min_interval))
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        if self.min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delay = self._last + self.min_interval - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = time.monotonic()
            self._last = now


# ---------------------------------------------------------------- HTML -> 文本


class _TextExtractor(HTMLParser):
    """把 Pixiv 小说正文的 HTML 片段转成纯文本（保留段落）。"""

    _BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
    _SKIP = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in self._BLOCK:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        lines = [ln.strip() for ln in raw.split("\n")]
        out: list[str] = []
        blank = False
        for ln in lines:
            if ln:
                out.append(ln)
                blank = False
            elif not blank:
                out.append("")
                blank = True
        return "\n".join(out).strip()


def html_to_text(html: str) -> str:
    if not html:
        return ""
    parser = _TextExtractor()
    parser.feed(html)
    parser.close()
    text = parser.text()
    return unescape(text)


# ------------------------------------------------------------------ 数据模型


def _as_int_list(value: Any) -> list[int]:
    """把 ``{"123": null}`` / ``[1,2]`` / ``{"ids":[1,2]}`` 统一成 int 列表。"""
    if not value:
        return []
    if isinstance(value, Mapping):
        value = list(value.keys())
    if not isinstance(value, (list, tuple, set)):
        return []
    out: list[int] = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------- 客户端


class PixivClient:
    """一个轻量的 Pixiv 网页 API 封装。

    使用方式::

        async with PixivClient(cookie) as client:
            ids, manga_ids = await client.illust_ids(114430563)
    """

    def __init__(
        self,
        cookie: str = "",
        *,
        ua: str = DEFAULT_UA,
        min_interval: float = 0.6,
        max_retries: int = 5,
        image_concurrency: int = 3,
    ) -> None:
        self.cookie = (cookie or "").strip()
        self.ua = ua
        self.max_retries = max(1, int(max_retries))
        self.limiter = RateLimiter(min_interval)
        self._client: httpx.AsyncClient | None = None
        self._image_sem = asyncio.Semaphore(max(1, int(image_concurrency)))

    # -- 生命周期 --------------------------------------------------------

    async def __aenter__(self) -> "PixivClient":
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
        )
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- 请求基础 --------------------------------------------------------

    def _headers(self, *, accept_json: bool = True, referer: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.ua,
            "Referer": referer or f"{BASE}/",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.8",
        }
        if accept_json:
            headers["Accept"] = "application/json"
            # pixiv 的 ajax 接口会检查这几个自定义头
            headers["X-Requested-With"] = "XMLHttpRequest"
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    async def _request(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept_json: bool = True,
        referer: str | None = None,
        max_retries: int | None = None,
    ) -> httpx.Response:
        if self._client is None:
            raise PixivError("客户端未初始化（请用 async with）")

        retries = self.max_retries if max_retries is None else max_retries
        last_error: Exception | None = None

        for attempt in range(retries):
            await self.limiter.wait()
            try:
                response = await self._client.get(
                    url,
                    params=params,
                    headers=self._headers(accept_json=accept_json, referer=referer),
                )
            except httpx.HTTPError as exc:
                last_error = exc
                await asyncio.sleep(min(2 ** attempt + random.random(), 30))
                continue

            status = response.status_code
            if status == 200:
                return response
            if status in (429, 500, 502, 503, 504):
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    wait = float(retry_after)
                else:
                    wait = 2 ** attempt + random.random()
                await asyncio.sleep(min(wait, 60))
                continue
            if status in (401, 403):
                raise NotLoggedIn(
                    f"HTTP {status}：登录态无效或请求被拒（{url}）"
                )
            raise PixivError(f"HTTP {status}：{url}")

        raise PixivError(f"重试 {retries} 次仍失败：{url}｜{last_error}")

    async def _json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        accept_404: bool = False,
    ) -> Any:
        url = path if path.startswith("http") else BASE + path
        try:
            response = await self._request(url, params=params)
        except PixivError as exc:
            if accept_404:
                return None
            raise

        try:
            payload = response.json()
        except ValueError as exc:
            raise PixivError(f"返回不是 JSON：{url}") from exc

        if isinstance(payload, dict) and payload.get("error"):
            message = payload.get("message") or "pixiv api error"
            raise PixivError(message)
        if isinstance(payload, dict) and "body" in payload:
            return payload["body"]
        return payload

    async def _first_ok(
        self,
        candidates: Sequence[tuple[str, Mapping[str, Any] | None]],
        *,
        pick,
    ) -> Any:
        """依次尝试候选端点，返回第一个 ``pick(body)`` 非空的结果。"""
        errors: list[str] = []
        for path, params in candidates:
            try:
                body = await self._json(path, params=params, accept_404=True)
            except PixivError as exc:
                errors.append(f"{path}: {exc}")
                continue
            if body is None:
                errors.append(f"{path}: 404")
                continue
            picked = pick(body)
            if picked:
                return picked
            errors.append(f"{path}: 空结果")
        raise PixivError("所有候选端点都没取到数据；" + " | ".join(errors))

    # -- 账号 ------------------------------------------------------------

    async def me(self) -> dict[str, Any] | None:
        """当前登录账号。未登录返回 ``None``。

        实测返回结构是 ``{"userData": {"id": "...", "name": "...", ...}}``：
        用户字段包在 ``userData`` 里面，这里拆一层返回，
        调用方可以直接取 ``name`` / ``id``。
        """
        try:
            body = await self._json("/ajax/user/self", params={"lang": "en"})
        except PixivError:
            return None
        if not isinstance(body, dict):
            return None
        user = body.get("userData")
        if isinstance(user, dict) and user:
            return user
        return body if body else None

    async def is_logged_in(self) -> bool:
        return await self.me() is not None

    # -- 作品列表 --------------------------------------------------------

    async def profile_all(self, user_id: int) -> dict[str, Any]:
        body = await self._json(f"/ajax/user/{user_id}/profile/all", params={"lang": "en"})
        return body if isinstance(body, dict) else {}

    async def illust_ids(self, user_id: int) -> tuple[list[int], list[int]]:
        """返回 ``(插画 id, 漫画 id)``。"""
        body = await self.profile_all(user_id)
        return _as_int_list(body.get("illusts")), _as_int_list(body.get("manga"))

    async def novel_ids(self, user_id: int) -> list[int]:
        """作者的全部小说 id。

        ``profile/all`` 的 body 里本来就带 ``novels`` 字段（实测确认），
        所以先走它；``/ajax/user/{id}/novels`` 那一类端点目前返回 400，
        只作为后备保留。
        """
        try:
            body = await self.profile_all(user_id)
        except PixivError:
            body = {}
        direct = _as_int_list(body.get("novels")) if isinstance(body, dict) else []
        if direct:
            return direct

        def pick(body: Any) -> list[int]:
            if isinstance(body, list):
                return _as_int_list(body)
            if isinstance(body, dict):
                for key in ("ids", "novels", "works", "list"):
                    if key in body:
                        return _as_int_list(body[key])
                # {"123": {...}} 这种
                return _as_int_list(body)
            return []

        collected: list[int] = []
        page = 1
        while page <= 200:
            try:
                ids = await self._first_ok(
                    [
                        (f"/ajax/user/{user_id}/novels", {"lang": "en", "p": page}),
                        (f"/ajax/user/{user_id}/profile/novels", {"lang": "en"}),
                    ],
                    pick=pick,
                )
            except PixivError:
                break
            fresh = [i for i in ids if i not in collected]
            if not fresh:
                break
            collected.extend(fresh)
            if len(ids) < 2:
                break
            page += 1
        return collected

    # -- 单个作品 --------------------------------------------------------

    async def illust_detail(self, illust_id: int) -> dict[str, Any]:
        body = await self._json(f"/ajax/illust/{illust_id}", params={"lang": "en"})
        return body if isinstance(body, dict) else {}

    async def illust_details_bulk(
        self, user_id: int, illust_ids_: Sequence[int], *, chunk: int = 20
    ) -> dict[int, dict[str, Any]]:
        """批量取作品元数据，缺失的逐个补。"""
        out: dict[int, dict[str, Any]] = {}
        ids = list(illust_ids_)
        for i in range(0, len(ids), chunk):
            part = ids[i : i + chunk]
            try:
                body = await self._json(
                    f"/ajax/user/{user_id}/profile/illusts",
                    params={
                        "ids[]": part,
                        "work_category": "illustManga",
                        "is_first_page": 1,
                        "lang": "en",
                    },
                )
            except PixivError:
                continue
            works = {}
            if isinstance(body, dict):
                if isinstance(body.get("works"), dict):
                    works = body["works"]
                elif isinstance(body.get("works"), list):
                    works = {str(w.get("id")): w for w in body["works"] if w.get("id")}
            for key, value in works.items():
                try:
                    out[int(key)] = value
                except (TypeError, ValueError):
                    continue

        for illust_id in ids:
            if illust_id in out:
                continue
            try:
                out[illust_id] = await self.illust_detail(illust_id)
            except PixivError:
                continue
        return out

    async def series_illust_ids(self, first_illust_id: int, total: int) -> list[int]:
        """沿 ``seriesNavData.next`` 把一个连载/系列的作品 id 串起来。

        系列作品不会出现在 ``profile/all`` 的 ``illusts`` 或 ``manga`` 里
        （实测：作者有 6 话系列，两个列表都是空的），只能从首话顺着 next 走。
        """
        ids: list[int] = []
        seen: set[int] = set()
        current: int | None = first_illust_id
        limit = max(1, min(int(total), 500))
        while current and current not in seen and len(ids) < limit:
            seen.add(current)
            ids.append(current)
            try:
                detail = await self.illust_detail(current)
            except PixivError:
                break
            nav = detail.get("seriesNavData") or {}
            nxt = (nav.get("next") or {}).get("id")
            try:
                current = int(nxt) if nxt else None
            except (TypeError, ValueError):
                current = None
        return ids

    async def illust_pages(self, illust_id: int) -> list[dict[str, Any]]:
        body = await self._json(f"/ajax/illust/{illust_id}/pages", params={"lang": "en"})
        return body if isinstance(body, list) else []

    async def ugoira_meta(self, illust_id: int) -> dict[str, Any]:
        body = await self._json(
            f"/ajax/illust/{illust_id}/ugoira_meta", params={"lang": "en"}
        )
        return body if isinstance(body, dict) else {}

    # -- 小说 ------------------------------------------------------------

    async def novel_detail(self, novel_id: int) -> dict[str, Any]:
        body = await self._json(f"/ajax/novel/{novel_id}", params={"lang": "en"})
        return body if isinstance(body, dict) else {}

    async def novel_text(self, novel_id: int) -> str:
        """取小说正文纯文本。

        正常情况 ``/ajax/novel/{id}`` 的 ``content`` 就是全文；若为空则尝试
        旧式 ``novel/show.php`` 页面兜底。
        """
        detail = await self.novel_detail(novel_id)
        content = detail.get("content") or ""
        if content:
            return html_to_text(content)

        url = f"{BASE}/novel/show.php?id={novel_id}"
        try:
            response = await self._request(url, accept_json=False)
        except PixivError:
            return ""
        return html_to_text(response.text)

    # -- 下载 ------------------------------------------------------------

    async def download_file(
        self,
        url: str,
        dest: Path,
        *,
        referer: str | None = None,
        max_retries: int | None = None,
    ) -> int:
        """流式下载到 ``dest``（先写 .part 再原子改名）。返回字节数。"""
        if self._client is None:
            raise PixivError("客户端未初始化（请用 async with）")

        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        retries = self.max_retries if max_retries is None else max_retries
        headers = self._headers(accept_json=False, referer=referer)
        headers["Accept"] = "*/*"

        async with self._image_sem:
            last_error: Exception | None = None
            for attempt in range(retries):
                try:
                    async with self._client.stream("GET", url, headers=headers) as response:
                        status = response.status_code
                        if status == 200:
                            size = 0
                            with tmp.open("wb") as handle:
                                async for chunk in response.aiter_bytes(65536):
                                    handle.write(chunk)
                                    size += len(chunk)
                            if size == 0:
                                tmp.unlink(missing_ok=True)
                                raise PixivError(f"下载到 0 字节：{url}")
                            os.replace(tmp, dest)
                            return size
                        if status in (429, 500, 502, 503, 504):
                            last_error = PixivError(f"HTTP {status}")
                            await asyncio.sleep(min(2 ** attempt + random.random(), 30))
                            continue
                        raise PixivError(f"HTTP {status}：{url}")
                except httpx.HTTPError as exc:
                    last_error = exc
                    tmp.unlink(missing_ok=True)
                    await asyncio.sleep(min(2 ** attempt + random.random(), 30))

        tmp.unlink(missing_ok=True)
        raise PixivError(f"下载失败：{url}｜{last_error}")


# -------------------------------------------------------------------- 诊断


async def diagnose(cookie: str, user_id: int | None = None) -> dict[str, Any]:
    """给 GUI 用的自检：把各个端点的返回结构摊平，方便核对字段。"""
    report: dict[str, Any] = {"cookie_present": bool(cookie), "endpoints": {}}
    async with PixivClient(cookie, min_interval=0.3) as client:
        try:
            me = await client.me()
            report["me"] = {"ok": me is not None, "name": (me or {}).get("name")}
        except PixivError as exc:
            report["me"] = {"ok": False, "error": str(exc)}

        if user_id:
            try:
                body = await client.profile_all(user_id)
                report["endpoints"]["profile_all"] = {
                    "ok": True,
                    "keys": sorted(body.keys()),
                    "illusts": len(_as_int_list(body.get("illusts"))),
                    "manga": len(_as_int_list(body.get("manga"))),
                    "novels_key_present": "novels" in body,
                }
            except PixivError as exc:
                report["endpoints"]["profile_all"] = {"ok": False, "error": str(exc)}

            try:
                ids = await client.novel_ids(user_id)
                report["endpoints"]["novel_ids"] = {"ok": True, "count": len(ids)}
            except PixivError as exc:
                report["endpoints"]["novel_ids"] = {"ok": False, "error": str(exc)}

            illusts, _ = await client.illust_ids(user_id)
            if illusts:
                sample = illusts[0]
                try:
                    detail = await client.illust_detail(sample)
                    report["endpoints"]["illust_detail"] = {
                        "ok": True,
                        "keys": sorted(detail.keys()),
                        "xRestrict": detail.get("xRestrict"),
                        "createDate": detail.get("createDate"),
                        "pageCount": detail.get("pageCount"),
                        "illustType": detail.get("illustType"),
                    }
                except PixivError as exc:
                    report["endpoints"]["illust_detail"] = {
                        "ok": False,
                        "error": str(exc),
                    }
    return report
