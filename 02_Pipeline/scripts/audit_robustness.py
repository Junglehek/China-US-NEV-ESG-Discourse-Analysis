#!/usr/bin/env python3
"""Audit of the 2025 robustness corpus, kept separate from the 58 regression checks of the 2024 main corpus."""
from __future__ import annotations

import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path

from audit_outputs import audit_document, check, sha256
from prepare_corpus import (
    DOCUMENTS,
    FOOTNOTE_DIGIT_WHITELIST,
    LOG_ROOT,
    OUT_ROOT,
    ROOT,
    TESLA_2025_OCR_REPLACE_PAGES,
)

REPORT_DIR = ROOT / "03_QA" / "reports"
MANIFEST_PATH = ROOT / "02_Pipeline" / "artifacts_manifest_robustness.json"


def main() -> None:
    results: list[dict] = []
    configs = [d for d in DOCUMENTS if d.set_name == "robustness"]
    docs = [audit_document(config, results) for config in configs]

    unresolved = {
        config.doc_id: [p["page"] for p in json.loads(
            (LOG_ROOT / f"prepare_{config.doc_id}.json").read_text(encoding="utf-8")
        ).get("low_quality_pages", [])]
        for config in configs
    }
    unresolved = {doc: pages for doc, pages in unresolved.items() if pages}
    check("robustness: no unresolved isolated pages", not unresolved,
          f"unresolved={unresolved}", results)

    footnote_residuals = {}
    for path in sorted((OUT_ROOT / "robustness" / "no_tables").glob("*.txt")):
        text = path.read_text(encoding="utf-8")
        tokens = [m.group(0) for m in re.finditer(r"\b([A-Za-z]{4,})(\d{1,2})\b", text)
                  if m.group(1).lower() not in FOOTNOTE_DIGIT_WHITELIST]
        years = [m.group(0) for m in re.finditer(r"\b(20\d{2})(\d{1,2}(?:,\d{1,2})?)\b", text)
                 if 2000 <= int(m.group(1)) <= 2050
                 and all(1 <= int(x) <= 99 for x in m.group(2).split(","))]
        if tokens or years:
            footnote_residuals[path.name] = sorted(set(tokens + years))
    check("robustness: inline footnote markers removed", not footnote_residuals,
          f"residuals={footnote_residuals}", results)

    sys.path.insert(0, str(ROOT / "03_QA" / "scripts"))
    from scan_residual_interleaving import scan as scan_interleaving
    interleaving = scan_interleaving("no_tables", "robustness")
    check("robustness: residual interleaving within cap",
          interleaving["candidate_count"] <= 12 and interleaving["candidate_rate"] <= 0.001,
          f"candidates={interleaving['candidate_count']}/12; "
          f"rate={interleaving['candidate_rate']:.4%}/0.1000%", results)

    quality_path = LOG_ROOT / "quality_metrics_robustness.json"
    quality_rows = json.loads(quality_path.read_text(encoding="utf-8")) if quality_path.exists() else []
    no_quality = {row["doc_id"]: row for row in quality_rows if row["version"] == "no_tables"}
    for config in configs:
        row = no_quality.get(config.doc_id, {})
        thresholds_ok = bool(row) and (
            row["lowercase_start_fragment_pct"] <= 10
            and row["very_long_sentence_pct"] <= 1
            and row["low_alpha_line_pct"] <= 1
            and row["duplicate_long_line_pct"] <= 1
            and row["oov_type_pct"] <= 22
        )
        check(f"{config.doc_id}: formal no_tables quality thresholds", thresholds_ok,
              (f"lower={row.get('lowercase_start_fragment_pct')}; "
               f"very_long={row.get('very_long_sentence_pct')}; "
               f"low_alpha={row.get('low_alpha_line_pct')}; "
               f"duplicate={row.get('duplicate_long_line_pct')}; "
               f"OOV={row.get('oov_type_pct')}") if row else "missing quality row", results)

    tesla_log = json.loads((LOG_ROOT / "prepare_tesla_2025.json").read_text(encoding="utf-8"))
    replaced = tesla_log.get("counters", {}).get("tesla_2025_native_replaced", 0)
    check("tesla_2025: all pre-identified garbled native pages OCR-replaced",
          replaced == len(TESLA_2025_OCR_REPLACE_PAGES),
          f"replaced={replaced}; expected={len(TESLA_2025_OCR_REPLACE_PAGES)}", results)
    tesla = (OUT_ROOT / "robustness" / "no_tables" / "tesla_2025.txt").read_text(encoding="utf-8")
    garbled = [x for x in ("r remaactr", "I ear 225", "ce ta at", "Greenouse", "mpoee") if x in tesla]
    check("tesla_2025: known Type3 garble absent from formal corpus", not garbled,
          f"remaining={garbled}", results)

    period_requirements = {
        "byd_2025": ("1 January 2025", "31 December 2025"),
        "nio_2025": ("January 1, 2025", "December 31, 2025"),
        "xpeng_2025": ("January 1, 2025", "December 31, 2025"),
        "tesla_2025": ("December 31, 2025",),
    }
    for doc_id, phrases in period_requirements.items():
        text = (OUT_ROOT / "robustness" / "no_tables" / f"{doc_id}.txt").read_text(encoding="utf-8")
        missing = [phrase for phrase in phrases if phrase not in text]
        check(f"{doc_id}: 2025 reporting-period evidence retained", not missing,
              f"missing={missing}", results)

    artifact_roots = [
        ROOT / "02_Pipeline" / "scripts",
        ROOT / "02_Pipeline" / "tests",
        ROOT / "03_QA" / "scripts",
        ROOT / "03_QA" / "reports",
        ROOT / "01_Source" / "manual_overrides" / "approved",
        OUT_ROOT / "robustness",
    ]
    artifacts = {}
    excluded = {
        "output_audit_robustness.md", "output_audit_robustness.json",
        "artifacts_manifest_robustness.json", "output_audit.pdf",
    }
    for base in artifact_roots:
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.name not in excluded and "__pycache__" not in path.parts:
                artifacts[str(path.relative_to(ROOT))] = {
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
    versions = {}
    for package in ("pymupdf", "pymupdf4llm", "pdfplumber", "pysbd", "wordninja"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    try:
        versions["tesseract"] = subprocess.check_output(
            ["tesseract", "--version"], text=True,
            stderr=subprocess.STDOUT
        ).splitlines()[0]
    except Exception as exc:
        versions["tesseract"] = f"unavailable: {exc}"

    summary = {
        "set": "robustness",
        "status": "PASS" if all(r["status"] == "PASS" for r in results) else "FAIL",
        "checks": results,
        "documents": docs,
        "versions": versions,
        "artifacts": artifacts,
    }
    MANIFEST_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (REPORT_DIR / "output_audit_robustness.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# ESG_V3 end-to-end audit of the 2025 robustness corpus", "", f"**Overall status: {summary['status']}**", "",
          "| Check | Status | Details |", "|---|---|---|"]
    for row in results:
        md.append(f"| {row['check']} | {row['status']} | {row['detail'].replace('|', '/')} |")
    md += ["", "## Dependency versions", ""]
    for package, version in versions.items():
        md.append(f"- `{package}`: {version}")
    md += ["", f"Full SHA-256 manifest: `{MANIFEST_PATH.relative_to(ROOT)}`"]
    (REPORT_DIR / "output_audit_robustness.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    for row in results:
        print(f"[{row['status']}] {row['check']}: {row['detail']}")
    print(f"\nTOTAL: {summary['status']} "
          f"({sum(r['status']=='PASS' for r in results)}/{len(results)} PASS)")
    print(f"Manifest: {MANIFEST_PATH}")
    raise SystemExit(0 if summary["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
