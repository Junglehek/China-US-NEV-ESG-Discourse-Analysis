#!/usr/bin/env python3
"""Build the side-by-side HTML used to check 28 pages of the 2025 robustness corpus by hand."""
from __future__ import annotations

import html
import json
from pathlib import Path

import pymupdf

from prepare_corpus import DOCUMENTS, LOG_ROOT, OUT_ROOT, ROOT

OUT_DIR = ROOT / "03_QA" / "samples" / "2025_robustness_manual_check"
SECTIONS = [
    ("Group 1: five pages isolated automatically or judged high-risk (checked and restored)", [
        ("byd_2025", 21), ("byd_2025", 30), ("byd_2025", 161),
        ("nio_2025", 33), ("xpeng_2025", 31),
    ]),
    ("Group 2: further pages with the same kinds of problems (checked and restored)", [
        ("byd_2025", 25), ("byd_2025", 94), ("byd_2025", 108),
        ("nio_2025", 32), ("nio_2025", 36), ("nio_2025", 98), ("nio_2025", 160),
        ("xpeng_2025", 15), ("xpeng_2025", 17), ("xpeng_2025", 21),
        ("xpeng_2025", 32), ("xpeng_2025", 33), ("xpeng_2025", 63),
    ]),
    ("Group 3: Tesla pages with a broken text layer, recovered by four-column OCR (checked and restored)", [
        ("tesla_2025", 14), ("tesla_2025", 56), ("tesla_2025", 87),
        ("tesla_2025", 145), ("tesla_2025", 152), ("tesla_2025", 184),
        ("tesla_2025", 193), ("tesla_2025", 194),
        ("tesla_2025", 212), ("tesla_2025", 213),
    ]),
]


def nonempty(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    configs = {d.doc_id: d for d in DOCUMENTS if d.set_name == "robustness"}
    cache = {}
    for doc_id, config in configs.items():
        page_path = OUT_ROOT / "robustness" / "pages" / f"{doc_id}.jsonl"
        records = {r["page"]: r for r in (json.loads(line) for line in page_path.read_text(encoding="utf-8").splitlines() if line)}
        isolated = {p["page"]: p for p in json.loads((LOG_ROOT / f"prepare_{doc_id}.json").read_text(encoding="utf-8")).get("low_quality_pages", [])}
        cache[doc_id] = {"doc": pymupdf.open(config.path), "records": records, "isolated": isolated}

    def card(doc_id: str, page_no: int) -> str:
        data = cache[doc_id]
        image_name = f"{doc_id}_p{page_no}.jpg"
        data["doc"][page_no - 1].get_pixmap(dpi=135, alpha=False).save(
            str(OUT_DIR / image_name), jpg_quality=88)
        draft_dir = ROOT / "01_Source" / "manual_overrides" / "drafts" / doc_id
        draft_no = nonempty(draft_dir / f"p{page_no:03d}_no_tables.txt")
        draft_with = nonempty(draft_dir / f"p{page_no:03d}_with_tables.txt")
        record = data["records"].get(page_no, {})
        approved_file = (ROOT / "01_Source" / "manual_overrides" / "approved" / doc_id
                         / f"p{page_no:03d}_approval.json")
        if approved_file.exists():
            no_lines, with_lines = record.get("no_tables", draft_no), record.get("with_tables", draft_with)
            label = "Checked and included in the corpus"
            badge = "approved-badge"
        elif draft_no or (draft_dir / f"p{page_no:03d}_no_tables.txt").exists():
            no_lines, with_lines = draft_no, draft_with
            label = "Manual draft awaiting approval"
            badge = "draft-badge"
        else:
            no_lines, with_lines = record.get("no_tables", []), record.get("with_tables", [])
            label = "Current automatic or OCR output"
            badge = "ocr-badge" if doc_id == "tesla_2025" else "current-badge"
        if no_lines:
            no_html = "".join(f"<p>{html.escape(line)}</p>" for line in no_lines)
        else:
            no_html = '<p class="empty">no_tables is meant to be empty here: the page has no running text that meets the corpus standard.</p>'
        with_html = "".join(f"<p class='with'>{html.escape(line)}</p>" for line in with_lines)
        if not with_html:
            with_html = '<p class="empty">with_tables is empty or not rebuilt. This version is kept for the record only.</p>'
        reason = data["isolated"].get(page_no)
        warning = ""
        if reason:
            warning = (f"<p class='warning'>Automatic failure signals: bullet-like sentences {reason.get('multi_bullet_sentences', 0)}/"
                       f"{reason.get('sentences', 0)}, lower-case fragments {reason.get('lowercase_start_ratio', 0):.0%}.</p>")
        return f"""
<section class="card"><header><b>{html.escape(doc_id)} · PDF p{page_no}</b>
<span class="{badge}">{label}</span><label><input type="checkbox"> Checked</label></header>
{warning}<div class="grid"><div><img src="{image_name}"></div><div class="text">
<h3>A: no_tables (the only version used in the analysis)</h3>{no_html}
<details><summary>B: with_tables (kept for the record, click to open)</summary>{with_html}</details>
</div></div></section>"""

    section_html = []
    for title, pages in SECTIONS:
        section_html.append(f"<h1>{html.escape(title)}</h1>" + "".join(card(d, p) for d, p in pages))
    total = sum(len(pages) for _, pages in SECTIONS)
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Manual check of the 2025 robustness corpus</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f5f6f8;margin:20px;color:#222}}
.intro,.card{{background:#fff;border:1px solid #ddd;border-radius:10px;padding:16px;margin:18px 0}}
h1{{position:sticky;top:0;background:#355c7d;color:#fff;padding:12px 16px;border-radius:8px;z-index:3}}
header{{display:flex;gap:14px;align-items:center;font-size:18px}}header label{{margin-left:auto;font-size:14px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:12px;align-items:start}}img{{width:100%;border:1px solid #bbb}}
.text{{max-height:760px;overflow:auto;padding:0 12px;line-height:1.5}}p{{padding:6px 8px;margin:4px 0;background:#f8fafc;border-left:3px solid #6685a4}}
.with{{background:#fff5cf;border-left-color:#bd8b00}}details{{margin-top:12px}}summary{{cursor:pointer;background:#f0e1a9;padding:8px}}
.warning{{background:#fff0ed;border-left-color:#d34}}.empty{{background:#edf1f4;color:#52606a}}
.draft-badge,.ocr-badge,.current-badge,.approved-badge{{font-size:13px;padding:3px 8px;border-radius:12px}}.draft-badge{{background:#fff0bb}}.ocr-badge{{background:#dff2ff}}.current-badge{{background:#e4f5e8}}.approved-badge{{background:#ccefd6;color:#175c2d}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body><div class="intro"><h2>2025 robustness corpus: final check of 28 pages</h2>
<p>Compare the PDF on the left with version A on the right. Look for missing words, wrong order, inserted text, and text that should have been kept or removed. Version B is not used in the analysis.</p>
<p><b>Suggested reply:</b> "Everything else passes. Company, page p: original text, should read ...". If there are no problems, reply "All 28 pages pass".</p>
<p>Pages: {total}. This HTML is for local viewing only and is not published.</p></div>{''.join(section_html)}</body></html>"""
    path = OUT_DIR / "index.html"
    path.write_text(document, encoding="utf-8")
    print(f"Review: {path}")
    print(f"Pages: {total}")


if __name__ == "__main__":
    main()
