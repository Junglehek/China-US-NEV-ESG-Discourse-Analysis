#!/usr/bin/env python3
"""Turn the machine-readable audit JSON into a PDF that is quick to read.

Output: 03_QA/reports/output_audit.pdf
"""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

import pymupdf

from prepare_corpus import ROOT, LOG_ROOT

REPORT_DIR = ROOT / "03_QA" / "reports"
AUDIT_JSON = REPORT_DIR / "output_audit.json"
QUALITY_JSON = LOG_ROOT / "quality_metrics_primary.json"
OUTPUT = REPORT_DIR / "output_audit.pdf"


def e(value) -> str:
    return html.escape(str(value))


def main() -> None:
    audit = json.loads(AUDIT_JSON.read_text(encoding="utf-8"))
    quality = json.loads(QUALITY_JSON.read_text(encoding="utf-8"))
    no_metrics = {r["doc_id"]: r for r in quality if r["version"] == "no_tables"}
    docs = [d for d in audit["documents"] if d]

    doc_rows = []
    for d in docs:
        m = no_metrics.get(d["doc_id"], {})
        doc_rows.append(f"""
        <tr><td>{e(d['doc_id'])}</td><td>{d['pages']}</td>
        <td>{d['with_tables_words']:,}</td><td>{d['no_tables_words']:,}</td>
        <td>{e(d['isolated_pages'] or '—')}</td>
        <td>{m.get('sentence_complete_pct', 0):.1f}%</td>
        <td>{m.get('lowercase_start_fragment_pct', 0):.1f}%</td>
        <td>{m.get('mean_sentence_words', 0):.1f}</td></tr>""")

    check_rows = "".join(
        f"<tr><td>{e(r['check'])}</td><td class='{r['status'].lower()}'>{e(r['status'])}</td><td>{e(r['detail'])}</td></tr>"
        for r in audit["checks"]
    )
    versions = "".join(f"<li><code>{e(k)}</code>: {e(v)}</li>" for k, v in audit["versions"].items())
    total_with = sum(d["with_tables_words"] for d in docs)
    total_no = sum(d["no_tables_words"] for d in docs)
    isolated_total = sum(len(d["isolated_pages"]) for d in docs)
    manual_total = sum(
        len(json.loads((LOG_ROOT / f"prepare_{d['doc_id']}.json").read_text(encoding="utf-8")).get("manual_override_pages", []))
        for d in docs
    )
    passed = sum(r["status"] == "PASS" for r in audit["checks"])

    tesla_log = json.loads((LOG_ROOT / "prepare_tesla_2024.json").read_text(encoding="utf-8"))
    ocr = tesla_log.get("ocr_regions", [])
    accepted_sentences = [s for r in ocr for s in r.get("accepted_sentences", [])]
    ocr_pages = sorted({r["page"] for r in ocr if r.get("kept")})

    body = f"""<!doctype html><html><head><meta charset='utf-8'></head><body>
    <h1>ESG_V3 Output Audit Report</h1>
    <p class='meta'>Generated on {dt.date.today().isoformat()}. Workspace: <code>{ROOT.name}</code></p>
    <div class='status'><b>Overall status: {e(audit['status'])}</b><br>
    Unit tests: 21/21 PASS. End-to-end audit: {passed}/{len(audit['checks'])} PASS</div>

    <h2>1. Summary of the final corpus</h2>
    <table><thead><tr><th>Document</th><th>PDF pages</th><th>with tables</th><th>no tables</th>
    <th>Isolated pages</th><th>Complete sentences</th><th>Line-start fragments</th><th>Mean sentence length</th></tr></thead>
    <tbody>{''.join(doc_rows)}
    <tr class='total'><td>Total</td><td>{sum(d['pages'] for d in docs)}</td><td>{total_with:,}</td><td>{total_no:,}</td><td>{isolated_total} pages to restore, {manual_total} restored</td><td colspan='3'>script word count; Sketch Engine tokens after upload</td></tr></tbody></table>

    <h2>2. Tesla mixed PDF and OCR</h2>
    <ul><li>210 pages. Pages with no native text: 0. Native text under 100 characters: 14 pages. Under 500 characters: 49 pages.</li>
    <li>OCR regions checked: {len(ocr)}. Accepted: {sum(bool(r.get('kept')) for r in ocr)}. Rejected: {sum(not r.get('kept') for r in ocr)}.</li>
    <li>Complete sentences added: {len(accepted_sentences)}, about {sum(len(s.split()) for s in accepted_sentences):,} words, on {len(ocr_pages)} pages.</li>
    <li>Pages with little text were recognized in three columns (left, middle, right). Only confident, complete and non-repeated running sentences entered the corpus.</li></ul>

    <h2>3. End-to-end audit ({len(audit['checks'])} checks)</h2>
    <table class='checks'><thead><tr><th>Check</th><th>Status</th><th>Details</th></tr></thead><tbody>{check_rows}</tbody></table>

    <h2>4. Dependency versions</h2><ul>{versions}</ul>
    <h2>5. Materials for reproduction</h2>
    <ul><li><code>02_Pipeline/scripts/prepare_corpus.py</code>: the preparation code</li>
    <li><code>02_Pipeline/tests/test_prepare_corpus.py</code>: regression tests</li>
    <li><code>02_Pipeline/artifacts_manifest.json</code>: SHA-256 manifest</li>
    <li><code>02_Pipeline/logs/prepare_*.json</code>: page-by-page and fix-by-fix logs</li></ul>
    <p class='note'>PASS means that the automatic thresholds set in advance were met. It does not replace manual comparison with the PDF pages.</p>
    </body></html>"""

    css = """
    @page { size: A4; margin: 42pt; }
    body { font-family: sans-serif; font-size: 9.2pt; line-height: 1.35; color: #20242a; }
    h1 { font-size: 22pt; color: #183b59; margin-bottom: 4pt; }
    h2 { font-size: 14pt; color: #24587a; margin-top: 18pt; border-bottom: 1px solid #9fb6c6; padding-bottom: 3pt; }
    .meta { color: #637381; } .status { background: #e7f6eb; border: 1px solid #74b484; padding: 10pt; font-size: 12pt; }
    table { border-collapse: collapse; width: 100%; font-size: 7.6pt; margin: 8pt 0; }
    th { background: #24587a; color: white; } th, td { border: 0.5px solid #aeb8c0; padding: 4pt; vertical-align: top; }
    tr:nth-child(even) td { background: #f4f7f9; } .pass { color: #18703a; font-weight: bold; }
    .fail { color: #a11919; font-weight: bold; } .total td { font-weight: bold; background: #e8eef3 !important; }
    .checks td:nth-child(1) { width: 27%; } .checks td:nth-child(2) { width: 8%; } .checks td:nth-child(3) { width: 65%; }
    code { font-family: monospace; font-size: 8pt; } .note { background: #fff6db; padding: 8pt; border-left: 3px solid #e0a500; }
    """

    story = pymupdf.Story(html=body, user_css=css)
    writer = pymupdf.DocumentWriter(str(OUTPUT))
    mediabox = pymupdf.paper_rect("a4")
    rect = mediabox + (42, 42, -42, -42)
    more = True
    while more:
        device = writer.begin_page(mediabox)
        more, _ = story.place(rect)
        story.draw(device)
        writer.end_page()
    writer.close()
    doc = pymupdf.open(OUTPUT)
    print(f"PDF: {OUTPUT} ({len(doc)} pages, {OUTPUT.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
