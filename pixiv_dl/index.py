"""下载索引：让重跑时复用上次的文件名。

为什么光靠「目标文件是否存在」判重不够：文件名里的序号是按
``(日期, 标题)`` 累计分配的，重跑时序号会从「已有最大值 + 1」重新开始，
文件名一变就变成「文件不存在」，于是整批重下。

索引把「作品 id -> 每页的序号和文件名」固定下来，重跑时编号不变，
判重才真正生效。索引落在输出目录里（``.pixiv_dl_index.json``），
跟着文件走，换目录重跑会重新建一份。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

INDEX_NAME = ".pixiv_dl_index.json"
INDEX_VERSION = 1


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return path.name


@dataclass
class DownloadIndex:
    root: Path
    works: dict[int, dict[str, Any]] = field(default_factory=dict)
    _dirty: bool = field(default=False, repr=False)

    # -- 读写 -----------------------------------------------------------

    @classmethod
    def load(cls, root: Path) -> "DownloadIndex":
        index = cls(root=root)
        try:
            raw = (root / INDEX_NAME).read_text(encoding="utf-8")
        except OSError:
            return index
        try:
            data = json.loads(raw)
        except ValueError:
            return index
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return index
        works = data.get("works")
        if isinstance(works, dict):
            for key, value in works.items():
                try:
                    index.works[int(key)] = value
                except (TypeError, ValueError):
                    continue
        return index

    def save(self) -> None:
        if not self._dirty:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": INDEX_VERSION,
            "works": {str(k): v for k, v in self.works.items()},
        }
        target = self.root / INDEX_NAME
        tmp = target.with_name(INDEX_NAME + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, target)
        self._dirty = False

    # -- 查询 -----------------------------------------------------------

    @staticmethod
    def _pages_of(entry: dict[str, Any]) -> list[dict[str, Any]]:
        pages = entry.get("pages")
        return [p for p in pages if isinstance(p, dict)] if isinstance(pages, list) else []

    def pages(self, work_id: int) -> list[dict[str, Any]]:
        entry = self.works.get(work_id)
        return self._pages_of(entry) if isinstance(entry, dict) else []

    def existing_path(self, work_id: int, page_index: int) -> Path | None:
        """索引里记录的第 page_index 页文件路径（不保证存在）。"""
        pages = self.pages(work_id)
        if page_index >= len(pages):
            return None
        rel = pages[page_index].get("file")
        return self.root / str(rel) if rel else None

    def already_done(self, work_id: int) -> bool:
        """该作品记录过的文件是否都还在硬盘上且有内容。"""
        pages = self.pages(work_id)
        if not pages:
            return False
        for page in pages:
            rel = page.get("file")
            if not rel:
                return False
            path = self.root / str(rel)
            try:
                if not path.is_file() or path.stat().st_size <= 0:
                    return False
            except OSError:
                return False
        return True

    def restore_names(self) -> dict[tuple[str, str], int]:
        """把索引记录的 (日期, 标题) -> 最大序号 交给 Namer，避免编号撞车。"""
        state: dict[tuple[str, str], int] = {}
        for entry in self.works.values():
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not (isinstance(key, list) and len(key) == 2):
                continue
            bucket = (str(key[0]), str(key[1]))
            best = state.get(bucket, 0)
            for page in self._pages_of(entry):
                try:
                    best = max(best, int(page.get("seq")))
                except (TypeError, ValueError):
                    continue
            state[bucket] = best
        return state

    # -- 写入 -----------------------------------------------------------

    def record(
        self,
        work_id: int,
        key: tuple[str, str],
        title: str,
        pages: list[tuple[int, Path]],
    ) -> None:
        """记录一个作品的编号分配：``pages`` 是 ``[(序号, 绝对路径)]``。"""
        self.works[work_id] = {
            "key": [key[0], key[1]],
            "title": title,
            "pages": [
                {"seq": seq, "file": _rel(path, self.root)} for seq, path in pages
            ],
        }
        self._dirty = True
