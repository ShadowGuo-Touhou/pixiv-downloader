"""极简测试运行器，不依赖 pytest。

    python run_tests.py

支持两种写法：
  * 模块级 ``test_xxx`` 函数
  * ``TestXxx`` 类里的 ``test_xxx`` 方法
参数名叫 ``tmp_path`` 时自动注入一个临时目录。
"""

from __future__ import annotations

import importlib.util
import inspect
import shutil
import sys
import tempfile
import traceback
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

PASSED = 0
FAILED = 0
FAILURES: list[str] = []

#: 临时目录放在项目内，避免被系统的沙箱/权限策略拦住
TMP_ROOT = ROOT / "_tmp_tests"


def _mkdtemp() -> Path:
    """自己 mkdir 而不是 tempfile.mkdtemp。

    mkdtemp 在 Windows 上会用受限 ACL 建目录，某些受限令牌（例如沙箱）
    随后写不进去；普通 mkdir 会继承父目录的权限，更稳。
    """
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / f"t_{uuid.uuid4().hex[:10]}"
    path.mkdir()
    return path


def _call(func, label: str) -> None:
    global PASSED, FAILED
    kwargs = {}
    if "tmp_path" in inspect.signature(func).parameters:
        temp = _mkdtemp()
        kwargs["tmp_path"] = temp
    try:
        func(**kwargs)
    except Exception:  # noqa: BLE001
        FAILED += 1
        FAILURES.append(label)
        print(f"  FAIL  {label}")
        traceback.print_exc()
    else:
        PASSED += 1
        print(f"  ok    {label}")
    finally:
        if "tmp_path" in kwargs:
            shutil.rmtree(kwargs["tmp_path"], ignore_errors=True)


def _run_module(path: Path) -> None:
    print(f"\n{path.name}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        print(f"  skip  {path.name}（无法加载）")
        return
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    for name, obj in vars(module).items():
        if name.startswith("test_") and callable(obj):
            _call(obj, f"{path.stem}::{name}")
        elif name.startswith("Test") and inspect.isclass(obj):
            instance = obj()
            for method_name in dir(instance):
                if not method_name.startswith("test_"):
                    continue
                method = getattr(instance, method_name)
                if not callable(method):
                    continue
                label = f"{path.stem}::{name}::{method_name}"
                if "tmp_path" in inspect.signature(method).parameters:
                    # 类方法版：自己传临时目录
                    temp = _mkdtemp()
                    try:
                        method(temp)
                    except Exception:  # noqa: BLE001
                        global FAILED
                        FAILED += 1
                        FAILURES.append(label)
                        print(f"  FAIL  {label}")
                        traceback.print_exc()
                    else:
                        global PASSED
                        PASSED += 1
                        print(f"  ok    {label}")
                    finally:
                        shutil.rmtree(temp, ignore_errors=True)
                else:
                    _call(method, label)


def main() -> int:
    tests = sorted((ROOT / "tests").glob("test_*.py"))
    if not tests:
        print("tests/ 下没有测试文件")
        return 1
    for path in tests:
        _run_module(path)

    print(f"\n合计：通过 {PASSED}，失败 {FAILED}")
    if FAILURES:
        print("失败项：")
        for item in FAILURES:
            print("  -", item)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
