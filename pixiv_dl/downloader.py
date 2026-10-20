"""下载引擎：计划生成 + 执行。

一个 :class:`Job` 代表「把某位作者的全部作品下载到某个目录」这件事。
流程是两段的：

1. :meth:`Job.plan` —— 拉作品清单、过滤、算出每个文件的目标路径（不下载）
2. :meth:`Job.run`  —— 按计划并发下载

分开的好处是 GUI 可以先给你看「将要下载什么」，确认后再真跑。
"""

from __future__ import annotations

import asyncio
import io
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .api import PixivClient, PixivError, _as_int_list, html_to_text
from .index import DownloadIndex
from .naming import (
    Namer,
    _EXISTING_RE,
    extension_from_url,
    format_date,
    render_novel_markdown,
    scan_existing,
    title_sanitize,
)

WORK_ILLUST = "illust"
WORK_UGOIRA = "ugoira"
WORK_NOVEL = "novel"


@dataclass
class Options:
    output_dir: Path
    include_illust: bool = True
    include_manga: bool = True
    include_novel: bool = True
    include_ugoira: bool = True
    r18: str = "all"  # all | safe | only
    timezone: str = "jst"
    concurrency: int = 3
    min_interval: float = 0.6
    date_from: str | None = None
    date_to: str | None = None
    #: >0 时只保留计划里的前 N 个文件，用来先试跑一小批
    max_files: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Options":
        return cls(
            output_dir=Path(str(data.get("outputDir") or ".")).expanduser(),
            include_illust=bool(data.get("includeIllust", True)),
            include_manga=bool(data.get("includeManga", True)),
            include_novel=bool(data.get("includeNovel", True)),
            include_ugoira=bool(data.get("includeUgoira", True)),
            r18=str(data.get("r18") or "all"),
            timezone=str(data.get("timezone") or "jst"),
            concurrency=max(1, min(8, int(data.get("concurrency") or 3))),
            min_interval=max(0.0, float(data.get("minIntervalMs") or 600) / 1000.0),
            date_from=data.get("dateFrom") or None,
            date_to=data.get("dateTo") or None,
            max_files=max(0, int(data.get("maxFiles") or 0)),
        )


@dataclass
class Item:
    """计划里的一行：一张图 / 一个动图 / 一篇小说。"""

    kind: str
    work_id: int
    title: str
    create_date: str
    date_str: str
    path: Path
    page_count: int = 1
    page_index: int = 0
    url: str = ""
    x_restrict: int = 0
    status: str = "pending"  # pending | done | skipped | failed
    error: str = ""
    bytes_written: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "workId": self.work_id,
            "title": self.title,
            "date": self.date_str,
            "path": str(self.path),
            "pageCount": self.page_count,
            "pageIndex": self.page_index,
            "status": self.status,
            "error": self.error,
        }


@dataclass
class Job:
    job_id: str
    user_id: int
    options: Options
    status: str = "created"  # created | planning | ready | running | paused | done | failed
    error: str = ""
    items: list[Item] = field(default_factory=list)
    works_total: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    bytes_total: int = 0

    _subscribers: list[asyncio.Queue] = field(default_factory=list, repr=False)
    _cancel: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _pause: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _task: asyncio.Task | None = field(default=None, repr=False)

    # -- 事件 -----------------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def emit(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass

    # -- 进度 -----------------------------------------------------------

    def counts(self) -> dict[str, int]:
        done = sum(1 for i in self.items if i.status == "done")
        skipped = sum(1 for i in self.items if i.status == "skipped")
        failed = sum(1 for i in self.items if i.status == "failed")
        return {
            "total": len(self.items),
            "done": done,
            "skipped": skipped,
            "failed": failed,
            "pending": len(self.items) - done - skipped - failed,
        }

    def snapshot(self) -> dict[str, Any]:
        counts = self.counts()
        return {
            "jobId": self.job_id,
            "userId": self.user_id,
            "status": self.status,
            "error": self.error,
            "outputDir": str(self.options.output_dir),
            "works": self.works_total,
            "counts": counts,
            "bytes": self.bytes_total,
        }

    # -- 计划 -----------------------------------------------------------

    async def plan(self, client: PixivClient) -> None:
        """拉清单并算出每个文件的目标路径。"""
        self.status = "planning"
        self.emit({"type": "status", "status": self.status})
        try:
            await self._build_items(client)
        except Exception as exc:  # noqa: BLE001
            self.status = "failed"
            self.error = str(exc)
            self.emit({"type": "error", "message": str(exc)})
            raise
        self.status = "ready"
        self.emit({"type": "status", "status": self.status, "works": self.works_total})

    async def _build_items(self, client: PixivClient) -> None:
        options = self.options
        output_dir = options.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)

        namer = Namer(root=output_dir, tz=options.timezone)
        namer.restore(scan_existing(output_dir))
        # 索引把「作品 -> 文件名」固定住：重跑时编号不变，判重才真正生效
        index = DownloadIndex.load(output_dir)
        namer.restore(index.restore_names())

        # 还没有索引（比如刚升级）时，把磁盘上已有的文件按序号顺序认领给作品，
        # 免得第一次重跑就把整批文件重新下一遍。
        disk_pool: dict[tuple[str, str], list[tuple[int, Path]]] = {}
        if not index.works:
            for path in sorted(output_dir.rglob("*")):
                if not path.is_file():
                    continue
                match = _EXISTING_RE.match(path.name)
                if not match:
                    continue
                pool_key = (match.group("date"), match.group("title"))
                disk_pool.setdefault(pool_key, []).append(
                    (int(match.group("seq")), path)
                )
            for bucket in disk_pool.values():
                bucket.sort()

        profile = await client.profile_all(self.user_id)
        illust_ids = _as_int_list(profile.get("illusts"))
        manga_ids = _as_int_list(profile.get("manga"))
        novel_ids = _as_int_list(profile.get("novels"))

        wanted: list[int] = []
        if options.include_illust:
            wanted.extend(illust_ids)
        if options.include_manga:
            wanted.extend(manga_ids)
            # 连载系列的作品不出现在 manga 列表里，必须沿 seriesNavData.next 单独抓
            for series in profile.get("mangaSeries") or []:
                first_id = series.get("firstIllustId")
                total = int(series.get("total") or 0)
                if not first_id or total <= 0:
                    continue
                try:
                    wanted.extend(await client.series_illust_ids(int(first_id), total))
                except PixivError as exc:
                    self.emit(
                        {
                            "type": "error",
                            "message": f"展开系列「{series.get('title')}」失败：{exc}",
                        }
                    )
        # 去重但保持稳定
        seen: set[int] = set()
        ordered: list[int] = []
        for wid in wanted:
            if wid not in seen:
                seen.add(wid)
                ordered.append(wid)

        self.emit({"type": "plan", "phase": "details", "works": len(ordered)})
        details = await client.illust_details_bulk(self.user_id, ordered)

        works: list[tuple[dict[str, Any], str]] = []
        for wid in ordered:
            detail = details.get(wid)
            if not detail:
                continue
            kind = WORK_UGOIRA if int(detail.get("illustType") or 0) == 2 else WORK_ILLUST
            works.append((detail, kind))

        novels: list[dict[str, Any]] = []
        if options.include_novel:
            if not novel_ids:
                try:
                    novel_ids = await client.novel_ids(self.user_id)
                except PixivError:
                    novel_ids = []
            self.emit({"type": "plan", "phase": "novels", "works": len(novel_ids)})
            for nid in novel_ids:
                try:
                    novels.append(await client.novel_detail(nid))
                except PixivError:
                    continue

        works.sort(key=lambda pair: (pair[0].get("createDate") or "", int(pair[0].get("id") or 0)))
        novels.sort(key=lambda d: (d.get("createDate") or "", int(d.get("id") or 0)))

        items: list[Item] = []

        total_works = len(works)
        for work_index, (detail, kind) in enumerate(works, start=1):
            # 每个作品都要单独请求一次分页，作品多时要跑一两分钟，必须有进度
            if work_index == 1 or work_index % 5 == 0 or work_index == total_works:
                self.emit(
                    {
                        "type": "plan",
                        "phase": "pages",
                        "done": work_index,
                        "total": total_works,
                    }
                )
            create_date = detail.get("createDate") or ""
            if not self._date_ok(create_date, options):
                continue
            x_restrict = int(detail.get("xRestrict") or 0)
            if not self._restrict_ok(x_restrict, options):
                continue
            if kind == WORK_UGOIRA and not options.include_ugoira:
                continue

            title = detail.get("title") or f"illust_{detail.get('id')}"
            wid = int(detail.get("id") or 0)
            key = namer.key_of(create_date, title)
            date_str = key[0]

            if kind == WORK_UGOIRA:
                recorded = index.existing_path(wid, 0)
                if recorded is None:
                    # 首次运行没有索引时，从磁盘上认领已存在的 .gif
                    pool = disk_pool.get(key, [])
                    for position, (seq, existing) in enumerate(pool):
                        if existing.suffix.lower() == ".gif":
                            del pool[position]
                            index.record(wid, key, title, [(seq, existing)])
                            recorded = existing
                            break
                if recorded is not None:
                    path = recorded
                else:
                    seq = namer.allocate(key, 1)[0]
                    path = namer.image_path(key, seq, 1, ".gif")
                    index.record(wid, key, title, [(seq, path)])
                items.append(
                    Item(
                        kind=WORK_UGOIRA,
                        work_id=wid,
                        title=title,
                        create_date=create_date,
                        date_str=date_str,
                        path=path,
                        page_count=1,
                        url=f"https://www.pixiv.net/artworks/{wid}",
                        x_restrict=x_restrict,
                    )
                )
                continue

            try:
                pages = await client.illust_pages(wid)
            except PixivError as exc:
                self.emit({"type": "error", "message": f"取分页失败 {wid}: {exc}"})
                continue
            if not pages:
                continue

            # 索引里已有记录就复用上次的编号和文件名，否则重新分配并记下来
            recorded = index.pages(wid)
            if not recorded:
                pool = disk_pool.get(key, [])
                if len(pool) >= len(pages):
                    claimed = pool[: len(pages)]
                    del pool[: len(pages)]
                    index.record(wid, key, title, claimed)
                    recorded = index.pages(wid)
            reuse = len(recorded) == len(pages) and all(p.get("file") for p in recorded)
            if reuse:
                paths = [output_dir / str(p.get("file")) for p in recorded]
            else:
                seqs = namer.allocate(key, len(pages))
                paths = []
                for page_index, page in enumerate(pages):
                    url = (page.get("urls") or {}).get("original") or ""
                    ext = extension_from_url(url)
                    paths.append(namer.image_path(key, seqs[page_index], len(pages), ext))
                index.record(wid, key, title, list(zip(seqs, paths)))

            for page_index, page in enumerate(pages):
                url = (page.get("urls") or {}).get("original") or ""
                items.append(
                    Item(
                        kind=WORK_ILLUST,
                        work_id=wid,
                        title=title,
                        create_date=create_date,
                        date_str=date_str,
                        path=paths[page_index],
                        page_count=len(pages),
                        page_index=page_index,
                        url=url,
                        x_restrict=x_restrict,
                    )
                )

        for detail in novels:
            create_date = detail.get("createDate") or ""
            if not self._date_ok(create_date, options):
                continue
            x_restrict = int(detail.get("xRestrict") or 0)
            if not self._restrict_ok(x_restrict, options):
                continue
            title = detail.get("title") or f"novel_{detail.get('id')}"
            key = namer.key_of(create_date, title)
            novel_id = int(detail.get("id") or 0)
            recorded = index.existing_path(novel_id, 0)
            if recorded is None:
                # 没有索引时，按命名规则猜一下已有文件
                guess = output_dir / f"{key[0]}_{key[1]}.md"
                if guess.is_file() and guess.stat().st_size > 0:
                    index.record(novel_id, key, title, [(1, guess)])
                    recorded = guess
            if recorded is None:
                recorded = namer.novel_path(key)
                index.record(novel_id, key, title, [(1, recorded)])
            items.append(
                Item(
                    kind=WORK_NOVEL,
                    work_id=novel_id,
                    title=title,
                    create_date=create_date,
                    date_str=key[0],
                    path=recorded,
                    url=f"https://www.pixiv.net/novel/show.php?id={novel_id}",
                    x_restrict=x_restrict,
                )
            )

        if options.max_files > 0:
            items = items[: options.max_files]

        # 计划阶段就把已经存在的文件标成「跳过」：
        # 预览里一眼能看出还差哪些，下载时也会直接跳过
        for planned in items:
            try:
                if planned.path.is_file() and planned.path.stat().st_size > 0:
                    planned.status = "skipped"
            except OSError:
                continue

        index.save()
        self.items = items
        self.works_total = len({i.work_id for i in items})
        self.emit(
            {
                "type": "planned",
                "items": len(items),
                "works": self.works_total,
                "preview": [i.to_dict() for i in items[:50]],
            }
        )

    @staticmethod
    def _date_ok(create_date: str, options: Options) -> bool:
        if not create_date:
            return True
        try:
            day = format_date(create_date, options.timezone)
        except ValueError:
            return True
        if options.date_from and day < options.date_from:
            return False
        if options.date_to and day > options.date_to:
            return False
        return True

    @staticmethod
    def _restrict_ok(x_restrict: int, options: Options) -> bool:
        if options.r18 == "safe":
            return x_restrict == 0
        if options.r18 == "only":
            return x_restrict >= 1
        return True

    # -- 执行 -----------------------------------------------------------

    async def run(self, client: PixivClient) -> None:
        if self.status not in ("ready", "paused", "failed"):
            raise RuntimeError(f"当前状态不能开始下载：{self.status}")
        self.status = "running"
        self.started_at = time.time()
        self._cancel.clear()
        self._pause.clear()
        self.emit({"type": "status", "status": "running"})

        semaphore = asyncio.Semaphore(self.options.concurrency)

        async def worker(item: Item) -> None:
            await semaphore.acquire()
            try:
                if self._cancel.is_set():
                    return
                while self._pause.is_set():
                    await asyncio.sleep(0.2)
                await self._process(item, client)
            finally:
                semaphore.release()

        try:
            await asyncio.gather(*(worker(i) for i in self.items if i.status == "pending"))
        finally:
            self.finished_at = time.time()
            if self.status != "paused":
                self.status = "done" if not self._cancel.is_set() else "cancelled"
            self.emit({"type": "finished", **self.snapshot()})

    async def _process(self, item: Item, client: PixivClient) -> None:
        if item.kind == WORK_NOVEL:
            await self._save_novel(item, client)
            return
        await self._save_image(item, client)

    async def _save_image(self, item: Item, client: PixivClient) -> None:
        if item.path.exists() and item.path.stat().st_size > 0:
            item.status = "skipped"
            self.emit({"type": "item", **item.to_dict()})
            return
        try:
            if item.kind == WORK_UGOIRA:
                await self._save_ugoira(item, client)
            else:
                size = await client.download_file(
                    item.url, item.path, referer=f"https://www.pixiv.net/artworks/{item.work_id}"
                )
                item.bytes_written = size
                self.bytes_total += size
            item.status = "done"
        except Exception as exc:  # noqa: BLE001
            item.status = "failed"
            item.error = str(exc)
            self.emit({"type": "error", "message": f"{item.path.name}: {exc}"})
        self.emit({"type": "item", **item.to_dict()})

    async def _save_ugoira(self, item: Item, client: PixivClient) -> None:
        meta = await client.ugoira_meta(item.work_id)
        zip_url = meta.get("originalSrc") or meta.get("src") or ""
        frames = meta.get("frames") or []
        if not zip_url or not frames:
            raise PixivError("取不到动图元数据（可能需要登录）")

        item.path.parent.mkdir(parents=True, exist_ok=True)
        # 每个动图用独立临时目录：多个动图并发下载时，共用一个 zip 文件名会撞车
        base_tmp = item.path.parent / ".ugoira_tmp"
        work_tmp = base_tmp / str(item.work_id)
        work_tmp.mkdir(parents=True, exist_ok=True)
        try:
            zip_path = work_tmp / "ugoira.zip"
            await client.download_file(
                zip_url, zip_path, referer=f"https://www.pixiv.net/artworks/{item.work_id}"
            )
            build_gif_from_zip(zip_path, frames, item.path)
            item.bytes_written = item.path.stat().st_size
            self.bytes_total += item.bytes_written
        finally:
            shutil.rmtree(work_tmp, ignore_errors=True)
            try:
                base_tmp.rmdir()  # 空了就顺手清掉
            except OSError:
                pass

    async def _save_novel(self, item: Item, client: PixivClient) -> None:
        if item.path.exists() and item.path.stat().st_size > 0:
            item.status = "skipped"
            self.emit({"type": "item", **item.to_dict()})
            return
        try:
            detail = await client.novel_detail(item.work_id)
            body = html_to_text(detail.get("content") or "")
            if not body:
                body = await client.novel_text(item.work_id)
            markdown = render_novel_markdown(
                title=item.title,
                link=item.url,
                description=detail.get("description") or detail.get("caption") or "",
                body=body,
            )
            item.path.parent.mkdir(parents=True, exist_ok=True)
            item.path.write_text(markdown, encoding="utf-8")
            item.bytes_written = item.path.stat().st_size
            self.bytes_total += item.bytes_written
            item.status = "done"
        except Exception as exc:  # noqa: BLE001
            item.status = "failed"
            item.error = str(exc)
            self.emit({"type": "error", "message": f"{item.path.name}: {exc}"})
        self.emit({"type": "item", **item.to_dict()})

    # -- 控制 -----------------------------------------------------------

    def pause(self) -> None:
        if self.status == "running":
            self._pause.set()
            self.status = "paused"
            self.emit({"type": "status", "status": "paused"})

    def resume(self) -> None:
        if self.status == "paused":
            self._pause.clear()
            self.status = "running"
            self.emit({"type": "status", "status": "running"})

    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()


# ------------------------------------------------------------------ 动图合成


def _gif_frame(image: Image.Image) -> Image.Image:
    """把一帧转成 GIF 能稳定保存的调色板模式。

    直接把 RGBA 多帧塞给 Pillow 存 GIF 会抛 ``invalid palette size``，
    所以先把透明通道压到白底，再量化成 256 色。
    """
    if image.mode in ("RGBA", "LA", "P"):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        image = background
    rgb = image.convert("RGB")
    try:
        return rgb.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
    except Exception:  # noqa: BLE001
        return rgb.quantize(colors=256)


def build_gif_from_zip(zip_path: Path, frames: list[dict[str, Any]], dest: Path) -> None:
    """把 ugoira 的 zip + 帧延迟表合成一个 GIF。"""
    durations: list[int] = []
    images: list[Image.Image] = []

    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        for frame in frames:
            name = frame.get("file") or ""
            if name not in names:
                continue
            with archive.open(name) as handle:
                data = handle.read()
            images.append(_gif_frame(Image.open(io.BytesIO(data))))
            durations.append(max(20, int(frame.get("delay") or 100)))

    if not images:
        raise PixivError("动图 zip 里没有可用的帧")

    first, rest = images[0], images[1:]
    first.save(
        dest,
        format="GIF",
        save_all=True,
        append_images=rest,
        duration=durations,
        loop=0,
        disposal=2,
        optimize=False,
    )
    for image in images:
        image.close()
