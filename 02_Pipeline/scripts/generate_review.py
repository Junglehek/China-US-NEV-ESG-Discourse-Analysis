#!/usr/bin/env python3
"""Build the local HTML for manual checks: the original PDF page on the left, the extracted text or restored draft on the right.

The order follows the review rounds: first the Tesla p177 pilot, then the other six restored pages,
then the ordinary sample. Feedback was given in conversation, not in the HTML or other files.
The page images are for local quality checks only and must not be uploaded to a public repository.
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pymupdf

from prepare_corpus import ROOT, DOCUMENTS, OUT_ROOT, LOG_ROOT

OUT_DIR = ROOT / "03_QA" / "samples" / "manual_check_side_by_side"

RECOVERY_SECTIONS = [
    ("Round 1: Tesla p177 pilot (passed)", [("tesla_2024", 177)]),
    ("Round 2: six pages restored by hand (passed and restored)", [
        ("byd_2024", 24),
        ("nio_2024", 31),
        ("nio_2024", 32),
        ("xpeng_2024", 6),
        ("xpeng_2024", 13),
        ("lucid_2023", 80),
    ]),
]

QA_SECTIONS = [
    ("Round 3: ordinary sample checked and fixed", [
        ("byd_2024", 25), ("byd_2024", 79), ("byd_2024", 132),
        ("nio_2024", 24), ("nio_2024", 79), ("nio_2024", 132),
        ("xpeng_2024", 18), ("xpeng_2024", 51), ("xpeng_2024", 83),
        ("tesla_2024", 5), ("tesla_2024", 10), ("tesla_2024", 28),
        ("tesla_2024", 33), ("tesla_2024", 54), ("tesla_2024", 105),
        ("tesla_2024", 108), ("tesla_2024", 167), ("tesla_2024", 178),
        ("rivian_2024", 6), ("rivian_2024", 17), ("rivian_2024", 28),
        ("lucid_2023", 14), ("lucid_2023", 43), ("lucid_2023", 71),
    ]),
]
ALL_SECTIONS = RECOVERY_SECTIONS + QA_SECTIONS


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    configs = {d.doc_id: d for d in DOCUMENTS if d.set_name == "primary"}
    cache = {}
    for doc_id, config in configs.items():
        cache[doc_id] = {
            "doc": pymupdf.open(config.path),
            "records": {
                r["page"]: r
                for r in (
                    json.loads(line)
                    for line in (OUT_ROOT / "primary" / "pages" / f"{doc_id}.jsonl").read_text(encoding="utf-8").splitlines()
                    if line
                )
            },
            "isolated": {
                p["page"]: p
                for p in json.loads((LOG_ROOT / f"prepare_{doc_id}.json").read_text(encoding="utf-8")).get("low_quality_pages", [])
            },
        }

    def render_card(doc_id: str, page_no: int) -> str:
        data = cache[doc_id]
        image_name = f"{doc_id}_p{page_no}.jpg"
        pix = data["doc"][page_no - 1].get_pixmap(dpi=135, alpha=False)
        pix.save(str(OUT_DIR / image_name), jpg_quality=88)
        if page_no in data["records"]:
            record = data["records"][page_no]
            no_lines = record["no_tables"]
            with_lines = record["with_tables"]
            no_html = "".join(f"<p>{html.escape(p)}</p>" for p in no_lines)
            if no_lines == with_lines:
                right = "<h3>Both versions are identical</h3>" + no_html
            else:
                with_html = "".join(f"<p class='with'>{html.escape(p)}</p>" for p in with_lines)
                right = ("<h3>A: no_tables (main corpus)</h3>" + no_html
                         + "<details><summary><b>B: with_tables (click to open)</b></summary>"
                         + with_html + "</details>")
            badge = '<span class="kept">Currently in the corpus</span>'
        else:
            reason = data["isolated"].get(page_no, {})
            samples = reason.get("samples", [])
            draft_root = ROOT / "01_Source" / "manual_overrides" / "drafts" / doc_id
            no_path = draft_root / f"p{page_no:03d}_no_tables.txt"
            with_path = draft_root / f"p{page_no:03d}_with_tables.txt"
            no_lines = ([line.strip() for line in no_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                        if no_path.exists() else [])
            with_lines = ([line.strip() for line in with_path.read_text(encoding="utf-8").splitlines() if line.strip()]
                          if with_path.exists() else [])
            no_html = ("".join(f"<p class='draft'>{html.escape(p)}</p>" for p in no_lines)
                       if no_lines else "<p class='empty'>Suggested to stay empty: the page has no running text that meets the main-corpus standard.</p>")
            if no_lines == with_lines and no_lines:
                drafts_html = "<h3>Yellow: restored draft, identical in both versions</h3>" + no_html
            elif no_lines or with_lines:
                with_html = ("".join(f"<p class='draft with'>{html.escape(p)}</p>" for p in with_lines)
                             if with_lines else "<p class='empty'>The with_tables draft is empty.</p>")
                drafts_html = ("<h3>Yellow A: no_tables draft</h3>" + no_html
                               + "<details open><summary><b>Yellow B: with_tables draft (click to fold or open)</b></summary>"
                               + with_html + "</details>")
            else:
                drafts_html = "<p class='waiting'>No draft has been filled in yet. Nothing to do for now.</p>"
            right = (
                "<h3>This page is not yet in the final corpus</h3>"
                f"<p>Failure signals: joined list items {reason.get('multi_bullet_sentences', 0)}/"
                f"{reason.get('sentences', 0)}, near-duplicate pairs {reason.get('near_duplicate_pairs', 0)}, "
                f"lower-case line-start fragments {reason.get('lowercase_start_ratio', 0):.0%}.</p>"
                + drafts_html
                + ("<h3>Red: example of the automatic error (do not use)</h3>"
                   + "".join(f"<p class='bad'>{html.escape(p)}</p>" for p in samples) if samples else "")
            )
            badge = '<span class="excluded">Waiting for manual restoration</span>'
        return f"""
        <section class="page-card">
          <header><strong>{html.escape(doc_id)} · PDF page {page_no}</strong>{badge}
            <label><input type="checkbox"> Feedback given</label></header>
          <div class="columns"><div class="source"><img src="{image_name}"></div>
          <div class="extracted">{right}</div></div>
        </section>"""

    sections = []
    for index, (title, pages) in enumerate(ALL_SECTIONS, 1):
        cards = "".join(render_card(doc_id, page_no) for doc_id, page_no in pages)
        phase_class = "done"
        sections.append(f'<section class="phase {phase_class}"><h1>{html.escape(title)}</h1>{cards}</section>')

    total = sum(len(pages) for _, pages in ALL_SECTIONS)
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>ESG_V3 manual checks, side by side</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;margin:20px;background:#f5f6f8;color:#222}}
.intro,.page-card{{background:white;border:1px solid #ddd;border-radius:10px;padding:16px;margin:18px 0}}
.phase>h1{{position:sticky;top:0;background:#183b59;color:white;padding:12px 16px;border-radius:8px;z-index:3}}
.phase.done>h1{{background:#27864a}} .phase.later>h1{{background:#657786}} .page-card header{{display:flex;gap:16px;align-items:center;font-size:18px}}
.page-card header label{{margin-left:auto;font-size:14px}} .columns{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin-top:14px;align-items:start}}
.source img{{width:100%;border:1px solid #bbb}} .extracted{{max-height:760px;overflow:auto;padding:0 12px;line-height:1.55}}
.extracted p{{padding:7px 9px;margin:5px 0;background:#f8fafc;border-left:3px solid #8aa4c3}}
.bad{{background:#fff1f0!important;border-left-color:#d44!important}} .draft{{background:#fff8df!important;border-left-color:#d69c00!important}}
.draft.with,.extracted p.with{{background:#fff1c2!important;border-left-color:#d69c00!important}} .empty{{background:#eef2f5!important;border-left-color:#8998a3!important}} details{{margin:12px 0}} summary{{cursor:pointer;padding:8px;background:#f1e2a6}}
.waiting{{background:#edf1f4!important;color:#53616b}} .kept,.excluded{{padding:3px 8px;border-radius:12px;font-size:13px}}
.kept{{background:#dcf6e5;color:#175c2d}} .excluded{{background:#ffe2df;color:#8b1e16}}
@media(max-width:900px){{.columns{{grid-template-columns:1fr}}}}
</style></head><body><div class="intro"><h1>ESG_V3 manual checks (in review order)</h1>
<p><b>Feedback was given in conversation only.</b> Nothing is typed into the HTML, Word or TXT files. The HTML is only for viewing the PDF on the left and the draft on the right.</p>
<ol><li>The Tesla p177 pilot passed.</li><li>The six pages of round 2 passed and were restored to the corpus.</li><li>The ordinary sample of 24 pages in round 3 is complete, and the fixes it prompted have been applied to the whole corpus.</li></ol>
<p><b>All three rounds of manual checks are complete. This page is kept as a local audit record and needs no further action.</b></p>
<p>Checked: reading order, missing words, wrong words, and text that should have been kept or removed. Pages: {total}.</p></div>{''.join(sections)}</body></html>"""
    index = OUT_DIR / "index.html"
    index.write_text(document, encoding="utf-8")
    print(f"Review: {index}")
    print(f"Pages: {total}")


if __name__ == "__main__":
    main()
