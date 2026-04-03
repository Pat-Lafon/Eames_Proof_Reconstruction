# Eames Proof Reconstruction

## Project Overview

Research project on proof reconstruction. The report is a LaTeX document covering related work in this area.

## Repository Structure

- `report/` — LaTeX report
  - `main.tex` — Main document, includes sections from `sections/`
  - `sources.txt` — List of paper identifiers (one per line: `arxiv:ID`, `doi:ID`, or `file:manual/name.bib`)
  - `references.bib` — **Auto-generated** from `sources.txt`, do not edit manually
  - `manual/` — Hand-written `.bib` entries for papers without DOIs or arXiv IDs
  - `figures/` — Images and diagrams
  - `sections/` — Section `.tex` files
- `scripts/` — Build and validation scripts
  - `generate_bib.py` — Generates `references.bib` by fetching metadata from arXiv/DOI APIs
- `.github/workflows/build-report.yml` — CI workflow

## Bibliography Workflow

References are managed via API lookups, not manual bib entries:

1. Add an identifier to `report/sources.txt` (e.g., `arxiv:ID`, `doi:ID`, or `file:manual/name.bib` for papers without standard identifiers)
2. Run `python3 scripts/generate_bib.py report/sources.txt report/references.bib` to regenerate the bib file
3. CI will verify `references.bib` matches what the APIs return for the identifiers in `sources.txt`

**Do not edit `references.bib` by hand** — it is generated from `sources.txt`.

## CI

The GitHub Actions workflow (`.github/workflows/build-report.yml`) runs on changes to `report/` or `scripts/`:

1. **validate-bib** — Regenerates `references.bib` from `sources.txt` and diffs against the committed version
2. **build** — Compiles `main.tex` to PDF and uploads it as an artifact

## Commands

- Generate bib: `python3 scripts/generate_bib.py report/sources.txt report/references.bib`
- Build PDF locally: `cd report && pdflatex main.tex && biber main && pdflatex main.tex`
