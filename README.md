# China-US-NEV-ESG-Discourse-Analysis

Corpus preparation code for a discourse analysis of the ESG reports of Chinese and U.S. new energy vehicle manufacturers.

Code for preparing the corpus used in a corpus-based discourse analysis of the English sustainability (ESG) reports of six new energy vehicle manufacturers: BYD, NIO and XPeng (China), and Tesla, Rivian and Lucid (United States).

The repository contains the preprocessing pipeline that turns the PDF reports into plain text for Sketch Engine, the quality checks, and the rule tests. The reports themselves, the extracted texts and the manually corrected pages are not included, because they reproduce the companies' copyrighted reports. `SOURCES.md` lists the official web pages and the checksums of the files used.

## What the pipeline does

Each page goes through six steps:

1. Extract the text in reading order (PyMuPDF and PyMuPDF4LLM layout analysis).
2. Repair broken lines, words split into letters and words run together (rules plus wordninja, checked against an English word list and a short list of domain terms).
3. Remove running headers and footers (text in a fixed position at the top or bottom of the page that repeats across pages).
4. Remove tables of contents.
5. Separate data tables from the running text. Two versions are written: `with_tables` keeps all cell text, `no_tables` keeps only complete sentences found in cells. Only `no_tables` is used for analysis.
6. Recognize text in image regions with Tesseract (only where a page has no text layer for that region) and compute quality measures for every page. Pages with unusual values are checked by hand.

Pages that the scripts could not handle were checked against the original PDFs. Twenty pages of the main corpus were replaced with manually corrected text approved by the researcher. These corrections are not distributed, so a rerun without them will differ on those pages.

## Tools and versions

| Tool | Version | Use |
|---|---|---|
| Python | 3.14.5 | All scripts |
| PyMuPDF, PyMuPDF4LLM | 1.28.2 | Reading PDFs, layout analysis, table and image regions |
| wordninja | 2.0.0 | Splitting words that were run together |
| pysbd | 0.3.4 | Splitting table and image text into sentences |
| Tesseract | 5.5.2 | Optical character recognition (English model) |
| pdfplumber | 0.11.10 | Early diagnostic scripts in `03_QA/diagnostics/` |

Exact Python package versions are in `02_Pipeline/requirements-lock.txt`.

## How to rerun

```bash
git clone https://github.com/Junglehek/China-US-NEV-ESG-Discourse-Analysis.git
cd China-US-NEV-ESG-Discourse-Analysis
python3 -m venv venv && source venv/bin/activate
pip install -r 02_Pipeline/requirements-lock.txt
# install Tesseract, e.g. `brew install tesseract` on macOS
# download the reports listed in SOURCES.md into 01_Source/pdf/

python 02_Pipeline/scripts/prepare_corpus.py --set primary      # main corpus (6 reports)
python 02_Pipeline/scripts/prepare_corpus.py --set robustness   # 2025 reports
python 02_Pipeline/scripts/quality_metrics.py --set primary
python -m unittest discover -s 02_Pipeline/tests -p "test_*.py"  # 33 rule tests
```

The word list used to judge whether a string is a real word is the system dictionary at `/usr/share/dict/words` (on macOS this is Webster's Second International, about 235,000 words). Linux systems usually provide a similar list in the same place.

Some comments in the code are still in Chinese and are being translated.

## Licence

MIT, see `LICENSE`.
