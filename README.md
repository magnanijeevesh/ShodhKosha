# ShodhKosha

An academic reference manager for the desktop. Import PDFs, have a local AI
model summarise, tag and file them, read and annotate them, and copy
BibTeX entries.

Everything runs on this computer. The only internet use is the optional
"Look Up DOI Online" button, which sends a DOI to Crossref.

## Setup (Windows)

    python -m venv .venv
    .venv\Scripts\Activate.ps1
    pip install -r requirements.txt

For AI filing, install Ollama from https://ollama.com and run once:

    ollama pull llama3.2

For scanned PDFs, install Tesseract OCR (optional). ShodhKosha finds it in
`C:\Program Files\Tesseract-OCR` or on the PATH:

    winget install UB-Mannheim.TesseractOCR

## Run

    python -m shodhkosha

## Test

    python -m unittest discover tests -v

## Where things are kept

    C:\Users\<you>\ShodhKosha Library\
        library.sqlite3     the database
        papers\             the library's copy of every imported PDF

Back up that folder to back up the whole library.

## Code layout

| File | Purpose |
|---|---|
| `__main__.py` | Starts the application |
| `main_window.py` | Three-pane window, import, menus |
| `pdf_viewer_widget.py` | PDF viewing, selection and annotation tools |
| `metadata_inspector_widget.py` | Details form and DOI lookup button |
| `theme.py` | Light and dark appearance |
| `library_database.py` | SQLite schema and all queries |
| `library_storage.py` | Importing files and mirroring folders on disk |
| `pdf_document_service.py` | PyMuPDF rendering, annotations, safe saving |
| `paper_analysis.py` | AI prompt, schema and filing rules |
| `ai_analysis_worker.py` | Background queue for AI analysis |
| `doi_lookup.py`, `doi_lookup_worker.py` | Crossref lookup |
| `bibtex_formatter.py` | BibTeX entries and .bib export |
| `text_index_queue.py` | Background full-text indexing and OCR of scans |
| `document_panels.py` | Highlights and search-hit lists |
| `annotation_export.py` | Highlights and notes as Markdown |
| `study_cards.py`, `study_cards_widget.py` | AI study cards and Anki export |
| `run_shodhkosha.py` (top folder) | Entry point for building the .exe |
