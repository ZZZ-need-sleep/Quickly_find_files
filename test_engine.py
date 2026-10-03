# -*- coding: utf-8 -*-
"""搜索内核的自测脚本（不启动界面）。

用法::

    python test_engine.py

会在临时目录里造一棵测试文件树，逐项验证：
文件名搜索、内容搜索、两者同时(OR)、AND/OR 多关键词、正则、
大小写、文件类型过滤、排除目录、隐藏文件、GBK 编码、二进制跳过、结果上限。
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quick_file_finder import (  # noqa: E402
    Matcher, SearchConfig, decode_bytes, human_size, iter_files,
    parse_ext_filter, parse_skip_items, drop_root_blocking_skips, run_search,
    MODE_BOTH, MODE_CONTENT, MODE_NAME,
)

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = ""):
    (PASS if condition else FAIL).append(name)
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f"  -> {detail}" if detail and not condition else ""))


def build_tree(base: str):
    def w(rel, text, encoding="utf-8"):
        path = os.path.join(base, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(text.encode(encoding))
        return path

    w("alpha_report.txt", "季度销售报告\n本月业绩良好。\n")
    w("beta_notes.md", "TODO list\nQUICKFINDER_TOKEN inside this file\nend\n")
    w("data.csv", "id,name\n1,QuickFinder\n2,other\n")
    w("sub/nested/deep.txt", "深层目录里的中文关键词：秘密代号\n")
    w("sub/plain.log", "nothing interesting here\n")
    # GBK 编码文件
    w("sub/gbk_legacy.txt", "遗留编码文件\n秘密代号在这里\n", encoding="gbk")
    # 被排除的目录
    w("node_modules/pkg/index.js", "QUICKFINDER_TOKEN in excluded dir\n")
    # 用于“按完整路径 / 通配符排除”的对照目录
    w("keep/target.txt", "KEEPME_TOKEN keep this one\n")
    w("junk/target.txt", "KEEPME_TOKEN exclude this one\n")
    w("junk/deep/inner.txt", "KEEPME_TOKEN nested\n")
    # 隐藏目录 / 隐藏文件
    w(".hidden/secret.txt", "QUICKFINDER_TOKEN in hidden dir\n")
    w(".hiddenfile", "QUICKFINDER_TOKEN hidden file\n")
    # 二进制文件
    with open(os.path.join(base, "image.png"), "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01")
    with open(os.path.join(base, "blob.bin"), "wb") as fh:
        fh.write(b"\x00\x01\x02QUICKFINDER_TOKEN\x00\x03")


def search(base: str, **kwargs):
    cfg = SearchConfig(root=base, **kwargs)
    matcher = Matcher(cfg)
    found = []
    stop = threading.Event()
    run_search(cfg, matcher, lambda msg: found.append(msg) if msg[0] == "result" else None, stop)
    return [m[1]["name"] for m in found], [m[1] for m in found], cfg


def main() -> int:
    # 测试文件树建在脚本同级目录内，避免受沙箱/权限限制
    # 注意：不要用 tempfile.mkdtemp（它以 0o700 建目录，在 Windows 沙箱下会变成不可写）
    tmp_parent = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp")
    base = os.path.join(tmp_parent, f"tree_{os.getpid()}_{int(time.time())}")
    os.makedirs(base, exist_ok=True)
    try:
        build_tree(base)

        # 1) 文件名搜索
        names, _, _ = search(base, query="report", mode=MODE_NAME)
        check("按文件名搜索命中 alpha_report.txt", names == ["alpha_report.txt"], str(names))

        # 2) 内容搜索
        names, _, _ = search(base, query="QUICKFINDER_TOKEN", mode=MODE_CONTENT)
        check("按内容搜索命中文件内关键词", names == ["beta_notes.md"], str(names))
        check("内容搜索跳过二进制文件", "image.png" not in names and "blob.bin" not in names, str(names))
        check("内容搜索默认排除隐藏文件", ".hiddenfile" not in names, str(names))
        check("内容搜索默认排除隐藏目录", "secret.txt" not in names, str(names))
        check("内容搜索默认排除 node_modules", "index.js" not in names, str(names))

        # 3) 两者同时（默认）：文件名命中或内容命中，满足其一即可
        names, _, _ = search(base, query="report")
        check("两者同时：文件名命中即可（无需读内容）", names == ["alpha_report.txt"], str(names))
        names, _, _ = search(base, query="QUICKFINDER_TOKEN")
        check("两者同时：内容命中即可", "beta_notes.md" in names, str(names))
        names, _, _ = search(base, query="QuickFinder")
        check("两者同时：内容命中覆盖多个文件", set(names) >= {"beta_notes.md", "data.csv"}, str(names))

        # 4) 多关键词 AND / OR
        names_and, _, _ = search(base, query="QUICKFINDER_TOKEN TODO", mode=MODE_CONTENT)
        check("多关键词 AND", set(names_and) == {"beta_notes.md"}, str(names_and))
        names_or, _, _ = search(base, query="QuickFinder beta", mode=MODE_CONTENT, logic="or")
        check("多关键词 OR 命中更多文件", set(names_or) == {"beta_notes.md", "data.csv"}, str(names_or))

        # 5) 正则（^ $ 按行匹配）
        names, _, _ = search(base, query=r"^id,name$", mode=MODE_CONTENT, use_regex=True)
        check("正则内容搜索 (^id,name$)", names == ["data.csv"], str(names))
        names, _, _ = search(base, query=r"^\d+,Quick\w+$", mode=MODE_CONTENT, use_regex=True)
        check("正则内容搜索 (^\\d+,Quick\\w+$)", names == ["data.csv"], str(names))
        names, _, _ = search(base, query=r"alpha.*\.txt$", mode=MODE_NAME, use_regex=True)
        check("正则文件名搜索", names == ["alpha_report.txt"], str(names))
        try:
            search(base, query="[unclosed", use_regex=True)
            check("非法正则会报错", False, "未抛出异常")
        except ValueError:
            check("非法正则会报错", True)

        # 6) 大小写
        names, _, _ = search(base, query="quickfinder_token", mode=MODE_CONTENT)
        check("默认不区分大小写", "beta_notes.md" in names, str(names))
        names, _, _ = search(base, query="quickfinder_token", mode=MODE_CONTENT, case_sensitive=True)
        check("区分大小写时不再命中", names == [], str(names))

        # 7) GBK 编码识别
        text, enc, _ = None, None, None
        with open(os.path.join(base, "sub", "gbk_legacy.txt"), "rb") as fh:
            raw = fh.read()
        from quick_file_finder import read_text_file
        text, enc, _ = read_text_file(os.path.join(base, "sub", "gbk_legacy.txt"), 1024 * 1024)
        check("GBK 文件能正确解码", text is not None and "秘密代号" in text, f"{enc} {text!r}")
        names, _, _ = search(base, query="秘密代号", mode=MODE_CONTENT)
        check("中文关键词内容搜索", set(names) == {"deep.txt", "gbk_legacy.txt"}, str(names))

        # 8) 文件类型过滤
        names, _, _ = search(base, query="QUICKFINDER_TOKEN", ext_filter_text=".md")
        check("扩展名过滤 .md", names == ["beta_notes.md"], str(names))
        names, _, _ = search(base, query="QuickFinder", ext_filter_text="*.csv *.md")
        check("通配符过滤 *.csv *.md", set(names) == {"beta_notes.md", "data.csv"}, str(names))

        # 9) 递归 / 深度
        names, _, _ = search(base, query="deep", mode=MODE_NAME, recursive=False)
        check("不递归时只搜顶层", names == [], str(names))
        names, _, _ = search(base, query="deep", mode=MODE_NAME, max_depth=1)
        check("深度=1 时不进入 sub/nested", names == [], str(names))
        names, _, _ = search(base, query="deep", mode=MODE_NAME, max_depth=2)
        check("深度=2 时命中 nested/deep.txt", names == ["deep.txt"], str(names))

        # 10) 包含隐藏文件
        names, _, _ = search(base, query="QUICKFINDER_TOKEN", include_hidden=True)
        check("包含隐藏文件后能搜到 .hiddenfile", ".hiddenfile" in names, str(names))

        # 11) 跳过目录可配置
        names, _, _ = search(base, query="QUICKFINDER_TOKEN", skip_text="")
        check("清空排除清单后能搜到 node_modules", "index.js" in names, str(names))

        # 11b) 按“完整路径”排除（整盘搜索时跳过肯定没有的地址）
        names, _, _ = search(base, query="KEEPME_TOKEN")
        check("默认不排除时 keep/junk 都能搜到", len(names) == 3, str(names))
        names, _, _ = search(base, query="KEEPME_TOKEN",
                             skip_text=os.path.join(base, "junk"))
        check("按完整路径排除 junk 后只剩 keep", names == ["target.txt"], str(names))
        names, _, _ = search(base, query="KEEPME_TOKEN",
                             skip_text=os.path.join(base, "junk") + os.sep)
        check("路径末尾带分隔符同样有效", names == ["target.txt"], str(names))
        names, _, _ = search(base, query="KEEPME_TOKEN",
                             skip_text=os.path.join(base, "junk", "deep"))
        check("排除子目录只影响该子树",
              len(names) == 2 and "inner.txt" not in names, str(names))
        names, _, _ = search(base, query="KEEPME_TOKEN",
                             skip_text=os.path.join(base, "junk").upper())
        check("路径排除不区分大小写", names == ["target.txt"], str(names))

        # 11c) 按“通配符”排除
        names, _, _ = search(base, query="KEEPME_TOKEN", skip_text="*junk*")
        check("通配符 *junk* 排除整个目录", names == ["target.txt"], str(names))
        names, _, _ = search(base, query="", mode=MODE_NAME, skip_text="*.log")
        check("通配符 *.log 排除文件本身", "plain.log" not in names, str(names))
        names, _, _ = search(base, query="KEEPME_TOKEN", skip_text="inner.txt")
        check("排除清单里的纯文件名按目录名处理（不误伤文件）",
              len(names) == 3, str(names))

        # 11d) 排除清单解析 / 自我保护
        sample = "node_modules\n" + "E:\\Games\n" + "E:\\\n" + "*.tmp\n\n; .git"
        names_set, prefixes, globs = parse_skip_items(sample)
        check("parse_skip_items 分类正确",
              names_set == {"node_modules", ".git"} and globs == ("*.tmp",) and
              len(prefixes) == 2, f"{names_set} {prefixes} {globs}")
        kept, dropped = drop_root_blocking_skips(base, frozenset(
            {os.path.normpath(base).lower(), "c:\\nonexistent"}))
        check("排除项包含搜索目录本身时会被剔除",
              os.path.normpath(base).lower() not in kept and
              "c:\\nonexistent" in kept and dropped == [os.path.normpath(base).lower()],
              f"{kept} {dropped}")
        kept_all, dropped_all = drop_root_blocking_skips(base, frozenset({os.path.splitdrive(base)[0].lower() + os.sep}))
        check("整盘排除项会连带剔除（因为包含了搜索目录）",
              not kept_all and len(dropped_all) == 1, f"{kept_all} {dropped_all}")

        # 11e) 目录名排除仍可与路径/通配符混用
        names, _, _ = search(base, query="KEEPME_TOKEN", skip_text="junk node_modules")
        check("目录名排除仍可用（junk 按名字）", names == ["target.txt"], str(names))

        # 12) 结果上限
        names, _, _ = search(base, query="", mode=MODE_NAME, max_results=3)
        check("最多结果数生效", len(names) == 3, str(names))

        # 13) 命中方式与排序字段
        _, recs, _ = search(base, query="alpha", mode=MODE_BOTH)
        check("命中方式标记为「文件名」", recs and recs[0]["kind"] == "文件名", str(recs[:1]))

        # 14) 辅助函数
        check("parse_ext_filter", parse_ext_filter("*.log .PY, txt") == ((".py", ".txt"), ("*.log",)),
              str(parse_ext_filter("*.log .PY, txt")))
        check("human_size", human_size(1536) == "1.5 KB", human_size(1536))
        check("decode_bytes 二进制判定", decode_bytes(b"\x00\x01abc") == (None, None))
        check("iter_files 默认跳过隐藏项", len(list(iter_files(base))) == 12,
              str(len(list(iter_files(base)))))
        check("iter_files 含隐藏项时能遍历到全部",
              len(list(iter_files(base, include_hidden=True))) == 14,
              str(len(list(iter_files(base, include_hidden=True)))))
        counted = {}
        listed = list(iter_files(base, skip_names=frozenset({"junk"}),
                                 stats=counted))
        check("iter_files 统计被排除的目录", counted.get("skip_dirs") == 1 and
              len(listed) == 10, f"{counted} {len(listed)}")

        print("\n" + "=" * 60)
        print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
        if FAIL:
            print("失败项：" + ", ".join(FAIL))
        return 1 if FAIL else 0
    finally:
        shutil.rmtree(base, ignore_errors=True)
        try:
            os.rmdir(tmp_parent)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
