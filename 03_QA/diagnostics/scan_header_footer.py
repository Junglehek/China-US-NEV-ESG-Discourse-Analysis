#!/usr/bin/env python3
"""页眉页脚扫描器：按页+坐标识别跨页重复的结构性文本。

用法：
    cd <repository root>
    QA/venv/bin/python QA/scripts/scan_header_footer.py
    QA/venv/bin/python QA/scripts/scan_header_footer.py --pdf Source/pdf/XPeng_2024_ESG_Report.pdf
    QA/venv/bin/python QA/scripts/scan_header_footer.py --sample 40 --zone 0.12

原理
----
页眉/页脚有三个共同特征：①落在页面顶部或底部的固定纵向区间
②跨多个页面重复出现 ③内容固定（页码除外，其数字部分逐页变化）。
本脚本据此三点自动识别，无需人工逐页阅读。

页码处理：把所有数字替换为 # 后再比对，故 "12" 与 "13" 会归为同一模式 "#"。
输出：人类可读报告，每项标明「出现在多少页 / 纵向坐标 / 原文 / 建议」。
"""
import argparse
import collections
import re
from pathlib import Path

import pdfplumber

PDF_DIR = Path("Source/pdf")
OUT_DIR = Path("QA/reports")

# 已知的六份语料，按体积从小到大排（便于先看小的）
KNOWN = [
    "Rivian_2024_Impact_Report.pdf",
    "Lucid-Group-Inc-2023-Sustainability-Report.pdf",
    "XPeng_2024_ESG_Report.pdf",
    "BYD_2024_Sustainability_Report_EN.pdf",
    "NIO_2024_ESG_Report_English.pdf",
    "2024-Tesla-Impact-Report-Extended-EN.pdf",
]

NUM_RE = re.compile(r"\d+")
WS_RE = re.compile(r"\s+")


def normalise(text: str) -> str:
    """把数字替换为 #，压缩空白 —— 用于跨页模式比对（页码因此可被识别）。"""
    return WS_RE.sub(" ", NUM_RE.sub("#", text)).strip()


def rows_of_page(page, tol: float = None):
    """把页面上的词按纵向位置聚合成行，返回 [(top, text)]。

    ⚠️ tol 必须自适应页高：XPeng 报告为 1920×1080 横版 PPT 式排版，
    同一条章节导航栏内三段文字的 top 值有 2–3pt 微差（66.7 / 68.9 / 69.6），
    固定小容差会把它们切成不同"行"，导致同一导航栏在不同页被拼成不同字符串，
    跨页模式比对因此失效。tol 取页高的 1.5% 可将同一水平带的文字归并到一起。
    """
    if tol is None:
        tol = max(page.height * 0.015, 3.0)
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    if not words:
        return []
    buckets = collections.defaultdict(list)
    for w in words:
        key = round(w["top"] / tol)
        buckets[key].append(w)
    rows = []
    for key in sorted(buckets):
        ws = sorted(buckets[key], key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in ws)
        rows.append((min(w["top"] for w in ws), text))
    return rows


def sample_pages(total: int, n: int):
    """等距抽样，跳过封面前 2 页与封底最后 1 页。"""
    lo, hi = 2, max(total - 1, 3)
    if hi - lo <= n:
        return list(range(lo, hi))
    step = (hi - lo) / n
    return [int(lo + step * i) for i in range(n)]


def scan(pdf_path: Path, sample_n: int, zone: float):
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        idxs = sample_pages(total, sample_n)
        header = collections.defaultdict(list)   # pattern -> [(page, top, raw)]
        footer = collections.defaultdict(list)
        body_rep = collections.defaultdict(list)  # 正文区的跨页重复（章节导航栏常在此）
        for i in idxs:
            page = pdf.pages[i]
            h = page.height
            top_limit = h * zone
            bot_limit = h * (1 - zone)
            for top, text in rows_of_page(page):
                if len(text) < 2:
                    continue
                pat = normalise(text)
                if not pat:
                    continue
                if top <= top_limit:
                    header[pat].append((i + 1, round(top), text))
                elif top >= bot_limit:
                    footer[pat].append((i + 1, round(top), text))
                else:
                    body_rep[pat].append((i + 1, round(top), text))
    return total, len(idxs), header, footer, body_rep


def render(name, total, n_sampled, header, footer, body_rep, min_ratio=0.4):
    lines = [f"\n{'═' * 78}", f"  {name}", f"  共 {total} 页，抽样 {n_sampled} 页", f"{'═' * 78}"]
    threshold = max(2, int(n_sampled * min_ratio))

    def block(title, store, note, extra_filter=None):
        items = [(p, v) for p, v in store.items() if len(v) >= threshold]
        if extra_filter:
            items = [(p, v) for p, v in items if extra_filter(p, v)]
        items.sort(key=lambda kv: -len(kv[1]))
        lines.append(f"\n【{title}】（判定线：出现 ≥{threshold}/{n_sampled} 页）")
        if not items:
            lines.append("   —— 未发现跨页重复项")
            return []
        for pat, occ in items:
            pages = len(occ)
            tops = [t for _, t, _ in occ]
            raw = occ[0][2]
            shown = raw if len(raw) <= 62 else raw[:59] + "..."
            lines.append(f"   出现 {pages:>3}/{n_sampled} 页 │ y≈{min(tops)}-{max(tops)} │ \"{shown}\"")
            if pat != normalise(raw) or "#" in pat:
                lines.append(f"        └ 模式：\"{pat}\"（# 表示逐页变化的数字，如页码）")
            lines.append(f"        └ {note}")
        return items

    block("页眉区（页面顶部）", header, "→ 建议删除")
    block("页脚区（页面底部）", footer, "→ 建议删除（含页码）")
    # 正文区的跨页重复：只报纵向位置稳定的（章节导航栏特征），排除正常正文复现
    def stable_position(pat, occ):
        tops = [t for _, t, _ in occ]
        return max(tops) - min(tops) <= 30
    block("正文区但位置固定的重复项（章节导航栏 / 侧边栏）", body_rep,
          "→ ⚠️ 高度可疑，需 grep 上下文确认后删除", stable_position)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", type=Path, help="只扫这一个文件")
    ap.add_argument("--sample", type=int, default=30, help="抽样页数（默认 30）")
    ap.add_argument("--zone", type=float, default=0.10, help="顶部/底部区域占页高比例（默认 0.10）")
    ap.add_argument("--out", type=Path, default=OUT_DIR / "样张_页眉页脚扫描.txt")
    args = ap.parse_args()

    targets = [args.pdf] if args.pdf else [PDF_DIR / n for n in KNOWN]
    report = ["页眉页脚扫描报告",
              f"抽样页数：{args.sample}／每份　顶底区域：页高的 {args.zone:.0%}",
              "说明：数字已归一化为 # 以便识别页码；y 值越小越靠页面顶部"]
    for p in targets:
        if not p.exists():
            report.append(f"\n⚠️ 未找到：{p}")
            continue
        total, n, hd, ft, bd = scan(p, args.sample, args.zone)
        text = render(p.name, total, n, hd, ft, bd)
        report.append(text)
        print(text)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(report), encoding="utf-8")
    print(f"\n\n报告已保存：{args.out}")


if __name__ == "__main__":
    main()
