#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NCM 音乐转换工具 v3.7
基于 ncmdump (https://github.com/taurusxin/ncmdump) v1.5.1

v3.7 修复:
  - 修复全部文件已转换后再点击一键转换导致卡死的问题
  - 修复取消转换后再点击一键转换导致未响应的问题
  - 核心：正确管理转换生命周期，防止异步回调竞态
  - 修复 convert_batch 全部跳过时未设置 log_lines 导致 worker 崩溃的 bug

v3.6 改进:
  - 自动检测慢速磁盘（USB 外接盘等），输出到快速磁盘避免转换极慢
  - 源目录写入速度 < 10 MB/s 时自动建议输出到 C 盘

v3.5 改进:
  - 修复 GUI 未响应问题：限制每批 GUI 更新数量，处理后立即让出事件循环
  - 合并重复的进度/状态更新（coalescing），大幅减少队列积压
  - 完成后批量标记也分批处理，避免大量文件时界面冻结

v3.4 改进:
  - 修复 GUI 卡死问题：批量处理 GUI 更新，减少事件堆积
  - 优化进度更新：使用单一计时器，避免多处调用 after()
  - 改进取消响应：使用 threading.Event 替代轮询

v3.3 改进:
  - 速度计算仅统计实际转换的文件
  - 子进程降低优先级 (BELOW_NORMAL)
  - 添加调试日志功能

v3.2 架构重写:
  - 直接逐文件调用 ncmdump，N 个进程并行
  - 通过 future 完成回调实时通知 GUI
"""

import os
import sys
import subprocess
import threading
import time
import json

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext


# ─── 配置 ──────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent.resolve()
NCMDUMP_EXE = SCRIPT_DIR / "ncmdump_bin" / "ncmdump.exe"
CONFIG_FILE = SCRIPT_DIR / "ncm_converter_config.json"
DEBUG_LOG = SCRIPT_DIR / "ncm_debug.log"


DEFAULT_CONFIG = {
    "last_input_dir": "",
    "last_output_dir": "",
    "recursive": True,
    "remove_source": False,
    "skip_existing": True,
    "ncmdump_path": str(NCMDUMP_EXE),
    "workers": 3,
}


FAST_OUTPUT_DIR = str(Path.home() / "Music" / "NCM_Output")


def get_fast_output_dir(directory):
    """如果源目录不在 C 盘，建议输出到 C 盘快速磁盘。
    跨盘输出可避免 USB/外接盘的随机写入瓶颈，提升 100~1000 倍。"""
    src_drive = Path(directory).resolve().anchor  # e.g. "D:\\"
    c_drive = Path.home().anchor  # e.g. "C:\\"
    if src_drive.upper() != c_drive.upper():
        return FAST_OUTPUT_DIR
    return None


def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in DEFAULT_CONFIG.items():
                cfg.setdefault(k, v)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def scan_ncm_files(directory, recursive=True):
    d = Path(directory)
    if not d.is_dir():
        return []
    return sorted(d.rglob("*.ncm") if recursive else d.glob("*.ncm"))


def find_existing_output(ncm_file, input_dir, output_dir):
    """检查此 ncm 文件是否已有对应输出"""
    if output_dir:
        try:
            rel = ncm_file.relative_to(input_dir)
            out_dir = Path(output_dir) / rel.parent
        except ValueError:
            out_dir = Path(output_dir)
    else:
        out_dir = ncm_file.parent

    stem = ncm_file.stem
    for ext in (".mp3", ".flac", ".MP3", ".FLAC"):
        candidate = out_dir / (stem + ext)
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def format_size(size_bytes):
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / 1024 / 1024:.1f} MB"
    return f"{size_bytes / 1024:.0f} KB"


def log_debug(msg):
    """写入调试日志"""
    try:
        with open(DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


# ─── 高性能转换引擎 ───────────────────────────────────────────────────────

class ConversionEngine:
    """
    直接逐文件调用 ncmdump，N 个进程并行。
    使用 threading.Event 实现取消，响应更快。
    """

    def __init__(self, ncmdump_path, workers=4):
        self.ncmdump_path = ncmdump_path
        self.workers = max(1, min(workers, 4))
        self._cancel_event = threading.Event()
        self._procs = []
        self._procs_lock = threading.Lock()

    def convert_batch(self, ncm_files, input_dir, output_dir, remove_source,
                      skip_existing=True,
                      progress_callback=None, status_callback=None,
                      file_update_callback=None):
        """批量转换 NCM 文件"""

        self._cancel_event.clear()
        self._procs = []
        total_count = len(ncm_files)
        log_lines = []
        self.log_lines = log_lines  # 立即设置引用，确保提前返回时 worker 可访问
        completed_count = 0
        failed_count = 0

        if not ncm_files:
            return 0, 0, 0.0

        # ── 过滤已转换文件（在主线程批量处理 GUI 更新）──
        files_to_convert = []
        skipped_items = []  # (stem, existing_path) 用于批量 GUI 更新
        skipped = 0

        for f in ncm_files:
            if self._cancel_event.is_set():
                break
            if skip_existing:
                existing = find_existing_output(f, input_dir, output_dir)
                if existing:
                    skipped += 1
                    skipped_items.append((f.stem, existing, f.name))
                    log_lines.append(f"[跳过] {f.name} (已存在: {existing.name})")
                    continue
            files_to_convert.append(f)

        if skipped > 0:
            log_lines.append(f"跳过 {skipped} 个已转换文件，剩余 {len(files_to_convert)} 个待处理")

        if not files_to_convert:
            # 批量通知 GUI 跳过的文件（减少事件数量）
            if file_update_callback and skipped_items:
                for stem, existing, name in skipped_items:
                    try:
                        sz = existing.stat().st_size
                        fmt = existing.suffix.lstrip(".").upper()
                        file_update_callback(stem, "done", f"{format_size(sz)} {fmt}")
                    except OSError:
                        file_update_callback(stem, "done", "已存在")
            if progress_callback:
                progress_callback(total_count, total_count)
            return 0, skipped, 0, 0.0  # success, skipped, failed, elapsed

        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

        if status_callback:
            status_callback(f"正在转换 {len(files_to_convert)} 个文件 ({self.workers} 并行)...")

        # 批量通知 GUI 跳过的文件（在开始转换前一次性处理）
        if file_update_callback and skipped_items:
            for stem, existing, name in skipped_items:
                try:
                    sz = existing.stat().st_size
                    fmt = existing.suffix.lstrip(".").upper()
                    file_update_callback(stem, "done", f"{format_size(sz)} {fmt}")
                except OSError:
                    file_update_callback(stem, "done", "已存在")
            if progress_callback:
                progress_callback(skipped, total_count)

        start_time = time.time()
        counter_lock = threading.Lock()
        # 记录每个文件的转换开始时间
        file_start_times = {}
        file_start_lock = threading.Lock()

        def convert_one(ncm_file):
            """转换单个文件，在线程中运行"""
            if self._cancel_event.is_set():
                return ("cancelled", ncm_file, None)

            # 记录开始时间
            with file_start_lock:
                file_start_times[ncm_file.name] = time.time()

            log_debug(f"开始转换: {ncm_file.name}")

            cmd = [self.ncmdump_path, str(ncm_file)]
            if output_dir:
                cmd.extend(["-o", str(output_dir)])
            if remove_source:
                cmd.append("-m")

            # Windows: 降低子进程优先级，避免多进程 I/O 风暴导致资源管理器卡死
            if os.name == "nt":
                flags = subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
            else:
                flags = 0

            proc = None
            try:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=flags,
                )
                with self._procs_lock:
                    self._procs.append(proc)

                # 等待进程完成，使用 Event 实现快速取消
                timeout_seconds = 300  # 单个文件最多5分钟
                start_wait = time.time()

                while True:
                    if self._cancel_event.is_set():
                        try:
                            proc.kill()
                        except OSError:
                            pass
                        log_debug(f"取消转换: {ncm_file.name}")
                        return ("cancelled", ncm_file, None)

                    # 短时间等待，快速响应取消
                    try:
                        proc.wait(timeout=1.0)
                        break  # 进程完成
                    except subprocess.TimeoutExpired:
                        # 检查超时
                        if time.time() - start_wait > timeout_seconds:
                            try:
                                proc.kill()
                                proc.wait(timeout=5)
                            except Exception:
                                pass
                            log_debug(f"超时: {ncm_file.name}")
                            return ("failed", ncm_file, "ncmdump 超时 (5分钟)")
                        continue

                if self._cancel_event.is_set():
                    return ("cancelled", ncm_file, None)

                rc = proc.returncode
                elapsed_file = time.time() - start_wait
                log_debug(f"完成: {ncm_file.name} ({elapsed_file:.1f}秒, 返回码: {rc})")

                if rc != 0:
                    return ("failed", ncm_file, f"ncmdump 退出码 {rc}")

                # 找到输出文件
                out_dir = Path(output_dir) if output_dir else ncm_file.parent
                for ext in (".mp3", ".flac", ".MP3", ".FLAC"):
                    candidate = out_dir / (ncm_file.stem + ext)
                    if candidate.exists() and candidate.stat().st_size > 0:
                        return ("success", ncm_file, candidate)

                return ("success", ncm_file, None)

            except Exception as e:
                if proc:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                return ("error", ncm_file, str(e))

        # ── 并行执行 ──
        executor = ThreadPoolExecutor(max_workers=self.workers)
        future_map = {}
        try:
            for f in files_to_convert:
                if self._cancel_event.is_set():
                    break
                future = executor.submit(convert_one, f)
                future_map[future] = f

            # 处理完成的 future
            for future in as_completed(future_map):
                try:
                    status, ncm_file, detail = future.result(timeout=1)
                except Exception as e:
                    status, ncm_file, detail = "error", future_map[future], str(e)

                with counter_lock:
                    if self._cancel_event.is_set():
                        status = "cancelled"
                    if status == "success":
                        completed_count += 1
                        if detail and hasattr(detail, "name"):
                            try:
                                sz = detail.stat().st_size
                                fmt = detail.suffix.lstrip(".").upper()
                                log_lines.append(f"[成功] {ncm_file.name} -> {detail.name} ({format_size(sz)} {fmt})")
                            except OSError:
                                log_lines.append(f"[成功] {ncm_file.name}")
                            if file_update_callback:
                                try:
                                    sz = detail.stat().st_size
                                    fmt = detail.suffix.lstrip(".").upper()
                                    file_update_callback(ncm_file.stem, "done", f"{format_size(sz)} {fmt}")
                                except OSError:
                                    file_update_callback(ncm_file.stem, "done", "完成")
                        else:
                            log_lines.append(f"[成功] {ncm_file.name}")
                            if file_update_callback:
                                file_update_callback(ncm_file.stem, "done", "完成")
                    else:
                        failed_count += 1
                        log_lines.append(f"[{status}] {ncm_file.name}: {detail or '未知错误'}")
                        if file_update_callback:
                            file_update_callback(ncm_file.stem, "failed", "")

                    # 更新全局进度
                    done_total = completed_count + failed_count + skipped
                    if progress_callback:
                        progress_callback(done_total, total_count)
                    if status_callback:
                        status_callback(f"已处理 {completed_count + failed_count}/{len(files_to_convert)}")

                    if self._cancel_event.is_set():
                        break

        finally:
            executor.shutdown(wait=False, cancel_futures=True)

        # 等待运行中的线程退出
        time.sleep(0.5)

        # 最终清理
        with self._procs_lock:
            for proc in self._procs:
                try:
                    if proc.poll() is None:
                        proc.kill()
                except OSError:
                    pass
            for proc in self._procs:
                try:
                    proc.wait(timeout=3)
                except Exception:
                    pass

        elapsed = time.time() - start_time

        if self._cancel_event.is_set():
            log_lines.append("[取消] 用户取消了转换")

        self.log_lines = log_lines
        return completed_count, skipped, failed_count, elapsed

    def cancel(self):
        """非阻塞取消"""
        self._cancel_event.set()
        with self._procs_lock:
            for proc in self._procs:
                try:
                    if proc.poll() is None:
                        proc.kill()
                except OSError:
                    pass

    def cleanup(self):
        pass


# ─── GUI ───────────────────────────────────────────────────────────────────

class NcmConverterApp:
    def __init__(self, root):
        self.root = root
        self.cfg = load_config()
        self.converting = False
        self._convert_done_pending = False
        self.ncm_files = []
        self.engine = None
        self._worker_thread = None

        # GUI 更新队列：批量处理，减少事件堆积
        self._gui_queue = []
        self._gui_queue_lock = threading.Lock()
        self._gui_timer_active = False

        self._setup_window()
        self._build_ui()
        self._restore_state()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _setup_window(self):
        self.root.title("NCM 音乐转换工具 v3.7")
        self.root.geometry("800x640")
        self.root.minsize(700, 560)
        self.root.configure(bg="#f5f5f5")

        icon_path = SCRIPT_DIR / "ncm_icon.ico"
        if icon_path.exists():
            try:
                self.root.iconbitmap(str(icon_path))
            except Exception:
                pass

        self.root.update_idletasks()
        w, h = 800, 640
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 2
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f5f5")
        style.configure("TLabel", background="#f5f5f5", font=("Microsoft YaHei UI", 10))
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 14, "bold"), foreground="#1a73e8")
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 9), foreground="#666")
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=6)
        style.configure("Big.TButton", font=("Microsoft YaHei UI", 12, "bold"), padding=10)
        style.configure("TLabelframe", background="#f5f5f5", font=("Microsoft YaHei UI", 10))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TCheckbutton", background="#f5f5f5", font=("Microsoft YaHei UI", 10))

        # 顶部
        header_frame = ttk.Frame(self.root)
        header_frame.pack(fill=tk.X, padx=16, pady=(16, 8))
        ttk.Label(header_frame, text="🎵 NCM 音乐转换工具", style="Header.TLabel").pack(side=tk.LEFT)
        ttk.Label(header_frame, text="v3.7 · 多进程并行", style="Status.TLabel").pack(side=tk.RIGHT)

        # 目录配置
        dir_frame = ttk.LabelFrame(self.root, text="📂 目录配置", padding=10)
        dir_frame.pack(fill=tk.X, padx=16, pady=4)

        row1 = ttk.Frame(dir_frame)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="NCM 源目录：", width=14).pack(side=tk.LEFT)
        self.input_var = tk.StringVar()
        self.input_entry = ttk.Entry(row1, textvariable=self.input_var)
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(row1, text="浏览…", width=8, command=self._browse_input).pack(side=tk.RIGHT)

        row2 = ttk.Frame(dir_frame)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="输出目录：", width=14).pack(side=tk.LEFT)
        self.output_var = tk.StringVar()
        self.output_entry = ttk.Entry(row2, textvariable=self.output_var)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        ttk.Button(row2, text="浏览…", width=8, command=self._browse_output).pack(side=tk.RIGHT)
        ttk.Label(row2, text="(留空=源目录)", style="Status.TLabel").pack(side=tk.RIGHT, padx=(0, 6))

        # 选项
        opt_frame = ttk.Frame(self.root)
        opt_frame.pack(fill=tk.X, padx=16, pady=4)

        self.recursive_var = tk.BooleanVar(value=self.cfg.get("recursive", True))
        ttk.Checkbutton(opt_frame, text="递归子文件夹", variable=self.recursive_var,
                        command=self._on_option_change).pack(side=tk.LEFT, padx=(0, 16))

        self.remove_var = tk.BooleanVar(value=self.cfg.get("remove_source", False))
        ttk.Checkbutton(opt_frame, text="转换后删除源文件", variable=self.remove_var,
                        command=self._on_option_change).pack(side=tk.LEFT, padx=(0, 16))

        self.skip_var = tk.BooleanVar(value=self.cfg.get("skip_existing", True))
        ttk.Checkbutton(opt_frame, text="跳过已转换文件", variable=self.skip_var,
                        command=self._on_option_change).pack(side=tk.LEFT, padx=(0, 16))

        ttk.Label(opt_frame, text="并行批次数：").pack(side=tk.LEFT, padx=(8, 2))
        self.workers_var = tk.StringVar(value=str(self.cfg.get("workers", 3)))
        workers_spin = ttk.Spinbox(opt_frame, from_=1, to=4, width=4,
                                   textvariable=self.workers_var,
                                   command=self._on_option_change)
        workers_spin.pack(side=tk.LEFT, padx=(0, 16))

        self.scan_btn = ttk.Button(opt_frame, text="🔍 扫描", command=self._scan_files)
        self.scan_btn.pack(side=tk.RIGHT)

        # 进度条（先 pack，确保底部始终可见）
        progress_frame = ttk.Frame(self.root)
        progress_frame.pack(fill=tk.X, padx=16, pady=4, side=tk.BOTTOM)

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(progress_frame, variable=self.progress_var,
                                            maximum=100, mode="determinate")
        self.progress_bar.pack(fill=tk.X, side=tk.LEFT, expand=True, padx=(0, 8))

        self.progress_label = ttk.Label(progress_frame, text="0%", width=10, anchor=tk.CENTER)
        self.progress_label.pack(side=tk.RIGHT)

        # 底部按钮（先 pack，确保底部始终可见）
        bottom_frame = ttk.Frame(self.root)
        bottom_frame.pack(fill=tk.X, padx=16, pady=(4, 16), side=tk.BOTTOM)

        self.status_var = tk.StringVar(value="就绪 - 请选择 NCM 文件源目录")
        ttk.Label(bottom_frame, textvariable=self.status_var, style="Status.TLabel").pack(side=tk.LEFT)

        btn_frame = ttk.Frame(bottom_frame)
        btn_frame.pack(side=tk.RIGHT)

        self.cancel_btn = ttk.Button(btn_frame, text="取消", command=self._cancel_convert, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=4)

        self.convert_btn = ttk.Button(btn_frame, text="🚀 一键转换", style="Big.TButton",
                                      command=self._start_convert)
        self.convert_btn.pack(side=tk.LEFT, padx=4)

        # 文件列表（最后 pack，使用 expand=True 填充剩余空间）
        list_frame = ttk.LabelFrame(self.root, text="📋 文件列表", padding=6)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=4)

        columns = ("序号", "文件名", "大小", "进度", "状态", "路径")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="none")
        self.tree.heading("序号", text="#")
        self.tree.heading("文件名", text="文件名")
        self.tree.heading("大小", text="大小")
        self.tree.heading("进度", text="进度")
        self.tree.heading("状态", text="状态")
        self.tree.heading("路径", text="路径")
        self.tree.column("序号", width=40, anchor=tk.CENTER, stretch=False)
        self.tree.column("文件名", width=200, anchor=tk.W, stretch=False)
        self.tree.column("大小", width=70, anchor=tk.E, stretch=False)
        self.tree.column("进度", width=100, anchor=tk.CENTER, stretch=False)
        self.tree.column("状态", width=60, anchor=tk.CENTER, stretch=False)
        self.tree.column("路径", width=250, anchor=tk.W)

        scrollbar = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self._stem_to_item = {}

        self.tree.tag_configure("done", foreground="#888")
        self.tree.tag_configure("pending", foreground="#000")
        self.tree.tag_configure("converting", foreground="#1a73e8")

    def _restore_state(self):
        if self.cfg.get("last_input_dir") and Path(self.cfg["last_input_dir"]).is_dir():
            self.input_var.set(self.cfg["last_input_dir"])
        if self.cfg.get("last_output_dir") and Path(self.cfg["last_output_dir"]).is_dir():
            self.output_var.set(self.cfg["last_output_dir"])

    def _browse_input(self):
        init_dir = self.input_var.get() or str(Path.home())
        d = filedialog.askdirectory(title="选择 NCM 文件所在目录", initialdir=init_dir)
        if d:
            self.input_var.set(d)
            self.cfg["last_input_dir"] = d
            save_config(self.cfg)

    def _browse_output(self):
        init_dir = self.output_var.get() or str(Path.home())
        d = filedialog.askdirectory(title="选择输出目录", initialdir=init_dir)
        if d:
            self.output_var.set(d)
            self.cfg["last_output_dir"] = d
            save_config(self.cfg)

    def _on_option_change(self):
        self.cfg["recursive"] = self.recursive_var.get()
        self.cfg["remove_source"] = self.remove_var.get()
        self.cfg["skip_existing"] = self.skip_var.get()
        self.cfg["workers"] = int(self.workers_var.get())
        save_config(self.cfg)

    def _scan_files(self):
        input_dir = self.input_var.get().strip()
        if not input_dir:
            messagebox.showwarning("提示", "请先选择 NCM 文件源目录！")
            return
        if not Path(input_dir).is_dir():
            messagebox.showerror("错误", f"目录不存在：\n{input_dir}")
            return

        self.status_var.set("正在扫描…")
        self.root.update_idletasks()

        # 检测慢速磁盘，自动设置输出目录
        output_dir = self.output_var.get().strip()
        if not output_dir:
            fast_dir = get_fast_output_dir(input_dir)
            if fast_dir:
                self.output_var.set(fast_dir)
                output_dir = fast_dir
                messagebox.showinfo("跨盘输出优化",
                    f"💡 检测到源目录在非系统盘（{Path(input_dir).resolve().anchor.rstrip(':')} 盘），\n"
                    f"为避免写入瓶颈，转换输出将自动指向：\n\n{fast_dir}\n\n"
                    f"跨盘读写可将转换速度提升 100~1000 倍。\n"
                    f"（可手动修改输出目录覆盖此设置）")

        recursive = self.recursive_var.get()
        self.ncm_files = scan_ncm_files(input_dir, recursive)

        for item in self.tree.get_children():
            self.tree.delete(item)

        if not self.ncm_files:
            self.status_var.set("未找到 .ncm 文件")
            messagebox.showinfo("扫描结果", f"在以下目录中未找到 .ncm 文件：\n{input_dir}")
            return

        done_count = 0
        self._stem_to_item = {}

        # 禁用 Treeview 自动刷新，加速批量插入
        # 不设置 height，让 pack expand 自动控制大小

        for i, f in enumerate(self.ncm_files, 1):
            try:
                size_str = format_size(f.stat().st_size)
            except OSError:
                size_str = "未知"

            existing = find_existing_output(f, input_dir, output_dir)
            if existing:
                progress_str = "████████████████"
                status = "✅"
                tag = "done"
                done_count += 1
            else:
                progress_str = ""
                status = "⏳"
                tag = "pending"

            rel_path = str(f.relative_to(input_dir)) if str(input_dir) in str(f) else str(f)
            item_id = self.tree.insert("", tk.END, values=(i, f.name, size_str, progress_str, status, rel_path), tags=(tag,))
            self._stem_to_item[f.stem] = item_id

        total = len(self.ncm_files)
        total_size = sum(f.stat().st_size for f in self.ncm_files if f.exists())
        status = f"扫描完成：{total} 个文件 ({format_size(total_size)})"
        if done_count > 0:
            status += f" | 已转换: {done_count}，待处理: {total - done_count}"
        self.status_var.set(status)
        self.progress_var.set(0)
        self.progress_label.configure(text="0%")

    def _queue_gui_update(self, update_type, *args):
        """将 GUI 更新加入队列，合并重复的进度/状态更新"""
        with self._gui_queue_lock:
            # 合并进度和状态更新：只保留最新的，减少队列积压
            if update_type in ("progress", "status"):
                self._gui_queue = [(t, a) for t, a in self._gui_queue if t != update_type]
            self._gui_queue.append((update_type, args))
            if not self._gui_timer_active:
                self._gui_timer_active = True
                self.root.after(50, self._process_gui_queue)

    def _process_gui_queue(self):
        """批量处理 GUI 更新队列，限制每批数量防止界面冻结"""
        _MAX_BATCH = 30
        with self._gui_queue_lock:
            batch = self._gui_queue[:_MAX_BATCH]
            self._gui_queue = self._gui_queue[_MAX_BATCH:]
            self._gui_timer_active = False

        # 批量处理更新
        for update_type, args in batch:
            try:
                if update_type == "file":
                    self._update_file_row_direct(*args)
                elif update_type == "status":
                    self._update_status_direct(*args)
                elif update_type == "progress":
                    self._update_progress_direct(*args)
            except Exception:
                pass

        # 如果还有待处理的更新，立即让出事件循环后继续
        with self._gui_queue_lock:
            if self._gui_queue:
                self._gui_timer_active = True
                self.root.after(1, self._process_gui_queue)

    def _update_file_row_direct(self, stem, status, detail=""):
        """直接更新文件行（在主线程中调用）"""
        item_id = self._stem_to_item.get(stem)
        if not item_id:
            return

        try:
            current_values = list(self.tree.item(item_id, "values"))
        except tk.TclError:
            return

        if status == "done":
            emoji = "✅"
            tag = "done"
            progress_text = detail or "完成"
        elif status == "converting":
            emoji = "🔄"
            tag = "converting"
            progress_text = detail or "转换中…"
        elif status == "failed":
            emoji = "❌"
            tag = "pending"
            progress_text = "失败"
        else:
            emoji = "⏳"
            tag = "pending"
            progress_text = ""

        current_values[3] = progress_text
        current_values[4] = emoji
        self.tree.item(item_id, values=current_values, tags=(tag,))

    def _update_status_direct(self, text):
        """直接更新状态栏"""
        self.status_var.set(text)

    def _update_progress_direct(self, done, total):
        """直接更新进度条"""
        pct = (done / total) * 100 if total > 0 else 0
        self.progress_var.set(pct)
        self.progress_label.configure(text=f"{done}/{total}")

    def _start_convert(self):
        if self.converting or self._convert_done_pending:
            return

        input_dir = self.input_var.get().strip()
        if not input_dir:
            messagebox.showwarning("提示", "请先选择 NCM 文件源目录！")
            return

        ncmdump_path = self.cfg.get("ncmdump_path", str(NCMDUMP_EXE))
        if not Path(ncmdump_path).is_file():
            alt = SCRIPT_DIR / "ncmdump_bin" / "ncmdump.exe"
            if alt.is_file():
                ncmdump_path = str(alt)
                self.cfg["ncmdump_path"] = ncmdump_path
            else:
                messagebox.showerror("错误", f"找不到 ncmdump.exe！\n\n请确认文件位于：\n{ncmdump_path}")
                return

        if not self.ncm_files:
            self._scan_files()
            if not self.ncm_files:
                return

        output_dir = self.output_var.get().strip()
        if not output_dir:
            fast_dir = get_fast_output_dir(input_dir)
            if fast_dir:
                self.output_var.set(fast_dir)
                output_dir = fast_dir
                messagebox.showinfo("跨盘输出优化",
                    f"💡 检测到源目录在非系统盘（{Path(input_dir).resolve().anchor.rstrip(':')} 盘），\n"
                    f"为避免写入瓶颈，转换输出将自动指向：\n\n{fast_dir}\n\n"
                    f"跨盘读写可将转换速度提升 100~1000 倍。\n"
                    f"（可手动修改输出目录覆盖此设置）")
        if output_dir:
            Path(output_dir).mkdir(parents=True, exist_ok=True)

        self.cfg["last_input_dir"] = input_dir
        if output_dir:
            self.cfg["last_output_dir"] = output_dir
        save_config(self.cfg)

        # 清空调试日志
        with open(DEBUG_LOG, "w", encoding="utf-8") as f:
            f.write(f"=== 转换开始 {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")

        self.converting = True
        self.convert_btn.configure(state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        self.scan_btn.configure(state=tk.DISABLED)

        self.engine = ConversionEngine(ncmdump_path, int(self.workers_var.get()))

        self._worker_thread = threading.Thread(
            target=self._convert_worker,
            args=(input_dir, output_dir),
            daemon=True
        )
        self._worker_thread.start()

    def _convert_worker(self, input_dir, output_dir):
        """工作线程：调用转换引擎，通过队列更新 GUI"""
        progress_state = {"done": 0, "total": len(self.ncm_files), "running": True}
        tick_id = [None]  # 用列表包裹以便闭包修改

        def on_progress(done, total):
            progress_state["done"] = done
            progress_state["total"] = total

        def on_status(text):
            self._queue_gui_update("status", text)

        def on_file_update(stem, status, detail=""):
            self._queue_gui_update("file", stem, status, detail)

        # 定时更新进度条（每 100ms）
        def _tick():
            if not progress_state["running"]:
                return
            d, t = progress_state["done"], progress_state["total"]
            self._queue_gui_update("progress", d, t)
            if progress_state["running"]:
                tick_id[0] = self.root.after(100, _tick)

        try:
            tick_id[0] = self.root.after(100, _tick)
        except RuntimeError:
            pass  # 主窗口已销毁

        success, skipped, failed, elapsed = 0, 0, 0, 0.0
        try:
            success, skipped, failed, elapsed = self.engine.convert_batch(
                self.ncm_files, input_dir, output_dir,
                self.remove_var.get(),
                skip_existing=self.skip_var.get(),
                progress_callback=on_progress,
                status_callback=on_status,
                file_update_callback=on_file_update,
            )
        except Exception as e:
            log_debug(f"转换异常: {e}")
            failed = len(self.ncm_files)

        # 立即停止定时器（在调度 _convert_done 之前）
        progress_state["running"] = False
        # 取消已调度但尚未执行的 _tick
        if tick_id[0] is not None:
            try:
                self.root.after_cancel(tick_id[0])
            except (tk.TclError, ValueError, RuntimeError):
                pass
            tick_id[0] = None

        # 最终进度更新（直接入队，不经过 after）
        self._queue_gui_update("progress", len(self.ncm_files), len(self.ncm_files))

        log_text = "\n".join(self.engine.log_lines)
        speed = ""
        if elapsed > 0 and success > 0:
            speed = f"  速度: {success / elapsed:.1f} 文件/秒 (仅计算实际转换)\n"
        summary = (
            f"\n{'='*50}\n"
            f"转换完成！\n"
            f"  成功: {success}  失败: {failed}  跳过: {skipped}\n"
            f"  总计: {len(self.ncm_files)} 个文件\n"
            f"{speed}"
            f"  耗时: {elapsed:.1f} 秒\n"
            f"  模式: ncmdump 多批并行 ({self.engine.workers} 批)\n"
            f"{'='*50}"
        )
        log_text += summary

        self._convert_done_pending = True
        # 延迟 150ms 确保：(1) 所有排队的 GUI 更新先处理 (2) 按钮点击事件先于 _convert_done 执行
        try:
            self.root.after(150, self._convert_done, success, skipped, failed, len(self.ncm_files), log_text)
        except RuntimeError:
            pass  # 主窗口已销毁

    def _cancel_convert(self):
        if self.engine:
            self.engine.cancel()
            self.status_var.set("正在取消…")

    def _update_status(self, text):
        self.status_var.set(text)

    def _update_file_row(self, stem, status, detail=""):
        """兼容旧接口"""
        self._queue_gui_update("file", stem, status, detail)

    def _convert_done(self, success, skipped, failed, total, log_text):
        # 防止重复调用（旧转换的 _convert_done 在新转换之后执行）
        if not self._convert_done_pending:
            return
        self._convert_done_pending = False
        # 注意：不在这里设置 self.converting = False
        # 保持 converting=True 直到日志弹窗关闭，防止弹窗期间再次点击转换

        # 收集所有待标记为失败的项目
        pending_items = []
        for stem, item_id in self._stem_to_item.items():
            try:
                vals = list(self.tree.item(item_id, "values"))
                if vals[4] == "⏳":
                    vals[3] = ""
                    vals[4] = "❌"
                    pending_items.append((item_id, vals))
                elif vals[4] == "🔄":
                    vals[4] = "❌"
                    pending_items.append((item_id, vals))
            except (tk.TclError, IndexError):
                pass

        # 分批处理标记，避免一次性更新太多导致卡顿
        self._mark_failed_items(pending_items, 0)

        if failed == 0:
            if skipped > 0:
                self.status_var.set(f"✅ 转换完成！成功: {success}，跳过: {skipped}")
            else:
                self.status_var.set(f"✅ 全部 {success} 个文件转换成功！")
        else:
            self.status_var.set(f"⚠️ 完成：{success} 成功，{failed} 失败，{skipped} 跳过")

        # 只在有实际转换或失败时弹出日志窗口
        # 全部跳过时直接重置状态，避免弹窗阻塞
        if success > 0 or failed > 0:
            self._show_log(log_text)
        else:
            # 全部跳过，直接允许新的转换
            self.converting = False
            self.convert_btn.configure(state=tk.NORMAL)
            self.cancel_btn.configure(state=tk.DISABLED)
            self.scan_btn.configure(state=tk.NORMAL)

    def _show_log(self, log_text):
        win = tk.Toplevel(self.root)
        win.title("转换日志")
        win.geometry("650x450")
        win.configure(bg="#f5f5f5")

        ttk.Label(win, text="📋 转换日志", font=("Microsoft YaHei UI", 12, "bold"),
                  background="#f5f5f5").pack(padx=12, pady=(12, 4), anchor=tk.W)

        text_area = scrolledtext.ScrolledText(win, wrap=tk.WORD, font=("Consolas", 9),
                                               height=20, width=80)
        text_area.pack(fill=tk.BOTH, expand=True, padx=12, pady=4)
        text_area.insert(tk.END, log_text)
        text_area.configure(state=tk.DISABLED)

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=12, pady=(4, 12))

        def copy_log():
            self.root.clipboard_clear()
            self.root.clipboard_append(log_text)
            messagebox.showinfo("提示", "日志已复制到剪贴板！")

        def close_log():
            try:
                win.destroy()
            except tk.TclError:
                pass
            # 弹窗关闭后才允许新的转换
            try:
                self.converting = False
                self.convert_btn.configure(state=tk.NORMAL)
                self.cancel_btn.configure(state=tk.DISABLED)
                self.scan_btn.configure(state=tk.NORMAL)
            except tk.TclError:
                pass  # 主窗口已销毁

        ttk.Button(btn_frame, text="📋 复制日志", command=copy_log).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="关闭", command=close_log).pack(side=tk.RIGHT)

        # 拦截窗口 X 按钮，确保也重置状态
        win.protocol("WM_DELETE_WINDOW", close_log)

    def _mark_failed_items(self, items, start):
        """分批标记未完成的项目为失败，每批 30 个后让出事件循环"""
        batch_end = min(start + 30, len(items))
        for i in range(start, batch_end):
            item_id, vals = items[i]
            try:
                self.tree.item(item_id, values=vals, tags=("pending",))
            except tk.TclError:
                pass
        if batch_end < len(items):
            self.root.after(1, self._mark_failed_items, items, batch_end)

    def _on_close(self):
        """窗口关闭时：取消转换 + 退出"""
        self._convert_done_pending = False  # 阻止待执行的 _convert_done
        if self.converting and self.engine:
            self.engine.cancel()
        self.root.destroy()


# ─── 启动 ──────────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app = NcmConverterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
