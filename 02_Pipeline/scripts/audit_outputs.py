#!/usr/bin/env python3
"""End-to-end audit of the final outputs, with a SHA-256 manifest that can be checked later.

Run:
    cd <repository root>
    02_Pipeline/venv/bin/python 02_Pipeline/scripts/audit_outputs.py

The script exits with code 1 if anything fails. Nothing that fails a check may be uploaded to Sketch Engine.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import re
import subprocess
import sys
from pathlib import Path

import pymupdf

from prepare_corpus import ROOT, DOCUMENTS, OUT_ROOT, LOG_ROOT, FOOTNOTE_DIGIT_WHITELIST, words

REPORT_DIR = ROOT / "03_QA" / "reports"
MANIFEST_PATH = ROOT / "02_Pipeline" / "artifacts_manifest.json"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check(name: str, condition: bool, detail: str, results: list[dict]) -> None:
    results.append({"check": name, "status": "PASS" if condition else "FAIL", "detail": detail})


def audit_document(config, results: list[dict]) -> dict:
    pdf = config.path
    with_file = OUT_ROOT / config.set_name / "with_tables" / f"{config.doc_id}.txt"
    no_file = OUT_ROOT / config.set_name / "no_tables" / f"{config.doc_id}.txt"
    page_file = OUT_ROOT / config.set_name / "pages" / f"{config.doc_id}.jsonl"
    log_file = LOG_ROOT / f"prepare_{config.doc_id}.json"
    required = (pdf, with_file, no_file, page_file, log_file)
    check(f"{config.doc_id}: required files", all(p.exists() for p in required),
          ", ".join(str(p.relative_to(ROOT)) for p in required), results)
    if not all(p.exists() for p in required):
        return {}

    log = json.loads(log_file.read_text(encoding="utf-8"))
    pdf_pages = len(pymupdf.open(pdf))
    page_records = [json.loads(line) for line in page_file.read_text(encoding="utf-8").splitlines() if line]
    low_quality = len(log.get("low_quality_pages", []))
    expected = pdf_pages - len(config.toc_pages) - low_quality
    check(f"{config.doc_id}: page accounting", len(page_records) == expected,
          f"PDF {pdf_pages} - TOC {len(config.toc_pages)} - isolated {low_quality} = {expected}; JSONL {len(page_records)}", results)
    check(f"{config.doc_id}: V3 source path", str(log.get("source_path", "")).startswith(str(ROOT)),
          str(log.get("source_path", "")), results)

    with_text = with_file.read_text(encoding="utf-8", errors="ignore")
    no_text = no_file.read_text(encoding="utf-8", errors="ignore")
    with_words, no_words = len(words(with_text)), len(words(no_text))
    check(f"{config.doc_id}: table version ordering", with_words >= no_words,
          f"with_tables={with_words:,}; no_tables={no_words:,}", results)
    check(f"{config.doc_id}: replacement chars", "�" not in no_text,
          f"replacement-character count={no_text.count('�')}", results)
    return {
        "doc_id": config.doc_id,
        "pages": pdf_pages,
        "isolated_pages": [p["page"] for p in log.get("low_quality_pages", [])],
        "with_tables_words": with_words,
        "no_tables_words": no_words,
        "files": {str(p.relative_to(ROOT)): sha256(p) for p in required},
    }


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    results: list[dict] = []
    docs = []
    for config in (d for d in DOCUMENTS if d.set_name == "primary"):
        docs.append(audit_document(config, results))

    # Regression checks for defects that were fixed earlier.
    nio = (OUT_ROOT / "primary" / "no_tables" / "nio_2024.txt").read_text(encoding="utf-8")
    xpeng = (OUT_ROOT / "primary" / "no_tables" / "xpeng_2024.txt").read_text(encoding="utf-8")
    rivian = (OUT_ROOT / "primary" / "no_tables" / "rivian_2024.txt").read_text(encoding="utf-8")
    tesla = (OUT_ROOT / "primary" / "no_tables" / "tesla_2024.txt").read_text(encoding="utf-8")
    check("regression: NIO names", not any(x in nio for x in ("NI OF", "iN IO", "uNI Ock")),
          "forbidden forms: NI OF / iN IO / uNI Ock", results)
    check("regression: XPeng navigation", "Directory About This Report Message from" not in xpeng,
          "repeated navigation absent", results)
    check("regression: Rivian footer", "All rights reserved" not in rivian,
          "copyright footer absent", results)

    forbidden_spacing = {
        "byd_2024": ("AppendixII", "AppendixIII", "decisionmaking", "nextgeneration"),
        "rivian_2024": ("calendaryear", "emissionsfirst", "scaleand"),
        "tesla_2024": ("partyreviewed", "wideranging", "wouldbe", "witha", "Cyber truck", "variousfor"),
        "xpeng_2024": ("andopen", "onlionly"),
    }
    output_texts = {
        doc: (OUT_ROOT / "primary" / "no_tables" / f"{doc}.txt").read_text(encoding="utf-8")
        for doc in forbidden_spacing
    }
    remaining = {doc: [x for x in forms if x in output_texts[doc]] for doc, forms in forbidden_spacing.items()}
    remaining = {doc: forms for doc, forms in remaining.items() if forms}
    check("regression: verified spacing errors", not remaining,
          "remaining=" + repr(remaining), results)

    footnote_residuals = {}
    for version in ("no_tables", "with_tables"):
        for path in sorted((OUT_ROOT / "primary" / version).glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            tokens = [m.group(0) for m in re.finditer(r"\b([A-Za-z]{4,})(\d{1,2})\b", text)
                      if m.group(1).lower() not in FOOTNOTE_DIGIT_WHITELIST]
            years = [m.group(0) for m in re.finditer(r"\b(20\d{2})(\d{1,2}(?:,\d{1,2})?)\b", text)
                     if 2000 <= int(m.group(1)) <= 2050
                     and all(1 <= int(x) <= 99 for x in m.group(2).split(","))]
            if tokens or years:
                footnote_residuals[f"{version}/{path.name}"] = sorted(set(tokens + years))
    check("regression: inline footnote markers removed corpus-wide", not footnote_residuals,
          f"residuals={footnote_residuals}", results)

    # Remaining interleaving candidates: the geometry stage fixes column interleaving, but overlapping layouts can still leave traces.
    # This count does not have to be zero (some lists in the original text really do end with "and"),
    # but it has an upper limit so that later changes cannot quietly bring interleaving back.
    sys.path.insert(0, str(ROOT / "03_QA" / "scripts"))
    from scan_residual_interleaving import scan as scan_interleaving
    interleaving_caps = {"no_tables": 3, "with_tables": 15}
    for version, cap in interleaving_caps.items():
        data = scan_interleaving(version, "primary")
        check(f"regression: residual interleaving within cap ({version})",
              data["candidate_count"] <= cap,
              f"candidates={data['candidate_count']} (cap {cap}), "
              f"rate={data['candidate_rate']:.4%} of {data['total_lines']} lines", results)

    tesla_log = json.loads((LOG_ROOT / "prepare_tesla_2024.json").read_text(encoding="utf-8"))
    tesla_isolated = [p["page"] for p in tesla_log.get("low_quality_pages", [])]
    tesla_overrides = [p["page"] for p in tesla_log.get("manual_override_pages", [])]
    p177_restored = (
        177 in tesla_overrides
        and 177 not in tesla_isolated
        and "Phlogopite mica is used as a thermal barrier" in tesla
        and "variousfor variousvarious" not in tesla
    )
    check("regression: Tesla p177 manual recovery applied", p177_restored,
          f"manual_overrides={tesla_overrides}; isolated={tesla_isolated}", results)

    expected_overrides = {
        "byd_2024": [24, 25, 132],
        "nio_2024": [24, 31, 32, 79],
        "xpeng_2024": [6, 13, 83],
        "tesla_2024": [33, 105, 106, 167, 177, 178],
        "lucid_2023": [80],
        "rivian_2024": [6, 17, 28],
    }
    actual_overrides = {}
    for doc_id in expected_overrides:
        doc_log = json.loads((LOG_ROOT / f"prepare_{doc_id}.json").read_text(encoding="utf-8"))
        actual_overrides[doc_id] = sorted(p["page"] for p in doc_log.get("manual_override_pages", []))
    check("manual recovery: all 20 approved pages applied", actual_overrides == expected_overrides,
          f"actual={actual_overrides}", results)

    def page_record(doc_id: str, page_no: int) -> dict:
        page_path = OUT_ROOT / "primary" / "pages" / f"{doc_id}.jsonl"
        for line in page_path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["page"] == page_no:
                return record
        return {}

    nio32 = page_record("nio_2024", 32)
    nio32_with = nio32.get("with_tables", [])
    first_ethics = nio32_with.index("Business ethics") if "Business ethics" in nio32_with else -1
    nio32_correct = (
        nio32.get("no_tables") == []
        and first_ethics >= 0
        and first_ethics + 1 < len(nio32_with)
        and nio32_with[first_ethics + 1] == "Climate change mitigation"
    )
    check("manual recovery: NIO p32 correction and empty no_tables", nio32_correct,
          f"first Business ethics index={first_ethics}; no_tables lines={len(nio32.get('no_tables', []))}", results)

    lucid80 = page_record("lucid_2023", 80)
    lucid80_correct = (
        "Metrics and Targets" not in lucid80.get("no_tables", [])
        and "Metrics and Targets" in lucid80.get("with_tables", [])
    )
    check("manual recovery: Lucid p80 table heading policy", lucid80_correct,
          "Metrics and Targets excluded from no_tables and retained in with_tables", results)

    byd24 = page_record("byd_2024", 24)
    byd_roles = ["Board of Directors", "Strategy and Sustainable Development Committee",
                 "ESG Management Committee", "ESG and Carbon Emissions Management Team",
                 "Business Units and Business Divisions"]
    byd_lines = byd24.get("no_tables", [])
    byd_order = all(role in byd_lines for role in byd_roles) and [byd_lines.index(r) for r in byd_roles] == sorted(byd_lines.index(r) for r in byd_roles)
    check("manual recovery: BYD p24 five-level order", byd_order,
          f"roles present/order={[(r, byd_lines.index(r) if r in byd_lines else -1) for r in byd_roles]}", results)

    xp6 = page_record("xpeng_2024", 6)
    xp6_policy = (
        "Corresponding Chapters" not in xp6.get("no_tables", [])
        and "Corresponding Chapters" in xp6.get("with_tables", [])
    )
    check("manual recovery: XPeng p6 chapter-label policy", xp6_policy,
          "Corresponding Chapters excluded from no_tables and retained in with_tables", results)

    xp13 = page_record("xpeng_2024", 13)
    xp13_policy = (
        "Government and regulatory authorities" not in xp13.get("no_tables", [])
        and "Government and regulatory authorities" in xp13.get("with_tables", [])
    )
    check("manual recovery: XPeng p13 matrix policy", xp13_policy,
          "stakeholder matrix excluded from no_tables and retained in with_tables", results)

    # Fixes requested item by item during the third round of manual checks, kept here as end-to-end assertions.
    byd25 = page_record("byd_2024", 25)
    byd25_no, byd25_with = byd25.get("no_tables", []), byd25.get("with_tables", [])
    byd25_required = ["Shareholders and investors", "Regular information disclosure",
                      "Protection of customer information and privacy", "Enhancing data security management",
                      "Industry association", "Participating in industry standards development", "Media and the public"]
    check("quality sample: BYD p25 stakeholder table corrections",
          "Summary of BYD Stakeholder Engagement" not in byd25_no and all(x in byd25_with for x in byd25_required),
          "required labels restored; table title excluded from no_tables", results)

    byd132 = page_record("byd_2024", 132)
    byd132_with = byd132.get("with_tables", [])
    check("quality sample: BYD p132 numeric table reconstructed",
          byd132.get("no_tables") == [] and "Employee Turnover Rate31" not in byd132_with
          and "Female Person" not in byd132_with and "4.00 out of 5" in byd132_with,
          f"no_lines={len(byd132.get('no_tables', []))}; with_lines={len(byd132_with)}", results)

    nio24 = page_record("nio_2024", 24).get("no_tables", [])
    check("quality sample: NIO p24 missing headings restored",
          bool(nio24) and nio24[0] == "Efficient Governance"
          and nio24.index("1.1 Efficient Corporate Governance") < nio24.index("1.2 ESG Governance"),
          f"lines={nio24}", results)

    nio79 = page_record("nio_2024", 79).get("no_tables", [])
    nio79_ok = (
        nio79.index("Passive Safety Beyond Five Stars")
        < nio79.index("Adopting an aerodynamic body design and high-strength materials, the ET9 boasts a torsional stiffness of 52,600 N·m/deg.")
        < nio79.index("Tire Burst Mitigation")
        and nio79.index("Pioneering Seven Safety Redundancies")
        < nio79.index("With the introduction of redundancy into the automotive products, including drive, braking, steering, perception, computing, communication, and power distribution systems, the safety, security, and reliability in the event of primary system failure are significantly enhanced through backup systems.")
        < nio79.index("Intelligent Collision Mitigation")
        and nio79.index("NIO ES6 was awarded the five-star safety certification and the 2024 safe model.")
        < nio79.index("During the reporting period, China Merchants Testing Vehicle Technology Research Institute Co., Ltd. released the latest evaluation results of the China Electric Vehicle Fire Safety Index (C-EVFI).")
    )
    check("quality sample: NIO p79 reading order", nio79_ok, "four requested section-order constraints", results)

    xp83 = page_record("xpeng_2024", 83)
    check("quality sample: XPeng p83 table policy and line breaks",
          "Number of suppliers who have signed the Integrity Commitment Letter" not in xp83.get("no_tables", [])
          and "Number of suppliers who have signed the Integrity Commitment Letter" in xp83.get("with_tables", [])
          and sum(x.startswith("Target description:") for x in xp83.get("no_tables", [])) == 3
          and sum(x.startswith("Target description:") for x in xp83.get("with_tables", [])) == 3,
          "signed-letter label policy plus three independent target descriptions", results)

    tesla33 = page_record("tesla_2024", 33)
    check("quality sample: Tesla p33 graph-label policy and OCR corrections",
          "U.S. Electricity Grid Generation Mix" not in tesla33.get("no_tables", [])
          and "U.S. Electricity Grid Generation Mix" in tesla33.get("with_tables", [])
          and any("LCAs" in x for x in tesla33.get("no_tables", []))
          and any("after 17 years" in x for x in tesla33.get("no_tables", [])),
          "labels only in with_tables; LCAs/17 years corrected", results)

    tesla105 = page_record("tesla_2024", 105).get("no_tables", [])
    check("quality sample: Tesla p105 raster text restored",
          tesla105 == ["People & Community", "We are committed to attracting, developing and investing in exceptional talent and supporting their growth, regardless of their background."],
          f"lines={tesla105}", results)

    tesla106 = page_record("tesla_2024", 106).get("no_tables", [])
    check("quality follow-up: Tesla p106 displaced text corrected",
          tesla106 == [
              "Building a future powered by sustainable energy",
              "To sustain our pace of innovation, we must attract, develop and retain a talented workforce.",
              "As of 2024, Tesla has created over 125,000 clean energy-related jobs globally.",
              "Our programs are designed to support students in the development of their skills, bringing the best candidates to Tesla and creating a workplace where all of our employees feel valued and can thrive.",
          ] and "People & Community" not in tesla106,
          f"lines={tesla106}", results)

    tesla167 = page_record("tesla_2024", 167).get("no_tables", [])
    check("quality sample: Tesla p167 risk activities restored",
          bool(tesla167) and tesla167[0] == "Nickel in Indonesia" and len(tesla167) == 11
          and any("2 due diligence trips" in x for x in tesla167)
          and any("RMI Nickel Working Group" in x for x in tesla167),
          f"lines={len(tesla167)}", results)

    tesla178 = page_record("tesla_2024", 178).get("no_tables", [])
    tesla178_headers = ["Supply Chain Tier", "2024 Assessment Conducted", "Tesla Employees Shadowed?", "Findings", "Mitigation Actions"]
    check("quality sample: Tesla p178 qualitative table reconstructed",
          all(x in tesla178 for x in tesla178_headers) and "Yes" not in tesla178
          and any(x.startswith("Current pay structure in mica supply chain") for x in tesla178)
          and any(x.startswith("SLR (formerly RCS Global)") for x in tesla178),
          f"lines={len(tesla178)}", results)

    rivian6 = "\n".join(page_record("rivian_2024", 6).get("no_tables", []))
    check("quality sample: Rivian p6 footnote markers removed",
          not any(x in rivian6 for x in ("20301,2", "electricity3", "20304", "regional grid5")),
          "superscript markers 1-5 removed", results)

    rivian17 = "\n".join(page_record("rivian_2024", 17).get("no_tables", []))
    check("quality sample: Rivian p17 footnote markers removed",
          "203011" not in rivian17 and "facility12" not in rivian17,
          "superscript markers 11/12 removed", results)

    rivian28 = page_record("rivian_2024", 28)
    check("quality sample: Rivian p28 numeric table reconstructed",
          rivian28.get("no_tables") == [] and len(rivian28.get("with_tables", [])) == 99
          and "Global Employees" in rivian28.get("with_tables", [])
          and "Executive Leadership" in rivian28.get("with_tables", []),
          f"no_lines={len(rivian28.get('no_tables', []))}; with_lines={len(rivian28.get('with_tables', []))}", results)

    # The Tesla text that exists only as images must be restored; otherwise the OCR path for mixed PDFs did not run.
    tesla_required = (
        "Task Force on Climate-Related Financial Disclosures",
        "lifecycle assessment (LCA)",
        "waste heat recovery system",
        "skills necessary to be successful in school and at work",
        "responsible sourcing efforts focused on Indonesia",
    )
    missing = [phrase for phrase in tesla_required if phrase not in tesla]
    check("regression: Tesla raster prose recovered", not missing,
          "missing=" + repr(missing), results)

    # Hashes and dependency versions of all scripts, tests, corpus files, logs and reports.
    artifact_roots = [
        ROOT / "02_Pipeline" / "scripts",
        ROOT / "02_Pipeline" / "tests",
        ROOT / "02_Pipeline" / "logs",
        ROOT / "03_QA" / "reports",
        ROOT / "01_Source" / "manual_overrides" / "approved",
        OUT_ROOT / "primary",
    ]
    artifacts = {}
    for base in artifact_roots:
        for path in sorted(base.rglob("*")):
            # The audit report is rewritten by this script after the hashes are collected, so its old version cannot be listed,
            # and run_output_audit.log is still being written by the shell. Both self-referencing files are left out.
            if (path.name in {"output_audit.md", "output_audit.json", "output_audit.pdf", "run_output_audit.log"}
                    or "__pycache__" in path.parts):
                continue
            if path.is_file():
                artifacts[str(path.relative_to(ROOT))] = {
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
    try:
        tesseract_version = subprocess.check_output(
            ["tesseract", "--version"], text=True, stderr=subprocess.STDOUT
        ).splitlines()[0]
    except Exception as exc:
        tesseract_version = f"unavailable: {exc}"
    versions = {}
    for package in ("pymupdf", "pymupdf4llm", "pdfplumber", "pysbd", "wordninja"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not-installed"
    versions["tesseract"] = tesseract_version

    summary = {
        "status": "PASS" if all(r["status"] == "PASS" for r in results) else "FAIL",
        "checks": results,
        "documents": docs,
        "versions": versions,
        "artifacts": artifacts,
    }
    MANIFEST_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    report_json = REPORT_DIR / "output_audit.json"
    report_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    md = ["# ESG_V3 end-to-end output audit", "", f"**Overall status: {summary['status']}**", "",
          "| Check | Status | Details |", "|---|---|---|"]
    for row in results:
        md.append(f"| {row['check']} | {row['status']} | {row['detail'].replace('|', '/')} |")
    md += ["", "## Dependency versions", ""]
    for package, version in versions.items():
        md.append(f"- `{package}`: {version}")
    md += ["", f"Full SHA-256 manifest: `{MANIFEST_PATH.relative_to(ROOT)}`"]
    (REPORT_DIR / "output_audit.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    for row in results:
        print(f"[{row['status']}] {row['check']}: {row['detail']}")
    print(f"\nTOTAL: {summary['status']} ({sum(r['status']=='PASS' for r in results)}/{len(results)} PASS)")
    print(f"Manifest: {MANIFEST_PATH}")
    raise SystemExit(0 if summary["status"] == "PASS" else 1)


if __name__ == "__main__":
    main()
