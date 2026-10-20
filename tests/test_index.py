"""下载索引的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pixiv_dl.index import DownloadIndex  # noqa: E402


def test_record_and_reload(tmp_path: Path) -> None:
    index = DownloadIndex.load(tmp_path)
    index.record(
        111,
        ("2024-05-01", "Blue"),
        "Blue",
        [(1, tmp_path / "a_1.png"), (2, tmp_path / "a_2.png")],
    )
    index.save()

    again = DownloadIndex.load(tmp_path)
    pages = again.pages(111)
    assert len(pages) == 2
    assert pages[0]["file"] == "a_1.png"
    assert pages[1]["seq"] == 2
    assert again.existing_path(111, 0) == tmp_path / "a_1.png"


def test_subfolder_path_roundtrip(tmp_path: Path) -> None:
    folder = tmp_path / "2024-05-01_Big"
    index = DownloadIndex.load(tmp_path)
    index.record(222, ("2024-05-01", "Big"), "Big", [(1, folder / "2024-05-01_Big_1.png")])
    index.save()

    again = DownloadIndex.load(tmp_path)
    page = again.pages(222)[0]
    assert page["file"] == "2024-05-01_Big/2024-05-01_Big_1.png"
    assert again.existing_path(222, 0) == folder / "2024-05-01_Big_1.png"


def test_missing_work(tmp_path: Path) -> None:
    index = DownloadIndex.load(tmp_path)
    assert index.pages(999) == []
    assert index.existing_path(999, 0) is None
    assert index.existing_path(999, 5) is None


def test_restore_names_takes_max_seq(tmp_path: Path) -> None:
    index = DownloadIndex.load(tmp_path)
    index.record(1, ("2024-05-01", "Blue"), "Blue", [(1, tmp_path / "x1"), (3, tmp_path / "x3")])
    index.record(2, ("2024-05-01", "Blue"), "Blue", [(4, tmp_path / "x4")])
    assert index.restore_names() == {("2024-05-01", "Blue"): 4}


def test_already_done_checks_files(tmp_path: Path) -> None:
    index = DownloadIndex.load(tmp_path)
    target = tmp_path / "f.png"
    target.write_bytes(b"data")
    index.record(1, ("d", "t"), "t", [(1, target)])
    assert index.already_done(1) is True

    target.unlink()
    assert index.already_done(1) is False


def test_zero_byte_counts_as_missing(tmp_path: Path) -> None:
    index = DownloadIndex.load(tmp_path)
    target = tmp_path / "f.png"
    target.write_bytes(b"")
    index.record(1, ("d", "t"), "t", [(1, target)])
    assert index.already_done(1) is False


def test_corrupt_index_ignored(tmp_path: Path) -> None:
    (tmp_path / ".pixiv_dl_index.json").write_text("{not json", encoding="utf-8")
    index = DownloadIndex.load(tmp_path)
    assert index.works == {}


def test_save_is_noop_without_changes(tmp_path: Path) -> None:
    index = DownloadIndex.load(tmp_path)
    index.save()
    assert not (tmp_path / ".pixiv_dl_index.json").exists()
