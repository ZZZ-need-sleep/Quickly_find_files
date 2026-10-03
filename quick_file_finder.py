# -*- coding: utf-8 -*-
"""快速文件搜索器 (Quick File Finder)
==================================

一个带图形界面的本地文件搜索工具：既能按 **文件名** 查找，也能按 **文件内容** 查找。
默认两种方式同时进行，只要满足其中之一就算命中（OR 语义），结果实时列在窗口里。

运行方式::

    python quick_file_finder.py

只依赖 Python 标准库（tkinter），无需安装任何第三方包。

主要功能
--------
* 图形界面，搜索在后台线程执行，界面不卡死，可随时“停止”。
* 关键词支持用空格分隔多个词，可切换「全部满足(AND) / 任意满足(OR)」。
* 支持 区分大小写、全字匹配、正则表达式（`^` `$` 按行匹配）、匹配完整路径。
* 文件类型过滤（`.py .txt` 或 `*.log` 均可），可控制递归深度、选择是否包含隐藏文件。
* **排除位置**：整盘搜索时把「肯定没有目标」的地址直接跳过，三种写法混用，每行一条：
  目录名（`node_modules`）、完整路径（`E:\\Games`）、通配符（`*.tmp`、`*cache*`）。
  被排除的目录在进入之前就剪枝，因此整盘搜索明显更快。点「整盘常用排除」可一键
  填入 `Windows`、`Program Files`、`$RECYCLE.BIN`、`AppData`、`Temp` 等常见位置。
* 内容搜索自动跳过二进制文件，自动尝试 UTF-8 / GB18030 / UTF-16 等编码，
  只读取文件开头指定大小的内容（默认 2 MB）以免卡住。
* 结果表格：命中方式、文件名、大小、修改时间、命中片段、所在目录；点击表头排序。
* 选中结果即在下方预览文件内容并高亮关键词；双击打开文件，
  右键菜单可打开所在文件夹、复制路径、复制命中片段。
* 结果可一键导出为 CSV（Excel 可直接打开）。

快捷键：F5 / Ctrl+Enter 开始搜索，Esc 停止，Ctrl+O 打开文件，Ctrl+E 导出 CSV。
"""

from __future__ import annotations

import csv
import dataclasses
import datetime as _dt
import fnmatch
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

APP_TITLE = "快速文件搜索器 —— 文件名 + 内容 双模式搜索"
CONFIG_NAME = ".quick_file_finder.json"


# ===========================================================================
# 一、常量
# ===========================================================================

IS_WINDOWS = os.name == "nt"

#: 默认排除项（每行一个）。不带路径分隔符的按“目录名”匹配，
#: 带分隔符的按“完整路径前缀”排除，含 * ? [ 的按通配符匹配。
DEFAULT_SKIP_DIRS = (
    "$RECYCLE.BIN\nSystem Volume Information\n$WinREAgent\n"
    ".git\n.svn\n.hg\nnode_modules\n__pycache__\n.venv\nvenv\nenv\n"
    ".idea\n.vscode\n.mypy_cache\n.pytest_cache\n.ruff_cache\n"
    "dist\nbuild\n.tox\n.eggs\nsite-packages\nAppData"
)

#: “整盘搜索常用排除”一键填入项（搜整个盘时基本不会有目标文件的系统位置）
COMMON_DRIVE_SKIPS = (
    "$RECYCLE.BIN", "System Volume Information", "$WinREAgent", "$SysReset",
    "Windows", "Program Files", "Program Files (x86)", "ProgramData",
    "AppData", "MSOCache", "Recovery", "PerfLogs", "Config.Msi",
    "System32", "WinSxS", "Installer", "WindowsApps",
    "node_modules", "__pycache__", ".git", ".venv", "venv",
    "Temp", "Cache", "CacheStorage", "GPUCache", "Code Cache",
)

#: 明确按二进制对待的扩展名，内容搜索时直接跳过（不影响按文件名命中）
BINARY_EXTS = {
    ".exe", ".dll", ".sys", ".so", ".dylib", ".bin", ".dat", ".db", ".sqlite",
    ".sqlite3", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".tif", ".tiff", ".psd", ".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a",
    ".wma", ".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".rmvb",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".cab", ".iso",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".pyc", ".pyo",
    ".class", ".o", ".obj", ".lib", ".a", ".pdb", ".jar", ".war", ".ttf",
    ".otf", ".woff", ".woff2", ".eot", ".lnk", ".msi", ".apk", ".ipa", ".dmg",
    ".vmdk", ".lib", ".exp", ".res", ".pak", ".pyz",
}

#: 带 BOM 的编码（顺序重要：utf-32 要在 utf-16 之前判断）
_BOM_ENCODINGS = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
)

#: 无 BOM 时依次尝试的编码
_TEXT_ENCODINGS = ("utf-8", "gb18030", "big5", "cp1252")

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4

MODE_BOTH = "both"
MODE_NAME = "name"
MODE_CONTENT = "content"


# ===========================================================================
# 二、搜索内核（与界面无关，可单独测试）
# ===========================================================================

@dataclasses.dataclass
class SearchConfig:
    """一次搜索的全部参数。"""

    root: str = "."
    query: str = ""
    mode: str = MODE_BOTH            # both / name / content
    match_full_path: bool = False    # 文件名匹配时是否也匹配完整路径
    case_sensitive: bool = False
    use_regex: bool = False
    whole_word: bool = False
    logic: str = "and"               # and / or
    recursive: bool = True
    max_depth: int = 0               # 0 表示不限制
    include_hidden: bool = False
    skip_text: str = DEFAULT_SKIP_DIRS   # 排除项：目录名 / 完整路径 / 通配符，每行一个
    ext_filter_text: str = ""
    max_read_mb: float = 2.0         # 内容搜索每个文件最多读取多少 MB
    max_results: int = 1000


def human_size(num: float) -> str:
    """把字节数格式化成易读字符串。"""
    try:
        num = float(num)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} TB"


def human_time(ts: float) -> str:
    try:
        return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "-"


def decode_bytes(data: bytes):
    """把原始字节解码成文本。

    返回 ``(text, encoding)``；若判断为二进制内容则返回 ``(None, None)``。
    """
    for bom, enc in _BOM_ENCODINGS:
        if data.startswith(bom):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                break
    # 前 4KB 出现 NUL 字节，基本可判定为二进制
    if b"\x00" in data[:4096]:
        return None, None
    for enc in _TEXT_ENCODINGS:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace"), "utf-8(替换)"


def read_text_file(path: str, max_bytes: int):
    """读取文件开头 ``max_bytes`` 字节并解码。

    返回 ``(text, encoding, truncated)``；二进制文件返回 ``(None, None, False)``。
    """
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    if not data:
        return "", "empty", False
    text, enc = decode_bytes(data)
    return text, enc, truncated


def _is_hidden_entry(entry: os.DirEntry) -> bool:
    """判断目录项是否为隐藏/系统文件。"""
    if entry.name.startswith("."):
        return True
    if IS_WINDOWS:
        try:
            attrs = entry.stat(follow_symlinks=False).st_file_attributes
            return bool(attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))
        except (OSError, AttributeError):
            return False
    return False


def parse_skip_items(text: str):
    """解析“排除”清单，返回 ``(目录名集合, 路径前缀集合, 通配符元组)``。

    每行一项（也接受分号分隔）。含路径分隔符的行整行当作一个路径（这样可以写带空格的
    `C:\\Program Files`）；不含分隔符的行还会再按空格拆成多项，方便一行写多个名字或通配符。

    * ``node_modules``        —— 不带分隔符，按 **目录名** 排除任意位置的同名目录
    * ``E:\\Games``            —— 带路径，排除该目录/文件及其全部子项
    * ``E:\\`` 或 ``E:``       —— 整个盘（一般不会这么填）
    * ``*.tmp`` / ``*cache*`` —— 含通配符，按 **文件名或完整路径** 匹配
    """
    names, prefixes, globs = set(), set(), []

    def _add(item: str):
        low = item.lower()
        if any(ch in item for ch in "*?["):
            globs.append(low.replace("/", os.sep))
            return
        if not ((os.sep in item) or ("/" in item)
                or re.fullmatch(r"[a-zA-Z]:[\\/]?", item)):
            names.add(low)
            return
        norm = os.path.normpath(item)
        if re.fullmatch(r"[a-zA-Z]:", norm):        # 只写了盘符，补一个分隔符
            norm = norm + os.sep
        prefixes.add(norm.lower())

    for raw in re.split(r"[\r\n;]+", text or ""):
        line = raw.strip().strip('"').strip("'")
        if not line:
            continue
        is_path = (os.sep in line) or ("/" in line) or bool(re.fullmatch(r"[a-zA-Z]:[\\/]?", line))
        for item in ([line] if is_path else line.split()):
            if item:
                _add(item)
    return names, frozenset(prefixes), tuple(globs)


def _under_prefix(path_low: str, prefixes) -> bool:
    """``path_low`` 自身或其任一上级目录命中排除路径时返回 True。"""
    if not prefixes:
        return False
    cur = path_low
    while True:
        if cur in prefixes:
            return True
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return False
        cur = parent


def _glob_excluded(path_low: str, name_low: str, globs) -> bool:
    for pat in globs:
        if fnmatch.fnmatchcase(path_low, pat) or fnmatch.fnmatchcase(name_low, pat):
            return True
    return False


def drop_root_blocking_skips(root: str, prefixes):
    """剔除会把搜索目录本身排除掉的路径项，返回 ``(保留的, 被剔除的)``。"""
    root_low = os.path.normpath(os.path.abspath(root)).lower()
    keep, dropped = set(), []
    for pre in prefixes:
        if pre == root_low or root_low.startswith(pre.rstrip(os.sep) + os.sep):
            dropped.append(pre)
        else:
            keep.add(pre)
    return frozenset(keep), dropped


def iter_files(root: str, recursive: bool = True, max_depth: int = 0,
               skip_names=frozenset(), skip_prefixes=frozenset(), skip_globs=(),
               include_hidden: bool = False, stats: dict | None = None):
    """深度优先遍历 ``root``，逐个产出文件路径（生成器，异常自动跳过）。

    ``skip_names`` / ``skip_prefixes`` / ``skip_globs`` 三套排除规则在进入目录前就
    剪枝，因此被排除的大目录根本不会被打开 —— 这是整盘搜索提速的关键。
    传入 ``stats`` 时会累计 ``skip_dirs`` / ``skip_files`` 两个计数。
    """
    stack = [(root, 0)]
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                entries = list(it)
        except (PermissionError, OSError):
            continue
        for entry in entries:
            try:
                name = entry.name
                if entry.is_dir(follow_symlinks=False):
                    if not recursive:
                        continue
                    if not include_hidden and _is_hidden_entry(entry):
                        continue
                    if max_depth and depth + 1 > max_depth:
                        continue
                    low = os.path.normpath(entry.path).lower()
                    if (name.lower() in skip_names
                            or _under_prefix(low, skip_prefixes)
                            or _glob_excluded(low, name.lower(), skip_globs)):
                        if stats is not None:
                            stats["skip_dirs"] = stats.get("skip_dirs", 0) + 1
                        continue
                    stack.append((entry.path, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    if not include_hidden and _is_hidden_entry(entry):
                        continue
                    low = os.path.normpath(entry.path).lower()
                    if (_under_prefix(low, skip_prefixes)
                            or _glob_excluded(low, name.lower(), skip_globs)):
                        if stats is not None:
                            stats["skip_files"] = stats.get("skip_files", 0) + 1
                        continue
                    yield entry.path
            except OSError:
                continue


def parse_ext_filter(text: str):
    """解析文件类型过滤串，返回 ``(扩展名元组, 通配符元组)``。

    支持 ``.py .txt``、``py,txt``、``*.log``、``test_*.py`` 等写法。
    """
    tokens = [t.strip().lower() for t in re.split(r"[;,\s]+", text or "") if t.strip()]
    exts, globs = [], []
    for tok in tokens:
        if any(ch in tok for ch in "*?["):
            globs.append(tok if tok.startswith("*") else "*" + tok)
        else:
            exts.append(tok if tok.startswith(".") else "." + tok)
    return tuple(exts), tuple(globs)


def ext_allowed(name: str, exts, globs) -> bool:
    if not exts and not globs:
        return True
    low = name.lower()
    if os.path.splitext(low)[1] in exts:
        return True
    return any(fnmatch.fnmatch(low, g) for g in globs)


class Matcher:
    """把用户输入编译成一组正则, 并判断文本是否命中。"""

    def __init__(self, cfg: SearchConfig):
        self.cfg = cfg
        self.terms = []
        self.empty = not (cfg.query or "").strip()
        # MULTILINE 让 ^ $ 按行匹配，更符合“在文件里找一行”的直觉
        flags = re.MULTILINE
        if not cfg.case_sensitive:
            flags |= re.IGNORECASE
        raw = cfg.query or ""
        if cfg.use_regex:
            parts = [raw]
        else:
            parts = [t for t in re.split(r"\s+", raw.strip()) if t]
        for part in parts:
            src = part if cfg.use_regex else re.escape(part)
            if cfg.whole_word and not cfg.use_regex:
                # 只在关键词为 ASCII 时加词边界，避免中文被 \b 误判
                if all(ord(ch) < 128 for ch in part):
                    src = r"(?<![0-9A-Za-z_])" + src + r"(?![0-9A-Za-z_])"
            try:
                self.terms.append(re.compile(src, flags))
            except re.error as exc:
                raise ValueError(f"正则表达式错误：{exc}") from exc

    def hits(self, text: str) -> bool:
        if self.empty:
            return True
        results = [bool(t.search(text)) for t in self.terms]
        return any(results) if self.cfg.logic == "or" else all(results)

    def first_match(self, text: str):
        for t in self.terms:
            m = t.search(text)
            if m:
                return m
        return None

    def is_exact_name(self, name: str) -> bool:
        if self.empty or self.cfg.use_regex or len(self.terms) != 1:
            return False
        q = (self.cfg.query or "").strip()
        return (q == name) if self.cfg.case_sensitive else (q.lower() == name.lower())


def make_snippet(text: str, matcher: Matcher, width: int = 90):
    """从命中的文本里抽出一行摘要，返回 ``(摘要, 行号)``。"""
    m = matcher.first_match(text)
    if not m:
        return "", 0
    line_start = text.rfind("\n", 0, m.start()) + 1
    line_end = text.find("\n", m.end())
    if line_end == -1:
        line_end = len(text)
    line = text[line_start:line_end].rstrip("\r\n")
    lineno = text.count("\n", 0, line_start) + 1
    line = line.replace("\t", "    ")
    if len(line) > 240:
        rel = m.start() - line_start
        s = max(0, rel - width)
        e = min(len(line), rel + width + 60)
        line = ("…" if s > 0 else "") + line[s:e] + ("…" if e < len(line) else "")
    return line, lineno


def run_search(cfg: SearchConfig, matcher: Matcher, emit, stop_event: threading.Event):
    """执行一次搜索。``emit`` 形如 ``emit(("result", rec))``，线程安全由调用方保证。"""
    stats = {
        "scanned": 0, "matched": 0, "binary": 0, "errors": 0,
        "skipped_ext": 0, "bytes": 0, "start": time.time(),
        "elapsed": 0.0, "reason": "完成", "truncated": False,
        "skip_dirs": 0, "skip_files": 0,
    }
    exts, globs = parse_ext_filter(cfg.ext_filter_text)
    skip_names, skip_prefixes, skip_globs = parse_skip_items(cfg.skip_text)
    # 排除项里如果写了搜索目录自身，会导致什么都搜不到，这里自动剔除并告知
    skip_prefixes, dropped = drop_root_blocking_skips(cfg.root, skip_prefixes)
    stats["dropped_skips"] = dropped
    max_bytes = max(1, int(max(0.01, cfg.max_read_mb) * 1024 * 1024))
    limit = max(1, int(cfg.max_results))
    last_tick = 0.0

    def tick(current_path="", force=False):
        nonlocal last_tick
        now = time.time()
        if force or now - last_tick > 0.12:
            last_tick = now
            stats["elapsed"] = now - stats["start"]
            emit(("progress", dict(stats), current_path))

    try:
        for path in iter_files(cfg.root, cfg.recursive, cfg.max_depth,
                               skip_names, skip_prefixes, skip_globs,
                               cfg.include_hidden, stats):
            if stop_event.is_set():
                stats["reason"] = "已手动停止"
                break
            stats["scanned"] += 1
            name = os.path.basename(path)

            if not ext_allowed(name, exts, globs):
                stats["skipped_ext"] += 1
                tick(path)
                continue

            name_hit = False
            content_hit = False
            snippet = ""
            lineno = 0

            if matcher.empty or cfg.mode in (MODE_NAME, MODE_BOTH):
                target = path if cfg.match_full_path else name
                name_hit = matcher.hits(target)

            if (not matcher.empty and not name_hit
                    and cfg.mode in (MODE_CONTENT, MODE_BOTH)):
                ext = os.path.splitext(name)[1].lower()
                if ext in BINARY_EXTS:
                    stats["binary"] += 1
                else:
                    try:
                        text, _enc, _trunc = read_text_file(path, max_bytes)
                        stats["bytes"] += min(os.path.getsize(path), max_bytes)
                    except (OSError, ValueError):
                        stats["errors"] += 1
                        text = None
                    if text is None:
                        stats["binary"] += 1
                    elif matcher.hits(text):
                        content_hit = True
                        snippet, lineno = make_snippet(text, matcher)

            if not (name_hit or content_hit):
                tick(path)
                continue

            try:
                st = os.stat(path)
                size, mtime = st.st_size, st.st_mtime
            except OSError:
                size, mtime = 0, 0.0

            if name_hit and content_hit:
                kind, score = "文件名+内容", 90
            elif name_hit:
                kind = "文件名"
                score = 100 if matcher.is_exact_name(name) else 80
            else:
                kind, score = "内容", 50

            stats["matched"] += 1
            emit(("result", {
                "kind": kind,
                "name": name,
                "path": path,
                "folder": os.path.dirname(path),
                "size": size,
                "mtime": mtime,
                "snippet": snippet,
                "line": lineno,
                "score": score,
            }))

            if stats["matched"] >= limit:
                stats["truncated"] = True
                stats["reason"] = f"已达到结果上限 {limit} 条"
                break
            tick(path)
        else:
            stats["reason"] = "完成"
    except Exception as exc:                       # noqa: BLE001 - 兜底，避免线程静默死亡
        stats["reason"] = f"出错：{exc}"
        emit(("error", f"搜索过程中出现异常：{exc}"))
    finally:
        stats["elapsed"] = time.time() - stats["start"]
        emit(("done", stats))


# ===========================================================================
# 三、图形界面
# ===========================================================================

def _pick_font(root: tk.Misc) -> str:
    preferred = ["Microsoft YaHei UI", "Microsoft YaHei", "微软雅黑",
                 "PingFang SC", "Noto Sans CJK SC", "SimHei", "DejaVu Sans"]
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        available = set()
    for fam in preferred:
        if fam in available:
            return fam
    return "TkDefaultFont"


def _open_path(path: str) -> None:
    """用系统默认程序打开文件。"""
    if IS_WINDOWS:
        os.startfile(path)                          # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _reveal_in_folder(path: str) -> None:
    """在文件管理器里定位并选中该文件。"""
    folder = os.path.dirname(path) or "."
    if IS_WINDOWS:
        try:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            return
        except OSError:
            pass
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        subprocess.Popen(["xdg-open", folder])


class FinderApp:
    """主窗口。"""

    COLUMNS = (
        ("kind", "命中方式", 92, "center", False),
        ("name", "文件名", 250, "w", True),
        ("size", "大小", 78, "e", False),
        ("mtime", "修改时间", 130, "center", False),
        ("snippet", "命中片段", 330, "w", True),
        ("folder", "所在目录", 340, "w", True),
    )

    def __init__(self, root: tk.Tk):
        self.root = root
        self.font_family = _pick_font(root)
        self.records: list[dict] = []
        self.iid2rec: dict[str, dict] = {}
        self.q: "queue.Queue[tuple]" = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.searching = False
        self.sort_col = "score"
        self.sort_desc = True
        self._iid_seq = 0

        root.title(APP_TITLE)
        # 自适应屏幕：小屏上自动变矮，避免底部被任务栏遮住
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        width = max(980, min(1260, sw - 60))
        height = max(620, min(880, sh - 180))
        root.geometry(f"{width}x{height}+{max(0, (sw - width) // 2)}+{max(0, (sh - height) // 3)}")
        root.minsize(980, 600)
        self._build_style()
        self._build_vars()
        self._build_ui()
        self._bind_keys()
        self._load_config()

    # ------------------------------------------------------------------ 样式
    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista" if IS_WINDOWS else "clam")
        except tk.TclError:
            pass
        base = (self.font_family, 10)
        style.configure(".", font=base)
        style.configure("TButton", padding=(10, 5))
        style.configure("Accent.TButton", font=(self.font_family, 10, "bold"))
        style.configure("TLabelframe.Label", font=(self.font_family, 10, "bold"))
        style.configure("Treeview", font=base, rowheight=24)
        style.configure("Treeview.Heading", font=(self.font_family, 10, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Status.TLabel", foreground="#333333")

    def _build_vars(self):
        home = os.path.expanduser("~")
        self.var_dir = tk.StringVar(value=os.getcwd() or home)
        self.var_query = tk.StringVar()
        self.var_mode = tk.StringVar(value=MODE_BOTH)
        self.var_logic = tk.StringVar(value="and")
        self.var_case = tk.BooleanVar(value=False)
        self.var_word = tk.BooleanVar(value=False)
        self.var_regex = tk.BooleanVar(value=False)
        self.var_fullpath = tk.BooleanVar(value=False)
        self.var_recursive = tk.BooleanVar(value=True)
        self.var_hidden = tk.BooleanVar(value=False)
        self.var_depth = tk.StringVar(value="0")
        self.var_maxread = tk.StringVar(value="2")
        self.var_maxresults = tk.StringVar(value="1000")
        self.var_ext = tk.StringVar()
        self.var_skip_hint = tk.StringVar(value="")
        self.var_status = tk.StringVar(value="就绪。选择目录、输入关键词后点击「开始搜索」或按 F5。")
        self.var_count = tk.StringVar(value="命中 0 条")

    # -------------------------------------------------------------------- UI
    def _build_ui(self):
        root = self.root
        outer = ttk.Frame(root, padding=8)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        row = 0

        # --- 搜索位置 ---
        box1 = ttk.LabelFrame(outer, text=" 1. 搜索位置 ", padding=(8, 6))
        box1.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        box1.columnconfigure(0, weight=1)
        entry_dir = ttk.Entry(box1, textvariable=self.var_dir)
        entry_dir.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Button(box1, text="浏览…", command=self._choose_dir).grid(row=0, column=1)
        ttk.Button(box1, text="当前目录", command=self._use_cwd).grid(row=0, column=2, padx=(6, 0))
        row += 1

        # --- 关键词 ---
        box2 = ttk.LabelFrame(outer, text=" 2. 关键词（多个词用空格分隔） ", padding=(8, 6))
        box2.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        box2.columnconfigure(0, weight=1)
        entry_q = ttk.Entry(box2, textvariable=self.var_query, font=(self.font_family, 12))
        entry_q.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.entry_query = entry_q
        modes = ttk.Frame(box2)
        modes.grid(row=0, column=1, sticky="e")
        ttk.Radiobutton(modes, text="文件名", value=MODE_NAME,
                        variable=self.var_mode).pack(side="left")
        ttk.Radiobutton(modes, text="内容", value=MODE_CONTENT,
                        variable=self.var_mode).pack(side="left", padx=6)
        ttk.Radiobutton(modes, text="两者同时（默认）", value=MODE_BOTH,
                        variable=self.var_mode).pack(side="left")
        row += 1

        # --- 匹配选项 ---
        box3 = ttk.LabelFrame(outer, text=" 3. 匹配选项 ", padding=(8, 6))
        box3.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        ttk.Checkbutton(box3, text="区分大小写", variable=self.var_case).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(box3, text="全字匹配", variable=self.var_word).grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Checkbutton(box3, text="正则表达式", variable=self.var_regex).grid(row=0, column=2, sticky="w", padx=(12, 0))
        ttk.Checkbutton(box3, text="文件名匹配完整路径", variable=self.var_fullpath).grid(
            row=0, column=3, sticky="w", padx=(12, 0))
        logic = ttk.Frame(box3)
        logic.grid(row=0, column=4, sticky="e", padx=(24, 0))
        ttk.Label(logic, text="多关键词：").pack(side="left")
        ttk.Radiobutton(logic, text="全部满足", value="and",
                        variable=self.var_logic).pack(side="left")
        ttk.Radiobutton(logic, text="任意满足", value="or",
                        variable=self.var_logic).pack(side="left", padx=(6, 0))
        box3.columnconfigure(4, weight=1)
        row += 1

        # --- 范围与过滤 ---
        box4 = ttk.LabelFrame(outer, text=" 4. 范围与过滤 ", padding=(8, 6))
        box4.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        box4.columnconfigure(1, weight=1)
        box4.columnconfigure(4, weight=1)

        ttk.Checkbutton(box4, text="包含子目录", variable=self.var_recursive).grid(
            row=0, column=0, sticky="w")
        ttk.Checkbutton(box4, text="包含隐藏/系统文件", variable=self.var_hidden).grid(
            row=0, column=1, sticky="w")

        ttk.Label(box4, text="递归深度(0=不限)").grid(row=0, column=2, sticky="e")
        ttk.Spinbox(box4, from_=0, to=99, width=5, textvariable=self.var_depth).grid(
            row=0, column=3, sticky="w", padx=(4, 16))

        ttk.Label(box4, text="内容读取上限(MB)").grid(row=0, column=4, sticky="e")
        ttk.Spinbox(box4, from_=0.1, to=200, increment=0.5, width=6,
                    textvariable=self.var_maxread).grid(row=0, column=5, sticky="w", padx=(4, 16))

        ttk.Label(box4, text="最多结果数").grid(row=0, column=6, sticky="e")
        ttk.Spinbox(box4, from_=10, to=200000, increment=100, width=8,
                    textvariable=self.var_maxresults).grid(row=0, column=7, sticky="w", padx=(4, 0))

        ttk.Label(box4, text="文件类型过滤").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(box4, textvariable=self.var_ext).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(6, 16), pady=(6, 0))
        ttk.Label(box4, text="（如：.py .txt 或 *.log，留空=全部）",
                  style="Hint.TLabel").grid(row=1, column=4, columnspan=4, sticky="w", pady=(6, 0))
        row += 1

        # --- 排除位置（整盘搜索时把“肯定没有”的地址直接跳过） ---
        box5 = ttk.LabelFrame(
            outer, text=" 5. 排除位置 —— 这些地址不用搜（每行一个，支持目录名 / 完整路径 / 通配符） ",
            padding=(8, 6))
        box5.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        box5.columnconfigure(0, weight=1)
        box5.rowconfigure(0, weight=1)

        self.txt_skip = tk.Text(box5, height=3, wrap="none", undo=True,
                                font=("Consolas" if IS_WINDOWS else "Monospace", 9))
        self._set_skip_text(DEFAULT_SKIP_DIRS)
        self.txt_skip.bind("<<Modified>>", self._on_skip_modified)
        skip_vsb = ttk.Scrollbar(box5, orient="vertical", command=self.txt_skip.yview)
        self.txt_skip.configure(yscrollcommand=skip_vsb.set)
        self.txt_skip.grid(row=0, column=0, sticky="ew")
        skip_vsb.grid(row=0, column=1, sticky="ns")

        skipbtns = ttk.Frame(box5)
        skipbtns.grid(row=1, column=0, columnspan=2, sticky="e", pady=(4, 0))
        ttk.Button(skipbtns, text="添加文件夹…", command=self._add_skip_dir).pack(side="left")
        ttk.Button(skipbtns, text="添加当前目录", command=self._add_root_as_skip).pack(
            side="left", padx=(6, 0))
        ttk.Button(skipbtns, text="整盘常用排除", command=self._fill_common_skips).pack(
            side="left", padx=(6, 0))
        ttk.Button(skipbtns, text="清空", command=lambda: self._set_skip_text("")).pack(
            side="left", padx=(6, 0))

        hint_row = ttk.Frame(box5)
        hint_row.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Label(
            hint_row,
            text="目录名跳过任意位置的同名目录；完整路径（E:\\Games）跳过整棵目录树；"
                 "通配符（*.tmp、*cache*）按文件名或路径匹配。",
            style="Hint.TLabel").pack(side="left")
        ttk.Label(hint_row, textvariable=self.var_skip_hint,
                  style="Hint.TLabel").pack(side="right")
        row += 1

        # --- 按钮条 ---
        bar = ttk.Frame(outer)
        bar.grid(row=row, column=0, sticky="ew", pady=(0, 6))
        self.btn_search = ttk.Button(bar, text="开始搜索 (F5)", style="Accent.TButton",
                                     command=self.start_search)
        self.btn_search.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止 (Esc)", command=self.stop_search,
                                   state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Button(bar, text="打开文件", command=self.open_selected).pack(side="left")
        ttk.Button(bar, text="打开所在文件夹", command=self.reveal_selected).pack(side="left", padx=6)
        ttk.Button(bar, text="复制完整路径", command=self.copy_path).pack(side="left")
        ttk.Button(bar, text="导出 CSV", command=self.export_csv).pack(side="left", padx=6)
        ttk.Button(bar, text="清空结果", command=self.clear_results).pack(side="left")
        self.lbl_count = ttk.Label(bar, textvariable=self.var_count, style="Status.TLabel")
        self.lbl_count.pack(side="right")
        row += 1

        # --- 进度 ---
        prog = ttk.Frame(outer)
        prog.grid(row=row, column=0, sticky="ew")
        prog.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(prog, mode="indeterminate")
        self.progress.grid(row=0, column=0, sticky="ew")
        ttk.Label(outer, textvariable=self.var_status, style="Status.TLabel").grid(
            row=row + 1, column=0, sticky="w", pady=(4, 6))
        row += 2

        # --- 结果 + 预览 ---
        paned = ttk.Panedwindow(outer, orient="vertical")
        paned.grid(row=row, column=0, sticky="nsew")
        outer.rowconfigure(row, weight=1)

        tree_frame = ttk.Frame(paned)
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_frame, columns=[c[0] for c in self.COLUMNS],
                                 show="headings", selectmode="extended")
        for key, title, width, anchor, stretch in self.COLUMNS:
            self.tree.heading(key, text=title, command=lambda k=key: self.sort_by(k))
            self.tree.column(key, width=width, anchor=anchor, stretch=stretch)
        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        paned.add(tree_frame, weight=3)

        prev_frame = ttk.LabelFrame(paned, text=" 内容预览（命中关键词已高亮） ", padding=4)
        prev_frame.rowconfigure(0, weight=1)
        prev_frame.columnconfigure(0, weight=1)
        self.preview = tk.Text(prev_frame, wrap="none", height=8, undo=False,
                               font=("Consolas" if IS_WINDOWS else "Monospace", 10),
                               background="#fcfcfc")
        pvsb = ttk.Scrollbar(prev_frame, orient="vertical", command=self.preview.yview)
        phsb = ttk.Scrollbar(prev_frame, orient="horizontal", command=self.preview.xview)
        self.preview.configure(yscrollcommand=pvsb.set, xscrollcommand=phsb.set)
        self.preview.grid(row=0, column=0, sticky="nsew")
        pvsb.grid(row=0, column=1, sticky="ns")
        phsb.grid(row=1, column=0, sticky="ew")
        self.preview.tag_configure("hit", background="#ffe58f")
        self.preview.tag_configure("hitline", background="#e6f4ff")
        self.preview.configure(state="disabled")
        paned.add(prev_frame, weight=2)

        self.tree.tag_configure("kind-name", background="#eaf7ea")
        self.tree.tag_configure("kind-content", background="#eaf1fb")
        self.tree.tag_configure("kind-both", background="#fdf6e3")

        self.tree.bind("<Double-1>", lambda e: self.open_selected())
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Button-3>", self._popup_menu)

        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="打开文件", command=self.open_selected)
        self.menu.add_command(label="打开所在文件夹", command=self.reveal_selected)
        self.menu.add_separator()
        self.menu.add_command(label="复制完整路径", command=self.copy_path)
        self.menu.add_command(label="复制命中片段", command=self.copy_snippet)
        self.menu.add_separator()
        self.menu.add_command(label="导出 CSV", command=self.export_csv)

        # 结果区默认占 3/5 高度，预览区占 2/5
        self.root.after_idle(lambda: self._set_initial_sash(paned))

    def _set_initial_sash(self, paned: ttk.Panedwindow):
        try:
            total = paned.winfo_height()
            if total > 200:
                paned.sashpos(0, int(total * 0.62))
        except tk.TclError:
            pass

    def _bind_keys(self):
        self.root.bind("<F5>", lambda e: self.start_search())
        self.root.bind("<Control-Return>", lambda e: self.start_search())
        self.root.bind("<Escape>", lambda e: self.stop_search())
        self.root.bind("<Control-o>", lambda e: self.open_selected())
        self.root.bind("<Control-e>", lambda e: self.export_csv())

    # ------------------------------------------------------------- 配置读写
    def _config_path(self) -> str:
        folder = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(folder, CONFIG_NAME)

    def _load_config(self):
        import json
        try:
            with open(self._config_path(), "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        mapping = {
            "dir": self.var_dir, "query": self.var_query, "mode": self.var_mode,
            "logic": self.var_logic, "case": self.var_case, "word": self.var_word,
            "regex": self.var_regex, "fullpath": self.var_fullpath,
            "recursive": self.var_recursive, "hidden": self.var_hidden,
            "depth": self.var_depth, "maxread": self.var_maxread,
            "maxresults": self.var_maxresults, "ext": self.var_ext,
        }
        for key, var in mapping.items():
            if key in data:
                try:
                    var.set(data[key])
                except tk.TclError:
                    pass
        if isinstance(data.get("skipitems"), str):
            self._set_skip_text(data["skipitems"])

    def _save_config(self):
        import json
        data = {
            "dir": self.var_dir.get(), "query": self.var_query.get(),
            "mode": self.var_mode.get(), "logic": self.var_logic.get(),
            "case": self.var_case.get(), "word": self.var_word.get(),
            "regex": self.var_regex.get(), "fullpath": self.var_fullpath.get(),
            "recursive": self.var_recursive.get(), "hidden": self.var_hidden.get(),
            "depth": self.var_depth.get(), "maxread": self.var_maxread.get(),
            "maxresults": self.var_maxresults.get(), "ext": self.var_ext.get(),
            "skipitems": self._get_skip_text(),
        }
        try:
            with open(self._config_path(), "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    # ------------------------------------------------------- 排除清单的读写
    def _get_skip_text(self) -> str:
        return self.txt_skip.get("1.0", "end-1c")

    def _set_skip_text(self, text: str):
        self.txt_skip.delete("1.0", "end")
        self.txt_skip.insert("1.0", text)
        self._update_skip_hint()

    def _skip_lines(self) -> list[str]:
        return [ln.strip() for ln in re.split(r"[\r\n;]+", self._get_skip_text()) if ln.strip()]

    def _update_skip_hint(self):
        try:
            names, prefixes, globs = parse_skip_items(self._get_skip_text())
        except Exception:                                   # noqa: BLE001
            return
        total = len(names) + len(prefixes) + len(globs)
        self.var_skip_hint.set(
            f"已设置 {total} 条排除：目录名 {len(names)} 条，路径 {len(prefixes)} 条，"
            f"通配符 {len(globs)} 条")

    def _on_skip_modified(self, _event=None):
        try:
            if self.txt_skip.edit_modified():
                self.txt_skip.edit_modified(False)
                self._update_skip_hint()
        except tk.TclError:
            pass

    def _add_skip_dir(self):
        chosen = filedialog.askdirectory(title="选择要排除的文件夹（其下全部内容都会被跳过）",
                                         initialdir=self.var_dir.get() or "/")
        if chosen:
            self._append_skip(os.path.normpath(chosen))

    def _add_root_as_skip(self):
        cur = self.var_dir.get().strip().strip('"')
        if cur:
            self._append_skip(os.path.normpath(cur))

    def _append_skip(self, item: str):
        lines = self._skip_lines()
        if any(ln.lower() == item.lower() for ln in lines):
            self.var_status.set(f"排除清单里已经有：{item}")
            return
        lines.append(item)
        self._set_skip_text("\n".join(lines))
        self.var_status.set(f"已加入排除清单：{item}")

    def _fill_common_skips(self):
        lines = self._skip_lines()
        existing = {ln.lower() for ln in lines}
        added = [item for item in COMMON_DRIVE_SKIPS if item.lower() not in existing]
        self._set_skip_text("\n".join(lines + added))
        self.var_status.set(f"已补充 {len(added)} 条整盘搜索常用排除项。")

    # --------------------------------------------------------------- 小工具
    def _choose_dir(self):
        initial = self.var_dir.get()
        if not os.path.isdir(initial):
            initial = os.path.expanduser("~")
        chosen = filedialog.askdirectory(title="选择要搜索的文件夹", initialdir=initial)
        if chosen:
            self.var_dir.set(os.path.normpath(chosen))

    def _use_cwd(self):
        self.var_dir.set(os.getcwd())

    def _int_var(self, var: tk.StringVar, default: int) -> int:
        try:
            return int(float(var.get()))
        except (TypeError, ValueError):
            return default

    def _float_var(self, var: tk.StringVar, default: float) -> float:
        try:
            return float(var.get())
        except (TypeError, ValueError):
            return default

    def _selected_records(self) -> list[dict]:
        return [self.iid2rec[i] for i in self.tree.selection() if i in self.iid2rec]

    # ----------------------------------------------------------------- 搜索
    def start_search(self):
        if self.searching:
            return
        root_dir = self.var_dir.get().strip().strip('"')
        if not root_dir:
            messagebox.showwarning("缺少目录", "请先选择要搜索的文件夹。")
            return
        if not os.path.isdir(root_dir):
            messagebox.showerror("目录不存在", f"找不到文件夹：\n{root_dir}")
            return

        cfg = SearchConfig(
            root=root_dir,
            query=self.var_query.get(),
            mode=self.var_mode.get(),
            match_full_path=self.var_fullpath.get(),
            case_sensitive=self.var_case.get(),
            use_regex=self.var_regex.get(),
            whole_word=self.var_word.get(),
            logic=self.var_logic.get(),
            recursive=self.var_recursive.get(),
            max_depth=max(0, self._int_var(self.var_depth, 0)),
            include_hidden=self.var_hidden.get(),
            skip_text=self._get_skip_text(),
            ext_filter_text=self.var_ext.get(),
            max_read_mb=max(0.01, self._float_var(self.var_maxread, 2.0)),
            max_results=max(1, self._int_var(self.var_maxresults, 1000)),
        )
        try:
            matcher = Matcher(cfg)
        except ValueError as exc:
            messagebox.showerror("关键词有误", str(exc))
            return

        self._save_config()
        self.clear_results()
        self.searching = True
        self.stop_event = threading.Event()
        self.btn_search.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.progress.configure(mode="indeterminate", value=0)
        self.progress.start(12)
        self.var_status.set(f"正在搜索：{root_dir} …")
        self.var_count.set("命中 0 条")

        self.worker = threading.Thread(
            target=run_search, args=(cfg, matcher, self._emit, self.stop_event),
            daemon=True, name="finder-search")
        self.worker.start()
        self.root.after(80, self._poll_queue)

    def _emit(self, message: tuple):
        """工作线程 -> 队列（线程安全）。"""
        self.q.put(message)

    def stop_search(self):
        if self.searching:
            self.stop_event.set()
            self.var_status.set("正在停止…")

    def _poll_queue(self):
        drained = 0
        while drained < 600:
            try:
                msg = self.q.get_nowait()
            except queue.Empty:
                break
            drained += 1
            kind = msg[0]
            if kind == "result":
                self._add_record(msg[1])
            elif kind == "progress":
                self._update_progress(msg[1], msg[2])
            elif kind == "done":
                self._on_done(msg[1])
            elif kind == "error":
                self.var_status.set(msg[1])
        if self.searching or not self.q.empty():
            self.root.after(80, self._poll_queue)

    def _add_record(self, rec: dict):
        self.records.append(rec)
        self._iid_seq += 1
        if len(self.iid2rec) < 20000:
            self._insert_row(f"r{self._iid_seq}", rec)
        self.var_count.set(f"命中 {len(self.records)} 条")

    def _insert_row(self, iid: str, rec: dict):
        tag = {"文件名": "kind-name", "内容": "kind-content",
               "文件名+内容": "kind-both"}.get(rec["kind"], "")
        self.tree.insert("", "end", iid=iid, tags=(tag,), values=(
            rec["kind"], rec["name"], human_size(rec["size"]),
            human_time(rec["mtime"]), rec["snippet"], rec["folder"],
        ))
        self.iid2rec[iid] = rec

    def _update_progress(self, stats: dict, current: str):
        self.var_count.set(
            f"已扫描 {stats['scanned']} 个文件，命中 {stats['matched']} 条"
            f"（{stats['elapsed']:.1f} 秒）")
        if current:
            self.var_status.set(f"正在扫描：{current}")

    def _on_done(self, stats: dict):
        self.searching = False
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.btn_search.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self._sort_records(self.sort_col, self.sort_desc)
        self._render_tree()
        self.var_count.set(f"命中 {len(self.records)} 条")
        self.var_status.set(
            f"{stats['reason']}｜扫描 {stats['scanned']} 个文件，"
            f"命中 {len(self.records)} 条，跳过二进制 {stats['binary']} 个，"
            f"按排除清单跳过 {stats.get('skip_dirs', 0)} 个目录 / "
            f"{stats.get('skip_files', 0)} 个文件，"
            f"读取失败 {stats['errors']} 个，用时 {stats['elapsed']:.2f} 秒"
            + ("（结果已达上限，可调大「最多结果数」）" if stats.get("truncated") else ""))
        dropped = stats.get("dropped_skips") or []
        if dropped:
            self.var_status.set(
                self.var_status.get()
                + "；注意：排除清单里的 " + "、".join(dropped)
                + " 包含了搜索目录本身，已自动忽略该条")

    # ----------------------------------------------------------------- 排序
    def sort_by(self, col: str):
        if col == self.sort_col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = col in ("size", "mtime", "score")
        self._sort_records(self.sort_col, self.sort_desc)
        self._render_tree()

    def _sort_records(self, col: str, desc: bool):
        if col == "score":
            self.records.sort(key=lambda r: -r["score"] if desc else r["score"])
            return
        keymap = {
            "kind": lambda r: r["kind"],
            "name": lambda r: r["name"].lower(),
            "size": lambda r: r["size"],
            "mtime": lambda r: r["mtime"],
            "snippet": lambda r: (r["snippet"] or "").lower(),
            "folder": lambda r: r["folder"].lower(),
        }
        key = keymap.get(col, lambda r: r["name"].lower())
        self.records.sort(key=key, reverse=desc)

    def _render_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.iid2rec.clear()
        for index, rec in enumerate(self.records):
            if index >= 20000:
                break
            self._insert_row(f"s{index}", rec)

    def clear_results(self):
        self.records.clear()
        self.iid2rec.clear()
        self.tree.delete(*self.tree.get_children())
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.configure(state="disabled")
        self.var_count.set("命中 0 条")

    # ----------------------------------------------------------------- 预览
    def _on_select(self, _event=None):
        recs = self._selected_records()
        if recs:
            self._show_preview(recs[0])

    def _show_preview(self, rec: dict):
        limit = 300 * 1024
        try:
            with open(rec["path"], "rb") as fh:
                data = fh.read(limit + 1)
        except OSError as exc:
            self._set_preview(f"无法读取该文件：{exc}")
            return
        truncated = len(data) > limit
        text, enc = decode_bytes(data[:limit])
        if text is None:
            self._set_preview("（二进制文件，无法以文本方式预览）\n\n" + rec["path"])
            return

        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        matcher = self._current_matcher()
        if matcher is not None and not matcher.empty:
            count = 0
            for term in matcher.terms:
                for m in term.finditer(text):
                    if count >= 500:
                        break
                    try:
                        self.preview.tag_add("hit", f"1.0 + {m.start()} chars",
                                             f"1.0 + {m.end()} chars")
                    except tk.TclError:
                        break
                    count += 1
        head = f"# {rec['path']}    [{human_size(rec['size'])}]  编码猜测：{enc}" \
               + ("（仅显示前 300 KB）" if truncated else "") + "\n"
        self.preview.insert("1.0", head, ("hitline",))
        self.preview.configure(state="disabled")
        if rec.get("line"):
            self.preview.see(f"{rec['line'] + 1}.0")
        else:
            self.preview.see("1.0")

    def _set_preview(self, text: str):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def _current_matcher(self):
        try:
            return Matcher(SearchConfig(
                root=".", query=self.var_query.get(), mode=self.var_mode.get(),
                match_full_path=self.var_fullpath.get(),
                case_sensitive=self.var_case.get(), use_regex=self.var_regex.get(),
                whole_word=self.var_word.get(), logic=self.var_logic.get()))
        except ValueError:
            return None

    # ----------------------------------------------------------- 结果操作
    def _first_selected(self):
        recs = self._selected_records()
        if not recs:
            messagebox.showinfo("未选择", "请先在结果列表中选中一条记录。")
            return None
        return recs[0]

    def open_selected(self):
        rec = self._first_selected()
        if not rec:
            return
        try:
            _open_path(rec["path"])
        except OSError as exc:
            messagebox.showerror("打开失败", f"无法打开文件：\n{rec['path']}\n\n{exc}")

    def reveal_selected(self):
        rec = self._first_selected()
        if not rec:
            return
        try:
            _reveal_in_folder(rec["path"])
        except OSError as exc:
            messagebox.showerror("打开失败", f"无法打开文件夹：\n{rec['folder']}\n\n{exc}")

    def copy_path(self):
        recs = self._selected_records()
        if not recs:
            messagebox.showinfo("未选择", "请先在结果列表中选中记录。")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(r["path"] for r in recs))
        self.var_status.set(f"已复制 {len(recs)} 条完整路径到剪贴板。")

    def copy_snippet(self):
        rec = self._first_selected()
        if not rec:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(rec["snippet"] or "")
        self.var_status.set("已复制命中片段到剪贴板。")

    def _popup_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if iid:
            if iid not in self.tree.selection():
                self.tree.selection_set(iid)
            self.menu.tk_popup(event.x_root, event.y_root)

    def export_csv(self):
        if not self.records:
            messagebox.showinfo("没有结果", "当前没有可导出的结果。")
            return
        path = filedialog.asksaveasfilename(
            title="导出搜索结果", defaultextension=".csv",
            initialfile="搜索结果.csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["命中方式", "文件名", "大小(字节)", "修改时间",
                                 "命中片段", "所在目录", "完整路径"])
                for rec in self.records:
                    writer.writerow([rec["kind"], rec["name"], rec["size"],
                                     human_time(rec["mtime"]), rec["snippet"],
                                     rec["folder"], rec["path"]])
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc))
            return
        self.var_status.set(f"已导出 {len(self.records)} 条结果到 {path}")
        messagebox.showinfo("导出完成", f"已导出 {len(self.records)} 条结果：\n{path}")

    def on_close(self):
        self._save_config()
        if self.searching:
            self.stop_event.set()
        self.root.destroy()


def main() -> int:
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print("无法启动图形界面（tkinter）：", exc, file=sys.stderr)
        print("请确认已安装带 tkinter 的 Python，并在有桌面环境的会话中运行。",
              file=sys.stderr)
        return 1
    app = FinderApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
