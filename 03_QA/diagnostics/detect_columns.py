#!/usr/bin/env python3
"""Column detection: does a PDF page use several columns, and would a plain extraction mix text across columns?

Usage:
    cd <repository root>
    QA/venv/bin/python QA/scripts/detect_columns.py

How it works
----
On a page with columns, the words in the same horizontal band (around the same y coordinate) fall into separate clusters along x,
with a clear vertical gap (gutter) between them. If such a gap exists and the text is extracted by y position only,
words from different columns are interleaved. Sentences break apart and collocations appear that are not in the text.

Rule: the page is cut into 100 vertical strips and the word density of each strip is counted. Six or more empty strips in a row (6% of the page width)
with text on both sides count as a gutter.
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
    """Return the number of gutters found on the page (0 means a single column)."""
    words = page.extract_words()
    if len(words) < 40:          # pages with very little text (covers, inserts) are skipped
        return None
    w = page.width
    occupied = [0] * BINS
    for word in words:
        lo = max(0, min(BINS - 1, int(word["x0"] / w * BINS)))
        hi = max(0, min(BINS - 1, int(word["x1"] / w * BINS)))
        for b in range(lo, hi + 1):
            occupied[b] += 1
    # find runs of empty strips in the middle of the page
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
    """Check whether extracting by y position would mix columns:
    count horizontal bands that contain words from different columns."""
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
    print("Column detection")
    print("=" * 76)
    print(f"{'Report':<34}{'Pages':>6}{'Multi-column %':>16}{'Mixing risk':>14}")
    print("-" * 76)
    for name in KNOWN:
        p = PDF_DIR / name
        if not p.exists():
            print(f"{name:<34}  not found")
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
            print(f"{name[:32]:<34}{0:>6}      too little text, skipped")
            continue
        avg_risk = sum(risky) / len(risky) if risky else 0
        flag = " ⚠️" if multi / valid > 0.3 else ""
        print(f"{name[:32]:<34}{valid:>6}{multi / valid:>11.0%}{avg_risk:>13.0%}{flag}")
    print("-" * 76)
    print("Multi-column %: share of pages with at least one gutter")
    print("Mixing risk: share of horizontal bands that contain text from both columns, which is")
    print("             how likely an extraction by y position is to interleave sentences")


if __name__ == "__main__":
    main()
