#!/usr/bin/env python3
"""Header and footer scanner: finds structural text that repeats across pages, using page position.

Usage:
    cd <repository root>
    QA/venv/bin/python QA/scripts/scan_header_footer.py
    QA/venv/bin/python QA/scripts/scan_header_footer.py --pdf Source/pdf/XPeng_2024_ESG_Report.pdf
    QA/venv/bin/python QA/scripts/scan_header_footer.py --sample 40 --zone 0.12

How it works
----
Headers and footers share three features: (1) they sit in a fixed band at the top or bottom of the page,
(2) they repeat across many pages, and (3) their wording stays the same (except page numbers, whose digits change).
The script uses these three features to find them, so nobody has to read every page.

Page numbers: all digits are replaced with # before comparison, so "12" and "13" become the same pattern "#".
Output: a readable report that gives, for each item, the number of pages it appears on, its vertical position, its text and a suggestion.
"""
import argparse
import collections
import re
from pathlib import Path

import pdfplumber

PDF_DIR = Path("Source/pdf")
OUT_DIR = Path("QA/reports")

# The six reports of the corpus, from smallest to largest (so the small ones can be read first)
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
    """Replace digits with # and collapse white space, so that patterns can be compared across pages (this is how page numbers are found)."""
    return WS_RE.sub(" ", NUM_RE.sub("#", text)).strip()


def rows_of_page(page, tol: float = None):
    """Group the words on a page into lines by vertical position and return [(top, text)].

    The tolerance must scale with the page height. The XPeng report uses a 1920 x 1080 landscape, slide-like layout,
    and the three parts of one navigation bar differ in top position by 2 to 3 points (66.7 / 68.9 / 69.6).
    A small fixed tolerance splits them into different "lines", so the same bar becomes a different string on each page
    and the comparison across pages fails. A tolerance of 1.5% of the page height keeps text in the same band together.
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
    """Sample pages at equal intervals, skipping the first two pages (cover) and the last page (back cover)."""
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
        body_rep = collections.defaultdict(list)  # repeats inside the body area (navigation bars are often here)
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
    lines = [f"\n{'═' * 78}", f"  {name}", f"  {total} pages, {n_sampled} sampled", f"{'═' * 78}"]
    threshold = max(2, int(n_sampled * min_ratio))

    def block(title, store, note, extra_filter=None):
        items = [(p, v) for p, v in store.items() if len(v) >= threshold]
        if extra_filter:
            items = [(p, v) for p, v in items if extra_filter(p, v)]
        items.sort(key=lambda kv: -len(kv[1]))
        lines.append(f"\n[{title}] (threshold: on at least {threshold}/{n_sampled} pages)")
        if not items:
            lines.append("   no repeated items found")
            return []
        for pat, occ in items:
            pages = len(occ)
            tops = [t for _, t, _ in occ]
            raw = occ[0][2]
            shown = raw if len(raw) <= 62 else raw[:59] + "..."
            lines.append(f"   on {pages:>3}/{n_sampled} pages | y≈{min(tops)}-{max(tops)} | \"{shown}\"")
            if pat != normalise(raw) or "#" in pat:
                lines.append(f"        pattern: \"{pat}\" (# stands for digits that change from page to page, such as page numbers)")
            lines.append(f"        └ {note}")
        return items

    block("Header area (top of page)", header, "-> suggest removing")
    block("Footer area (bottom of page)", footer, "-> suggest removing (includes page numbers)")
    # Repeats inside the body: only those with a stable vertical position (typical of navigation bars) are reported, so that ordinary repeated text is left out
    def stable_position(pat, occ):
        tops = [t for _, t, _ in occ]
        return max(tops) - min(tops) <= 30
    block("Body area, repeated in a fixed position (navigation bars or side bars)", body_rep,
          "-> very likely boilerplate, check the context before removing", stable_position)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", type=Path, help="scan only this file")
    ap.add_argument("--sample", type=int, default=30, help="number of pages to sample (default 30)")
    ap.add_argument("--zone", type=float, default=0.10, help="share of the page height counted as top or bottom area (default 0.10)")
    ap.add_argument("--out", type=Path, default=OUT_DIR / "sample_header_footer_scan.txt")
    args = ap.parse_args()

    targets = [args.pdf] if args.pdf else [PDF_DIR / n for n in KNOWN]
    report = ["Header and footer scan",
              f"Pages sampled per report: {args.sample}. Top and bottom areas: {args.zone:.0%} of the page height",
              "Digits are replaced with # so that page numbers can be found. Smaller y values are nearer the top of the page"]
    for p in targets:
        if not p.exists():
            report.append(f"\nNot found: {p}")
            continue
        total, n, hd, ft, bd = scan(p, args.sample, args.zone)
        text = render(p.name, total, n, hd, ft, bd)
        report.append(text)
        print(text)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(report), encoding="utf-8")
    print(f"\n\nReport saved to {args.out}")


if __name__ == "__main__":
    main()
