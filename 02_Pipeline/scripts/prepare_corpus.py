#!/usr/bin/env python3
"""ESG 语料 v3 正式制备流水线。

目标
----
从原始 PDF 重新制备可用于 Sketch Engine 的语料，而不是修补旧 cleaned 文件。
流水线遵循 TenTen / jusText 的核心原则：
1. 版式感知提取（恢复多栏阅读顺序）
2. 句内断行修复与软连字符修复
3. 页眉页脚、目录移除
4. 表格生成两个版本：with_tables（保留全部单元格文本）与
   no_tables（移除数字/标签，保留单元格中的完整叙述句）
5. 页面级 JSONL 与逐步日志，保证可追溯、可复现

注意
----
- 该脚本不删除或覆盖任何旧语料。
- 表格版本名 no_tables 的准确含义是：移除表格数据与标签，但保留表格里的完整句子。
- Tesla 2024 为混合扫描件：仅对布局引擎识别出的图片文本区做局部 OCR；
  不对已有原生文本重新 OCR，避免制造额外错字。

运行
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

# 项目根：本文件位于 <ROOT>/02_Pipeline/scripts/prepare_corpus.py
ROOT = Path(__file__).resolve().parents[2]
PDF_ROOT = ROOT / "01_Source" / "pdf"
PDF_SUBDIR = {"primary": "2024_main", "robustness": "2025_robustness"}
OUT_ROOT = ROOT / "01_Source" / "v3"
MANUAL_OVERRIDE_ROOT = ROOT / "01_Source" / "manual_overrides" / "approved"
LOG_ROOT = ROOT / "02_Pipeline" / "logs"
TESSERACT = Path(shutil.which("tesseract") or "tesseract")

# 功能词表用于区分连续正文与标签/表格。不是用来判断句子语法是否完整。
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

# 遇到换行连字符时，这些独立前缀通常是保留连字符的真实复合词。
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


_NINJA_COST = wordninja.DEFAULT_LANGUAGE_MODEL._wordcost  # wordninja 内部词表，作真词第二依据

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
    """扩展词典判断：系统词典 + wordninja 词表 + 领域词 + 词缀剥离。

    目的不是拼写纠错，而是区分「真词」与「粘连/拆碎产物」。
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
            # 前缀 + 带后缀词干
            for suffix, repl in _SUFFIXES:
                if rest.endswith(suffix) and len(rest) > len(suffix) + 2:
                    stem = rest[:-len(suffix)] + repl
                    if stem in SYSTEM_WORDS or stem in DOMAIN_TERMS or stem in _NINJA_COST:
                        return True
    return False


# 专有名词/型号缩写：不得参与「碎词合并」，否则 NIO F1 会被并成 NI OF1
# （因 of 是真词，会把后续字母吸附过去）。
PROTECTED_TOKEN_RE = re.compile(r"^(?:[A-Z]{2,}|[A-Z]\d+|[A-Z]{1,3}\d*)$")
FUNCTION_WORD_MAXLEN = 3


def is_protected_token(piece: str) -> bool:
    """全大写缩写（NIO/BYD/ESG）、字母+数字型号（F1/E1/X9）受保护。"""
    return bool(PROTECTED_TOKEN_RE.match(piece)) and not piece.islower()


def suspicious_fragment(piece: str) -> bool:
    q = piece.lower()
    if len(q) == 1:
        # e 需保护：CO2e in 会被 token 化为 ... 'e' + 'in'；若把 e 当碎片，
        # 会因 ein 也是真词而误并成 CO2ein。代价是罕见的 'e mail' 不自动合并。
        return q not in {"a", "i", "e"}
    if len(q) == 2:
        return q not in SYSTEM_WORDS
    # 词典外的人名/地名不是「碎片」。Rawlinson 不在词典中，曾使
    # `Peter Rawlinson` 被判为碎词串、拼成 PeterRawlinson 后由 wordninja
    # 重切为 `Peter Raw lins on`（Lucid p4 实测）。PDF 真碎片的形态是短片段
    # 或小写延续片段，而首字母大写且长度 ≥6 的形态是典型专名，予以保护。
    if len(piece) >= 6 and piece[:1].isupper() and piece[1:].islower():
        return False
    return not known_simple(piece)


def merge_spaced_fragments(text: str) -> str:
    """修复 PDF 字符层把一个词拆成多个空格片段的问题。

    两段式策略，拒绝贪心逐段匹配（贪心会把 'h a s p...' 误并成 hasp）：
    1. 检测「碎词串」：单空格分隔的连续字母单元，且可疑碎片 ≥2，
       或 ≥1 可疑碎片且整串拼接后是真词。
    2. 整串拼接：直接是真词则合并；否则交给 wordninja 做全局最优切分，
       只有切出的每一段都是真词才接受，否则原样保留。
    保守原则：'in to'、'a lot of' 这类正常两词组合不会被触碰。
    """
    units = re.findall(r"[A-Za-z]+|[^A-Za-z]+", text)
    out, i, n = [], 0, len(units)
    while i < n:
        if not units[i].isalpha():
            out.append(units[i]); i += 1; continue
        # 撇号后的缩约后缀不得与下一词合并：That's the 曾被重新并回 That'sthe。
        if i > 0 and units[i - 1] in {"'", "’"} and units[i].lower() in {"s", "t", "re", "ve", "ll", "d", "m"}:
            out.append(units[i]); i += 1; continue
        # 受保护缩写/型号（NIO/BYD/F1）单独输出，不进入碎词 run，
        # 否则它会阻断同一行后续真实碎词的修复（BYD h a s → BYD h a s）。
        if is_protected_token(units[i]):
            out.append(units[i]); i += 1; continue
        # 收集连续 run：字母单元之间只隔单个空格，遇受保护 token 即停
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
        consumed = j  # 默认消费整个 run 末端
        # run 内已排除受保护 token（在收集阶段剥离），此处仅作兜底。
        has_protected = any(is_protected_token(p) for p in pieces)
        # 合并结果是短功能词时，要求原片段全为小写，避免 O+F1 → OF1 这类误并。
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
                # 整串失败时回退：取含可疑碎片的最长已知前缀
                # （如 'w ho pursues' → 前缀 'who' + 正常保留 'pursues'）。
                for wsize in range(len(pieces) - 1, 1, -1):
                    sub = pieces[:wsize]
                    if not any(suspicious_fragment(p) for p in sub):
                        break
                    prefix = "".join(sub)
                    if len(prefix) >= 3 and known_simple(prefix):
                        replaced = prefix.upper() if all(p.isupper() for p in sub) else prefix
                        record_repair("merge_spaced_fragments_prefix", " ".join(sub), replaced)
                        consumed = i + 2 * wsize - 2  # 消费到前缀末尾
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
    """仅拆分非词典长串；接受前要求拆分后的每一段均为合法词/领域词。"""
    def repl(match: re.Match) -> str:
        token = match.group(0)
        # 低于 15 字符的串不动：双段复合词（whistleblower / workforce）常被
        # 词典漏收，短串误拆的风险大于收益，留给人工 OOV 复核。
        if known_simple(token) or len(token) < 15:
            return token
        internal_caps = sum(c.isupper() for c in token[1:])
        if internal_caps and len(token) < 25:
            return token
        parts = merge_domain_initialisms(wordninja.split(token))
        # 长串（≥20）允许双段拆分（如重复的 communicationcommunication）；
        # 更短的串须拆出 ≥3 段且每段均为真词，防止误拆复合词。
        enough_parts = len(parts) >= 3 or (len(parts) == 2 and len(token) >= 20)
        if not enough_parts or not all(known_simple(p) and len(p) >= 2 for p in parts):
            return token
        after = " ".join(parts)
        record_repair("split_concatenated_words", token, after)
        return after

    # 连字符两侧分别处理，真实连字符本身保留。
    return re.sub(r"[A-Za-z]{10,}", repl, text)


def repair_company_initialisms(text: str) -> str:
    """把被 PDF 字符层拆开的公司缩写重新组合。

    只针对已知公司名的字母序列，避免通用合并规则误伤（NIO F1 曾被并成 NI OF1）。
    """
    patterns = {
        "NIO": r"N\s*I\s*O", "BYD": r"B\s*Y\s*D",
        "XPENG": r"X\s*P\s*E\s*N\s*G", "ESG": r"E\s*S\s*G",
    }
    for target, pat in patterns.items():
        # 左右两侧都必须是「非字母」且不能紧邻孤立的单字母碎片。
        # 反例：PDF 把 unlocked 拆成 'u N I O ck ed'，若不检查左右语境，
        # 其中的 'N I O' 会被误合成 NIO，产出 'u NIO ck ed'。
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
    """只修经 PDF 视觉对照确认的提取空格错误，不修改作者原文拼写错误。"""
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
    """移除粘在正文词/年份后的上标脚注号，保护产品名、版本号和单位。

    PDF 上标在 NFKC 后会变成普通数字：`charging⁷`→`charging7`、
    `2030¹,²`→`20301,2`。不能简单删所有词后数字，否则会误伤 Drive48、
    Scope3、NADArch2.0 等。规则只处理当前语料中可验证的形态并记录前后值。
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
    # NIO p79 的 unlocked 被 PDF 字形稳定抽成 `uNI Ock ed`（l→I）；
    # 该精确形态在上下文中无歧义，先于公司缩写规则修复，避免误认成 NIO。
    text = re.sub(r"\bu\s*N\s*I\s*Ock\s*ed\b", "unlocked", text, flags=re.I)
    text = repair_company_initialisms(text)
    # 经 PDF 对照确认的精确规则必须先执行；否则通用 wordninja 会先把
    # partyreviewed→party reviewed、onlionly→on lion ly，原模式便无法匹配。
    text = apply_verified_spacing_corrections(text)
    text = re.sub(r"(?<=[,;:])(?=[A-Za-z])", " ", text)
    # 所有格/缩约后丢空格：children'swell-rounded、can'tdo → children's well-rounded、can't do
    text = re.sub(r"([A-Za-z]+['’](?:s|t|re|ve|ll|d|m))(?=[a-z])", r"\1 ", text, flags=re.I)
    text = merge_spaced_fragments(text)
    text = split_concatenated_words(text)
    # Wordninja 可能把 BYD 分成 BY + D；领域缩写在空格层再合并一次。
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
    """页眉页脚跨页比对模式：小写化、数字归一化、压缩标点空白。"""
    text = normalize_unicode(text).lower()
    text = re.sub(r"\d+", "#", text)
    text = re.sub(r"[^a-z#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def dehyphenate_join(lines: Iterable[str]) -> str:
    """把同一版式块内的视觉换行恢复为连续文本。"""
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
    """按 (y, x) 恢复 box 内的视觉阅读顺序。

    版式引擎返回的 textlines 未必按纵向位置排列。Tesla p74 的同一 box 内
    `Leveraging autonomy to`（y=220）排在了 `accelerate EV adoption`（y=267）
    之后，产出倒置的标题 `accelerate EV adoption Leveraging autonomy to`；
    p148 的 `Modern Slavery Statement.`（y=340）同样排到了引出它的
    `...see our`（y=317）之前。y 取整到 1pt，避免同一视觉行因浮点差异被拆序。
    """
    return sorted(lines, key=lambda l: (round(float((l.get("bbox") or [0, 0])[1])),
                                        float((l.get("bbox") or [0, 0])[0])))


def detect_column_groups(box: dict[str, Any], page_width: float) -> list[list[dict[str, Any]]]:
    """检测单个版式块内部是否含多栏，返回按栏分组的（重组）textlines。

    版式引擎偶尔把整片多栏区域并成一个 box（实测 XPeng 第 6 页、BYD 第 27 页），
    更严重的情形是它把左右两栏并进**同一条 textline**（实测 Lucid 第 4 页 CEO
    致辞：每行起点都是左栏起点 604，右栏内容作为该行的后续 span），此时按
    「行起点」分组永远无法分栏，最终产出
    `...ensuring a robust governance The next pages outline our first steps in our
    structure to support our efforts.` 这类跨栏交错，伪造词邻接。

    因此按 **span** 而非 textline 归栏：
    1. 把 box 内所有 span 投影到横轴，求 1pt 精度占用图；
    2. 取零占用带作为候选 gutter；
    3. 关键判据不是 gutter 宽度，而是有多少行**跨越**该 gutter（左右两侧同时
       有 span）。正常单栏段落右边界参差不齐，只会在右侧留白，不会反复出现
       「右侧另有文字块」；真正的分栏才会被多行跨越。故要求跨越行数 ≥3。
    4. 每栏内按 (y, x) 重排并重建 textline，各栏独立成文。

    宽度门槛因此可以放宽到 1.2% 页宽（Lucid p4 的 gutter 仅 26pt/1920=1.35%，
    旧的 4% 阈值会漏检），由跨越判据承担防误切职责。
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
    """从 span 坐标恢复行文本。

    PDF 中相邻 span 不一定自带尾空格。旧实现直接拼接，曾产生
    `after-salesservicenetwork...`。现在依据横向间距补空格，并记入修复日志。
    """
    return lines_from_textlines(box.get("textlines") or [])


def span_reading_order(span: dict[str, Any]) -> tuple[int, int, float]:
    """span 在 PDF 原始文本层中的阅读顺序键。

    版式引擎会把原本属于不同原始行的 span 并进同一条 textline。若只按 x 排序，
    这些 span 会互相插入。NIO p61 实测：同一 textline 内含

        (x= 48, block=1, line= 9) 'Assessment and'
        (x= 48, block=1, line=10) 'reporting period. This further unifies...'
        (x=106, block=1, line= 9) 'Management Process '
        (x=181, block=1, line= 9) 'during the '

    按 x 排序会把 line 10 插进 line 9 中间，产出
    `the Energy Assessment and reporting period.` /
    `This further unifies the factories' energy Management Process during the
    management processes...`——句子被拦腰交换。按 (block, line, x) 排序即恢复
    `Assessment and Management Process during the reporting period. This further
    unifies the factories' energy ...`。
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


# 明确的乱码信号：替换字符、C1 控制区、私用区
GARBLED_SIGNAL_RE = re.compile(r"[\ufffd\u0080-\u009f\ue000-\uf8ff\u0000-\u0008\u000b\u000c\u000e-\u001f]")
# 允许保留的非 ASCII：CJK、度数符号、常用标点、日式列表符号
ALLOWED_NON_ASCII_RE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\u00b0\u00b7\u2010-\u203a\u30fb\uff0c\u2022\u00a9\u00ae\u2122]")


def _garbled_token(token: str) -> bool:
    """判断单个空白分片是否为乱码。"""
    if GARBLED_SIGNAL_RE.search(token):
        return True
    chars = [c for c in token if not c.isspace()]
    if not chars:
        return False
    odd = [c for c in chars
           if ord(c) > 127 and not ALLOWED_NON_ASCII_RE.match(c) and not c.isdigit()]
    # 分片中异常非 ASCII 字符占比过半 → 乱码
    return len(odd) / len(chars) >= 0.5 and len(odd) >= 2


def strip_garbled_runs(text: str) -> str:
    """清除字体编码错乱产生的乱码片段。

    XPeng 报告部分页面使用非标准字体编码，提取出 `����` 或
    `\\x0eÇÀÀ\\x99Á...` 之类的片段。这类片段不是任何语言的文本，会污染词表；
    但同一行的正常英文与合法非 ASCII（°C、中文、‒）必须保留，
    故按空白分片逐片判断，而非整行删除或按字符类粗暴过滤。
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


# 已知合法的驼峰/大小写混排，不计入「块粘连」证据
CAMEL_WHITELIST_RE = re.compile(
    r"\b(?:XPeng|XPENG|iPhone|eVTOL|eV|iOS|kWh|MWh|GWh|tCO|gCO|CO2e|IoT|AIoT|"
    r"NIO|BYD|ESG|GRI|TCFD|IFRS|ISO|UNGC|CSRD|HKEx|SBTi|NCAP|R&D|"
    # 产品/品牌驼峰名：BYD SkyShuttle/SkyRail、Tesla Powerwall 等。
    # 不加会把 SkyShuttle 的 'yS' 当作块粘连，导致正常页被误判为版式失败页
    # （实测 BYD 第 120 页 10 处「粘连」全部来自 SkyShuttle）。
    r"SkyShuttle|SkyRail|SkyTrain|SkyPass|PowerWall|Powerwall|Megapack|Powerpack|"
    r"MegaCharger|Supercharger|CyberTruck|Cybertruck|SkyWell|BladeBattery|DiPilot|DiSus|"
    r"[A-Z][a-z]+(?:[A-Z][a-z]+)+|"          # 通用驼峰专名（两段以上）
    r"[A-Z]{2,}s?|[A-Z]\d+)\b"
)
BLOCK_GLUE_RE = re.compile(r"[a-z][A-Z]")
BULLET_RE = re.compile(r"[·•・▪●]")


def layout_failure_signals(texts: list[str]) -> dict[str, Any]:
    """检测「同一水平带上多个独立文本框被并入一行」的版式解析失败。

    两类证据：
    1. 多条目拼接：单句内出现 ≥2 个项目符号 —— 独立条目本应各自成句，
       出现在同一句说明不同文本框的内容被交错拼接（实测 XPeng 第 6 页）。
    2. 字符级粘连：'AreasManagement' 式小写紧接大写（驼峰专名已白名单排除）。
       注意该信号会被「按坐标补空格」步骤消除，故只作辅助证据，不能单独依赖。

    这类页面的词序与邻接关系不可信，会伪造搭配，不能进入分析语料。
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
    """判定阈值：多条目拼接是主判据，字符粘连为辅。

    要求至少 5 句以避免短页误判；比例 ≥15% 表明该页普遍性交错，
    而非个别条目内的合法多符号列举。
    """
    if sig["sentences"] >= 5 and sig["multi_bullet_sentences"] >= 2 and sig["multi_bullet_ratio"] >= 0.15:
        return True
    # Tesla p177 型：两栏被交错拼接后，同一段落部分重复，同时产生大量
    # 小写行首碎片；两信号组合在六份现有语料中只命中该页。
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
    # 乱码清除必须在所有入口统一执行，尤其是 table_cells；旧版仅在正文路径
    # 调用，导致 XPeng 表格单元格残留 73 个替换字符。
    return strip_garbled_runs(text)


def sentence_like(text: str) -> bool:
    """判断表格/图片 OCR 文本是否为叙述句，而非数据或标签。

    停用词密度只作为 prose-vs-label 判据，不作为语法完整性的唯一判据。
    """
    ws = words(text)
    if len(ws) < 3 or alpha_ratio(text) < 0.58:
        return False
    terminal = bool(re.search(r"[.!?;:]\s*$", text))
    has_function_words = stopword_density(text) >= 0.16
    # 3-4 词短块必须有句末标点和功能词；更长块满足其一即可。
    if len(ws) < 5:
        return terminal and has_function_words
    return has_function_words or terminal


TABLE_TERMINAL_RE = re.compile(r"[.!?;:][\"')\]]?$")


def table_prose_sentences(text: str) -> list[str]:
    """从表格单元格中只保留完整叙述句，排除数字、指标名和标签。

    用户规则是「删表格数据，保留完整句子」。旧版只看停用词密度，导致
    `Package material consumption for whole vehicle` 因含 for 而误入 no_tables。
    现在必须同时满足：≥5词、句末标点、停用词密度≥0.12、字母占比≥0.58。
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
    """正文按句输出；标题单独保留。"""
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
    """仅 OCR 指定版式区域；返回全文、段落、置信度与词数。

    Tesseract 的 TSV 给出每个词的 block / paragraph / line 与坐标。除总体文本外，
    这里保留段落边界；后续只从高置信度段落中选完整叙述句，避免把标题、目录、
    图表刻度与正文拼成一条假句。
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
    """删除因 box 边界重叠而产生的截断前缀碎片。

    PyMuPDF4LLM 的 box 划分偶尔重叠：BYD p27 的 BOX20 末行捕获了
    `With these updates, we have formulated the`（该句左栏首行），而 BOX22
    又完整给出同一句两行。分栏后前者成为悬挂碎片，与完整句构成重复。

    判据取三重交集，确保不误删标题（标题短且不以功能词结尾）：
    1. 词数 ≥5；
    2. 不以句末标点收尾，且以悬挂功能词结尾；
    3. 是同页另一条更长文本的前缀。
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
    """只执行经 PDF 视觉对照确认、且由文档与页码共同限定的精确修复。"""
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
    """保守去重：短标题出现在长句中，不得据此删除整句。

    旧判据只要短串是长串子串就判重复，曾把包含 `32 million metric` 的完整
    OCR 正文整句删除。现在要求子串覆盖较短/较长文本长度比 ≥70%，或整体
    SequenceMatcher 相似度 ≥90%。
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
    """修正常见、可无歧义判定的 OCR 字形错误；全部修复仍会进入 repairs 日志。"""
    before = text
    # Tesla 字体中的「2」反复被识别成 z / c / q：COze、COcze、COQ ze → CO2e
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
    # Tesla p187 中 useful 被稳定识别为 `use tul`；该搭配在此语境无歧义。
    text = re.sub(r"\buse\s+tul\b", "useful", text, flags=re.I)
    # 清除图表 OCR 产生的孤立竖线与「3:」式坐标噪声，不碰正常数字。
    text = re.sub(r"\s+[|_=<>]+\s+", " ", text)
    text = re.sub(r"\s+\d:\s+", " ", text)
    text = clean_plain_text(text)
    if text != before:
        record_repair("clean_ocr_artifacts", before[:150], text[:150])
    return text


def strict_ocr_sentences(result: dict[str, Any], existing: list[str]) -> list[str]:
    """从 OCR 区域中只保留高置信度的完整叙述句。

    标题、目录、图例和数字表即使 OCR 置信度很高也不进入语料。阈值为项目预设
    质控线：段落均值置信度 ≥91.5、句末标点、≥8 词、停用词密度 ≥0.15、
    字母字符占比 ≥0.82、数字词占比 ≤25%。0.15 是为保留含大量专名的技术句
    （如 NEDC/WLTP/EPA 句实测密度 0.171）；其余四道条件继续阻断图表标签。
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
    """页面是否含覆盖 ≥80% 的位图背景（Tesla 混合 PDF 的栅格正文载体）。"""
    page_area = max(1.0, page.rect.width * page.rect.height)
    for image in page.get_images(full=True):
        try:
            if any(rect.width * rect.height / page_area >= 0.80 for rect in page.get_image_rects(image[0])):
                return True
        except Exception:
            continue
    return False


def stitch_across_boxes(items: list[str]) -> list[str]:
    """把跨版式块被切断的同一句子重新接上。

    版式引擎会把一段正文按视觉区域切成多个 box，分句在 box 内独立进行，
    于是 `...which has laid a solid` 与 `foundation for NIO...` 会成为两句。
    判据：前句无句末标点，后句以小写字母开头；标题类短行不参与。
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
        # 标题须短且大部分实词为 Title Case/全大写；不能仅按词数判断，
        # 否则 `The service model laid a solid`（6词句子碎片）会被误当标题。
        prev_is_heading = (
            len(heading_words) <= 10
            and (titlecase_ratio >= 0.60 or (prev.isupper() and len(heading_words) >= 2))
        )
        # 标题跨 box 换行：XPeng p27 的 `Rights Protection and` 与
        # `Infringement Dispute Response` 是同一 block 的相邻 box，后句首字母
        # 大写故不满足 next_lower。以「前句悬挂功能词结尾」为触发信号——正常
        # 标题不会以 and/of/for 收尾，该形态本身即断裂证据。
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
    """找跨页重复的顶部/底部版式块，返回要删除的 (page_idx, box_idx)。"""
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
    """读取经两次核验批准的人工覆盖页；草稿永远不会自动进入语料。

    目录约定：
      01_Source/manual_overrides/approved/<doc_id>/p024_with_tables.txt
      01_Source/manual_overrides/approved/<doc_id>/p024_no_tables.txt
      01_Source/manual_overrides/approved/<doc_id>/p024_approval.json

    approval.json 必须明确 approved=true，且两个版本文件都存在。no_tables 文件可为空
    （如 NIO p32 只有矩阵标签）；with_tables 应包含按单元格/阅读顺序恢复的文本。
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
        raise ValueError(f"人工覆盖 metadata 不匹配: {approval_file}")
    if not with_file.exists() or not no_file.exists():
        raise FileNotFoundError(f"已批准覆盖缺少文本文件: {folder}/{prefix}_*.txt")

    def load_lines(path: Path) -> list[str]:
        text = normalize_unicode(path.read_text(encoding="utf-8"))
        if "�" in text:
            raise ValueError(f"人工覆盖含替换字符: {path}")
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
    # 禁用自动 OCR：原生文本绝不重复 OCR；需要 OCR 的图片区域由本脚本局部处理。
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
                # 唯一且未进入 repeated_remove 的页眉/页脚仍删除，但完整写入日志。
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
                # Tesla 先 OCR 布局引擎明确圈出的图片区（p5 的两段正文可精准裁剪）；
                # 低文本整页稍后另走三栏 OCR。两路结果用保守模糊去重合并。
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

            # 版式块内部可能含多栏（引擎偶发合并），按栏切分后各栏独立成文，
            # 否则不同栏的行会交错拼接、伪造词邻接关系。
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
                # 多栏：每栏单独走后续处理，不拼成一行
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
            # 英文语料：高比例非拉丁字符且几乎无英文词的块移除。
            if len(words(text)) < 2 and alpha_ratio(text) < 0.45:
                page_log["non_english_or_noise_removed"] += 1
                continue
            # 图例/图片内导航残留：@ 与 © 是 Tesla 等报告把图标符号编进文本层的产物，
            # 一行出现 ≥2 个即为「@标签1 @标签2」式图例，不是正文。
            if text.count("@") + text.count("©") >= 2:
                page_log["symbol_label_lines_removed"] += 1
                continue
            heading = cls in {"section-header", "caption"} and len(words(text)) <= 20
            segs = segment_paragraph(text, heading=heading)
            with_parts.extend(segs)
            no_parts.extend(segs)
            page_log[f"{cls}_boxes_kept"] += 1

        # Tesla 2024 是混合型 PDF：几乎每页有一张整页位图，部分正文只存在于
        # 位图中而不在原生文本层。局部 picture-box OCR 会漏掉这些正文（实测
        # p10/p20/p26/p28/p54/p66/p106/p108/p167/p187）。对原生文本不足 500
        # 字符且有整页位图的页面，按左/中/右三栏裁剪 OCR，避免整页 OCR 把三栏
        # 横向交错。只把 strict_ocr_sentences 通过的完整高置信度句子补入。
        if (
            config.ocr_mode == "all_picture_regions"
            and native_chars[pi] < 500
            and page_has_full_background_image(doc[pi])
        ):
            W, H = doc[pi].rect.width, doc[pi].rect.height
            column_ranges = ((0.00, 0.345), (0.325, 0.675), (0.655, 1.00))
            for column_no, (left, right) in enumerate(column_ranges, 1):
                bbox = (W * left, H * 0.04, W * right, H * 0.96)
                # PSM 3 会按自然段分组；实测在 Tesla p54 的双段正文上明显优于
                # PSM 6（后者把图片噪声插进正文）与 PSM 11（逐行碎裂）。
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

        # 页面内部精确去重：布局引擎有时会把同一文本层和图片 OCR 同时返回。
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

        # 版式解析失败页的判定与隔离：
        # 若一页的「块粘连密度」过高，说明引擎把多个独立文本框并入同一行，
        # 该页文本的词序与邻接已不可信（会伪造搭配），不能进入分析语料。
        # 这类页面从语料中排除，但完整记入日志与单独清单，供人工复核，
        # 不做静默丢弃。
        signals = layout_failure_signals(no_parts)
        # 人工覆盖不仅用于自动判定为版式失败的页面，也用于普通抽检发现的
        # 局部漏字、错序、脚注污染或表格单元格串行。只要 approved metadata
        # 合法，即以人工核验版本替换整页；否则才按自动失败判据决定是否隔离。
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
    # Counter 不能直接 JSON 化。
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
            raise SystemExit(f"未知 doc_id: {args.doc}")
        return selected
    return [d for d in DOCUMENTS if d.set_name == args.set]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("primary", "robustness"), default="primary")
    ap.add_argument("--doc", help="只处理指定 doc_id")
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
