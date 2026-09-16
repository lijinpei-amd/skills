#!/usr/bin/env python3
"""ISA reference PDFs -> enrich.db (pseudocode and notes).  NOT YET IMPLEMENTED.

Deliberately separate from isa.db: this content derives from AMD's ISA reference
PDFs, whose Specification Agreement permits review only and forbids passing any
part to anyone else. Keeping it in its own file makes "can this be shared?" a
per-file question instead of a per-column audit.

Intended shape:

    CREATE TABLE inst_detail (
        arch TEXT, name TEXT, pseudocode TEXT, notes TEXT,
        source_pdf TEXT, page_printed INTEGER, page_physical INTEGER,
        PRIMARY KEY (arch, name));

isa.py already attaches this database when present and prints pseudocode in
`show`, so implementing it needs no change to the query side.

Requires pymupdf, so this stage -- alone -- needs a venv:
    python3 -m venv .venv && .venv/bin/pip install pymupdf
"""

import sys

sys.exit(
    "pdf_to_db.py is not implemented yet.\n"
    "The XML-derived corpus (build.py xml) is complete and is the publishable\n"
    "core; this stage would add locally-only pseudocode and notes on top.")
