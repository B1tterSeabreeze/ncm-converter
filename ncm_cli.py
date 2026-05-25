#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NCM 音乐转换工具 - 命令行版
用法: python ncm_cli.py [选项]

示例:
  python ncm_cli.py -d D:/Music/NCM                    # 扫描并转换
  python ncm_cli.py -d D:/Music/NCM -o D:/Music/MP3    # 指定输出目录
  python ncm_cli.py -d D:/Music/NCM -r                  # 递归子文件夹
  python ncm_cli.py -d D:/Music/NCM -r -m               # 递归+删除源文件
  python ncm_cli.py 1.ncm 2.ncm                          # 转换指定文件
"""

import os
import sys
import subprocess
import argparse
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
NCMDUMP_EXE = SCRIPT_DIR / "ncmdump_bin" / "ncmdump.exe"


def find_ncmdump():
    """查找 ncmdump.exe"""
    if NCMDUMP_EXE.is_file():
        return str(NCMDUMP_EXE)
    # 尝试 PATH
    import shutil
    found = shutil.which("ncmdump")
    if found:
        return found
    return None


def scan_ncm(directory, recursive=True):
    """扫描目录下的 .ncm 文件"""
    d = Path(directory)
    if recursive:
        return sorted(d.rglob("*.ncm"))
    return sorted(d.glob("*.ncm"))


def format_size(size_bytes):
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024:.0f} KB"


def convert_file(ncmdump, ncm_file, output_dir=None, remove_source=False, input_dir=None):
    """转换单个 NCM 文件"""
    cmd = [ncmdump]

    if output_dir:
        if input_dir:
            try:
                rel = ncm_file.relative_to(input_dir)
                out_sub = Path(output_dir) / rel.parent
            except ValueError:
                out_sub = Path(output_dir)
        else:
            out_sub = Path(output_dir)
        out_sub.mkdir(parents=True, exist_ok=True)
        cmd.extend(["-o", str(out_sub)])

    if remove_source:
        cmd.append("-m")

    cmd.append(str(ncm_file))

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=120, encoding="utf-8", errors="replace"
        )
        return result.returncode == 0, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "超时(120s)"
    except Exception as e:
        return False, str(e)


def main():
    parser = argparse.ArgumentParser(
        description="NCM 音乐转换工具 - 基于 ncmdump",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n"
               "  python ncm_cli.py -d D:/Music/NCM\n"
               "  python ncm_cli.py -d D:/Music/NCM -o D:/Music/Output -r\n"
               "  python ncm_cli.py file1.ncm file2.ncm\n"
    )
    parser.add_argument("files", nargs="*", help="NCM 文件路径（可多个）")
    parser.add_argument("-d", "--directory", help="NCM 文件目录")
    parser.add_argument("-o", "--output", help="输出目录（默认源目录）")
    parser.add_argument("-r", "--recursive", action="store_true", help="递归子文件夹（需配合 -d）")
    parser.add_argument("-m", "--remove", action="store_true", help="转换成功后删除源文件")

    args = parser.parse_args()

    # 查找 ncmdump
    ncmdump = find_ncmdump()
    if not ncmdump:
        print("❌ 找不到 ncmdump.exe！")
        print(f"   请将 ncmdump.exe 放在: {NCMDUMP_EXE}")
        sys.exit(1)

    print(f"🔧 使用 ncmdump: {ncmdump}")
    print()

    # 收集文件
    ncm_files = []

    if args.files:
        for f in args.files:
            p = Path(f)
            if p.is_file() and p.suffix.lower() == ".ncm":
                ncm_files.append(p)
            elif p.is_file():
                print(f"⚠️  跳过非 NCM 文件: {p.name}")
            else:
                print(f"⚠️  文件不存在: {f}")

    if args.directory:
        if not Path(args.directory).is_dir():
            print(f"❌ 目录不存在: {args.directory}")
            sys.exit(1)
        ncm_files.extend(scan_ncm(args.directory, args.recursive))
        input_dir = args.directory
    else:
        input_dir = None

    if not ncm_files:
        print("❌ 未找到 NCM 文件！")
        print("   使用 -h 查看帮助")
        sys.exit(1)

    # 去重
    seen = set()
    unique_files = []
    for f in ncm_files:
        key = str(f.resolve())
        if key not in seen:
            seen.add(key)
            unique_files.append(f)
    ncm_files = unique_files

    total = len(ncm_files)
    total_size = sum(f.stat().st_size for f in ncm_files if f.exists())

    print(f"📁 找到 {total} 个 NCM 文件 ({format_size(total_size)})")
    if args.output:
        print(f"📂 输出到: {args.output}")
    if args.remove:
        print(f"🗑️  转换后删除源文件")
    print()

    # 转换
    success = 0
    failed = 0
    start_time = time.time()

    for i, ncm_file in enumerate(ncm_files, 1):
        print(f"  [{i:>{len(str(total))}}/{total}] {ncm_file.name} ... ", end="", flush=True)

        ok, msg = convert_file(ncmdump, ncm_file, args.output, args.remove, input_dir)

        if ok:
            success += 1
            # 查找输出文件
            for ext in (".mp3", ".flac", ".MP3", ".FLAC"):
                if args.output:
                    try:
                        rel = ncm_file.relative_to(input_dir)
                        candidate = Path(args.output) / rel.parent / (ncm_file.stem + ext)
                    except (ValueError, TypeError):
                        candidate = Path(args.output) / (ncm_file.stem + ext)
                else:
                    candidate = ncm_file.parent / (ncm_file.stem + ext)
                if candidate.exists():
                    fmt = ext.lstrip(".").upper()
                    size = format_size(candidate.stat().st_size)
                    print(f"✅ {fmt} ({size})")
                    break
            else:
                print("✅")
        else:
            failed += 1
            print(f"❌ {msg}")

    elapsed = time.time() - start_time

    print()
    print("=" * 50)
    print(f"✅ 成功: {success}   ❌ 失败: {failed}   总计: {total}")
    print(f"⏱️  耗时: {elapsed:.1f} 秒")
    print("=" * 50)


if __name__ == "__main__":
    main()
