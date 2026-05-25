#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文本切割程序 - TXT文件平均分割工具
支持大文件(100MB+)高效切割，智能编码检测，段落完整性保护
"""

import os
import sys
import time
import math
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# ========== 第三方库导入（带降级处理） ==========
try:
    import chardet
    HAS_CHARDET = True
except ImportError:
    HAS_CHARDET = False


# ========== 编码检测模块 ==========

# 常见编码列表（按优先级排序）
COMMON_ENCODINGS = ['utf-8', 'gbk', 'gb2312', 'utf-16-le', 'utf-16-be',
                    'latin-1', 'cp1252', 'iso-8859-1', 'utf-8-sig', 'ascii']


def detect_encoding(file_path, sample_size=65536):
    """
    自动检测TXT文件的编码格式。
    优先使用chardet库，失败时回退到启发式检测。

    Args:
        file_path: 文件路径
        sample_size: 采样大小（字节），默认64KB

    Returns:
        str: 检测到的编码名称(小写)
    """
    with open(file_path, 'rb') as f:
        raw_data = f.read(sample_size)

    # 检查BOM标记
    if raw_data.startswith(b'\xef\xbb\xbf'):
        return 'utf-8-sig'
    if raw_data.startswith(b'\xff\xfe'):
        return 'utf-16-le'
    if raw_data.startswith(b'\xfe\xff'):
        return 'utf-16-be'

    # 使用chardet检测（需要较高置信度）
    chardet_result = None
    if HAS_CHARDET:
        result = chardet.detect(raw_data)
        encoding = result.get('encoding', 'utf-8')
        confidence = result.get('confidence', 0)
        if encoding and confidence > 0.7:
            chardet_result = encoding.lower()
            # 统一编码名称
            if chardet_result in ('gb2312', 'gb18030', 'gbk'):
                return 'gbk'
            if chardet_result.startswith('utf-16'):
                return 'utf-16-le'
            return chardet_result

    # 启发式检测：优先尝试中文常见编码，用字符可见率评分
    candidates = ['utf-8', 'gbk', 'gb2312', 'utf-16-le', 'utf-16-be', 'latin-1']
    best_enc = 'utf-8'
    best_score = -1

    for enc in candidates:
        try:
            text = raw_data.decode(enc, errors='replace')
            # 评分：可打印字符占比（排除替换字符 �）
            total = len(text)
            if total == 0:
                continue
            printable = sum(1 for c in text if c.isprintable() or c in '\n\r\t')
            replacement = text.count('�')
            # 高分 = 高可打印率 + 低替换字符率
            score = (printable - replacement) / total
            if score > best_score:
                best_score = score
                best_enc = enc
        except (UnicodeDecodeError, LookupError):
            continue

    return best_enc


# ========== 文本分割核心模块 ==========

def _safe_encoding(file_path, encoding):
    """
    解决 utf-16 编码的 BOM 问题：
    Python 的 'utf-16' 要求流必须以 BOM 开头，否则直接报错。
    当文件没有 BOM 时，自动降级为 utf-16-le（Windows 默认小端序）。
    """
    if encoding == 'utf-16':
        with open(file_path, 'rb') as f:
            head = f.read(2)
        if head not in (b'\xff\xfe', b'\xfe\xff'):
            return 'utf-16-le'
    return encoding


def scan_file_info(file_path, encoding, max_empty_lines=1000000):
    """
    第一遍扫描：逐行统计文件总行数，并收集空行位置。
    采用内存友好的方式，不存储行内容，只存储行号。

    Args:
        file_path: 文件路径
        encoding: 文件编码
        max_empty_lines: 最大记录的空行数（防止极特殊情况内存溢出）

    Returns:
        tuple: (total_lines, empty_line_set)
               empty_line_set 为空行行号的集合(1-based)
    """
    total_lines = 0
    empty_lines = set()
    safe_enc = _safe_encoding(file_path, encoding)

    with open(file_path, 'r', encoding=safe_enc, errors='replace') as f:
        for line in f:
            total_lines += 1
            if line.strip() == '' and len(empty_lines) < max_empty_lines:
                empty_lines.add(total_lines)

    return total_lines, empty_lines


def compute_split_points(total_lines, parts, empty_lines):
    """
    基于空行位置计算智能分割点，优先在段落边界（空行）处分割。

    Args:
        total_lines: 总行数
        parts: 分割份数
        empty_lines: 空行行号的集合(1-based)

    Returns:
        list[int]: 每部分的结束行号（1-based，即为下一部分的起始行号前一行）
    """
    if parts <= 1:
        return []

    avg_lines = total_lines / parts
    split_points = []

    for i in range(1, parts):
        ideal_line = int(i * avg_lines)

        # 在理想行号前后各搜索30%范围寻找最佳分割点（空行/段落边界）
        search_range = max(1, int(avg_lines * 0.3))
        start_search = max(1, ideal_line - search_range)
        end_search = min(total_lines, ideal_line + search_range)

        best_point = ideal_line

        # 从理想行号向两边同时搜索空行
        found = False
        for offset in range(search_range):
            forward = ideal_line + offset
            backward = ideal_line - offset

            if backward >= start_search and backward in empty_lines:
                best_point = backward  # 空行本身属于上一部分
                found = True
                break

            if forward <= end_search and forward in empty_lines:
                best_point = forward  # 空行本身属于上一部分
                found = True
                break

        # 确保搜到Forward方向时也正确处理
        # 如果没有找到空行，就在理想位置精确分割
        if not found:
            best_point = ideal_line

        # 避免重复分割点并保持递增
        if split_points and best_point <= split_points[-1]:
            best_point = split_points[-1] + 1
        best_point = min(best_point, total_lines - (parts - i))  # 确保后面还有足够的行

        split_points.append(best_point)

    return split_points


def process_large_file(file_path, encoding, parts, output_dir, base_name, progress_callback):
    """
    处理大文件的文本分割（支持100MB+），采用两遍扫描策略。
    全程逐行读取，不将整个文件加载到内存。

    第一遍：统计总行数 + 收集空行位置
    第二遍：按分割点逐行写入各输出文件

    Args:
        file_path: 输入文件路径
        encoding: 文件编码
        parts: 分割份数
        output_dir: 输出目录
        base_name: 输出文件基础名称
        progress_callback: 进度回调函数 (percent, message)
    """
    # ===== 第一遍：扫描文件，统计行数与空行位置 =====
    progress_callback(3, "正在扫描文件(第一遍)...")
    total_lines, empty_lines = scan_file_info(file_path, encoding)

    if total_lines == 0:
        raise ValueError("文件内容为空，无法分割")

    if parts > total_lines:
        raise ValueError(f"分割份数({parts})不能大于文件总行数({total_lines})")

    progress_callback(8, f"文件共 {total_lines:,} 行，正在计算分割点...")

    # 计算分割点
    split_points = compute_split_points(total_lines, parts, empty_lines)
    # 释放空行集合以节省内存（如果文件很大）
    del empty_lines

    progress_callback(10, f"文件共 {total_lines:,} 行，开始分割为 {parts} 份...")

    # ===== 第二遍：逐行读取并写入各输出文件 =====
    safe_enc = _safe_encoding(file_path, encoding)
    output_files = []
    for i in range(parts):
        output_path = os.path.join(output_dir, f"{base_name}_{i + 1}.txt")
        f_out = open(output_path, 'w', encoding=safe_enc, errors='replace')
        output_files.append((f_out, output_path))

    try:
        part_sizes = [0] * parts
        line_count = 0
        part_idx = 0

        with open(file_path, 'r', encoding=safe_enc, errors='replace') as f_in:
            for line in f_in:
                if part_idx >= parts:
                    break

                f_out = output_files[part_idx][0]

                # 如果是该部分的第一行，写入头部注释
                if part_sizes[part_idx] == 0:
                    header = f"# 原文件的第 {part_idx + 1} 部分，共 {parts} 部分\n"
                    f_out.write(header)

                f_out.write(line)
                part_sizes[part_idx] += len(line.encode(safe_enc, errors='replace'))
                line_count += 1

                # 检查是否到达下一部分的分割点
                if part_idx < len(split_points) and line_count >= split_points[part_idx]:
                    part_idx += 1

                # 更新进度（每1000行更新一次）
                if line_count % 1000 == 0:
                    pct = min(95, 10 + int(85 * line_count / total_lines))
                    progress_callback(pct,
                        f"正在分割... {line_count:,}/{total_lines:,} 行 ({(line_count/total_lines*100):.1f}%)")

        # 确保所有部分至少有一个头部注释
        for i in range(parts):
            if part_sizes[i] == 0:
                output_files[i][0].write(f"# 原文件的第 {i + 1} 部分，共 {parts} 部分\n")

        progress_callback(98, "正在完成写入...")

    finally:
        # 确保所有输出文件关闭
        for f_out, _ in output_files:
            try:
                f_out.close()
            except Exception:
                pass

    # 获取各文件大小
    file_sizes = []
    for _, fpath in output_files:
        try:
            file_sizes.append(os.path.getsize(fpath))
        except Exception:
            file_sizes.append(0)

    progress_callback(100, f"分割完成！共生成 {parts} 个文件")
    return file_sizes


# ========== GUI模块 ==========

class TextSplitterApp:
    """文本切割程序主界面"""

    def __init__(self, root):
        self.root = root
        self.root.title("文本切割工具 - TXT文件平均分割")
        self.root.geometry("720x600")
        self.root.minsize(600, 500)

        # 设置应用图标（如果没有ico文件则忽略）
        try:
            if sys.platform == 'win32':
                self.root.iconbitmap(default='')
        except Exception:
            pass

        # 状态变量
        self.input_file = tk.StringVar()
        self.output_dir = tk.StringVar(value=os.path.expanduser("~\\Desktop"))
        self.parts_var = tk.StringVar(value="2")
        self.encoding_var = tk.StringVar(value="自动检测")
        self.status_var = tk.StringVar(value="就绪 - 请选择要切割的TXT文件")

        # 处理状态
        self.is_processing = False
        self.process_thread = None

        # 编码选项
        self.encoding_options = ["自动检测", "UTF-8", "UTF-8-SIG", "GBK", "GB2312",
                                  "UTF-16", "UTF-16-LE", "UTF-16-BE", "Latin-1", "ASCII"]

        self._create_widgets()
        self._setup_layout()

    def _create_widgets(self):
        """创建所有界面组件"""

        # ===== 样式配置 =====
        style = ttk.Style(self.root)
        style.theme_use('clam')

        # 自定义样式
        style.configure('Title.TLabel', font=('Microsoft YaHei', 14, 'bold'))
        style.configure('Section.TLabelframe', padding=10)
        style.configure('Section.TLabelframe.Label', font=('Microsoft YaHei', 10, 'bold'))
        style.configure('Action.TButton', font=('Microsoft YaHei', 10, 'bold'), padding=(20, 6))
        style.configure('Browse.TButton', padding=(8, 2))

        # ===== 主容器 =====
        self.main_frame = ttk.Frame(self.root, padding="15")
        self.main_frame.pack(fill=tk.BOTH, expand=True)

        # ===== 标题 =====
        title_frame = ttk.Frame(self.main_frame)
        title_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(title_frame, text="TXT文本文件平均分割工具", style='Title.TLabel').pack()

        # ===== 文件选择区 =====
        file_frame = ttk.LabelFrame(self.main_frame, text="文件选择", style='Section.TLabelframe')
        file_frame.pack(fill=tk.X, pady=(0, 8))

        # 输入文件
        row1 = ttk.Frame(file_frame)
        row1.pack(fill=tk.X, pady=3)
        ttk.Label(row1, text="输入文件:", width=10).pack(side=tk.LEFT)
        self.input_entry = ttk.Entry(row1, textvariable=self.input_file, state='readonly')
        self.input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row1, text="浏览...", style='Browse.TButton',
                   command=self._browse_input).pack(side=tk.RIGHT)

        # 输出目录
        row2 = ttk.Frame(file_frame)
        row2.pack(fill=tk.X, pady=3)
        ttk.Label(row2, text="输出目录:", width=10).pack(side=tk.LEFT)
        self.output_entry = ttk.Entry(row2, textvariable=self.output_dir)
        self.output_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 5))
        ttk.Button(row2, text="浏览...", style='Browse.TButton',
                   command=self._browse_output).pack(side=tk.RIGHT)

        # ===== 参数设置区 =====
        param_frame = ttk.LabelFrame(self.main_frame, text="参数设置", style='Section.TLabelframe')
        param_frame.pack(fill=tk.X, pady=(0, 8))

        param_inner = ttk.Frame(param_frame)
        param_inner.pack(fill=tk.X, pady=5)

        # 分割份数 - 输入框
        ttk.Label(param_inner, text="切割份数:", width=10).pack(side=tk.LEFT)
        self.parts_spinbox = ttk.Spinbox(param_inner, from_=1, to=1000,
                                          textvariable=self.parts_var, width=8,
                                          command=self._on_parts_change)
        self.parts_spinbox.pack(side=tk.LEFT, padx=(0, 10))

        # 分割份数 - 滑块
        self.parts_scale = ttk.Scale(param_inner, from_=1, to=1000, orient=tk.HORIZONTAL,
                                      command=self._on_scale_change)
        self.parts_scale.set(2)
        self.parts_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(5, 10))

        # 编码选择
        ttk.Label(param_inner, text="编码格式:", width=10).pack(side=tk.LEFT)
        self.encoding_combo = ttk.Combobox(param_inner, textvariable=self.encoding_var,
                                            values=self.encoding_options, state='readonly', width=14)
        self.encoding_combo.pack(side=tk.RIGHT, padx=(5, 0))

        # ===== 状态显示区 =====
        status_frame = ttk.LabelFrame(self.main_frame, text="处理状态", style='Section.TLabelframe')
        status_frame.pack(fill=tk.X, pady=(0, 8))

        # 进度条
        self.progress = ttk.Progressbar(status_frame, mode='determinate', length=100)
        self.progress.pack(fill=tk.X, pady=(5, 5), padx=10)

        # 状态文本
        self.status_label = ttk.Label(status_frame, textvariable=self.status_var,
                                       wraplength=650)
        self.status_label.pack(fill=tk.X, padx=10, pady=(0, 5))

        # ===== 文件信息预览区 =====
        info_frame = ttk.LabelFrame(self.main_frame, text="文件信息", style='Section.TLabelframe')
        info_frame.pack(fill=tk.X, pady=(0, 8))

        self.info_text = tk.Text(info_frame, height=4, wrap=tk.WORD, state=tk.DISABLED,
                                  font=('Consolas', 9), bg='#f5f5f5', relief=tk.FLAT)
        self.info_text.pack(fill=tk.X, padx=5, pady=5)

        # ===== 操作按钮区 =====
        btn_frame = ttk.Frame(self.main_frame)
        btn_frame.pack(fill=tk.X, pady=(5, 0))

        self.start_btn = ttk.Button(btn_frame, text="开始切割", style='Action.TButton',
                                     command=self._start_splitting)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10))

        self.cancel_btn = ttk.Button(btn_frame, text="取消", style='Action.TButton',
                                      command=self._cancel, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT)

        # 底部版权信息
        ttk.Label(self.main_frame, text="", font=('', 1)).pack()  # 间距

    def _setup_layout(self):
        """配置网格布局权重，实现自适应"""
        # 主区域随窗口缩放
        pass  # pack布局已使用fill和expand实现自适应

    # ========== 事件处理 ==========

    def _browse_input(self):
        """浏览选择输入文件"""
        file_path = filedialog.askopenfilename(
            title="选择TXT文本文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")]
        )
        if file_path:
            self.input_file.set(file_path)
            # 自动检测编码
            threading.Thread(target=self._auto_detect_encoding, args=(file_path,), daemon=True).start()
            self._update_file_info(file_path)

    def _browse_output(self):
        """浏览选择输出目录"""
        dir_path = filedialog.askdirectory(
            title="选择输出目录",
            initialdir=self.output_dir.get()
        )
        if dir_path:
            self.output_dir.set(dir_path)

    def _on_parts_change(self):
        """分割份数输入框变化时同步滑块"""
        try:
            val = int(self.parts_var.get())
            val = max(1, min(1000, val))
            self.parts_scale.set(val)
            self.parts_var.set(str(val))
        except ValueError:
            self.parts_var.set("2")
            self.parts_scale.set(2)

    def _on_scale_change(self, value):
        """滑块变化时同步输入框"""
        val = int(float(value))
        self.parts_var.set(str(val))

    def _auto_detect_encoding(self, file_path):
        """后台自动检测文件编码"""
        try:
            encoding = detect_encoding(file_path)
            # 规范化编码名称显示
            encoding_lower = encoding.lower()
            if encoding_lower == 'utf-8-sig':
                display_enc = 'UTF-8-SIG'
            elif encoding_lower == 'utf-16-le':
                display_enc = 'UTF-16-LE'
            elif encoding_lower == 'utf-16-be':
                display_enc = 'UTF-16-BE'
            elif encoding_lower in ('gbk', 'gb2312', 'gb18030'):
                display_enc = 'GBK'
            elif encoding_lower == 'utf-16':
                display_enc = 'UTF-16'
            elif encoding_lower == 'utf-8':
                display_enc = 'UTF-8'
            elif encoding_lower == 'ascii':
                display_enc = 'ASCII'
            elif encoding_lower == 'latin-1':
                display_enc = 'Latin-1'
            else:
                display_enc = encoding.upper()

            self.root.after(0, lambda: self.encoding_var.set(f"{display_enc} (检测)"))
            self.root.after(0, lambda: self._show_status(f"检测到编码: {display_enc}"))
        except Exception:
            import traceback
            err_msg = f"编码检测失败: {traceback.format_exc()}"
            self.root.after(0, lambda m=err_msg: self._show_status(m, is_error=True))

    def _update_file_info(self, file_path):
        """更新文件信息预览"""
        try:
            size_bytes = os.path.getsize(file_path)
            size_mb = size_bytes / (1024 * 1024)
            mtime = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(file_path)))

            info = (
                f"文件路径: {file_path}\n"
                f"文件大小: {size_mb:.2f} MB ({size_bytes:,} 字节)\n"
                f"修改时间: {mtime}\n"
            )
            if size_mb > 100:
                info += "⚠ 大文件模式：将采用逐行读取优化内存使用\n"

            self.info_text.config(state=tk.NORMAL)
            self.info_text.delete(1.0, tk.END)
            self.info_text.insert(tk.END, info)
            self.info_text.config(state=tk.DISABLED)
        except Exception as e:
            self._show_status(f"读取文件信息失败: {str(e)}", is_error=True)

    def _show_status(self, message, is_error=False):
        """更新状态显示"""
        self.status_var.set(message)
        if is_error:
            self.status_label.config(foreground='red')
        else:
            self.status_label.config(foreground='black')

    # ========== 核心操作 ==========

    def _validate_inputs(self):
        """验证用户输入"""
        # 检查输入文件
        input_path = self.input_file.get().strip()
        if not input_path:
            messagebox.showerror("错误", "请先选择要切割的TXT文件！")
            return False

        if not os.path.isfile(input_path):
            messagebox.showerror("错误", f"文件不存在:\n{input_path}")
            return False

        if not input_path.lower().endswith('.txt'):
            result = messagebox.askyesno("确认", "选中的文件不是 .txt 后缀，是否继续处理？")
            if not result:
                return False

        # 检查输出目录
        output_dir = self.output_dir.get().strip()
        if not output_dir:
            messagebox.showerror("错误", "请选择输出目录！")
            return False

        if not os.path.isdir(output_dir):
            try:
                os.makedirs(output_dir)
            except Exception as e:
                messagebox.showerror("错误", f"无法创建输出目录:\n{str(e)}")
                return False

        # 检查分割份数
        try:
            parts = int(self.parts_var.get())
            if parts < 1 or parts > 1000:
                raise ValueError
        except ValueError:
            messagebox.showerror("错误", "切割份数必须为1-1000之间的整数！")
            return False

        # 检查磁盘空间
        try:
            file_size = os.path.getsize(input_path)
            import shutil
            free_space = shutil.disk_usage(output_dir).free
            if free_space < file_size * 1.1:  # 留10%余量
                messagebox.showerror("错误",
                    f"输出目录磁盘空间不足！\n需要约 {file_size/(1024**2):.1f} MB，可用 {free_space/(1024**2):.1f} MB")
                return False
        except Exception:
            pass  # 磁盘检查失败不影响使用

        return True

    def _get_encoding(self):
        """获取用户选择的编码"""
        enc_choice = self.encoding_var.get()
        if "自动检测" in enc_choice or "检测" in enc_choice:
            # 重新检测
            return detect_encoding(self.input_file.get())
        else:
            # 用户手动选择
            encoding_map = {
                "UTF-8": "utf-8",
                "UTF-8-SIG": "utf-8-sig",
                "GBK": "gbk",
                "GB2312": "gb2312",
                "UTF-16": "utf-16-le",
                "UTF-16-LE": "utf-16-le",
                "UTF-16-BE": "utf-16-be",
                "Latin-1": "latin-1",
                "ASCII": "ascii",
            }
            return encoding_map.get(enc_choice, "utf-8")

    def _update_progress(self, percent, message):
        """线程安全的进度更新"""
        self.root.after(0, lambda: self._do_update_progress(percent, message))

    def _do_update_progress(self, percent, message):
        """在主线程中更新进度UI"""
        self.progress['value'] = percent
        self._show_status(message)

    def _start_splitting(self):
        """开始切割（在主线程中启动后台线程）"""
        if self.is_processing:
            return

        if not self._validate_inputs():
            return

        # 禁用开始按钮，启用取消按钮
        self.is_processing = True
        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.progress['value'] = 0
        self._show_status("正在准备切割...")

        # 在后台线程中执行切割
        self.process_thread = threading.Thread(target=self._do_splitting, daemon=True)
        self.process_thread.start()

    def _do_splitting(self):
        """在后台线程中执行实际切割操作"""
        input_path = self.input_file.get()
        output_dir = self.output_dir.get()
        try:
            parts = int(self.parts_var.get())
        except ValueError:
            parts = 2

        # 获取基础文件名（去除扩展名）
        base_name = os.path.splitext(os.path.basename(input_path))[0]
        # 清理文件名中的特殊字符
        base_name = base_name.replace(' ', '_')

        try:
            encoding = self._get_encoding()
            self._update_progress(2, f"使用编码: {encoding}，开始处理...")

            # 执行切割
            file_sizes = process_large_file(
                input_path, encoding, parts, output_dir, base_name,
                self._update_progress
            )

            # 完成后的处理
            self.root.after(0, lambda fs=file_sizes, od=output_dir, bn=base_name:
                            self._on_complete(fs, od, bn))

        except ValueError:
            import traceback
            err_msg = f"参数错误:\n{traceback.format_exc()}"
            self.root.after(0, lambda m=err_msg: self._on_error(m))
        except PermissionError:
            self.root.after(0, lambda m="文件被占用或无权限访问，请关闭相关程序后重试。": self._on_error(m))
        except MemoryError:
            self.root.after(0, lambda m="内存不足，请尝试减少切割份数或关闭其他程序。": self._on_error(m))
        except Exception:
            import traceback
            err_msg = f"切割过程中发生错误:\n{traceback.format_exc()}"
            self.root.after(0, lambda m=err_msg: self._on_error(m))

    def _on_complete(self, file_sizes, output_dir, base_name):
        """切割完成回调"""
        self.progress['value'] = 100
        self._show_status("切割完成！")

        # 构建结果信息
        total_size = sum(file_sizes)
        size_info = ""
        for i, sz in enumerate(file_sizes):
            size_info += f"  {base_name}_{i + 1}.txt: {sz:,} 字节 ({sz / 1024:.1f} KB)\n"

        message = (
            f"文本切割成功！\n\n"
            f"共生成 {len(file_sizes)} 个文件\n"
            f"总大小: {total_size:,} 字节\n\n"
            f"输出文件:\n{size_info}\n"
            f"输出目录:\n{output_dir}"
        )

        messagebox.showinfo("切割完成", message)
        self._reset_ui()

        # 打开输出目录
        try:
            os.startfile(output_dir)
        except Exception:
            pass

    def _on_error(self, error_msg):
        """错误回调"""
        self._show_status(error_msg, is_error=True)
        messagebox.showerror("切割失败", error_msg)
        self._reset_ui()

    def _cancel(self):
        """取消当前操作"""
        self._show_status("操作已取消")
        self._reset_ui()

    def _reset_ui(self):
        """重置UI状态"""
        self.is_processing = False
        self.start_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)
        if self.progress['value'] < 100:
            self.progress['value'] = 0


# ========== 程序入口 ==========

def main():
    """主函数"""
    root = tk.Tk()
    app = TextSplitterApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()