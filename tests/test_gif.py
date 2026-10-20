"""动图（ugoira）合成 GIF 的单元测试，用本地构造的 zip，不联网。"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pixiv_dl.downloader import build_gif_from_zip  # noqa: E402


def _png(color: str) -> bytes:
    image = Image.new("RGB", (16, 16), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _gif_frame_count(path: Path) -> int:
    image = Image.open(path)
    count = 1
    try:
        while True:
            image.seek(image.tell() + 1)
            count += 1
    except EOFError:
        pass
    finally:
        image.close()
    return count


def test_builds_gif_in_frame_order(tmp_path: Path) -> None:
    zip_path = tmp_path / "ugoira.zip"
    frames = []
    with zipfile.ZipFile(zip_path, "w") as archive:
        for index, color in enumerate(["red", "green", "blue"]):
            name = f"{index:06d}.png"
            archive.writestr(name, _png(color))
            frames.append({"file": name, "delay": 100 + index})

    dest = tmp_path / "out.gif"
    build_gif_from_zip(zip_path, frames, dest)

    assert dest.exists()
    assert dest.stat().st_size > 0
    assert _gif_frame_count(dest) == 3


def test_missing_frame_files_are_skipped(tmp_path: Path) -> None:
    zip_path = tmp_path / "ugoira.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("000000.png", _png("red"))

    frames = [
        {"file": "000000.png", "delay": 80},
        {"file": "999999.png", "delay": 80},  # zip 里没有
    ]
    dest = tmp_path / "out.gif"
    build_gif_from_zip(zip_path, frames, dest)
    assert _gif_frame_count(dest) == 1


def test_delay_floor(tmp_path: Path) -> None:
    """延迟为 0 或负数时不能生成非法 GIF。"""
    zip_path = tmp_path / "ugoira.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("000000.png", _png("white"))
        archive.writestr("000001.png", _png("black"))

    frames = [{"file": "000000.png", "delay": 0}, {"file": "000001.png", "delay": -5}]
    dest = tmp_path / "out.gif"
    build_gif_from_zip(zip_path, frames, dest)

    image = Image.open(dest)
    assert image.info.get("duration", 0) >= 20
    image.close()
