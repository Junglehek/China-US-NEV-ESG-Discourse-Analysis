#!/usr/bin/env python3
"""生成本地人工核验 HTML：左侧 PDF 原页，右侧提取文本/人工恢复草稿。

顺序严格对应对话工作流：第一轮 Tesla p177 pilot → 第二轮其余6个恢复页 →
第三轮普通抽检。用户只在对话框回复，不在 HTML/文件中填写反馈。
衍生页面图片仅供本地质量核验，不能上传公开仓库。
"""
from __future__ import annotations

import html
import json
from pathlib import Path

import pymupdf

from prepare_corpus import ROOT, DOCUMENTS, OUT_ROOT, LOG_ROOT

OUT_DIR = ROOT / "03_QA" / "samples" / "人工抽检_并排对照"

RECOVERY_SECTIONS = [
    ("第一轮：Tesla p177 pilot（已通过）", [("tesla_2024", 177)]),
    ("第二轮：六页人工恢复（已通过并插回）", [
        ("byd_2024", 24),
        ("nio_2024", 31),
        ("nio_2024", 32),
        ("xpeng_2024", 6),
        ("xpeng_2024", 13),
        ("lucid_2023", 80),
    ]),
]

QA_SECTIONS = [
    ("第三轮：普通抽检已完成并修复", [
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
                right = "<h3>两版本相同</h3>" + no_html
            else:
                with_html = "".join(f"<p class='with'>{html.escape(p)}</p>" for p in with_lines)
                right = ("<h3>A：no_tables（主语料）</h3>" + no_html
                         + "<details><summary><b>B：with_tables（点击展开核验）</b></summary>"
                         + with_html + "</details>")
            badge = '<span class="kept">当前进入语料</span>'
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
                       if no_lines else "<p class='empty'>建议为空：该页没有符合主语料标准的连续定性内容。</p>")
            if no_lines == with_lines and no_lines:
                drafts_html = "<h3>黄色：两版本相同的人工恢复草稿</h3>" + no_html
            elif no_lines or with_lines:
                with_html = ("".join(f"<p class='draft with'>{html.escape(p)}</p>" for p in with_lines)
                             if with_lines else "<p class='empty'>with_tables 草稿为空。</p>")
                drafts_html = ("<h3>黄色 A：no_tables 草稿</h3>" + no_html
                               + "<details open><summary><b>黄色 B：with_tables 草稿（点击收起/展开）</b></summary>"
                               + with_html + "</details>")
            else:
                drafts_html = "<p class='waiting'>草稿尚未预填：当前不用处理。</p>"
            right = (
                "<h3>该页尚未进入正式语料</h3>"
                f"<p>失败信号：多条目拼接 {reason.get('multi_bullet_sentences', 0)}/"
                f"{reason.get('sentences', 0)}；近重复对 {reason.get('near_duplicate_pairs', 0)}；"
                f"小写行首碎片率 {reason.get('lowercase_start_ratio', 0):.0%}。</p>"
                + drafts_html
                + ("<h3>红色：自动错误样例（不要采用）</h3>"
                   + "".join(f"<p class='bad'>{html.escape(p)}</p>" for p in samples) if samples else "")
            )
            badge = '<span class="excluded">待人工恢复</span>'
        return f"""
        <section class="page-card">
          <header><strong>{html.escape(doc_id)} · PDF 第 {page_no} 页</strong>{badge}
            <label><input type="checkbox"> 我已在对话框反馈</label></header>
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
<title>ESG_V3 人工抽检并排对照</title><style>
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
</style></head><body><div class="intro"><h1>ESG_V3 人工核验（按对话顺序）</h1>
<p><b>反馈渠道只有对话框。</b>不要在 HTML、Word 或 TXT 中输入答案；HTML 只用来看左侧 PDF 和右侧草稿。</p>
<ol><li>Tesla p177 pilot 已通过。</li><li>第二轮6页已通过并全部插回语料。</li><li>第三轮24页普通抽检已完成；用户反馈与同类全库修复均已落实。</li></ol>
<p><b>三轮人工核验全部完成。本页现作为本地审计记录保留，不再需要用户操作。</b></p>
<p>检查内容：阅读顺序、漏字、错字、该留/该删。总页面数：{total}。</p></div>{''.join(sections)}</body></html>"""
    index = OUT_DIR / "index.html"
    index.write_text(document, encoding="utf-8")
    print(f"Review: {index}")
    print(f"Pages: {total}")


if __name__ == "__main__":
    main()
