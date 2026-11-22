#!/usr/bin/env python3
"""多栏排版检测：判断 PDF 页面是否为多栏，以及默认提取是否会造成跨栏串行。

用法：
    cd <repository root>
    QA/venv/bin/python QA/scripts/detect_columns.py

原理
----
多栏排版的特征：同一水平带（相同 y 坐标附近）上，词的 x 坐标分成若干互不重叠的簇，
簇之间存在明显的横向空白间隙（gutter）。若存在这种间隙，而提取时仅按 y 排序，
则不同栏的文字会被交错拼接 —— 句子因此断裂，搭配关系被伪造。

判据：把页面横向切成 100 个格子，统计每格中词的密度；连续 ≥6 格（即页宽 6%）
密度为 0 且两侧均有内容 → 判定为栏间隙。
"""
import collections
from pathlib import Path

import pdfplumber

PDF_DIR = Path("Source/pdf")
KNOWN = [
    "Rivian_2024_Impact_Report.pdf",
    "Lucid-Group-Inc-2023-Sustainability-Report.pdf",
    "XPeng_2024_ESG_Report.pdf",
    "BYD_2024_Sustainability_Report_EN.pdf",
    "NIO_2024_ESG_Report_English.pdf",
]
BINS = 100
MIN_GAP_BINS = 6


def gutters_of_page(page):
    """返回该页检测到的栏间隙数量（0 = 单栏）。"""
    words = page.extract_words()
    if len(words) < 40:          # 内容过少的页（封面/插页）不计
        return None
    w = page.width
    occupied = [0] * BINS
    for word in words:
        lo = max(0, min(BINS - 1, int(word["x0"] / w * BINS)))
        hi = max(0, min(BINS - 1, int(word["x1"] / w * BINS)))
        for b in range(lo, hi + 1):
            occupied[b] += 1
    # 找出中间区域连续为 0 的段
    first = next((i for i, v in enumerate(occupied) if v), None)
    last = next((i for i in range(BINS - 1, -1, -1) if occupied[i]), None)
    if first is None or last is None:
        return None
    gaps, run = 0, 0
    for i in range(first, last + 1):
        if occupied[i] == 0:
            run += 1
        else:
            if run >= MIN_GAP_BINS:
                gaps += 1
            run = 0
    return gaps


def crossing_risk(page):
    """检查按 y 排序提取时是否会跨栏串行：
    统计同一水平带内是否同时存在分属不同栏的词。"""
    words = page.extract_words()
    if len(words) < 40:
        return None
    w = page.width
    mid = w / 2
    bands = collections.defaultdict(lambda: [0, 0])
    for word in words:
        key = round(word["top"] / 12)
        side = 0 if (word["x0"] + word["x1"]) / 2 < mid else 1
        bands[key][side] += 1
    both = sum(1 for v in bands.values() if v[0] >= 3 and v[1] >= 3)
    return both / max(len(bands), 1)


def main():
    print("多栏排版检测")
    print("=" * 76)
    print(f"{'报告':<34}{'抽样页':>6}{'多栏页占比':>12}{'跨栏串行风险':>14}")
    print("-" * 76)
    for name in KNOWN:
        p = PDF_DIR / name
        if not p.exists():
            print(f"{name:<34}  未找到")
            continue
        with pdfplumber.open(p) as pdf:
            total = len(pdf.pages)
            idxs = [int(2 + (total - 3) * i / 15) for i in range(15)]
            multi, risky, valid = 0, [], 0
            for i in idxs:
                g = gutters_of_page(pdf.pages[i])
                if g is None:
                    continue
                valid += 1
                if g >= 1:
                    multi += 1
                r = crossing_risk(pdf.pages[i])
                if r is not None:
                    risky.append(r)
        if not valid:
            print(f"{name[:32]:<34}{0:>6}      内容过少，跳过")
            continue
        avg_risk = sum(risky) / len(risky) if risky else 0
        flag = " ⚠️" if multi / valid > 0.3 else ""
        print(f"{name[:32]:<34}{valid:>6}{multi / valid:>11.0%}{avg_risk:>13.0%}{flag}")
    print("-" * 76)
    print("多栏页占比：检测到栏间隙的页面比例")
    print("跨栏串行风险：同一水平带内同时存在左右两栏文字的比例 —— 该比例即为")
    print("              按 y 排序提取时句子被交错拼接的风险程度")


if __name__ == "__main__":
    main()
