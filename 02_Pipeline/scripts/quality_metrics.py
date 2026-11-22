#!/usr/bin/env python3
"""Quality measures for the whole v3 corpus.

The measures are computed for every page and can be reproduced. Manual checks only confirm that the automatic measures have no systematic blind spots.
Output:
- QA/reports/v3_quality_metrics.csv
- QA/reports/v3_quality_report.md
- QA/logs/quality_metrics.json

Notes
--------
Function-word density separates running text from labels and tables. On its own it does not show that a sentence is grammatical.
Whether sentences are complete is judged from several measures together: final punctuation, fragments at line starts, sentence length, garbled characters and repetition.
The thresholds are quality-control lines set for this project, not general facts about language. Pages below a line are listed for manual review
and are never deleted silently by the script.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import re
import statistics
from pathlib import Path
from typing import Any

from prepare_corpus import ROOT, OUT_ROOT, STOPWORDS, WORD_RE, stopword_density, alpha_ratio, words

# The old (V2) corpus is used only for before-and-after comparison. V3 no longer keeps that folder, so the comparison is skipped if it is missing.
OLD_DIR = ROOT / "old_cleaned"  # early (pre-v3) corpus, not distributed; comparison is skipped if absent
REPORT_DIR = ROOT / "03_QA" / "reports"
LOG_DIR = ROOT / "02_Pipeline" / "logs"
DICT_PATH = Path("/usr/share/dict/words")

OLD_MAP = {
    "byd_2024": "BYD_2024_Sustainability_Report_EN_auto_cleaned.txt",
    "nio_2024": "NIO_2024_ESG_Report_English_auto_cleaned.txt",
    "xpeng_2024": "XPeng_2024_ESG_Report_auto_cleaned.txt",
    "tesla_2024": "Tesla_2024_Impact_Report_Extended_EN_auto_cleaned.txt",
    "rivian_2024": "Rivian_2024_Impact_Report_auto_cleaned.txt",
    "lucid_2023": "Lucid-Group-Inc-2023-Sustainability-Report_auto_cleaned.txt",
}

DOMAIN_WHITELIST = {
    "esg", "gri", "tcfd", "ifrs", "ungc", "esrs", "hkex", "kpi", "dei", "ghg", "co2e",
    "tco2e", "gco2e", "gwh", "mwh", "kwh", "v2g", "ncap", "adas", "ehs", "drc", "asm",
    "decarbonization", "decarbonize", "circularity", "remanufacture", "recyclability",
    "materiality", "gigafactory", "powerwall", "megapack", "photovoltaic", "lifecycle",
    "traceability", "cathode", "smelter", "refiner", "coppermark", "byd", "nio", "xpeng",
    "tesla", "rivian", "lucid", "skysuttle", "skyshuttle", "densus", "disus", "blade",
}

TERMINAL_RE = re.compile(r"[.!?;:][\"')\]]?$")
LOWER_START_RE = re.compile(r"^[\"'(\[]*[a-z]")
WEIRD_TOKEN_RE = re.compile(r"[^A-Za-z0-9'’-]")


def load_dictionary() -> set[str]:
    if not DICT_PATH.exists():
        return set()
    return {line.strip().lower() for line in DICT_PATH.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()}


def known_word(w: str, dictionary: set[str]) -> bool:
    x = w.lower().strip("'-’")
    if not x or x in dictionary or x in DOMAIN_WHITELIST or x in STOPWORDS:
        return True
    if "-" in x and all(p in dictionary or p in DOMAIN_WHITELIST for p in x.split("-") if p):
        return True
    for suffix, repl in (("s", ""), ("es", ""), ("ed", ""), ("ing", ""), ("ies", "y"), ("ied", "y")):
        if x.endswith(suffix) and len(x) > len(suffix) + 2:
            stem = x[:-len(suffix)] + repl
            if stem in dictionary or stem in DOMAIN_WHITELIST:
                return True
    return False


def metrics(path: Path, dictionary: set[str]) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]
    tokens = words(text)
    lower_tokens = [w.lower() for w in tokens]
    types = set(lower_tokens)

    # Running-text lines: completeness is computed after headings and labels are excluded.
    prose = [line for line in lines if len(words(line)) >= 5 and stopword_density(line) >= 0.12]
    complete = [line for line in prose if TERMINAL_RE.search(line)]
    lower_start = [line for line in prose if LOWER_START_RE.search(line)]
    lengths = [len(words(line)) for line in prose]
    very_long = [line for line in prose if len(words(line)) > 80]
    low_alpha = [line for line in lines if len(line) > 10 and alpha_ratio(line) < 0.45]

    # Long lines that repeat exactly. Some repetition is legitimate and still needs a human decision, so nothing is deleted here.
    long_lines = [line for line in lines if len(words(line)) >= 8]
    counts = collections.Counter(long_lines)
    repeated_occurrences = sum(c for c in counts.values() if c >= 3)
    repeated_types = sum(1 for c in counts.values() if c >= 3)

    oov = sorted(w for w in types if len(w) > 1 and not known_word(w, dictionary))
    long_alpha = sorted(w for w in types if len(re.sub(r"[^A-Za-z]", "", w)) >= 22 and not known_word(w, dictionary))
    densities = [stopword_density(line) for line in lines if words(line)]

    return {
        "path": str(path),
        "words": len(tokens),
        "types": len(types),
        "lines": len(lines),
        "prose_lines": len(prose),
        "sentence_complete_pct": round(100 * len(complete) / max(len(prose), 1), 2),
        "lowercase_start_fragment_pct": round(100 * len(lower_start) / max(len(prose), 1), 2),
        "mean_sentence_words": round(statistics.mean(lengths), 2) if lengths else 0,
        "median_sentence_words": round(statistics.median(lengths), 2) if lengths else 0,
        "very_long_sentence_pct": round(100 * len(very_long) / max(len(prose), 1), 2),
        "low_alpha_line_pct": round(100 * len(low_alpha) / max(len(lines), 1), 2),
        "duplicate_long_line_pct": round(100 * repeated_occurrences / max(len(long_lines), 1), 2),
        "duplicate_long_line_types": repeated_types,
        "mean_stopword_density": round(statistics.mean(densities), 4) if densities else 0,
        "low_stopword_line_pct": round(100 * sum(d < 0.1 for d in densities) / max(len(densities), 1), 2),
        "oov_type_pct": round(100 * len(oov) / max(len(types), 1), 2),
        "oov_types": len(oov),
        "oov_examples": oov[:40],
        "suspected_joined_words": long_alpha[:40],
    }


def status(m: dict[str, Any]) -> dict[str, str]:
    """Quality-control lines set for this project. They only trigger a review and never delete text."""
    return {
        "sentence_complete": "PASS" if m["sentence_complete_pct"] >= 85 else "REVIEW",
        "fragment": "PASS" if m["lowercase_start_fragment_pct"] <= 10 else "REVIEW",
        "sentence_length": "PASS" if 12 <= m["mean_sentence_words"] <= 35 else "REVIEW",
        "low_alpha": "PASS" if m["low_alpha_line_pct"] <= 2 else "REVIEW",
        "duplicates": "PASS" if m["duplicate_long_line_pct"] <= 1 else "REVIEW",
    }


def collect(set_name: str) -> list[dict[str, Any]]:
    dictionary = load_dictionary()
    rows = []
    for version in ("old", "with_tables", "no_tables"):
        if version == "old":
            if set_name != "primary":
                continue
            paths = [(doc, OLD_DIR / fname) for doc, fname in OLD_MAP.items()]
        else:
            folder = OUT_ROOT / set_name / version
            paths = [(p.stem, p) for p in sorted(folder.glob("*.txt"))]
        for doc_id, path in paths:
            if not path.exists():
                continue
            m = metrics(path, dictionary)
            m.update({"doc_id": doc_id, "version": version, "set": set_name, "status": status(m)})
            rows.append(m)
    return rows


def write_reports(rows: list[dict[str, Any]], set_name: str) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = REPORT_DIR / f"v3_quality_metrics_{set_name}.csv"
    fields = [
        "doc_id", "version", "words", "types", "lines", "prose_lines",
        "sentence_complete_pct", "lowercase_start_fragment_pct", "mean_sentence_words",
        "median_sentence_words", "very_long_sentence_pct", "low_alpha_line_pct",
        "duplicate_long_line_pct", "mean_stopword_density", "low_stopword_line_pct",
        "oov_type_pct", "oov_types",
    ]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)

    log_path = LOG_DIR / f"quality_metrics_{set_name}.json"
    log_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    # Markdown comparison table
    md = [
        f"# v3 corpus quality report ({set_name})", "",
        "> The measures cover all output text. Function-word density separates running text from labels and does not by itself show that a sentence is grammatical.",
        "> PASS/REVIEW thresholds are quality-control lines set for this project. REVIEW means the page goes to manual checking, not that it is rejected.", "",
        "## Summary", "",
        "| Document | Version | Words | Complete sentences % | Lower-case line-start fragments % | Mean sentence length | Very long sentences % | Low-letter lines % | Repeated long lines % | OOV types % | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for m in rows:
        review = [k for k, v in m["status"].items() if v != "PASS"]
        md.append(
            f"| {m['doc_id']} | {m['version']} | {m['words']:,} | {m['sentence_complete_pct']:.1f} | "
            f"{m['lowercase_start_fragment_pct']:.1f} | {m['mean_sentence_words']:.1f} | "
            f"{m['very_long_sentence_pct']:.1f} | {m['low_alpha_line_pct']:.1f} | "
            f"{m['duplicate_long_line_pct']:.1f} | {m['oov_type_pct']:.1f} | "
            f"{'REVIEW: '+','.join(review) if review else 'PASS'} |"
        )
    md += ["", "## Examples of doubtful words (for manual sorting, nothing is deleted automatically)", ""]
    for m in rows:
        if m["version"] == "no_tables":
            md.append(f"### {m['doc_id']}")
            md.append(f"- OOV examples: {', '.join(m['oov_examples'][:25]) or 'none'}")
            md.append(f"- Possible run-together words: {', '.join(m['suspected_joined_words'][:20]) or 'none'}")
            md.append("")
    report_path = REPORT_DIR / f"v3_quality_report_{set_name}.md"
    report_path.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"CSV: {csv_path}\nReport: {report_path}\nLog: {log_path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("primary", "robustness"), default="primary")
    args = ap.parse_args()
    rows = collect(args.set)
    write_reports(rows, args.set)


if __name__ == "__main__":
    main()
