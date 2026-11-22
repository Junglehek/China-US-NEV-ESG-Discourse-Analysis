#!/usr/bin/env python3
"""生成2025稳健性语料28页并排人工核验HTML。"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pymupdf

from prepare_corpus import DOCUMENTS, LOG_ROOT, OUT_ROOT, ROOT

OUT_DIR = ROOT / "03_QA" / "samples" / "2025稳健性_人工抽检"
SECTIONS = [
    ("第一组：5个自动隔离/高风险页（已核验并插回）", [
        ("byd_2025", 21), ("byd_2025", 30), ("byd_2025", 161),
        ("nio_2025", 33), ("xpeng_2025", 31),
    ]),
    ("第二组：同类问题扩展抽检（已核验并插回）", [
        ("byd_2025", 25), ("byd_2025", 94), ("byd_2025", 108),
        ("nio_2025", 32), ("nio_2025", 36), ("nio_2025", 98), ("nio_2025", 160),
        ("xpeng_2025", 15), ("xpeng_2025", 17), ("xpeng_2025", 21),
        ("xpeng_2025", 32), ("xpeng_2025", 33), ("xpeng_2025", 63),
    ]),
    ("第三组：Tesla Type3坏文本层→四栏OCR代表页（已核验并插回）", [
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
            label = "已核验并进入语料"
            badge = "approved-badge"
        elif draft_no or (draft_dir / f"p{page_no:03d}_no_tables.txt").exists():
            no_lines, with_lines = draft_no, draft_with
            label = "待批准人工草稿"
            badge = "draft-badge"
        else:
            no_lines, with_lines = record.get("no_tables", []), record.get("with_tables", [])
            label = "当前自动/OCR输出"
            badge = "ocr-badge" if doc_id == "tesla_2025" else "current-badge"
        if no_lines:
            no_html = "".join(f"<p>{html.escape(line)}</p>" for line in no_lines)
        else:
            no_html = '<p class="empty">no_tables拟为空：该页没有符合正式语料标准的顺序正文。</p>'
        with_html = "".join(f"<p class='with'>{html.escape(line)}</p>" for line in with_lines)
        if not with_html:
            with_html = '<p class="empty">with_tables为空或不重构；该版本仅留档。</p>'
        reason = data["isolated"].get(page_no)
        warning = ""
        if reason:
            warning = (f"<p class='warning'>自动失败信号：多项目符号 {reason.get('multi_bullet_sentences', 0)}/"
                       f"{reason.get('sentences', 0)}；小写碎片率 {reason.get('lowercase_start_ratio', 0):.0%}。</p>")
        return f"""
<section class="card"><header><b>{html.escape(doc_id)} · PDF p{page_no}</b>
<span class="{badge}">{label}</span><label><input type="checkbox"> 已检查</label></header>
{warning}<div class="grid"><div><img src="{image_name}"></div><div class="text">
<h3>A：no_tables（唯一正式分析版本）</h3>{no_html}
<details><summary>B：with_tables（仅留档，点击展开）</summary>{with_html}</details>
</div></div></section>"""

    section_html = []
    for title, pages in SECTIONS:
        section_html.append(f"<h1>{html.escape(title)}</h1>" + "".join(card(d, p) for d, p in pages))
    total = sum(len(pages) for _, pages in SECTIONS)
    document = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>2025稳健性语料人工抽检</title><style>
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
</style></head><body><div class="intro"><h2>2025 robustness：28页最终抽检</h2>
<p>只需比较左侧PDF与右侧A版，检查漏字、错序、错误插入和该留/该删。B版不用于正式分析。</p>
<p><b>建议反馈格式：</b>“其余通过；公司p页：原文→应改为……”；如全部无问题，回复“28页通过”。</p>
<p>页面数：{total}。HTML只供本地查看，不公开上传。</p></div>{''.join(section_html)}</body></html>"""
    path = OUT_DIR / "index.html"
    path.write_text(document, encoding="utf-8")
    print(f"Review: {path}")
    print(f"Pages: {total}")


if __name__ == "__main__":
    main()
