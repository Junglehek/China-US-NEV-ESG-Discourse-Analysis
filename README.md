# China-US-NEV-ESG-Discourse-Analysis

This repository contains the code we used to build the corpus for a study of how Chinese and U.S. electric carmakers write about environmental, social and governance (ESG) issues in their English sustainability reports. The study compares six companies: BYD, NIO and XPeng from China, and Tesla, Rivian and Lucid from the United States.

## Why we publish the code

Sustainability reports are made to be read as PDF files, not to be analysed as text. Their pages have several columns, running headers, navigation bars, tables and text inside images, and a simple conversion mixes all of this into the text. Since our findings depend on how the reports were turned into plain text, we want readers to be able to see exactly what was done and to rebuild the corpus themselves.

We do not include the reports or the texts extracted from them, because they reproduce the companies' copyrighted material. `SOURCES.md` lists where each report can be downloaded and gives a checksum, so you can make sure you have the same file we used.

## What is in the repository

- `02_Pipeline/scripts/` holds the preparation pipeline (`prepare_corpus.py`) and the scripts that measure and audit the quality of the output.
- `02_Pipeline/tests/` holds 33 tests. Each one comes from an error we actually ran into while cleaning the reports.
- `03_QA/diagnostics/` holds two early scripts we used to find headers, footers and pages with several columns.
- `SOURCES.md` lists the eleven reports, their official web pages and their checksums.

## How the pipeline works

Each page is handled in six steps. The text is first extracted in its reading order, using the layout analysis of PyMuPDF and PyMuPDF4LLM, so that sentences from neighbouring columns are not mixed. Broken lines and words are then repaired. Words that a PDF stores as separate letters are joined again, and words that run together are split with wordninja, after a check against an English word list and a short list of terms from the reports, such as BYD and CO2e. Running headers and footers are recognized by their fixed position at the top or bottom of the page and by the fact that they repeat across pages, and they are removed together with the tables of contents.

Data tables are kept apart from the running text. We write two versions of every report: one with the tables and one without them. Only the version without tables is used in the analysis, but complete sentences found inside table cells are kept in it. Finally, text that exists only as images, which is common in the Tesla reports, is recognized with Tesseract, and a few simple measures are computed for every page. Pages with unusual values are checked by hand.

Some problems cannot be solved by rules. We checked sample pages against the original PDFs and replaced twenty pages of the main corpus with corrected text. These corrections contain report text, so they are not included here, and a rerun without them will differ on those pages.

## Tools

| Tool | Version | What we used it for |
|---|---|---|
| Python | 3.14.5 | All scripts |
| PyMuPDF and PyMuPDF4LLM | 1.28.2 | Reading the PDFs and analysing the page layout |
| wordninja | 2.0.0 | Splitting words that run together |
| pysbd | 0.3.4 | Splitting table and image text into sentences |
| Tesseract | 5.5.2 | Recognizing text in images (English model) |
| pdfplumber | 0.11.10 | The early diagnostic scripts |

The exact versions of all Python packages are listed in `02_Pipeline/requirements-lock.txt`.

## Rebuilding the corpus

```bash
git clone https://github.com/Junglehek/China-US-NEV-ESG-Discourse-Analysis.git
cd China-US-NEV-ESG-Discourse-Analysis
python3 -m venv venv && source venv/bin/activate
pip install -r 02_Pipeline/requirements-lock.txt
```

You also need Tesseract (on macOS, `brew install tesseract`). Download the reports listed in `SOURCES.md` into `01_Source/pdf/2024_main/` and `01_Source/pdf/2025_robustness/`, and then run:

```bash
python 02_Pipeline/scripts/prepare_corpus.py --set primary       # the six main reports
python 02_Pipeline/scripts/prepare_corpus.py --set robustness    # the 2025 reports
python 02_Pipeline/scripts/quality_metrics.py --set primary
python -m unittest discover -s 02_Pipeline/tests -p "test_*.py"  # the 33 tests
```

To decide whether a string is a real word, the scripts use the system word list at `/usr/share/dict/words`. On macOS this is Webster's Second International Dictionary, with about 235,000 words. Most Linux systems provide a similar list in the same place.

## Licence

The code is released under the MIT Licence (see `LICENSE`).
