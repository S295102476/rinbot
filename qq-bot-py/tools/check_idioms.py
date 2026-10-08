#!/usr/bin/env python3
"""Read-only local idiom-bank diagnostics: no NoneBot, network or database.

Run from any directory:
    python tools/check_idioms.py
    python tools/check_idioms.py --root /opt/qq-bot-py
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path


def _load_module():
    # Import the standalone rules file, never plugins/minigames/__init__.py.
    path = Path(__file__).resolve().parents[1] / "plugins" / "minigames" / "idioms.py"
    spec = importlib.util.spec_from_file_location("_idiom_bank_diagnostic", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载纯题库模块：{path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description="只读检查成语题库路径、文件完整性和双空出题；不读取配置或连接数据库")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="项目根目录，默认是本工具所在项目，不是当前工作目录")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    print(f"项目根目录：{root}")
    print("检查模式：仅本地只读；不加载机器人、不联网、不读取密钥、不写数据库")
    try:
        idioms = _load_module()
    except Exception as exc:
        print(f"检查失败：无法载入纯题库模块（{type(exc).__name__}）")
        print(f"应补传模块：{Path(__file__).resolve().parents[1] / 'plugins/minigames/idioms.py'}")
        return 2
    # SFTP uploads are not atomic across files. Diagnose an old, otherwise
    # importable rules module before dereferencing its newer interfaces.
    incompatible = [name for name in ("data_directory", "load_default")
                    if not callable(getattr(idioms, name, None))]
    files = getattr(idioms, "DATA_FILES", None)
    if not isinstance(files, (tuple, list)) or not files or any(not isinstance(name, str) for name in files):
        incompatible.append("DATA_FILES")
    error_type = getattr(idioms, "IdiomBankError", None)
    if not isinstance(error_type, type) or not issubclass(error_type, Exception):
        incompatible.append("IdiomBankError")
    if incompatible:
        print("检查失败：诊断工具与题库模块版本不匹配，缺少或不兼容接口：" + "、".join(incompatible))
        print("请同步上传新版 plugins/minigames/idioms.py 与 tools/check_idioms.py，再重新运行检查")
        print(f"实际加载模块：{Path(__file__).resolve().parents[1] / 'plugins/minigames/idioms.py'}")
        return 2
    folder = idioms.data_directory(root)
    print(f"题库有效目录：{folder}")
    missing = []
    for filename in idioms.DATA_FILES:
        path = folder / filename
        present = path.is_file()
        print(f"[{'存在' if present else '缺失'}] {path}")
        if not present:
            missing.append(filename)
    readme = folder / "README.md"
    print(f"[{'存在' if readme.is_file() else '未上传'}] {readme}（说明文件，不影响加载）")
    print("缺失文件：" + ("、".join(missing) if missing else "无"))
    try:
        bank = idioms.load_default(root)
        question = bank.next_question()
    except idioms.IdiomBankError as exc:
        print(f"检查失败 [{getattr(exc, 'code', 'load_error')}]：{exc}")
        print("请补传缺失文件或修复报告的文件，再重新运行本工具；不要修改服务器密钥配置")
        return 1
    except Exception as exc:
        print(f"检查失败：题库规则载入异常（{type(exc).__name__}）")
        return 2
    print(f"清洗后四字词：{bank.word_count} 条")
    print(f"随机出题池：{bank.pool_size} 条")
    print(f"双空样题：{question['mask']}（{question['mask'].count('□')} 个空，合法答案 {len(question['answers'])} 个）")
    print("检查通过：本地题库可用于新开局；本工具未改变已有对局")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
