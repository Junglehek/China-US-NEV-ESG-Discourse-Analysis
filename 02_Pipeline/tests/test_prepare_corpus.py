#!/usr/bin/env python3
"""Regression tests for the preparation pipeline.

Each test comes from an error that was actually found during the audits. Run them after every change to the cleaning rules:
    cd <repository root>
    02_Pipeline/venv/bin/python -m unittest discover -s 02_Pipeline/tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import prepare_corpus as p


class SpacingRepairTests(unittest.TestCase):
    def test_fragmented_words_are_rejoined(self):
        cases = {
            "found atio n": "foundation",
            "n e t w o r k": "network",
            "w ho pursues": "who pursues",
            "h a s p u b l i s h e d": "has published",
            "after-salesservicenetworkprovidesconsiderate": "after-sales service network provides considerate",
            "stakeholdersinvestorsgovernment": "stakeholders investors government",
            "communicationcommunication": "communication communication",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(p.repair_internal_spacing(source), expected)

    def test_normal_phrases_are_not_merged(self):
        for source in ("in to the market", "a lot of people", "as well as", "up to 30 percent"):
            with self.subTest(source=source):
                self.assertEqual(p.repair_internal_spacing(source), source)

    def test_real_compounds_are_not_split(self):
        for source in ("Whistleblower", "innovatively", "deconstructing", "workforce", "feedback", "decarbonization"):
            with self.subTest(source=source):
                self.assertEqual(p.repair_internal_spacing(source), source)

    def test_pdf_verified_spacing_errors(self):
        cases = {
            "AppendixII": "Appendix II",
            "decisionmaking": "decision making",
            "nextgeneration": "next generation",
            "calendaryear": "calendar year",
            "emissionsfirst": "emissions-first",
            "scaleand": "scale and",
            "third partyreviewed study": "third party-reviewed study",
            "wideranging engagement": "wide-ranging engagement",
            "wouldbe buyers": "would-be buyers",
            "transparent andopen bridge": "transparent and open bridge",
            "onlionly ne training": "online training",
            "witha rollover risk": "with a rollover risk",
            "Cyber truck achieved": "Cybertruck achieved",
            "Cyber trucks with Star link": "Cybertrucks with Starlink",
            "probability of roll over": "probability of rollover",
            "Moro wali area": "Morowali area",
            "diagnosis-analysisimprovement": "diagnosis-analysis-improvement",
            "different scenarios-smaller-capacity batteries": "different scenarios - smaller-capacity batteries",
            "vehiclecharger-station-cloud-grid synergy": "vehicle-charger-station-cloud-grid synergy",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(p.repair_internal_spacing(source), expected)

    def test_contractions_and_possessives_get_missing_space(self):
        cases = {
            "That'sthe same": "That's the same",
            "children'swell-rounded": "children's well-rounded",
            "Tesla'smission": "Tesla's mission",
            "can'tdo this": "can't do this",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(p.repair_internal_spacing(source), expected)


class ProtectedEntityTests(unittest.TestCase):
    def test_company_names_and_model_codes(self):
        cases = {
            "NI O F2 and EDS Factory": "NIO F2 and EDS Factory",
            "N I O has established": "NIO has established",
            "NIO F1 dropped": "NIO F1 dropped",
            "B Y D h a s published": "BYD has published",
            "X P E N G X9": "XPENG X9",
            "E S G report": "ESG report",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(p.repair_internal_spacing(source), expected)

    def test_unlocked_does_not_become_nio(self):
        # The character layer of one PDF really did turn "unlocked" into "uNI Ock ed" (l read as I).
        result = p.repair_internal_spacing("doors automatically uNI Ock ed")
        self.assertEqual(result, "doors automatically unlocked")
        self.assertNotIn("NIO", result)

    def test_co2e_does_not_absorb_following_in(self):
        self.assertEqual(p.clean_ocr_artifacts("COze in 2024."), "CO2e in 2024.")
        self.assertEqual(p.clean_ocr_artifacts("CO2e in 2024."), "CO2e in 2024.")
        self.assertNotIn("CO2ein", p.clean_ocr_artifacts("CO2e in 2024."))

    def test_tesla_2025_ocr_paragraphs_stitched_before_sentence_filtering(self):
        result = {"paragraphs": [
            {"text": "In 2025, we continued to develop our involvement into intermediates",
             "confidence": 96.0, "words": 9},
            {"text": "production with the goal of improving circularity within our battery supply chain.",
             "confidence": 96.0, "words": 12},
            {"text": "At Tesla, recycling has four phases: 1) collection, 2) mechanical processing, 3) refining and 4) production of new batteries.",
             "confidence": 96.0, "words": 20},
        ]}
        accepted = p.tesla_2025_ocr_sentences(result, [])
        self.assertIn("In 2025, we continued to develop our involvement into intermediates production with the goal of improving circularity within our battery supply chain.", accepted)
        self.assertTrue(any("1) collection" in text and "4) production" in text for text in accepted))
        p87 = {"paragraphs": [
            {"text": "Model Y is the first vehicle to pass the National Highway Traffic Safety",
             "confidence": 95.6, "words": 13},
            {"text": "Administration's new Advanced Driver Assistance System tests.",
             "confidence": 94.0, "words": 8},
            {"text": "All vehicles met four new tests:", "confidence": 96.0, "words": 6},
            {"text": "Pedestrian automatic emergency braking", "confidence": 96.0, "words": 4},
            {"text": "Lane-keeping assistance", "confidence": 96.0, "words": 2},
        ]}
        p87_accepted = p.tesla_2025_ocr_sentences(p87, [])
        self.assertTrue(any("Traffic Safety Administration's" in text for text in p87_accepted))
        self.assertTrue(any(text.endswith("Lane-keeping assistance.") for text in p87_accepted))

    def test_tesla_2025_native_heading_filter_rejects_garbled_fragment(self):
        def box(y, text):
            return {"x0": 30, "x1": 400, "y0": y, "y1": y + 20,
                    "textlines": [{"bbox": [30, y, 400, y + 20], "spans": [
                        {"text": text, "size": 22, "bbox": [30, y, 400, y + 20],
                         "block": 1, "line": 1}]}]}
        page = {"boxes": [box(100, "Supplier Responsibility audit data"), box(140, "ce ta at")]}
        self.assertEqual(p.tesla_2025_native_headings(page, 200),
                         ["Supplier Responsibility audit data"])

    def test_tesla_2025_body_top_follows_large_heading(self):
        page = {"boxes": [{"textlines": [{"spans": [
            {"text": "Heading", "size": 40, "bbox": [30, 147, 500, 220]},
            {"text": "Body", "size": 22, "bbox": [30, 423, 300, 445]},
        ]}]}]}
        self.assertEqual(p.tesla_2025_body_top(page), 245.0)

    def test_byd_p69_inline_graphic_labels_removed_by_exact_page_rule(self):
        source = [
            "We improve product life and Technology Based, Innovation Oriented significantly reduce energy consumption.",
            "At the BYD Dream Day introduces new technologies same time, we improve design.",
        ]
        self.assertEqual(p.apply_verified_page_corrections("byd_2024", 69, source), [
            "We improve product life and significantly reduce energy consumption.",
            "At the same time, we improve design.",
        ])
        self.assertEqual(p.apply_verified_page_corrections("byd_2024", 68, source), source)

    def test_textlines_sorted_by_vertical_position(self):
        """Tesla p74/p148: text lines inside a box were not sorted by height, so a heading and its first sentence swapped places."""
        def textline(y, text):
            return {"bbox": [40, y, 300, y + 14],
                    "spans": [{"text": text, "bbox": [40, y, 300, y + 14], "size": 12,
                               "block": 1, "line": 0}]}
        box = {"x0": 40, "x1": 300, "textlines": [textline(267, "accelerate EV adoption"),
                                                  textline(220, "Leveraging autonomy to ")]}
        groups = p.detect_column_groups(box, 800.0)
        self.assertEqual([l.strip() for l in p.lines_from_textlines(groups[0])],
                         ["Leveraging autonomy to", "accelerate EV adoption"])

    def test_heading_split_across_boxes_is_rejoined(self):
        self.assertEqual(
            p.stitch_across_boxes(["Rights Protection and", "Infringement Dispute Response"]),
            ["Rights Protection and Infringement Dispute Response"])
        self.assertEqual(
            p.stitch_across_boxes(["Waste Generated in", "Manufacturing (metric tons)"]),
            ["Waste Generated in Manufacturing (metric tons)"])
        # Neighbouring headings that do not end in a function word must not be joined.
        for pair in (["Energy Management", "We manage energy carefully."],
                     ["Governance", "Economic performance"],
                     ["Our approach to safety", "Product Safety"]):
            with self.subTest(pair=pair):
                self.assertEqual(p.stitch_across_boxes(pair), pair)

    def test_spans_ordered_by_original_reading_order_not_x(self):
        """NIO p61: one text line mixed spans from different source lines, and sorting by x alone interleaved them."""
        def span(x, block, line, text):
            return {"text": text, "bbox": [x, 100, x + 40, 112], "block": block,
                    "line": line, "size": 8}
        textline = {"bbox": [48, 100, 240, 112], "spans": [
            span(48, 1, 9, "Assessment and "),
            span(48, 1, 10, "reporting period. This further unifies the factories' energy "),
            span(106, 1, 9, "Management Process "),
            span(181, 1, 9, "during the "),
        ]}
        result = p.lines_from_textlines([textline])[0]
        self.assertIn("Assessment and Management Process during the reporting period.", result)
        self.assertNotIn("Energy Assessment and reporting period", result)

    def test_truncated_prefix_fragment_dropped_but_headings_kept(self):
        full = "With these updates, we have formulated the following list of material ESG topics for 2024."
        frag = "With these updates, we have formulated the"
        self.assertEqual(p.drop_truncated_prefix_duplicates([frag, "01 Outlining", full]),
                         ["01 Outlining", full])
        # The heading is a prefix of the next sentence, but it is short and does not end in a function word, so it must stay.
        kept = ["Energy Management", "Energy Management is central to our operations."]
        self.assertEqual(p.drop_truncated_prefix_duplicates(kept), kept)
        # It ends in a function word but is not the prefix of any sentence, so it must stay.
        lone = ["We continue to invest in the", "A separate unrelated sentence."]
        self.assertEqual(p.drop_truncated_prefix_duplicates(lone), lone)

    def test_out_of_dictionary_proper_names_are_not_refragmented(self):
        # Lucid p4: Rawlinson is not in the dictionary. It was once joined to the previous word as PeterRawlinson
        # and then split again by wordninja into 'Peter Raw lins on'.
        self.assertEqual(p.clean_plain_text("Peter Rawlinson"), "Peter Rawlinson")
        self.assertEqual(p.clean_plain_text("A Message From Peter Rawlinson"),
                         "A Message From Peter Rawlinson")
        # Real fragments must still be repaired.
        self.assertEqual(p.clean_plain_text("found atio n"), "foundation")
        self.assertEqual(p.clean_plain_text("Moro wali"), "Morowali")

    def test_columns_split_by_span_when_engine_merges_two_columns_into_one_line(self):
        """Lucid p4 pattern: two columns merged into one text line, all lines start at the same x, and the gutter is only 1.35% of the page width."""
        def line(y, segments):
            return {"bbox": [segments[0][0], y, segments[-1][1], y + 12],
                    "spans": [{"text": t, "bbox": [a, y, b, y + 12]} for a, b, t in segments]}
        lines = [line(100 + i * 20, [(604, 1100, f"left column line {i} "),
                                     (1191, 1600, f"right column line {i} ")])
                 for i in range(10)]
        box = {"x0": 604, "x1": 1641, "textlines": lines}
        groups = p.detect_column_groups(box, 1920.0)
        self.assertEqual(len(groups), 2)
        left = p.lines_from_textlines(groups[0])
        right = p.lines_from_textlines(groups[1])
        self.assertTrue(all("left column" in l for l in left))
        self.assertTrue(all("right column" in l for l in right))

    def test_single_column_ragged_right_edge_is_not_split(self):
        """An ordinary single-column paragraph with a ragged right edge must not be split into columns."""
        def line(y, x1, text):
            return {"bbox": [100, y, x1, y + 12],
                    "spans": [{"text": text, "bbox": [100, y, x1, y + 12]}]}
        widths = [1500, 1180, 1490, 1200, 1470, 1150, 1495, 1210, 1480, 1160]
        lines = [line(100 + i * 20, w, f"ordinary prose line {i} ") for i, w in enumerate(widths)]
        box = {"x0": 100, "x1": 1500, "textlines": lines}
        self.assertEqual(len(p.detect_column_groups(box, 1920.0)), 1)

    def test_inline_footnotes_removed_but_versions_and_models_protected(self):
        cases = {
            "surveys7 assess": "surveys assess",
            "company5.": "company.",
            "factories12 in total": "factories in total",
            "by 20301,2": "by 2030",
            "charging⁷": "charging",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(p.clean_plain_text(source), expected)
        for source in ("Drive48", "Ecoinvent3.8", "kgCO2", "NADArch2.0", "SEPA2.0", "Scope3", "Industry4.0", "Model3"):
            with self.subTest(protected=source):
                self.assertEqual(p.clean_plain_text(source), source)


class GarbleAndLayoutTests(unittest.TestCase):
    def test_garble_removed_but_legitimate_unicode_kept(self):
        self.assertNotIn("�", p.strip_garbled_runs("Normal text ��������"))
        # The last string is Chinese on purpose: it checks that legitimate non-ASCII text, such as a Chinese source note, is not removed as garbled.
        for source in ("temperature below 1.5°C", "・NIO governance", "NEV‒APEAL Study", "数据来源：中国汽车工业协会"):
            with self.subTest(source=source):
                self.assertEqual(p.strip_garbled_runs(source), source)

    def test_infographic_interleaving_is_flagged(self):
        bad = [
            "· Product Safety: 5-star rating · Governance Enhancement: independent directors",
            "· Circular Economy: battery reuse · Employee Engagement: training programs",
            "· Climate Action: renewable electricity · Supply Chain: supplier audits",
            "normal supporting sentence one.",
            "normal supporting sentence two.",
            "normal supporting sentence three.",
        ]
        self.assertTrue(p.is_layout_failure(p.layout_failure_signals(bad)))

    def test_interleaved_duplicate_columns_are_flagged(self):
        # Tesla p177 pattern: two columns interleaved within one paragraph, giving near-duplicate sentences and several fragments that start in lower case.
        bad = [
            "Phlogopite mica",
            "Phlogopite mica is used as a thermal barrier for various applications in our products.",
            "It is relatively abundant, but the industry is opaque and has associated risks.",
            "Phlogopite mica is used as a thermal barrier for various materials in our products.",
            "associated human rights risks are often a consequence of systemic challenges.",
            "chain and uncovered broad contextual challenges including poverty and climate change.",
            "allows us to better communicate our requirements to the upstream supply chain.",
            "mandating that suppliers source from selected processors.",
        ]
        self.assertTrue(p.is_layout_failure(p.layout_failure_signals(bad)))

    def test_camelcase_brand_page_is_not_flagged(self):
        good = [
            "We launched SkyRail and SkyShuttle to ease congestion.",
            "The BYD Xi'an SkyShuttle spans approximately 17 kilometers.",
            "Powerwall and Megapack support renewable electricity.",
        ]
        self.assertFalse(p.is_layout_failure(p.layout_failure_signals(good)))


class BoundaryAndDedupTests(unittest.TestCase):
    def test_cross_box_sentence_is_stitched(self):
        source = [
            "The service model laid a solid",
            "foundation for NIO's international expansion.",
        ]
        result = p.stitch_across_boxes(source)
        self.assertEqual(result, ["The service model laid a solid foundation for NIO's international expansion."])

    def test_heading_is_not_stitched_to_lowercase_text(self):
        source = ["Climate Action", "reducing emissions across the value chain."]
        self.assertEqual(p.stitch_across_boxes(source), source)

    def test_short_substring_does_not_delete_long_sentence(self):
        candidate = "The fleet avoided nearly 32 million metric tons of CO2e in 2024."
        self.assertFalse(p.likely_duplicate(candidate, ["32 million metric"]))

    def test_exact_and_near_duplicates_are_detected(self):
        source = "We reduced emissions by 30 percent in 2024."
        self.assertTrue(p.likely_duplicate(source, [source]))
        self.assertTrue(p.likely_duplicate(source, ["We reduced emissions by 30 percent during 2024."]))


class TableFilteringTests(unittest.TestCase):
    def test_table_label_is_removed_from_no_tables(self):
        self.assertEqual(p.table_prose_sentences("Package material consumption for whole vehicle"), [])
        self.assertEqual(p.table_prose_sentences("Tonne of CO�/ CNY (thousand)"), [])

    def test_complete_table_sentence_is_kept(self):
        sentence = "We require all suppliers to comply with our Supplier Code of Conduct."
        self.assertEqual(p.table_prose_sentences(sentence), [sentence])


class ManualOverrideTests(unittest.TestCase):
    def test_only_explicitly_approved_override_is_loaded(self):
        config = p.DocumentConfig("demo", "demo.pdf", "primary", "China", 2024, ())
        with tempfile.TemporaryDirectory() as td, patch.object(p, "MANUAL_OVERRIDE_ROOT", Path(td)):
            folder = Path(td) / "demo"
            folder.mkdir()
            (folder / "p007_with_tables.txt").write_text("Heading\nAll table text\n", encoding="utf-8")
            (folder / "p007_no_tables.txt").write_text("A complete narrative sentence.\n", encoding="utf-8")
            approval = {"doc_id": "demo", "page": 7, "approved": False, "verified_by": "tester"}
            (folder / "p007_approval.json").write_text(json.dumps(approval), encoding="utf-8")
            self.assertIsNone(p.load_manual_override(config, 7))
            approval["approved"] = True
            (folder / "p007_approval.json").write_text(json.dumps(approval), encoding="utf-8")
            loaded = p.load_manual_override(config, 7)
            self.assertEqual(loaded["with_tables"], ["Heading", "All table text"])
            self.assertEqual(loaded["no_tables"], ["A complete narrative sentence."])


class OcrQualityTests(unittest.TestCase):
    def test_high_confidence_prose_is_kept(self):
        result = {
            "paragraphs": [{
                "text": "We reduced greenhouse gas emissions across our global value chain in 2024.",
                "confidence": 96.0,
                "words": 12,
            }]
        }
        self.assertEqual(len(p.strict_ocr_sentences(result, [])), 1)

    def test_chart_label_and_low_confidence_noise_are_rejected(self):
        chart = {"paragraphs": [{"text": "Nickel Lithium Cobalt 20% 40% 60% 80%", "confidence": 97.0, "words": 7}]}
        noise = {"paragraphs": [{"text": "This appears to be a sentence but OCR confidence is poor.", "confidence": 60.0, "words": 11}]}
        self.assertEqual(p.strict_ocr_sentences(chart, []), [])
        self.assertEqual(p.strict_ocr_sentences(noise, []), [])


if __name__ == "__main__":
    unittest.main()
