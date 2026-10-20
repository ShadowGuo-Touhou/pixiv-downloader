"""命名与目录布局规则。

这里只做纯逻辑（没有网络、没有副作用），方便单元测试。
规则汇总：

* 图片：``year-mon-day_Title_count.ext``
* 页数 > 8 的作品：文件放进同目录下的 ``year-mon-day_Title`` 文件夹
* 小说：``year-mon-day_Title.md``
* 同名同日期的后续作品，count 接续累计，而不是从 1 重来
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

#: 标题按 UTF-8 字节截断，给日期/序号/扩展名留余量，避免超 Windows 260 字符上限
MAX_TITLE_BYTES = 120
#: 页数超过这个值的作品放进单独文件夹
SPLIT_THRESHOLD = 8

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_WS = re.compile(r"\s+")
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_UID_RE = re.compile(r"/users/(\d+)")
_AID_RE = re.compile(r"/artworks/(\d+)")
_NID_RE = re.compile(r"/novel/show\.php\?id=(\d+)")
_QID_RE = re.compile(r"[?&]id=(\d+)")
_BARE_ID_RE = re.compile(r"^(\d{3,})$")

#: 用于从磁盘上已有文件反推序号（支持断点续传）
_EXISTING_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})_(?P<title>.+)_(?P<seq>\d+)\.[A-Za-z0-9]+$"
)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


# ---------------------------------------------------------------- 解析用户输入


def parse_user_id(text: str) -> int | None:
    """从作者主页 URL、纯 id 或 ``?id=`` 里取出用户 id。"""
    if not text:
        return None
    s = text.strip()
    m = _UID_RE.search(s)
    if m:
        return int(m.group(1))
    m = _BARE_ID_RE.match(s)
    if m:
        return int(m.group(1))
    m = _QID_RE.search(s)
    return int(m.group(1)) if m else None


def parse_artwork_id(text: str) -> int | None:
    if not text:
        return None
    m = _AID_RE.search(text)
    if m:
        return int(m.group(1))
    m = _BARE_ID_RE.match(text.strip())
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- 时间与标题


def parse_iso(value: str) -> datetime:
    """解析 Pixiv 的 ``createDate``（形如 ``2024-05-01T12:34:56+09:00``）。"""
    s = (value or "").strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    return datetime.fromisoformat(s)


def format_date(create_date: str, tz: str = "jst") -> str:
    """``createDate`` -> ``YYYY-MM-DD``。默认按 Pixiv 所在的 JST 切日。"""
    dt = parse_iso(create_date)
    if tz == "jst":
        dt = dt.astimezone(JST)
    elif tz == "local":
        dt = dt.astimezone()
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def title_sanitize(title: str, max_bytes: int = MAX_TITLE_BYTES) -> str:
    """去掉 Windows 非法字符、压缩空白、按字节截断、规避保留名。"""
    s = unicodedata.normalize("NFC", title or "")
    # 整个标题都是非法字符时，替换出来的 "_" 没有意义，直接给个占位名
    if not _ILLEGAL.sub("", s).strip(" ."):
        return "untitled"
    s = _ILLEGAL.sub("_", s)
    s = _WS.sub(" ", s).strip(" .")
    if not s:
        return "untitled"

    encoded = s.encode("utf-8")
    if len(encoded) > max_bytes:
        s = encoded[:max_bytes].decode("utf-8", "ignore").rstrip(" .")
        if not s:
            return "untitled"

    if s.upper() in _RESERVED:
        s += "_"
    return s


def extension_from_url(url: str, default: str = ".jpg") -> str:
    path = (url or "").split("?", 1)[0]
    ext = Path(path).suffix.lower()
    if ext == ".jpeg":
        return ".jpg"
    return ext if ext in _IMAGE_EXTS else default


# ---------------------------------------------------------------- 布局


@dataclass
class Namer:
    """把「作品元数据」映射成「磁盘上的相对路径」，并维护序号状态。

    序号状态按 ``(日期, 清洗后标题)`` 归类：同一组内，后一个作品的页序号
    接着前一个继续累加。
    """

    root: Path
    tz: str = "jst"
    split_threshold: int = SPLIT_THRESHOLD

    _next_seq: dict[tuple[str, str], int] = field(default_factory=dict, repr=False)
    _folder_taken: set[str] = field(default_factory=set, repr=False)
    _doc_taken: set[str] = field(default_factory=set, repr=False)
    _key_folder: dict[tuple[str, str], str] = field(default_factory=dict, repr=False)

    # -- 序号 ------------------------------------------------------------

    def key_of(self, create_date: str, title: str) -> tuple[str, str]:
        return (format_date(create_date, self.tz), title_sanitize(title))

    def allocate(self, key: tuple[str, str], count: int) -> list[int]:
        start = self._next_seq.get(key, 1)
        seqs = list(range(start, start + count))
        self._next_seq[key] = start + count
        return seqs

    def restore(self, state: dict[tuple[str, str], int]) -> None:
        """用磁盘上已有文件重建序号状态（断点续传）。"""
        for key, max_seq in state.items():
            self._next_seq[key] = max(self._next_seq.get(key, 1), max_seq + 1)

    # -- 目录 ------------------------------------------------------------

    def _folder_name(self, key: tuple[str, str]) -> str:
        if key in self._key_folder:
            return self._key_folder[key]
        date_str, title = key
        base = f"{date_str}_{title}"
        name = base
        n = 2
        while name in self._folder_taken:
            name = f"{base}_{n}"
            n += 1
        self._folder_taken.add(name)
        self._key_folder[key] = name
        return name

    def image_path(self, key: tuple[str, str], seq: int, page_count: int, ext: str) -> Path:
        date_str, title = key
        filename = f"{date_str}_{title}_{seq}{ext}"
        if page_count > self.split_threshold:
            return self.root / self._folder_name(key) / filename
        return self.root / filename

    # -- 小说 ------------------------------------------------------------

    def novel_path(self, key: tuple[str, str]) -> Path:
        date_str, title = key
        base = f"{date_str}_{title}"
        name = base
        n = 2
        while name in self._doc_taken:
            name = f"{base}_{n}"
            n += 1
        self._doc_taken.add(name)
        return self.root / f"{name}.md"


def scan_existing(root: Path) -> dict[tuple[str, str], int]:
    """扫描目标目录，返回 ``{(日期, 标题): 已用的最大序号}``。"""
    state: dict[tuple[str, str], int] = {}
    if not root.exists():
        return state
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        m = _EXISTING_RE.match(p.name)
        if not m:
            continue
        key = (m.group("date"), m.group("title"))
        seq = int(m.group("seq"))
        if seq > state.get(key, 0):
            state[key] = seq
    return state


# ---------------------------------------------------------------- 小说正文


def render_novel_markdown(title: str, link: str, description: str, body: str) -> str:
    """按约定的版式拼装小说 markdown。

    版式::

        [Title](Link)
        description

        ===

        Content
    """
    desc = (description or "").strip()
    parts = [f"[{title}]({link})", desc, "===", (body or "").strip()]
    return "\n".join(parts).rstrip() + "\n"
