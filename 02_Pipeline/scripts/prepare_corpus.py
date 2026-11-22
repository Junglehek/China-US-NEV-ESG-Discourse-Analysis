#!/usr/bin/env python3
"""The v3 preparation pipeline for the ESG corpus.

Purpose
----
Rebuild the corpus for Sketch Engine from the original PDFs instead of patching the old cleaned files.
The pipeline follows the main principles behind TenTen and jusText:
1. Layout-aware extraction (restores the reading order of pages with several columns)
2. Repair of line breaks inside sentences and of soft hyphens
3. Removal of headers, footers and tables of contents
4. Two versions for tables: with_tables (keeps all cell text) and
   no_tables (drops numbers and labels but keeps complete sentences found in cells)
5. Page-level JSONL output and step-by-step logs, so that every change can be traced and repeated

Notes
----
- The script never deletes or overwrites the old corpus.
- no_tables means exactly this: table data and labels are removed, complete sentences inside tables are kept.
- Tesla 2024 is a mixed PDF with scanned parts. OCR is applied only to image text regions found by the layout engine,
  and native text is never recognized again, so that no new errors are introduced.

Run
----
    cd <repository root>
    python 02_Pipeline/scripts/prepare_corpus.py --set primary
    python 02_Pipeline/scripts/prepare_corpus.py --set robustness
    python 02_Pipeline/scripts/prepare_corpus.py --doc byd_2024
"""
from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

import pymupdf
import pymupdf4llm
import pysbd
import wordninja

# Project root: this file lives at <ROOT>/02_Pipeline/scripts/prepare_corpus.py
ROOT = Path(__file__).resolve().parents[2]
PDF_ROOT = ROOT / "01_Source" / "pdf"
PDF_SUBDIR = {"primary": "2024_main", "robustness": "2025_robustness"}
OUT_ROOT = ROOT / "01_Source" / "v3"
MANUAL_OVERRIDE_ROOT = ROOT / "01_Source" / "manual_overrides" / "approved"
LOG_ROOT = ROOT / "02_Pipeline" / "logs"
TESSERACT = Path(shutil.which("tesseract") or "tesseract")

# The function-word list separates running text from labels and tables. It does not judge whether a sentence is grammatical.
STOPWORDS = {
    "a", "an", "the", "and", "or", "but", "if", "while", "although", "because",
    "as", "at", "by", "for", "from", "in", "into", "of", "on", "onto", "over",
    "through", "to", "under", "with", "without", "about", "against", "among",
    "between", "during", "before", "after", "above", "below", "up", "down", "out",
    "off", "again", "further", "then", "once", "here", "there", "when", "where",
    "why", "how", "all", "any", "both", "each", "few", "more", "most", "other",
    "some", "such", "no", "nor", "not", "only", "own", "same", "so", "than",
    "too", "very", "can", "will", "just", "should", "now", "is", "am", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "having", "do",
    "does", "did", "doing", "would", "could", "might", "must", "shall", "may",
    "i", "me", "my", "we", "our", "ours", "you", "your", "he", "him", "his",
    "she", "her", "it", "its", "they", "them", "their", "this", "that", "these",
    "those", "who", "whom", "which", "what", "whether", "also", "within", "across",
}

# When a hyphen falls at a line break, these free-standing prefixes usually belong to real compounds, so the hyphen is kept.
HYPHEN_PREFIXES = {
    "anti", "bio", "carbon", "climate", "closed", "co", "cross", "e", "end", "energy",
    "five", "forward", "full", "high", "human", "inter", "intra", "life", "location",
    "long", "low", "market", "multi", "net", "non", "on", "over", "post", "pre",
    "re", "self", "short", "supply", "third", "tier", "time", "under", "value", "well",
}

WORD_RE = re.compile(r"[A-Za-z]+(?:[-’'][A-Za-z]+)*")
URL_RE = re.compile(r"https?://\S+|www\.\S+", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PAGE_NUMBER_RE = re.compile(r"^\s*(?:page\s*)?\d{1,4}\s*$", re.I)
MARKDOWN_ARTIFACT_RE = re.compile(r"<!--.*?-->|-----\s*(?:Start|End) of picture text\s*-----", re.I | re.S)

SEGMENTER = pysbd.Segmenter(language="en", clean=False)
SYSTEM_DICTIONARY_PATH = Path("/usr/share/dict/words")
SYSTEM_WORDS = {
    line.strip().lower()
    for line in SYSTEM_DICTIONARY_PATH.read_text(encoding="utf-8", errors="ignore").splitlines()
    if line.strip()
} if SYSTEM_DICTIONARY_PATH.exists() else set()
DOMAIN_TERMS = {
    "adas", "asm", "byd", "cathode", "circularity", "co2e", "coppermark", "decarbonization",
    "decarbonize", "dei", "disus", "drc", "ehs", "esg", "esrs", "gco2e", "ghg", "gigafactory",
    "glm", "gri", "gwh", "hkex", "ifrs", "kpi", "kwh", "lifecycle", "materiality", "megapack",
    "mwh", "ncap", "nio", "photovoltaic", "powerwall", "recyclability", "remanufacture", "rivian",
    "skyshuttle", "smelter", "tco2e", "tcfd", "tesla", "ungc", "v2g", "xpeng",
}
REPAIR_COUNTS: collections.Counter = collections.Counter()
REPAIR_EXAMPLES: dict[str, list[dict[str, str]]] = collections.defaultdict(list)


def record_repair(kind: str, before: str, after: str) -> None:
    if before == after:
        return
    REPAIR_COUNTS[kind] += 1
    if len(REPAIR_EXAMPLES[kind]) < 30:
        REPAIR_EXAMPLES[kind].append({"before": before[:180], "after": after[:180]})


_NINJA_COST = wordninja.DEFAULT_LANGUAGE_MODEL._wordcost  # wordninja's internal vocabulary, used as a second check for real words

_SUFFIXES = (
    ("ally", "al"), ("ily", "y"), ("ies", "y"), ("ied", "y"),
    ("ly", ""), ("ness", ""), ("ments", ""), ("ment", ""), ("ists", ""), ("ist", ""),
    ("ism", ""), ("ers", ""), ("er", ""), ("or", ""), ("ing", ""), ("ed", ""),
    ("es", ""), ("s", ""), ("able", ""), ("ible", ""), ("ful", ""), ("less", ""),
    ("ities", "ity"), ("ity", ""), ("tion", ""), ("sion", ""),
)
_PREFIXES = (
    "counter", "inter", "micro", "multi", "non", "over", "post", "pre", "re",
    "semi", "sub", "super", "trans", "ultra", "under", "anti", "bio", "co",
    "de", "dis", "eco", "en", "fore", "hyper", "in", "im", "mid", "mini",
    "mis", "mono", "out", "poly", "pro", "self", "un", "up", "well",
)


def known_simple(token: str) -> bool:
    """Dictionary check: system dictionary + wordninja vocabulary + domain terms + affix stripping.

    The aim is not spelling correction but telling real words apart from pieces that were joined or split by mistake.
    """
    x = token.lower().strip("'-’")
    if not x:
        return False
    if x in SYSTEM_WORDS or x in STOPWORDS or x in DOMAIN_TERMS or x in _NINJA_COST:
        return True
    for suffix, repl in _SUFFIXES:
        if x.endswith(suffix) and len(x) > len(suffix) + 2:
            stem = x[:-len(suffix)] + repl
            if stem in SYSTEM_WORDS or stem in DOMAIN_TERMS or stem in _NINJA_COST:
                return True
    for prefix in _PREFIXES:
        if x.startswith(prefix) and len(x) > len(prefix) + 2:
            rest = x[len(prefix):]
            if rest in SYSTEM_WORDS or rest in DOMAIN_TERMS or rest in _NINJA_COST:
                return True
            # prefix + stem with a suffix
            for suffix, repl in _SUFFIXES:
                if rest.endswith(suffix) and len(rest) > len(suffix) + 2:
                    stem = rest[:-len(suffix)] + repl
                    if stem in SYSTEM_WORDS or stem in DOMAIN_TERMS or stem in _NINJA_COST:
                        return True
    return False


# Proper names and model codes must not take part in merging split words, otherwise "NIO F1" becomes "NI OF1"
# (because "of" is a real word and pulls the following letters into it).
PROTECTED_TOKEN_RE = re.compile(r"^(?:[A-Z]{2,}|[A-Z]\d+|[A-Z]{1,3}\d*)$")
FUNCTION_WORD_MAXLEN = 3


def is_protected_token(piece: str) -> bool:
    """All-caps abbreviations (NIO/BYD/ESG) and letter-digit model codes (F1/E1/X9) are protected."""
    return bool(PROTECTED_TOKEN_RE.match(piece)) and not piece.islower()


def suspicious_fragment(piece: str) -> bool:
    q = piece.lower()
    if len(q) == 1:
        # "e" needs protection: "CO2e in" is tokenized as ... 'e' + 'in'. If "e" were treated as a fragment,
        # it would be merged into "CO2ein", because "ein" is also a word. The cost is that the rare "e mail" is not merged.
        return q not in {"a", "i", "e"}
    if len(q) == 2:
        return q not in SYSTEM_WORDS
    # Names of people and places missing from the dictionary are not fragments. Rawlinson is not in the dictionary,
    # so "Peter Rawlinson" was once taken for a run of fragments, joined into PeterRawlinson and then split by wordninja
    # into "Peter Raw lins on" (seen on Lucid p4). Real PDF fragments are short pieces or lower-case continuations,
    # whereas a capitalized piece of six or more letters is typically a proper name, so it is protected.
    if len(piece) >= 6 and piece[:1].isupper() and piece[1:].islower():
        return False
    return not known_simple(piece)


def merge_spaced_fragments(text: str) -> str:
    """Repair words that the PDF character layer split into several pieces separated by spaces.

    This works in two stages instead of greedy matching piece by piece (greedy matching would turn 'h a s p...' into "hasp"):
    1. Find runs of fragments: consecutive letter units separated by single spaces, with at least two doubtful fragments,
       or at least one doubtful fragment if the whole run joined together is a real word.
    2. Join the whole run. If the result is a real word, keep it. Otherwise let wordninja find the best split,
       and accept the split only if every piece is a real word. If not, leave the text unchanged.
    The approach is deliberately cautious: ordinary pairs such as 'in to' and 'a lot of' are never touched.
    """
    units = re.findall(r"[A-Za-z]+|[^A-Za-z]+", text)
    out, i, n = [], 0, len(units)
    while i < n:
        if not units[i].isalpha():
            out.append(units[i]); i += 1; continue
        # A contraction suffix after an apostrophe must not be merged with the next word: "That's the" was once turned into "That'sthe".
        if i > 0 and units[i - 1] in {"'", "’"} and units[i].lower() in {"s", "t", "re", "ve", "ll", "d", "m"}:
            out.append(units[i]); i += 1; continue
        # Protected abbreviations and model codes (NIO/BYD/F1) are output on their own and kept out of fragment runs,
        # otherwise they would block the repair of real fragments later in the same line (BYD h a s would stay BYD h a s).
        if is_protected_token(units[i]):
            out.append(units[i]); i += 1; continue
        # Collect a run: letter units separated by single spaces only, stopping at a protected token
        j, pieces = i, []
        while j < n and units[j].isalpha() and not is_protected_token(units[j]):
            pieces.append(units[j])
            if (j + 2 <= n - 1 and units[j + 1] == " " and units[j + 2].isalpha()
                    and not is_protected_token(units[j + 2])):
                j += 2
            else:
                break
        susp = sum(suspicious_fragment(p) for p in pieces)
        joined = "".join(pieces)
        replaced = None
        consumed = j  # by default the whole run is consumed
        # Protected tokens were already removed while collecting the run. This is only a safety net.
        has_protected = any(is_protected_token(p) for p in pieces)
        # If the result is a short function word, all original pieces must be lower case, to avoid merges such as O + F1 -> OF1.
        merge_to_function_word = (
            len(joined) <= FUNCTION_WORD_MAXLEN and joined.lower() in STOPWORDS
            and not all(p.islower() for p in pieces)
        )
        if len(pieces) >= 2 and susp >= 1 and not has_protected and not merge_to_function_word:
            if known_simple(joined):
                replaced = joined.upper() if all(p.isupper() for p in pieces) else joined
            else:
                parts = merge_domain_initialisms(wordninja.split(joined))
                if len(parts) >= 2 and all(known_simple(p) and len(p) >= 2 for p in parts):
                    replaced = " ".join(parts)
            if replaced:
                record_repair("merge_spaced_fragments", " ".join(pieces), replaced)
            else:
                # If the whole run fails, fall back to the longest known prefix that contains a doubtful fragment
                # (for example 'w ho pursues' -> the prefix 'who', with 'pursues' left as it is).
                for wsize in range(len(pieces) - 1, 1, -1):
                    sub = pieces[:wsize]
                    if not any(suspicious_fragment(p) for p in sub):
                        break
                    prefix = "".join(sub)
                    if len(prefix) >= 3 and known_simple(prefix):
                        replaced = prefix.upper() if all(p.isupper() for p in sub) else prefix
                        record_repair("merge_spaced_fragments_prefix", " ".join(sub), replaced)
                        consumed = i + 2 * wsize - 2  # consume up to the end of the prefix
                        break
        if replaced is not None:
            out.append(replaced)
            if consumed != j:
                out.extend(units[consumed + 1:j + 1])
        else:
            out.extend(units[i:j + 1])
        i = j + 1
    return "".join(out)


def merge_domain_initialisms(parts: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(parts):
        merged = None
        for n in (4, 3, 2):
            if i + n <= len(parts):
                candidate = "".join(parts[i:i+n])
                if candidate.lower() in DOMAIN_TERMS:
                    merged = candidate.upper() if candidate.lower() in {"byd", "nio", "esg", "gri", "ghg"} else candidate
                    i += n; break
        if merged is not None:
            out.append(merged)
        else:
            out.append(parts[i]); i += 1
    return out


def split_concatenated_words(text: str) -> str:
    """Split only long strings that are not in the dictionary, and accept a split only if every piece is a valid word or domain term."""
    def repl(match: re.Match) -> str:
        token = match.group(0)
        # Strings shorter than 15 characters are left alone. Two-part compounds (whistleblower, workforce) are often
        # missing from dictionaries, and splitting short strings does more harm than good. They are left for manual review.
        if known_simple(token) or len(token) < 15:
            return token
        internal_caps = sum(c.isupper() for c in token[1:])
        if internal_caps and len(token) < 25:
            return token
        parts = merge_domain_initialisms(wordninja.split(token))
        # Long strings (20 characters or more) may be split into two pieces (for example a doubled communicationcommunication).
        # Shorter strings must split into at least three pieces that are all real words, so that compounds are not broken up.
        enough_parts = len(parts) >= 3 or (len(parts) == 2 and len(token) >= 20)
        if not enough_parts or not all(known_simple(p) and len(p) >= 2 for p in parts):
            return token
        after = " ".join(parts)
        record_repair("split_concatenated_words", token, after)
        return after

    # The two sides of a hyphen are handled separately, and real hyphens are kept.
    return re.sub(r"[A-Za-z]{10,}", repl, text)


def repair_company_initialisms(text: str) -> str:
    """Rejoin company abbreviations that the PDF character layer split apart.

    Only the letter sequences of known company names are handled, so that the general merge rules cannot misfire ("NIO F1" was once merged into "NI OF1").
    """
    patterns = {
        "NIO": r"N\s*I\s*O", "BYD": r"B\s*Y\s*D",
        "XPENG": r"X\s*P\s*E\s*N\s*G", "ESG": r"E\s*S\s*G",
    }
    for target, pat in patterns.items():
        # Both sides must be non-letters and must not sit next to an isolated single-letter fragment.
        # Counterexample: the PDF split "unlocked" into 'u N I O ck ed'. Without checking the context on both sides,
        # 'N I O' would be merged into NIO, giving 'u NIO ck ed'.
        full = re.compile(rf"(?<![A-Za-z])(?<!\b[A-Za-z]\s)({pat})(?![A-Za-z])(?!\s[A-Za-z]\b)")

        def sub(m: re.Match, target=target) -> str:
            if m.group(0) != target:
                record_repair("repair_company_initialisms", m.group(0), target)
            return target
        text = full.sub(sub, text)
    return text


VERIFIED_SPACING_CORRECTIONS = (
    (r"\bAppendixII\b", "Appendix II"),
    (r"\bAppendixIII\b", "Appendix III"),
    (r"\bdecisionmaking\b", "decision making"),
    (r"\bnextgeneration\b", "next generation"),
    (r"\bcalendaryear\b", "calendar year"),
    (r"\bemissionsfirst\b", "emissions-first"),
    (r"\bscaleand\b", "scale and"),
    (r"\bpartyreviewed\b", "party-reviewed"),
    (r"\bwideranging\b", "wide-ranging"),
    (r"\bwouldbe\b", "would-be"),
    (r"\bandopen\b", "and open"),
    (r"\bonlionly\s+ne\b", "online"),
    (r"\bwitha\b", "with a"),
    (r"\bCyber\s+trucks\b", "Cybertrucks"),
    (r"\bCyber\s+truck\b", "Cybertruck"),
    (r"\bStar\s+link\b", "Starlink"),
    (r"\bprobability of roll over\b", "probability of rollover"),
    (r"\bMoro\s+wali\b", "Morowali"),
    (r"\bdiagnosis-analysisimprovement\b", "diagnosis-analysis-improvement"),
    (r"\bscenarios-smaller-capacity\b", "scenarios - smaller-capacity"),
    (r"\bvehiclecharger-station-cloud-grid\b", "vehicle-charger-station-cloud-grid"),
)


def apply_verified_spacing_corrections(text: str) -> str:
    """Fix only spacing errors made during extraction and confirmed against the PDF, never the authors' own spelling."""
    for regex, replacement in VERIFIED_SPACING_CORRECTIONS:
        changed, count = re.subn(regex, replacement, text, flags=re.I)
        if count:
            record_repair("verified_spacing_corrections", text[:150], changed[:150])
            text = changed
    return text


FOOTNOTE_DIGIT_WHITELIST = {
    "drive", "ecoinvent", "kgco", "nadarch", "sepa", "model", "scope", "tier",
    "euro", "industry", "platform", "level", "type", "class", "stage", "phase",
}


def strip_inline_footnote_markers(text: str) -> str:
    """Remove superscript footnote numbers stuck to words or years, while protecting product names, versions and units.

    PDF superscripts become ordinary digits after NFKC normalization, for example `charging⁷` -> `charging7` and
    `2030¹,²` -> `20301,2`. Simply deleting every digit after a word would damage Drive48,
    Scope3, NADArch2.0 and similar names, so the rule handles only forms verified in this corpus and logs the text before and after.
    """
    before = text

    def word_marker(match: re.Match) -> str:
        word, digits = match.group(1), match.group(2)
        if word.lower() in FOOTNOTE_DIGIT_WHITELIST:
            return match.group(0)
        return word

    text = re.sub(r"\b([A-Za-z]{4,})(\d{1,2})\b", word_marker, text)

    def year_marker(match: re.Match) -> str:
        year, markers = match.group(1), match.group(2)
        value = int(year)
        if 2000 <= value <= 2050 and all(1 <= int(x) <= 99 for x in markers.split(",")):
            return year
        return match.group(0)

    text = re.sub(r"\b(20\d{2})(\d{1,2}(?:,\d{1,2})?)\b", year_marker, text)
    if text != before:
        record_repair("strip_inline_footnote_markers", before[:150], text[:150])
    return text


def repair_internal_spacing(text: str) -> str:
    before = text
    # On NIO p79 the PDF glyphs consistently turn "unlocked" into `uNI Ock ed` (l read as I).
    # This exact form is unambiguous in context and is fixed before the company-abbreviation rule, so that it is not mistaken for NIO.
    text = re.sub(r"\bu\s*N\s*I\s*Ock\s*ed\b", "unlocked", text, flags=re.I)
    text = repair_company_initialisms(text)
    # Exact rules confirmed against the PDF must run first. Otherwise the general wordninja step would already have turned
    # partyreviewed into "party reviewed" and onlionly into "on lion ly", and the original patterns would no longer match.
    text = apply_verified_spacing_corrections(text)
    text = re.sub(r"(?<=[,;:])(?=[A-Za-z])", " ", text)
    # Missing space after a possessive or contraction: children'swell-rounded, can'tdo -> children's well-rounded, can't do
    text = re.sub(r"([A-Za-z]+['’](?:s|t|re|ve|ll|d|m))(?=[a-z])", r"\1 ", text, flags=re.I)
    text = merge_spaced_fragments(text)
    text = split_concatenated_words(text)
    # wordninja may split BYD into BY + D, so domain abbreviations are joined again at the spacing stage.
    text = re.sub(r"\bB\s+Y\s+D\b", "BYD", text, flags=re.I)
    text = re.sub(r"\bE\s+S\s+G\b", "ESG", text, flags=re.I)
    text = apply_verified_spacing_corrections(text)
    text = strip_inline_footnote_markers(text)
    if text != before:
        record_repair("repair_internal_spacing", before, text)
    return text


@dataclass(frozen=True)
class DocumentConfig:
    doc_id: str
    filename: str
    set_name: str
    group: str
    report_year: int
    toc_pages: tuple[int, ...]
    ocr_mode: str = "low_text_pictures"  # none / low_text_pictures / all_picture_regions

    @property
    def path(self) -> Path:
        return PDF_ROOT / PDF_SUBDIR[self.set_name] / self.filename


TESLA_2025_OCR_REPLACE_PAGES = frozenset({
    12, 14, 21, 22, 24, 32, 34, 40, 43, 44, 47, 48, 52, 54, 55, 56, 57,
    58, 59, 60, 61, 65, 69, 70, 74, 77, 81, 86, 87, 92, 93, 94, 95, 96,
    102, 111, 113, 114, 115, 121, 122, 123, 124, 125, 127, 129, 133, 134,
    135, 141, 142, 144, 145, 146, 147, 148, 149, 150, 151, 152, 157, 161,
    163, 166, 167, 169, 171, 176, 179, 182, 183, 184, 186, 187, 190, 191,
    193, 194, 198, 199, 201, 205, 207, 208, 209, 210, 211, 212, 213, 214,
    215,
})


DOCUMENTS = [
    DocumentConfig("byd_2024", "BYD_2024_Sustainability_Report_EN.pdf", "primary", "China", 2024, (2,)),
    DocumentConfig("nio_2024", "NIO_2024_ESG_Report_English.pdf", "primary", "China", 2024, (2,)),
    DocumentConfig("xpeng_2024", "XPeng_2024_ESG_Report.pdf", "primary", "China", 2024, (2,)),
    DocumentConfig("tesla_2024", "2024-Tesla-Impact-Report-Extended-EN.pdf", "primary", "US", 2024, (2,), "all_picture_regions"),
    DocumentConfig("rivian_2024", "Rivian_2024_Impact_Report.pdf", "primary", "US", 2024, (4,)),
    DocumentConfig("lucid_2023", "Lucid-Group-Inc-2023-Sustainability-Report.pdf", "primary", "US", 2023, (5,)),
    DocumentConfig("byd_2025", "2025_BYD_Sustainability_Report.pdf", "robustness", "China", 2025, (2,)),
    DocumentConfig("nio_2025", "NIO-2025-ESG-Report-English.pdf", "robustness", "China", 2025, (2,)),
    DocumentConfig("xpeng_2025", "2025_Xpeng_socialAndGovernanceReport.pdf", "robustness", "China", 2025, (2,)),
    DocumentConfig("tesla_2025", "2025-extended-version-tesla-impact-report.pdf", "robustness", "US", 2025, (2,), "low_text_pictures"),
]


def normalize_unicode(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return (text.replace("\u00ad", "")
                .replace("ﬁ", "fi").replace("ﬂ", "fl")
                .replace("“", '"').replace("”", '"')
                .replace("‘", "'").replace("’", "'")
                .replace("–", "-").replace("—", "-")
                .replace("…", "..."))


def words(text: str) -> list[str]:
    return WORD_RE.findall(text)


def stopword_density(text: str) -> float:
    ws = [w.lower() for w in words(text)]
    if not ws:
        return 0.0
    return sum(w in STOPWORDS for w in ws) / len(ws)


def alpha_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return 0.0
    return sum(c.isalpha() for c in chars) / len(chars)


def pattern(text: str) -> str:
    """Pattern for comparing headers and footers across pages: lower case, digits normalized, punctuation and spaces collapsed."""
    text = normalize_unicode(text).lower()
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"[^a-z#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dehyphenate_join(lines: Iterable[str]) -> str:
    """Turn visual line breaks inside one layout block back into continuous text."""
    cleaned = [normalize_unicode(x).strip() for x in lines if normalize_unicode(x).strip()]
    if not cleaned:
        return ""
    out = cleaned[0]
    for nxt in cleaned[1:]:
        if out.endswith("-") and nxt and nxt[0].islower():
            m = re.search(r"([A-Za-z]+)-$", out)
            left = m.group(1).lower() if m else ""
            if left in HYPHEN_PREFIXES:
                out += nxt                    # low- + carbon -> low-carbon
            else:
                out = out[:-1] + nxt          # sustain- + ability -> sustainability
        else:
            out += " " + nxt
    return re.sub(r"\s+", " ", out).strip()


MIN_COLUMN_LINES = 3
GUTTER_STRADDLE_MIN = 3


def sort_textlines_by_reading_order(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Restore the visual reading order inside a box by (y, x).

    The text lines returned by the layout engine are not always sorted by vertical position. In one box on Tesla p74,
    `Leveraging autonomy to` (y=220) came after `accelerate EV adoption` (y=267),
    which produced the reversed heading `accelerate EV adoption Leveraging autonomy to`.
    On p148, `Modern Slavery Statement.` (y=340) likewise came before the line that introduces it,
    `...see our` (y=317). y is rounded to 1 pt so that one visual line is not split by floating-point differences.
    """
    return sorted(lines, key=lambda l: (round(float((l.get("bbox") or [0, 0])[1])),
                                        float((l.get("bbox") or [0, 0])[0])))


def detect_column_groups(box: dict[str, Any], page_width: float) -> list[list[dict[str, Any]]]:
    """Detect several columns inside one layout block and return the (rebuilt) text lines grouped by column.

    The layout engine sometimes merges a whole multi-column area into one box (seen on XPeng p6 and BYD p27).
    Worse, it may merge the left and right columns into the **same text line** (seen in the CEO letter on Lucid p4:
    every line starts at the left column's x = 604, and the right column's text follows as later spans of that line).
    Grouping by line start can then never separate the columns, and the output ends up as
    `...ensuring a robust governance The next pages outline our first steps in our
    structure to support our efforts.`, interleaving across columns that creates false word neighbours.

    Columns are therefore assigned by **span**, not by text line:
    1. Project all spans in the box onto the x axis and build an occupancy map at 1 pt resolution.
    2. Treat bands with zero occupancy as candidate gutters.
    3. The key test is not the width of a gutter but how many lines **cross** it (with spans on both sides).
       An ordinary single-column paragraph with a ragged right edge only leaves blank space on the right and never repeatedly
       shows another block of text further right. Only real columns are crossed by many lines, so at least three crossing lines are required.
    4. Inside each column, re-sort by (y, x) and rebuild the text lines, so that each column becomes its own text.

    This is why the width threshold can be relaxed to 1.2% of the page width (the gutter on Lucid p4 is only 26 pt / 1920 = 1.35%,
    which the old 4% threshold missed). The crossing test is what prevents false splits.
    """
    lines = [l for l in (box.get("textlines") or []) if (l.get("spans") or [])]
    if len(lines) < 8:
        return [sort_textlines_by_reading_order(lines)]
    x0_box, x1_box = float(box.get("x0", 0)), float(box.get("x1", 0))
    width = x1_box - x0_box
    if width < page_width * 0.5:
        return [sort_textlines_by_reading_order(lines)]

    span_boxes = [(float(s["bbox"][0]), float(s["bbox"][2]))
                  for l in lines for s in l["spans"] if s.get("bbox")]
    if not span_boxes:
        return [sort_textlines_by_reading_order(lines)]
    left = min(x for x, _ in span_boxes)
    right = max(x for _, x in span_boxes)
    n = int(right - left) + 1
    if n < 2:
        return [sort_textlines_by_reading_order(lines)]
    occupied = bytearray(n)
    for sx0, sx1 in span_boxes:
        lo = max(0, min(n - 1, int(sx0 - left)))
        hi = max(0, min(n - 1, int(sx1 - left)))
        for b in range(lo, hi + 1):
            occupied[b] = 1

    gutter_min = max(page_width * 0.012, 10.0)
    gaps, run_start = [], None
    for i in range(n):
        if not occupied[i]:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and run_start > 0 and i - run_start >= gutter_min:
                gaps.append(left + (run_start + i) / 2)
            run_start = None
    if not gaps:
        return [sort_textlines_by_reading_order(lines)]

    def straddling(mid: float) -> int:
        count = 0
        for line in lines:
            spans = line["spans"]
            if (any(float(s["bbox"][2]) <= mid for s in spans)
                    and any(float(s["bbox"][0]) >= mid for s in spans)):
                count += 1
        return count

    boundaries = [mid for mid in gaps if straddling(mid) >= GUTTER_STRADDLE_MIN]
    if not boundaries:
        return [sort_textlines_by_reading_order(lines)]

    edges = [float("-inf")] + boundaries + [float("inf")]
    groups: list[list[dict[str, Any]]] = [[] for _ in range(len(edges) - 1)]
    for line in lines:
        line_bbox = line.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        for k in range(len(edges) - 1):
            selected = [s for s in line["spans"]
                        if edges[k] <= float(s["bbox"][0]) < edges[k + 1]]
            if not selected:
                continue
            groups[k].append({
                "bbox": [min(float(s["bbox"][0]) for s in selected), float(line_bbox[1]),
                         max(float(s["bbox"][2]) for s in selected), float(line_bbox[3])],
                "spans": selected,
            })
    valid = [g for g in groups if len(g) >= MIN_COLUMN_LINES]
    if len(valid) < 2:
        return [sort_textlines_by_reading_order(lines)]
    REPAIR_COUNTS["box_split_into_columns"] += 1
    if len(REPAIR_EXAMPLES["box_split_into_columns"]) < 30:
        REPAIR_EXAMPLES["box_split_into_columns"].append({
            "before": f"single box, {len(lines)} lines, width {width / page_width:.0%} of page",
            "after": f"{len(valid)} columns: {[len(g) for g in valid]} lines",
        })
    return [sort_textlines_by_reading_order(g) for g in valid]


def box_lines(box: dict[str, Any]) -> list[str]:
    """Rebuild line text from span coordinates.

    Neighbouring spans in a PDF do not always carry a trailing space. The old code joined them directly and produced
    `after-salesservicenetwork...`. Spaces are now added from the horizontal gaps, and each addition is logged.
    """
    return lines_from_textlines(box.get("textlines") or [])


def span_reading_order(span: dict[str, Any]) -> tuple[int, int, float]:
    """Reading-order key of a span in the original PDF text layer.

    The layout engine can merge spans from different source lines into one text line. Sorting by x alone
    then interleaves them. On NIO p61, one text line contained

        (x= 48, block=1, line= 9) 'Assessment and'
        (x= 48, block=1, line=10) 'reporting period. This further unifies...'
        (x=106, block=1, line= 9) 'Management Process '
        (x=181, block=1, line= 9) 'during the '

    Sorting by x inserted line 10 into the middle of line 9, giving
    `the Energy Assessment and reporting period.` /
    `This further unifies the factories' energy Management Process during the
    management processes...`, so the two sentences were swapped halfway through. Sorting by (block, line, x) restores the order.
    `Assessment and Management Process during the reporting period. This further
    unifies the factories' energy ...`.
    """
    bbox = span.get("bbox") or [0.0]
    return (int(span.get("block") or 0), int(span.get("line") or 0), float(bbox[0]))


def lines_from_textlines(textlines: list[dict[str, Any]]) -> list[str]:
    lines = []
    for line in textlines:
        spans = sorted((line.get("spans") or []), key=span_reading_order)
        pieces, prev_x1, prev_size = [], None, None
        for span in spans:
            text = span.get("text", "")
            bbox = span.get("bbox") or [0, 0, 0, 0]
            size = float(span.get("size") or 8)
            if pieces and text:
                gap = float(bbox[0]) - float(prev_x1 or bbox[0])
                threshold = max(0.6, min(size, prev_size or size) * 0.14)
                if not pieces[-1].endswith((" ", "\t", "\n")) and not text.startswith((" ", "\t", "\n")) and gap >= threshold:
                    pieces.append(" ")
                    REPAIR_COUNTS["span_spaces_inserted"] += 1
            pieces.append(text)
            prev_x1, prev_size = float(bbox[2]), size
        txt = repair_internal_spacing("".join(pieces))
        if txt.strip():
            lines.append(txt)
    return lines


# Clear signs of garbled text: replacement characters, the C1 control range and the private use area
GARBLED_SIGNAL_RE = re.compile(r"[\ufffd\u0080-\u009f\ue000-\uf8ff\u0000-\u0008\u000b\u000c\u000e-\u001f]")
# Non-ASCII characters that may stay: CJK characters, the degree sign, common punctuation and Japanese-style list markers
ALLOWED_NON_ASCII_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\u00b0\u00b7\u2010-\u203a\u30fb\uff0c\u2022\u00a9\u00ae\u2122]")


def _garbled_token(token: str) -> bool:
    """Decide whether a single whitespace-separated piece is garbled."""
    if GARBLED_SIGNAL_RE.search(token):
        return True
    chars = [c for c in token if not c.isspace()]
    if not chars:
        return False
    odd = [c for c in chars
           if ord(c) > 127 and not ALLOWED_NON_ASCII_RE.match(c) and not c.isdigit()]
    # More than half of the piece consists of unusual non-ASCII characters, so it is garbled
    return len(odd) / len(chars) >= 0.5 and len(odd) >= 2


def strip_garbled_runs(text: str) -> str:
    """Remove garbled pieces caused by broken font encodings.

    Some pages of the XPeng report use non-standard font encodings and yield pieces such as `����` or
    `\\x0eÇÀÀ\\x99Á...`. These are not text in any language and would pollute the word list.
    Normal English and legitimate non-ASCII characters on the same line (°C, Chinese characters, ‒) must be kept,
    so each whitespace-separated piece is judged on its own instead of deleting the line or filtering by character class.
    """
    tokens = text.split()
    if not tokens:
        return text
    kept = [t for t in tokens if not _garbled_token(t)]
    if len(kept) == len(tokens):
        return text
    cleaned = " ".join(kept).strip()
    record_repair("strip_garbled_runs", text[:120], cleaned[:120])
    return cleaned if len(words(cleaned)) >= 2 else ""


# Known legitimate camel-case and mixed-case names, which do not count as evidence of blocks run together
CAMEL_WHITELIST_RE = re.compile(
    r"\b(?:XPeng|XPENG|iPhone|eVTOL|eV|iOS|kWh|MWh|GWh|tCO|gCO|CO2e|IoT|AIoT|"
    r"NIO|BYD|ESG|GRI|TCFD|IFRS|ISO|UNGC|CSRD|HKEx|SBTi|NCAP|R&D|"
    # Camel-case product and brand names: BYD SkyShuttle/SkyRail, Tesla Powerwall and so on.
    # Without them, the 'yS' in SkyShuttle would count as blocks run together, and normal pages would be flagged as layout failures
    # (all 10 "joins" on BYD p120 came from SkyShuttle).
    r"SkyShuttle|SkyRail|SkyTrain|SkyPass|PowerWall|Powerwall|Megapack|Powerpack|"
    r"MegaCharger|Supercharger|CyberTruck|Cybertruck|SkyWell|BladeBattery|DiPilot|DiSus|"
    r"[A-Z][a-z]+(?:[A-Z][a-z]+)+|"          # general camel-case proper names (two or more parts)
    r"[A-Z]{2,}s?|[A-Z]\d+)\b"
)
BLOCK_GLUE_RE = re.compile(r"[a-z][A-Z]")
BULLET_RE = re.compile(r"[·•・▪●]")


def layout_failure_signals(texts: list[str]) -> dict[str, Any]:
    """Detect layout failures in which several separate text boxes on one horizontal band were merged into one line.

    Two kinds of evidence:
    1. Joined list items: two or more bullet markers inside one sentence. Separate items should each form their own sentence,
       so finding them in one sentence means that text from different boxes was interleaved (seen on XPeng p6).
    2. Characters run together: a lower-case letter directly followed by a capital, as in 'AreasManagement' (camel-case names are whitelisted).
       This signal disappears once spaces are added from coordinates, so it is only supporting evidence and cannot be relied on alone.

    On such pages the word order and word neighbours cannot be trusted. They would create false collocations, so they cannot enter the analysis corpus.
    """
    total_words = sum(len(words(t)) for t in texts)
    multi_bullet = [t for t in texts if len(BULLET_RE.findall(t)) >= 2]
    glue = sum(len(BLOCK_GLUE_RE.findall(CAMEL_WHITELIST_RE.sub(" ", t))) for t in texts)
    lowercase_starts = [t for t in texts if re.match(r"^[a-z]", t.strip())]
    long_texts = [t for t in texts if len(words(t)) >= 8]
    near_duplicate_pairs = []
    for i in range(len(long_texts)):
        for j in range(i + 1, len(long_texts)):
            a, b = pattern(long_texts[i]), pattern(long_texts[j])
            ratio = difflib.SequenceMatcher(None, a, b).ratio()
            if ratio >= 0.80:
                near_duplicate_pairs.append({
                    "similarity": round(ratio, 3),
                    "a": long_texts[i][:120], "b": long_texts[j][:120],
                })
    return {
        "sentences": len(texts),
        "words": total_words,
        "multi_bullet_sentences": len(multi_bullet),
        "multi_bullet_ratio": len(multi_bullet) / len(texts) if texts else 0.0,
        "lowercase_start_sentences": len(lowercase_starts),
        "lowercase_start_ratio": len(lowercase_starts) / len(texts) if texts else 0.0,
        "near_duplicate_pairs": len(near_duplicate_pairs),
        "glue_points": glue,
        "glue_per_1000_words": glue / total_words * 1000 if total_words else 0.0,
        "samples": ([t[:160] for t in multi_bullet[:3]]
                    + [t[:160] for t in lowercase_starts[:2]]),
        "near_duplicate_samples": near_duplicate_pairs[:3],
    }


def is_layout_failure(sig: dict[str, Any]) -> bool:
    """Thresholds: joined list items are the main test, characters run together a supporting one.

    At least five sentences are required so that short pages are not misjudged. A share of 15% or more shows interleaving across the page
    rather than a legitimate list with several markers inside one item.
    """
    if sig["sentences"] >= 5 and sig["multi_bullet_sentences"] >= 2 and sig["multi_bullet_ratio"] >= 0.15:
        return True
    # Tesla p177 pattern: after two columns are interleaved, part of a paragraph repeats and many fragments
    # start in lower case. The two signals together match only this page among the six reports.
    if (sig["sentences"] >= 8 and sig["lowercase_start_ratio"] >= 0.20
            and sig["near_duplicate_pairs"] >= 1):
        return True
    return sig["glue_points"] >= 8 and sig["glue_per_1000_words"] >= 25


def clean_plain_text(text: str) -> str:
    text = normalize_unicode(text)
    text = MARKDOWN_ARTIFACT_RE.sub(" ", text)
    text = URL_RE.sub(" ", text)
    text = EMAIL_RE.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = repair_internal_spacing(text.strip(" \t|#"))
    # Garbled-text removal must run at every entry point, especially table_cells. The old version called it only on the body-text path,
    # which left 73 replacement characters in XPeng table cells.
    return strip_garbled_runs(text)


def sentence_like(text: str) -> bool:
    """Decide whether table or OCR text is a running sentence rather than data or a label.

    Function-word density only tells prose from labels. It is not the only test of whether a sentence is grammatical.
    """
    ws = words(text)
    if len(ws) < 3 or alpha_ratio(text) < 0.58:
        return False
    terminal = bool(re.search(r"[.!?;:]\s*$", text))
    has_function_words = stopword_density(text) >= 0.16
    # Blocks of three or four words need both final punctuation and function words. Longer blocks need one of the two.
    if len(ws) < 5:
        return terminal and has_function_words
    return has_function_words or terminal


TABLE_TERMINAL_RE = re.compile(r"[.!?;:][\"')\]]?$")


def table_prose_sentences(text: str) -> list[str]:
    """Keep only complete running sentences from table cells and drop numbers, indicator names and labels.

    The rule is to drop table data but keep complete sentences. The old version looked only at function-word density, so
    `Package material consumption for whole vehicle` entered no_tables just because it contains "for".
    A sentence must now meet all four conditions: at least 5 words, final punctuation, function-word density of at least 0.12 and a letter share of at least 0.58.
    """
    kept = []
    for sentence in segment_paragraph(clean_plain_text(text)):
        if (
            len(words(sentence)) >= 5
            and TABLE_TERMINAL_RE.search(sentence)
            and stopword_density(sentence) >= 0.12
            and alpha_ratio(sentence) >= 0.58
        ):
            kept.append(sentence)
    return kept


def segment_paragraph(text: str, *, heading: bool = False) -> list[str]:
    """Output body text sentence by sentence and keep headings on their own."""
    text = clean_plain_text(text)
    if not text or PAGE_NUMBER_RE.fullmatch(text):
        return []
    if heading:
        return [text]
    try:
        segs = [s.strip() for s in SEGMENTER.segment(text) if s.strip()]
    except Exception:
        segs = [text]
    return segs or [text]


def table_cells(box: dict[str, Any]) -> list[str]:
    table = box.get("table") or {}
    rows = table.get("extract") or []
    out = []
    for row in rows:
        for cell in row or []:
            if cell is None:
                continue
            text = dehyphenate_join(str(cell).splitlines())
            text = clean_plain_text(text)
            if text:
                out.append(text)
    return out


def bbox_area(box: dict[str, Any]) -> float:
    return max(0.0, box.get("x1", 0) - box.get("x0", 0)) * max(0.0, box.get("y1", 0) - box.get("y0", 0))


def overlap_ratio(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, x1 - x0) * max(0, y1 - y0)
    area = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    return inter / area


def ocr_region(
    doc: pymupdf.Document,
    page_index: int,
    bbox: tuple[float, float, float, float],
    dpi: int = 300,
    psm: int = 6,
) -> dict[str, Any]:
    """Run OCR on the given layout region only and return the text, paragraphs, confidence and word count.

    Tesseract's TSV output gives the block, paragraph, line and coordinates of each word. Besides the full text,
    paragraph boundaries are kept here. Later steps take complete running sentences only from confident paragraphs, so that headings, contents lists
    and chart scales are not joined with body text into a false sentence.
    """
    page = doc[page_index]
    rect = pymupdf.Rect(bbox) & page.rect
    if rect.width < 10 or rect.height < 10:
        return {"text": "", "confidence": 0.0, "words": 0, "paragraphs": []}
    pix = page.get_pixmap(clip=rect, dpi=dpi, alpha=False)
    with tempfile.TemporaryDirectory(prefix="esg_ocr_") as td:
        image = Path(td) / "region.png"
        outbase = Path(td) / "ocr"
        pix.save(str(image))
        cmd = [str(TESSERACT), str(image), str(outbase), "-l", "eng", "--psm", str(psm), "tsv"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        tsv = outbase.with_suffix(".tsv")
        if proc.returncode or not tsv.exists():
            return {"text": "", "confidence": 0.0, "words": 0, "paragraphs": [], "error": proc.stderr[-500:]}
        groups: dict[tuple[int, int, int], list[tuple[int, str, float, int, int]]] = collections.defaultdict(list)
        with tsv.open(encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f, delimiter="\t"):
                if row.get("level") != "5" or not row.get("text", "").strip():
                    continue
                try:
                    conf = float(row.get("conf", -1))
                    key = (int(row["block_num"]), int(row["par_num"]), int(row["line_num"]))
                    order = int(row["word_num"])
                    top, height = int(row["top"]), int(row["height"])
                except (ValueError, KeyError):
                    continue
                groups[key].append((order, row["text"].strip(), conf, top, height))

        line_records = []
        all_confs = []
        for key in sorted(groups):
            items = sorted(groups[key])
            text = " ".join(item[1] for item in items)
            confs = [item[2] for item in items if item[2] >= 0]
            top = min(item[3] for item in items)
            bottom = max(item[3] + item[4] for item in items)
            line_records.append({
                "key": key, "text": text, "top": top, "bottom": bottom,
                "confidence": sum(confs) / len(confs) if confs else 0.0,
                "word_count": len(items),
            })
            all_confs.extend(confs)

        heights = sorted(max(1, r["bottom"] - r["top"]) for r in line_records)
        median_height = heights[len(heights) // 2] if heights else 1
        para_groups: list[list[dict[str, Any]]] = []
        for record in line_records:
            if not para_groups:
                para_groups.append([record]); continue
            prev = para_groups[-1][-1]
            same_ocr_para = record["key"][:2] == prev["key"][:2]
            vertical_gap = record["top"] - prev["bottom"]
            if not same_ocr_para or vertical_gap > median_height * 1.5:
                para_groups.append([record])
            else:
                para_groups[-1].append(record)

        paragraphs = []
        for group in para_groups:
            para_text = clean_plain_text(dehyphenate_join(r["text"] for r in group))
            total_wc = sum(r["word_count"] for r in group)
            para_conf = sum(r["confidence"] * r["word_count"] for r in group) / max(total_wc, 1)
            if para_text:
                paragraphs.append({
                    "text": para_text, "confidence": para_conf,
                    "words": len(words(para_text)),
                })
        text = " ".join(p["text"] for p in paragraphs)
        return {
            "text": text,
            "confidence": sum(all_confs) / len(all_confs) if all_confs else 0.0,
            "words": len(words(text)),
            "paragraphs": paragraphs,
        }


DANGLING_TAIL_RE = re.compile(
    r"\b(and|or|but|of|for|the|a|an|in|on|to|with|by|from|at|as|that|which|who|"
    r"our|its|their|his|her|these|those|this)\s*$", re.I)


def drop_truncated_prefix_duplicates(items: list[str]) -> list[str]:
    """Delete truncated prefix fragments caused by overlapping box boundaries.

    PyMuPDF4LLM's boxes sometimes overlap. On BYD p27, the last line of BOX20 captured
    `With these updates, we have formulated the` (the first line of that sentence in the left column), while BOX22
    gives the same sentence in full on two lines. After column splitting, the former becomes a dangling fragment that duplicates the full sentence.

    Three conditions must all hold, so that headings are never deleted by mistake (headings are short and do not end in a function word):
    1. at least 5 words,
    2. no final punctuation, ending in a dangling function word,
    3. a prefix of another, longer text on the same page.
    """
    prepared = [(pattern(x), x) for x in items]
    drop: set[int] = set()
    for i, (pi, xi) in enumerate(prepared):
        if not pi or len(words(xi)) < 5:
            continue
        stripped = xi.strip()
        if re.search(r"[.!?:;]$", stripped) or not DANGLING_TAIL_RE.search(stripped):
            continue
        for j, (pj, _) in enumerate(prepared):
            if i != j and pj and len(pj) > len(pi) and pj.startswith(pi):
                drop.add(i)
                record_repair("drop_truncated_prefix_duplicates", xi[:150], "")
                break
    return [x for k, x in enumerate(items) if k not in drop]


VERIFIED_PAGE_CORRECTIONS = {
    ("byd_2024", 69): (
        ("improve product life and Technology Based, Innovation Oriented significantly reduce energy",
         "improve product life and significantly reduce energy"),
        ("At the BYD Dream Day introduces new technologies same time,",
         "At the same time,"),
    ),
}


def apply_verified_page_corrections(doc_id: str, page: int, items: list[str]) -> list[str]:
    """Apply only exact fixes confirmed against the PDF and limited to a given document and page."""
    corrections = VERIFIED_PAGE_CORRECTIONS.get((doc_id, page), ())
    if not corrections:
        return items
    out = []
    for item in items:
        changed = item
        for old, new in corrections:
            if old in changed:
                changed = changed.replace(old, new)
                record_repair("verified_page_corrections", old, new)
        out.append(changed)
    return out


def likely_duplicate(candidate: str, existing: list[str]) -> bool:
    """Cautious de-duplication: a short heading that also appears inside a long sentence must not cause the sentence to be deleted.

    The old test counted any short string inside a longer one as a duplicate, and once deleted a complete OCR sentence
    because it contained `32 million metric`. Now the shorter text must cover at least 70% of the longer one, or the two texts
    must reach a SequenceMatcher similarity of at least 90%.
    """
    p = pattern(candidate)
    if not p:
        return True
    for text in existing:
        q = pattern(text)
        if not q:
            continue
        if p == q:
            return True
        short, long = sorted((p, q), key=len)
        if len(short) >= 30 and len(short) / max(len(long), 1) >= 0.70 and short in long:
            return True
        if min(len(p), len(q)) >= 30 and difflib.SequenceMatcher(None, p, q).ratio() >= 0.90:
            return True
    return False


OCR_TERMINAL_RE = re.compile(r"[.!?][\"')\]]?$")
OCR_CO2E_RE = re.compile(r"\bCO(?:c|q|Q|z)?z?e\b", re.I)


def clean_ocr_artifacts(text: str) -> str:
    """Fix common OCR glyph errors that can be identified without ambiguity. Every fix is still written to the repairs log."""
    before = text
    # In the Tesla font the digit 2 is repeatedly read as z, c or q: COze, COcze, COQ ze -> CO2e
    text = re.sub(r"\bCOQ\s+ze\b", "CO2e", text, flags=re.I)
    text = OCR_CO2E_RE.sub("CO2e", text)
    text = re.sub(r"\bcarbon dioxide \(COz\)", "carbon dioxide (CO2)", text, flags=re.I)
    text = re.sub(r"\bmethane \(CHa\)", "methane (CH4)", text, flags=re.I)
    text = re.sub(r"\binperson\b", "in-person", text, flags=re.I)
    text = re.sub(r"\banew Employee\b", "a new Employee", text, flags=re.I)
    text = re.sub(r"\baccess to Support across\b", "access to support across", text)
    text = re.sub(r"\bacompany\b", "a company", text, flags=re.I)
    text = re.sub(r"\bremont\b", "Fremont", text, flags=re.I)
    text = re.sub(r"\bFremont actory\b", "Fremont Factory", text, flags=re.I)
    text = re.sub(r"\bGigafactory Teas\b", "Gigafactory Texas", text, flags=re.I)
    text = re.sub(r"\bLC As\b", "LCAs", text)
    text = re.sub(r"\bIS 1404([04])\b", r"ISO 1404\1", text)
    # On Tesla p187 "useful" is consistently read as `use tul`. In this context the correction is unambiguous.
    text = re.sub(r"\buse\s+tul\b", "useful", text, flags=re.I)
    # Remove stray vertical bars and axis noise such as "3:" left by OCR of charts, without touching ordinary numbers.
    text = re.sub(r"\s+[|_=<>]+\s+", " ", text)
    text = re.sub(r"\s+\d:\s+", " ", text)
    text = clean_plain_text(text)
    if text != before:
        record_repair("clean_ocr_artifacts", before[:150], text[:150])
    return text


def strict_ocr_sentences(result: dict[str, Any], existing: list[str]) -> list[str]:
    """Keep only confident, complete running sentences from an OCR region.

    Headings, contents lists, legends and tables of figures stay out even when OCR is confident about them. The thresholds are quality-control lines
    set for this project: mean paragraph confidence of at least 91.5, final punctuation, at least 8 words, function-word density of at least 0.15,
    a letter share of at least 0.82 and no more than 25% numeric words. The value 0.15 keeps technical sentences full of names
    (a sentence about NEDC/WLTP/EPA measured 0.171). The other four conditions still keep chart labels out.
    """
    accepted = []
    paragraphs = result.get("paragraphs") or [{
        "text": result.get("text", ""), "confidence": result.get("confidence", 0),
        "words": result.get("words", 0),
    }]
    for para in paragraphs:
        if para.get("confidence", 0) < 91.5:
            continue
        text = clean_ocr_artifacts(para.get("text", ""))
        for sentence in segment_paragraph(text):
            sentence = re.sub(r"^\s*\d{1,3}\s+", "", sentence).strip()
            ws = words(sentence)
            numeric = len(re.findall(r"\b\d+(?:[.,]\d+)?%?\b", sentence))
            if (
                len(ws) >= 8
                and OCR_TERMINAL_RE.search(sentence)
                and stopword_density(sentence) >= 0.15
                and alpha_ratio(sentence) >= 0.82
                and numeric / max(len(ws), 1) <= 0.25
                and not likely_duplicate(sentence, existing + accepted)
            ):
                accepted.append(sentence)
    return accepted


def page_has_full_background_image(page: pymupdf.Page) -> bool:
    """Does the page have a bitmap background covering at least 80% of it (the carrier of image-only text in the mixed Tesla PDF)?"""
    page_area = max(1.0, page.rect.width * page.rect.height)
    for image in page.get_images(full=True):
        try:
            if any(rect.width * rect.height / page_area >= 0.80 for rect in page.get_image_rects(image[0])):
                return True
        except Exception:
            continue
    return False


def stitch_across_boxes(items: list[str]) -> list[str]:
    """Rejoin sentences that were cut where one layout block ends and the next begins.

    The layout engine cuts a paragraph into several boxes by visual area, and sentences are split inside each box,
    so `...which has laid a solid` and `foundation for NIO...` become two sentences.
    Test: the first part has no final punctuation and the second starts with a lower-case letter. Short heading-like lines are excluded.
    """
    if not items:
        return items
    out = [items[0]]
    for nxt in items[1:]:
        prev = out[-1]
        prev_open = bool(prev) and not re.search(r"[.!?;:!？。]['\")\]]?$", prev.strip())
        next_lower = bool(re.match(r"^['\u2019]?[a-z]", nxt.strip()))
        heading_words = words(prev)
        titlecase_ratio = sum(w[:1].isupper() for w in heading_words) / max(len(heading_words), 1)
        # A heading must be short, with most content words in title case or capitals. Word count alone is not enough,
        # otherwise `The service model laid a solid` (a six-word fragment of a sentence) would be taken for a heading.
        prev_is_heading = (
            len(heading_words) <= 10
            and (titlecase_ratio >= 0.60 or (prev.isupper() and len(heading_words) >= 2))
        )
        # Headings broken across boxes: on XPeng p27, `Rights Protection and` and
        # `Infringement Dispute Response` are neighbouring boxes of the same block, and the second starts with a capital,
        # so next_lower does not hold. The trigger is a first part ending in a dangling function word. Normal
        # headings do not end in and, of or for, so this form is itself evidence of a break.
        next_words = words(nxt)
        heading_continuation = (
            prev_open
            and not next_lower
            and DANGLING_TAIL_RE.search(prev.strip())
            and len(heading_words) <= 10
            and 1 <= len(next_words) <= 10
            and bool(re.match(r"^[A-Z]", nxt.strip()))
        )
        if (prev_open and next_lower and not prev_is_heading) or heading_continuation:
            merged = f"{prev.rstrip()} {nxt.lstrip()}"
            record_repair("stitch_across_boxes", f"{prev[-60:]} || {nxt[:60]}", merged[-90:])
            segs = segment_paragraph(merged)
            out[-1:] = segs if segs else [merged]
        else:
            out.append(nxt)
    return out


TESLA_2025_OCR_COLUMN_RANGES = ((0.0, 0.25), (0.25, 0.495), (0.495, 0.745), (0.745, 1.0))


def tesla_2025_body_top(page: dict[str, Any]) -> float:
    large_bottoms = []
    for box in page.get("boxes") or []:
        for line in box.get("textlines") or []:
            for span in line.get("spans") or []:
                bbox = span.get("bbox") or [0, 0, 0, 0]
                if float(span.get("size") or 0) >= 30 and 80 <= float(bbox[1]) <= 400:
                    large_bottoms.append(float(bbox[3]))
    return min(400.0, max(large_bottoms, default=180.0) + 25.0)


def tesla_2025_native_headings(page: dict[str, Any], body_top: float) -> list[str]:
    headings = []
    for box in page.get("boxes") or []:
        y0, y1 = float(box.get("y0", 0)), float(box.get("y1", 0))
        if y0 < 75 or y1 > body_top:
            continue
        text = clean_plain_text(dehyphenate_join(box_lines(box)))
        ws = words(text)
        bad_short = sum(len(w) <= 2 and w.lower() not in STOPWORDS for w in ws)
        plausible = (
            text and len(ws) >= 2 and alpha_ratio(text) >= 0.75
            and bad_short / max(len(ws), 1) <= 0.20
            and not re.match(r"^[a-z]", text)
        )
        if plausible and not PAGE_NUMBER_RE.fullmatch(text) and "Impact Report" not in text:
            headings.extend(segment_paragraph(text, heading=True))
    return drop_truncated_prefix_duplicates(headings)


def stitch_ocr_column_paragraphs(paragraphs: list[str]) -> list[str]:
    out = []
    for text in paragraphs:
        if out and out[-1].endswith((":", ";")) and len(words(text)) <= 6:
            out[-1] = f"{out[-1]} {text};"
        elif out and not re.search(r"[.!?;:]$", out[-1].strip()):
            out[-1] = f"{out[-1].rstrip()} {text.lstrip()}"
        else:
            out.append(text)
    return [text[:-1] + "." if text.endswith(";") else text for text in out]


def tesla_2025_ocr_sentences(result: dict[str, Any], existing: list[str]) -> list[str]:
    paragraphs = []
    for para in result.get("paragraphs") or []:
        if para.get("confidence", 0) < 89.0:
            continue
        text = clean_ocr_artifacts(para.get("text", ""))
        if len(words(text)) >= 2 and alpha_ratio(text) >= 0.78:
            paragraphs.append(text)
    paragraphs = stitch_ocr_column_paragraphs(paragraphs)
    accepted = []
    for text in paragraphs:
        numbered_list = len(re.findall(r"\b\d\)", text)) >= 2 and OCR_TERMINAL_RE.search(text)
        segments = [text] if numbered_list else segment_paragraph(text)
        for sentence in segments:
            sentence = re.sub(r"^\s*\d{1,3}\s+", "", sentence).strip()
            ws = words(sentence)
            numeric = len(re.findall(r"\b\d+(?:[.,]\d+)?%?\b", sentence))
            list_like = ":" in sentence and len(ws) >= 8
            if (len(ws) >= 5 and OCR_TERMINAL_RE.search(sentence)
                    and (stopword_density(sentence) >= 0.10 or list_like)
                    and alpha_ratio(sentence) >= 0.78
                    and numeric / max(len(ws), 1) <= 0.30
                    and not likely_duplicate(sentence, existing + accepted)):
                accepted.append(sentence)
    return accepted


def tesla_2025_ocr_replacement(
    doc: pymupdf.Document,
    page_index: int,
    layout_page: dict[str, Any],
    page_log: collections.Counter,
    log: dict[str, Any],
) -> list[str]:
    page = doc[page_index]
    body_top = tesla_2025_body_top(layout_page)
    accepted = tesla_2025_native_headings(layout_page, body_top)
    for column_no, (left, right) in enumerate(TESLA_2025_OCR_COLUMN_RANGES, 1):
        bbox = (page.rect.width * left, body_top, page.rect.width * right, page.rect.height * 0.96)
        result = ocr_region(doc, page_index, bbox, dpi=400, psm=3)
        sentences = tesla_2025_ocr_sentences(result, accepted)
        result.update({
            "source": "tesla_2025_garbled_native_replacement", "page": page_index + 1,
            "column": column_no, "bbox": [round(x, 1) for x in bbox],
            "accepted_sentences": sentences, "kept": bool(sentences),
        })
        log["ocr_regions"].append(result)
        accepted.extend(sentences)
        if sentences:
            page_log["tesla_2025_ocr_columns_kept"] += 1
            page_log["ocr_sentences_kept"] += len(sentences)
        else:
            page_log["tesla_2025_ocr_columns_rejected"] += 1
    page_log["tesla_2025_native_replaced"] += 1
    return drop_truncated_prefix_duplicates(accepted)


def detect_repeated_marginal_boxes(pages: list[dict[str, Any]]) -> tuple[set[tuple[int, int]], dict[str, int]]:
    """Find top and bottom layout blocks that repeat across pages and return the (page_idx, box_idx) pairs to delete."""
    occurrence: dict[str, list[tuple[int, int, str, str]]] = collections.defaultdict(list)
    for pi, page in enumerate(pages):
        h = float(page.get("height") or 1)
        for bi, box in enumerate(page.get("boxes") or []):
            text = dehyphenate_join(box_lines(box))
            if not text:
                continue
            cls = box.get("boxclass", "")
            top = float(box.get("y0", 0)) / h
            bottom = float(box.get("y1", 0)) / h
            marginal = cls in {"page-header", "page-footer"} or top <= 0.13 or bottom >= 0.90
            if marginal:
                p = pattern(text)
                if p:
                    occurrence[p].append((pi, bi, cls, text))
    remove: set[tuple[int, int]] = set()
    summary: dict[str, int] = {}
    for p, items in occurrence.items():
        page_count = len({item[0] for item in items})
        is_page_number = p == "#" or PAGE_NUMBER_RE.fullmatch(items[0][3]) is not None
        explicit_header_footer = any(item[2] in {"page-header", "page-footer"} for item in items)
        if page_count >= 3 or is_page_number or (explicit_header_footer and page_count >= 2):
            for pi, bi, _, _ in items:
                remove.add((pi, bi))
            summary[items[0][3][:100]] = page_count
    return remove, summary


def native_page_chars(pdf_path: Path) -> list[int]:
    doc = pymupdf.open(pdf_path)
    return [len((page.get_text("text") or "").strip()) for page in doc]


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manual_override(config: DocumentConfig, page_no: int) -> dict[str, Any] | None:
    """Read manual override pages approved after two checks. Drafts never enter the corpus automatically.

    Folder layout:
      01_Source/manual_overrides/approved/<doc_id>/p024_with_tables.txt
      01_Source/manual_overrides/approved/<doc_id>/p024_no_tables.txt
      01_Source/manual_overrides/approved/<doc_id>/p024_approval.json

    approval.json must say approved=true, and both version files must exist. The no_tables file may be empty
    (NIO p32, for example, contains only matrix labels). with_tables should hold the text restored by cell and reading order.
    """
    folder = MANUAL_OVERRIDE_ROOT / config.doc_id
    prefix = f"p{page_no:03d}"
    with_file = folder / f"{prefix}_with_tables.txt"
    no_file = folder / f"{prefix}_no_tables.txt"
    approval_file = folder / f"{prefix}_approval.json"
    if not approval_file.exists():
        return None
    approval = json.loads(approval_file.read_text(encoding="utf-8"))
    if approval.get("approved") is not True:
        return None
    if approval.get("doc_id") != config.doc_id or int(approval.get("page", -1)) != page_no:
        raise ValueError(f"Manual override metadata does not match: {approval_file}")
    if not with_file.exists() or not no_file.exists():
        raise FileNotFoundError(f"Approved override is missing a text file: {folder}/{prefix}_*.txt")

    def load_lines(path: Path) -> list[str]:
        text = normalize_unicode(path.read_text(encoding="utf-8"))
        if "�" in text:
            raise ValueError(f"Manual override contains replacement characters: {path}")
        return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]

    def display_path(path: Path) -> str:
        try:
            return str(path.relative_to(ROOT))
        except ValueError:
            return str(path)

    return {
        "with_tables": load_lines(with_file),
        "no_tables": load_lines(no_file),
        "approval": approval,
        "files": {
            display_path(with_file): file_sha256(with_file),
            display_path(no_file): file_sha256(no_file),
            display_path(approval_file): file_sha256(approval_file),
        },
    }


def process_document(config: DocumentConfig) -> dict[str, Any]:
    started = dt.datetime.now()
    if not config.path.exists():
        raise FileNotFoundError(config.path)

    print(f"\n[{started:%H:%M:%S}] START {config.doc_id}: {config.path.name}", flush=True)
    REPAIR_COUNTS.clear()
    REPAIR_EXAMPLES.clear()
    # Automatic OCR is switched off: native text is never recognized again, and image regions that need OCR are handled locally by this script.
    raw_json = pymupdf4llm.to_json(
        str(config.path), show_progress=False, use_ocr=False, force_text=True
    )
    parsed = json.loads(raw_json)
    pages = parsed["pages"]
    native_chars = native_page_chars(config.path)
    repeated_remove, repeated_summary = detect_repeated_marginal_boxes(pages)
    doc = pymupdf.open(config.path)

    with_dir = OUT_ROOT / config.set_name / "with_tables"
    no_dir = OUT_ROOT / config.set_name / "no_tables"
    page_dir = OUT_ROOT / config.set_name / "pages"
    for directory in (with_dir, no_dir, page_dir, LOG_ROOT):
        directory.mkdir(parents=True, exist_ok=True)

    all_with: list[str] = []
    all_no: list[str] = []
    page_records = []
    log: dict[str, Any] = {
        "document": asdict(config),
        "source_path": str(config.path),
        "started": started.isoformat(),
        "pages_total": len(pages),
        "toc_pages_excluded": list(config.toc_pages),
        "repeated_marginal_patterns": repeated_summary,
        "counters": collections.Counter(),
        "ocr_regions": [],
        "low_quality_pages": [],
        "manual_override_pages": [],
        "pages": [],
    }

    for pi, page in enumerate(pages):
        page_no = int(page.get("page_number", pi + 1))
        if page_no in config.toc_pages:
            log["counters"]["toc_pages_removed"] += 1
            log["pages"].append({"page": page_no, "status": "excluded_toc"})
            continue

        with_parts: list[str] = []
        no_parts: list[str] = []
        page_log = collections.Counter()
        for bi, box in enumerate(page.get("boxes") or []):
            cls = box.get("boxclass", "text")
            if (pi, bi) in repeated_remove:
                page_log["marginal_boxes_removed"] += 1
                continue
            if cls in {"page-header", "page-footer"}:
                # Headers and footers that are unique and not in repeated_remove are still removed, but fully logged.
                page_log[f"{cls}_removed"] += 1
                continue

            if cls == "table":
                cells = table_cells(box)
                page_log["table_boxes"] += 1
                page_log["table_cells_total"] += len(cells)
                for cell in cells:
                    with_parts.extend(segment_paragraph(cell))
                    prose_sentences = table_prose_sentences(cell)
                    if prose_sentences:
                        no_parts.extend(prose_sentences)
                        page_log["table_cells_prose_kept"] += 1
                        page_log["table_prose_sentences_kept"] += len(prose_sentences)
                    else:
                        page_log["table_cells_data_removed"] += 1
                continue

            if cls == "picture":
                # For Tesla, the image regions marked by the layout engine are recognized first (the two paragraphs on p5 can be cropped exactly).
                # Whole pages with little text go through three-column OCR later. The two results are merged with cautious fuzzy de-duplication.
                should_ocr = config.ocr_mode == "all_picture_regions"
                if config.ocr_mode == "low_text_pictures" and pi < len(native_chars):
                    should_ocr = native_chars[pi] < 100
                if not should_ocr:
                    page_log["picture_boxes_ignored"] += 1
                    continue
                bbox = tuple(float(box.get(k, 0)) for k in ("x0", "y0", "x1", "y1"))
                result = ocr_region(doc, pi, bbox, dpi=400)
                result.update({
                    "source": "layout_picture_box", "page": page_no,
                    "bbox": [round(x, 1) for x in bbox],
                })
                page_area = max(1.0, float(page.get("width", 1)) * float(page.get("height", 1)))
                result["page_area_ratio"] = bbox_area(box) / page_area
                accepted = strict_ocr_sentences(result, with_parts)
                result["accepted_sentences"] = accepted
                result["kept"] = bool(accepted)
                log["ocr_regions"].append(result)
                if accepted:
                    with_parts.extend(accepted)
                    no_parts.extend(accepted)
                    page_log["ocr_regions_kept"] += 1
                    page_log["ocr_sentences_kept"] += len(accepted)
                else:
                    page_log["ocr_regions_rejected"] += 1
                continue

            # A layout block may contain several columns (the engine sometimes merges them). Each column is split off and treated as its own text,
            # otherwise lines from different columns would interleave and create false word neighbours.
            column_groups = detect_column_groups(box, float(page.get("width") or 1))
            texts = []
            for group in column_groups:
                joined = dehyphenate_join(lines_from_textlines(group))
                cleaned_col = clean_plain_text(joined)
                if cleaned_col:
                    texts.append(cleaned_col)
            if len(column_groups) > 1:
                page_log["boxes_split_into_columns"] += 1
            text = " ".join(texts) if len(texts) == 1 else None
            if text is None and texts:
                # Several columns: each column goes through the later steps on its own instead of being joined into one line
                heading_multi = False
                for col_text in texts:
                    col_text = strip_garbled_runs(col_text)
                    if not col_text:
                        continue
                    segs = segment_paragraph(col_text, heading=heading_multi)
                    with_parts.extend(segs)
                    no_parts.extend(segs)
                page_log[f"{cls}_boxes_kept"] += 1
                continue
            if not text:
                continue
            text = strip_garbled_runs(text)
            if not text:
                page_log["garbled_text_removed"] += 1
                continue
            # English corpus: blocks with a high share of non-Latin characters and almost no English words are removed.
            if len(words(text)) < 2 and alpha_ratio(text) < 0.45:
                page_log["non_english_or_noise_removed"] += 1
                continue
            # Leftover legend and in-image navigation: @ and © are icon symbols that Tesla and other reports put into the text layer.
            # Two or more of them on one line mark a legend such as "@label1 @label2", not body text.
            if text.count("@") + text.count("©") >= 2:
                page_log["symbol_label_lines_removed"] += 1
                continue
            heading = cls in {"section-header", "caption"} and len(words(text)) <= 20
            segs = segment_paragraph(text, heading=heading)
            with_parts.extend(segs)
            no_parts.extend(segs)
            page_log[f"{cls}_boxes_kept"] += 1

        # Tesla 2024 is a mixed PDF: almost every page carries a full-page bitmap, and some body text exists only
        # in the bitmap, not in the native text layer. Local picture-box OCR misses this text (seen on
        # p10/p20/p26/p28/p54/p66/p106/p108/p167/p187). Pages with fewer than 500 native characters
        # and a full-page bitmap are cropped into left, middle and right columns for OCR, so that whole-page OCR does not interleave the columns.
        # Only complete, confident sentences that pass strict_ocr_sentences are added.
        if (
            config.ocr_mode == "all_picture_regions"
            and native_chars[pi] < 500
            and page_has_full_background_image(doc[pi])
        ):
            W, H = doc[pi].rect.width, doc[pi].rect.height
            column_ranges = ((0.00, 0.345), (0.325, 0.675), (0.655, 1.00))
            for column_no, (left, right) in enumerate(column_ranges, 1):
                bbox = (W * left, H * 0.04, W * right, H * 0.96)
                # PSM 3 groups text by paragraph. On the two paragraphs of Tesla p54 it worked clearly better than
                # PSM 6 (which put image noise into the text) and PSM 11 (which broke the text into single lines).
                result = ocr_region(doc, pi, bbox, dpi=400, psm=3)
                result.update({
                    "source": "low_native_page_column", "page": page_no,
                    "column": column_no, "native_chars": native_chars[pi],
                    "bbox": [round(x, 1) for x in bbox],
                })
                accepted = strict_ocr_sentences(result, with_parts)
                result["accepted_sentences"] = accepted
                result["kept"] = bool(accepted)
                log["ocr_regions"].append(result)
                if accepted:
                    with_parts.extend(accepted)
                    no_parts.extend(accepted)
                    page_log["full_page_ocr_columns_kept"] += 1
                    page_log["ocr_sentences_kept"] += len(accepted)
                else:
                    page_log["full_page_ocr_columns_rejected"] += 1

        if config.doc_id == "tesla_2025" and page_no in TESLA_2025_OCR_REPLACE_PAGES:
            no_parts = tesla_2025_ocr_replacement(doc, pi, page, page_log, log)

        with_parts = stitch_across_boxes(with_parts)
        no_parts = stitch_across_boxes(no_parts)

        # Exact de-duplication within a page: the layout engine sometimes returns both the text layer and the image OCR of the same text.
        def dedupe(items: list[str]) -> list[str]:
            seen, out = set(), []
            for item in items:
                p = pattern(item)
                if p and p not in seen:
                    seen.add(p); out.append(item)
            return out

        with_parts = apply_verified_page_corrections(
            config.doc_id, page_no, drop_truncated_prefix_duplicates(dedupe(with_parts)))
        no_parts = apply_verified_page_corrections(
            config.doc_id, page_no, drop_truncated_prefix_duplicates(dedupe(no_parts)))

        # Detecting and isolating pages whose layout could not be parsed:
        # if a page has too many blocks run together, the engine has merged several separate text boxes into one line,
        # and the word order and neighbours on that page can no longer be trusted (they would create false collocations).
        # Such pages are kept out of the corpus but fully recorded in the log and in a separate list for manual review.
        # Nothing is dropped silently.
        signals = layout_failure_signals(no_parts)
        # Manual overrides are used not only for pages flagged as layout failures but also for problems found in the ordinary sample,
        # such as missing words, wrong order, footnote contamination or table cells mixed into the text. If the approval metadata
        # is valid, the whole page is replaced with the checked version. Otherwise the automatic failure test decides whether to isolate it.
        override = load_manual_override(config, page_no)
        if override is not None:
            with_parts = override["with_tables"]
            no_parts = override["no_tables"]
            log["manual_override_pages"].append({
                "page": page_no,
                "detected_failure": signals,
                "automatic_failure": is_layout_failure(signals),
                "with_tables_lines": len(with_parts),
                "no_tables_lines": len(no_parts),
                "approval": override["approval"],
                "file_sha256": override["files"],
            })
            page_log["manual_override_applied"] += 1
        elif is_layout_failure(signals):
            log["low_quality_pages"].append({"page": page_no, **signals})
            log["counters"]["pages_excluded_layout_failure"] += 1
            log["pages"].append({
                "page": page_no, "status": "excluded_layout_failure",
                "multi_bullet_ratio": round(signals["multi_bullet_ratio"], 3),
                "glue_per_1000_words": round(signals["glue_per_1000_words"], 1),
            })
            continue
        all_with.extend(with_parts)
        all_no.extend(no_parts)
        page_records.append({"page": page_no, "with_tables": with_parts, "no_tables": no_parts})
        status = "manual_override" if page_log.get("manual_override_applied") else "processed"
        log["pages"].append({"page": page_no, "status": status, **dict(page_log)})
        log["counters"].update(page_log)

    def render(parts: list[str]) -> str:
        return "\n".join(p.strip() for p in parts if p.strip()).strip() + "\n"

    with_text = render(all_with)
    no_text = render(all_no)
    (with_dir / f"{config.doc_id}.txt").write_text(with_text, encoding="utf-8")
    (no_dir / f"{config.doc_id}.txt").write_text(no_text, encoding="utf-8")
    with (page_dir / f"{config.doc_id}.jsonl").open("w", encoding="utf-8") as f:
        for record in page_records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    finished = dt.datetime.now()
    log["finished"] = finished.isoformat()
    log["elapsed_seconds"] = (finished - started).total_seconds()
    log["output"] = {
        "with_tables": str(with_dir / f"{config.doc_id}.txt"),
        "no_tables": str(no_dir / f"{config.doc_id}.txt"),
        "pages_jsonl": str(page_dir / f"{config.doc_id}.jsonl"),
        "with_tables_words": len(words(with_text)),
        "no_tables_words": len(words(no_text)),
    }
    # A Counter cannot be turned into JSON directly.
    log["counters"] = dict(log["counters"])
    log["repairs"] = {"counts": dict(REPAIR_COUNTS), "examples": dict(REPAIR_EXAMPLES)}
    log_path = LOG_ROOT / f"prepare_{config.doc_id}.json"
    log_path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[{finished:%H:%M:%S}] DONE  {config.doc_id}: "
        f"with={log['output']['with_tables_words']:,} words, "
        f"no={log['output']['no_tables_words']:,} words, "
        f"{log['elapsed_seconds']:.1f}s",
        flush=True,
    )
    return log


def select_documents(args) -> list[DocumentConfig]:
    if args.doc:
        selected = [d for d in DOCUMENTS if d.doc_id == args.doc]
        if not selected:
            raise SystemExit(f"Unknown doc_id: {args.doc}")
        return selected
    return [d for d in DOCUMENTS if d.set_name == args.set]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("primary", "robustness"), default="primary")
    ap.add_argument("--doc", help="process only this doc_id")
    args = ap.parse_args()

    configs = select_documents(args)
    manifest = {
        "pipeline": "ESG corpus v3",
        "created": dt.datetime.now().isoformat(),
        "documents": [],
    }
    for config in configs:
        try:
            manifest["documents"].append(process_document(config))
        except Exception as exc:
            import traceback
            trace = traceback.format_exc()
            manifest["documents"].append({"document": config.doc_id, "error": repr(exc), "traceback": trace})
            print(f"ERROR {config.doc_id}: {exc!r}\n{trace}", flush=True)
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    manifest_path = OUT_ROOT / f"manifest_{args.doc or args.set}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nManifest: {manifest_path}")


if __name__ == "__main__":
    main()
