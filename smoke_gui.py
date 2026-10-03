# -*- coding: utf-8 -*-
"""界面冒烟测试：真实创建窗口 -> 真实搜索 -> 预览 -> 导出 CSV -> 关闭。

用法::  python smoke_gui.py [截图输出路径]

需要能访问桌面会话（Windows 交互式会话）。退出码 0 表示全部通过。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import quick_file_finder as qff  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_OUT = os.path.join(HERE, "_smoke_result.csv")
SHOT_OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "screenshot.png")

ok, bad = [], []


def check(name, cond, detail=""):
    (ok if cond else bad).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if not cond else ""))


def pump(root, seconds=0.0):
    end = time.time() + seconds
    while True:
        root.update()
        if time.time() >= end:
            return
        time.sleep(0.01)


def run_and_wait(app, root, timeout=90):
    app.start_search()
    deadline = time.time() + timeout
    while app.searching and time.time() < deadline:
        pump(root, 0.05)
    pump(root, 0.4)


def main() -> int:
    # 界面里会弹的对话框在测试中自动化处理
    messagebox.showinfo = lambda *a, **k: None
    messagebox.showwarning = lambda *a, **k: None
    messagebox.showerror = lambda *a, **k: (_ for _ in ()).throw(AssertionError(a))
    filedialog.asksaveasfilename = lambda *a, **k: CSV_OUT
    filedialog.askdirectory = lambda *a, **k: HERE

    root = tk.Tk()
    app = qff.FinderApp(root)
    check("主窗口构建成功", root.winfo_exists() == 1)
    check("结果列表列数正确", len(app.tree["columns"]) == 6, str(app.tree["columns"]))
    check("排除位置面板存在", hasattr(app, "txt_skip") and app.txt_skip.winfo_exists() == 1)

    # ---- 真实搜索：在脚本所在目录里同时按文件名和内容找 ----
    app.var_dir.set(HERE)
    app.var_query.set("def search")
    app.var_mode.set(qff.MODE_BOTH)
    app.var_ext.set("")
    app._set_skip_text(qff.DEFAULT_SKIP_DIRS + "\n_selftest_tmp\n__pycache__")
    run_and_wait(app, root)

    check("搜索线程已结束", not app.searching)
    rows = app.tree.get_children()
    check("结果列表出现命中行", len(rows) > 0, f"rows={len(rows)}")
    check("命中 quick_file_finder.py",
          any(app.iid2rec[r]["name"] == "quick_file_finder.py" for r in rows))
    kinds = {app.iid2rec[r]["kind"] for r in rows}
    check("存在内容命中（命中方式列）", any("内容" in k for k in kinds), str(kinds))
    check("状态栏报告排除了多少目录", "按排除清单跳过" in app.var_status.get(),
          app.var_status.get())

    # ---- 预览 ----
    target = next((r for r in rows if app.iid2rec[r]["name"] == "quick_file_finder.py"), rows[0])
    app.tree.selection_set(target)
    pump(root, 0.5)
    text = app.preview.get("1.0", "end")
    check("选中后预览区有内容", len(text.strip()) > 200, f"len={len(text)}")
    check("预览高亮了关键词", len(app.preview.tag_ranges("hit")) > 0)
    check("状态栏显示统计信息", "扫描" in app.var_status.get(), app.var_status.get())

    # ---- 排序 ----
    app.sort_by("size")
    pump(root, 0.2)
    check("按大小排序后仍有结果", len(app.tree.get_children()) == len(rows))

    # ---- 截图（在“有结果 + 有高亮预览”的状态下抓，失败不影响测试结论） ----
    try:
        from PIL import ImageGrab
        app.tree.selection_set(target)
        root.deiconify()
        root.attributes("-topmost", True)      # 抓图前必须把窗口放到最前，否则会抓到桌面
        root.lift()
        root.focus_force()
        pump(root, 1.0)
        x, y = root.winfo_rootx(), root.winfo_rooty()
        w, h = root.winfo_width(), root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(SHOT_OUT)
        root.attributes("-topmost", False)
        check("已保存界面截图", os.path.getsize(SHOT_OUT) > 5000, SHOT_OUT)
    except Exception as exc:                       # noqa: BLE001
        print(f"[INFO] 截图跳过：{exc}")

    # ---- 导出 CSV ----
    app.export_csv()
    pump(root, 0.2)
    check("CSV 已导出", os.path.isfile(CSV_OUT))
    if os.path.isfile(CSV_OUT):
        with open(CSV_OUT, "r", encoding="utf-8-sig") as fh:
            lines = fh.read().strip().splitlines()
        check("CSV 行数 = 结果数 + 表头", len(lines) == len(app.records) + 1,
              f"{len(lines)} vs {len(app.records) + 1}")

    # ---- 空关键词（列出全部文件）与停止 ----
    app.var_query.set("")
    app.var_mode.set(qff.MODE_NAME)
    app.var_maxresults.set("20")
    app.start_search()
    pump(root, 0.3)
    app.stop_search()
    deadline = time.time() + 30
    while app.searching and time.time() < deadline:
        pump(root, 0.05)
    pump(root, 0.3)
    check("空关键词按文件名列出文件", len(app.records) > 0, str(len(app.records)))
    check("手动停止后回到可搜索状态", bool(app.btn_search.instate(["!disabled"])))

    # ---- 排除位置：路径排除 / 通配符排除 / 常用排除一键填入 ----
    exc_dir = os.path.join(HERE, "_smoke_excluded")
    os.makedirs(exc_dir, exist_ok=True)
    with open(os.path.join(exc_dir, "needle.txt"), "w", encoding="utf-8") as fh:
        fh.write("SMOKE_EXCLUDE_TOKEN 用于验证排除功能\n")

    def names_now():
        return {app.iid2rec[r]["name"] for r in app.tree.get_children()}

    app.var_query.set("SMOKE_EXCLUDE_TOKEN")
    app.var_mode.set(qff.MODE_CONTENT)
    app.var_maxresults.set("1000")
    app._set_skip_text("")
    run_and_wait(app, root)
    check("不排除时能找到 _smoke_excluded/needle.txt", "needle.txt" in names_now(),
          str(names_now()))

    app._append_skip(exc_dir)                     # 按“完整路径”排除
    run_and_wait(app, root)
    check("按完整路径排除后找不到该目录", "needle.txt" not in names_now(), str(names_now()))
    check("状态栏出现排除计数", "按排除清单跳过" in app.var_status.get(), app.var_status.get())

    app._set_skip_text(os.path.join(HERE, "_smoke*"))   # 通配符
    check("通配符写入排除清单后解析生效", "通配符 1 条" in app.var_skip_hint.get(),
          app.var_skip_hint.get())
    run_and_wait(app, root)
    check("通配符也能排除该目录", "needle.txt" not in names_now(), str(names_now()))

    app._set_skip_text("")
    empty_count = len(app._skip_lines())
    app._fill_common_skips()
    check("「整盘常用排除」一键填入", len(app._skip_lines()) >= empty_count + 20,
          f"{empty_count} -> {len(app._skip_lines())}")
    check("排除条数提示同步更新", "已设置" in app.var_skip_hint.get(), app.var_skip_hint.get())

    # 把搜索目录本身写进排除清单时，应自动忽略并提示
    app.var_query.set("def search")
    app.var_mode.set(qff.MODE_BOTH)
    app._set_skip_text(HERE)
    run_and_wait(app, root)
    check("排除清单包含搜索目录自身时自动忽略并提示",
          "包含了搜索目录本身" in app.var_status.get(), app.var_status.get())

    for path in (CSV_OUT,):
        try:
            os.remove(path)
        except OSError:
            pass
    shutil.rmtree(exc_dir, ignore_errors=True)
    app.on_close()
    print("\n" + "=" * 60)
    print(f"界面测试通过 {len(ok)} 项，失败 {len(bad)} 项")
    if bad:
        print("失败项：" + ", ".join(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
