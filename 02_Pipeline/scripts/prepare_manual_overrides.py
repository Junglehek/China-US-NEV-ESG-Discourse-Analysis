#!/usr/bin/env python3
"""Create editable drafts for pages whose layout could not be parsed. The final corpus is never touched.

Once a draft has been finished and checked twice, copy it to approved/ and add an approval.json file.
The pipeline reads only approved/ and never reads drafts/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pymupdf
import pymupdf4llm

from prepare_corpus import ROOT, DOCUMENTS, LOG_ROOT

OVERRIDE_ROOT = ROOT / "01_Source" / "manual_overrides"
DRAFT_ROOT = OVERRIDE_ROOT / "drafts"
APPROVED_ROOT = OVERRIDE_ROOT / "approved"


def main() -> None:
    configs = {d.doc_id: d for d in DOCUMENTS if d.set_name == "primary"}
    manifest = []
    for doc_id, config in configs.items():
        log = json.loads((LOG_ROOT / f"prepare_{doc_id}.json").read_text(encoding="utf-8"))
        pages = [p["page"] for p in log.get("low_quality_pages", [])]
        if not pages:
            continue
        doc = pymupdf.open(config.path)
        folder = DRAFT_ROOT / doc_id
        approved = APPROVED_ROOT / doc_id
        folder.mkdir(parents=True, exist_ok=True)
        approved.mkdir(parents=True, exist_ok=True)
        for page_no in pages:
            prefix = f"p{page_no:03d}"
            native = doc[page_no - 1].get_text("text") or ""
            try:
                chunk = pymupdf4llm.to_text(
                    str(config.path), pages=[page_no - 1], header=False, footer=False,
                    page_chunks=True, show_progress=False, use_ocr=False, force_text=True,
                )[0]
                layout = chunk.get("text", "")
            except Exception as exc:
                layout = f"[layout extraction failed: {exc!r}]"
            (folder / f"{prefix}_raw_native.txt").write_text(native, encoding="utf-8")
            (folder / f"{prefix}_raw_layout.txt").write_text(layout, encoding="utf-8")
            # Do not overwrite a draft that someone has already started editing.
            for version in ("with_tables", "no_tables"):
                draft = folder / f"{prefix}_{version}.txt"
                if not draft.exists():
                    draft.write_text("", encoding="utf-8")
            metadata = {
                "doc_id": doc_id,
                "page": page_no,
                "source_pdf": str(config.path.relative_to(ROOT)),
                "status": "draft",
                "rules": {
                    "verbatim": "Copy exact wording; do not paraphrase or silently correct author errors.",
                    "one_unit_per_line": "One complete sentence or one table cell per line.",
                    "no_tables": "Keep headings and complete narrative sentences only; exclude numbers, labels and cell headings.",
                    "with_tables": "Keep all textual cells in row-major reading order; separate cells by line.",
                },
            }
            (folder / f"{prefix}_metadata.json").write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            manifest.append(metadata)
    OVERRIDE_ROOT.mkdir(parents=True, exist_ok=True)
    (OVERRIDE_ROOT / "draft_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Draft pages: {len(manifest)}")
    for item in manifest:
        print(f"  {item['doc_id']} p{item['page']}")
    print(f"Root: {OVERRIDE_ROOT}")


if __name__ == "__main__":
    main()
