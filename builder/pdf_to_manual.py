#!/usr/bin/env python3
"""ISA reference PDFs -> one markdown file per page, plus three TSV indexes.

LOCAL ONLY. AMD's ISA reference PDFs grant review rights and forbid passing any
part to anyone else, so everything this writes stays on this machine. It lands in
build/manual/, which is gitignored, and `build.py export` excludes it from any
shareable artifact.

A page is the unit on purpose: ~364 tokens median, so an agent that reads one by
mistake pays almost nothing, and every page carries its own provenance in front
matter. Boilerplate is stripped by frequency -- text or images appearing in the
top/bottom band of most pages are running heads, not content.

Needs pymupdf, so this is the one stage that is not stdlib-only:
    python3 -m venv .venv && .venv/bin/pip install pymupdf
    .venv/bin/python builder/pdf_to_manual.py

Usage:  .venv/bin/python builder/pdf_to_manual.py [--only rdna4] [--out DIR]
"""

import argparse
import collections
import hashlib
import os
import re
import sqlite3
import sys

try:
    import pymupdf
except ImportError:
    sys.exit("pymupdf is required for this stage:\n"
             "    python3 -m venv .venv && .venv/bin/pip install pymupdf\n"
             "then re-run with .venv/bin/python")

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# Local filename -> arch key. The last three have no machine-readable XML, so
# they get pages and a TOC but no instruction index; they are still the only
# prose reference for those generations.
PDF_ARCH = {
    "instinct-mi100-cdna1-shader-instruction-set-architecture.pdf": "cdna1",
    "instinct-mi200-cdna2-instruction-set-architecture.pdf": "cdna2",
    "amd-instinct-mi300-cdna3-instruction-set-architecture.pdf": "cdna3",
    "amd-instinct-cdna4-instruction-set-architecture.pdf": "cdna4",
    "amd-instinct-cdna5-instruction-set-architecture.pdf": "cdna5",
    "rdna-shader-instruction-set-architecture.pdf": "rdna1",
    "rdna2-shader-instruction-set-architecture.pdf": "rdna2",
    "rdna3-shader-instruction-set-architecture-feb-2023_0.pdf": "rdna3",
    "rdna35_instruction_set_architecture.pdf": "rdna3_5",
    "rdna4-instruction-set-architecture.pdf": "rdna4",
    "vega-shader-instruction-set-architecture.pdf": "vega",
    "vega-7nm-shader-instruction-set-architecture.pdf": "vega7nm",
    "gcn3-instruction-set-architecture.pdf": "gcn3",
}

BAND = 0.07        # fraction of page height treated as header/footer
BOILERPLATE = 0.20 # appears on more than this share of pages -> running head
MIN_IMG = 3000     # ignore tiny images (rules, bullets, spacers)


def norm(s):
    """Whitespace-normalised key. GCN3's running head extracts with varying
    inter-letter spacing, so raw strings under-count as several variants."""
    return re.sub(r"\s+", " ", s).strip()


def printed_page_number(footer_texts):
    """Pull the printed page number out of footer text ('ii of 697', '12-44')."""
    for t in footer_texts:
        m = re.search(r"\b(\d{1,4})\s+of\s+\d{1,4}\b", t)
        if m:
            return m.group(1)
        m = re.fullmatch(r"\s*([ivxlcdm]{1,7}|\d{1,4}|\d+-\d+)\s*", t.strip(), re.I)
        if m:
            return m.group(1)
    return None


def scan_boilerplate(doc):
    """Text and images that recur in the header/footer band across the book."""
    n = len(doc)
    text_freq = collections.Counter()
    img_freq = collections.Counter()
    for i in range(n):
        page = doc[i]
        h = page.rect.height
        for b in page.get_text("blocks"):
            y0, y1, txt = b[1], b[3], (b[4] or "").strip()
            if txt and (y1 < h * BAND or y0 > h * (1 - BAND)):
                text_freq[norm(txt)] += 1
        for info in page.get_images(full=True):
            img_freq[info[0]] += 1
    cutoff = max(2, int(n * BOILERPLATE))
    return ({t for t, c in text_freq.items() if c > cutoff},
            {x for x, c in img_freq.items() if c > cutoff})


def chapter_for_page(toc, page_no):
    """Deepest outline entry at or before this page."""
    best = None
    for level, title, start in toc:
        if start <= page_no:
            if best is None or start >= best[2]:
                best = (level, title, start)
        else:
            break
    return best[1] if best else None


def save_image(doc, xref, img_dir):
    """Content-addressed, so the same figure reused on many pages is stored once."""
    try:
        img = doc.extract_image(xref)
    except Exception:
        return None
    blob = img["image"]
    if len(blob) < MIN_IMG:
        return None
    name = hashlib.sha1(blob).hexdigest()[:12] + "." + img["ext"]
    path = os.path.join(img_dir, name)
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(blob)
    return name


def convert(pdf_path, arch, out_root):
    doc = pymupdf.open(pdf_path)
    toc = [(lvl, t, p) for lvl, t, p in doc.get_toc()]
    drop_text, drop_img = scan_boilerplate(doc)

    arch_dir = os.path.join(out_root, arch)
    img_dir = os.path.join(arch_dir, "img")
    os.makedirs(img_dir, exist_ok=True)

    pages = []
    for i in range(len(doc)):
        page = doc[i]
        h = page.rect.height
        body, footer = [], []
        for b in sorted(page.get_text("blocks"), key=lambda b: (b[1], b[0])):
            y0, y1, txt = b[1], b[3], (b[4] or "").strip()
            if not txt:
                continue
            in_band = y1 < h * BAND or y0 > h * (1 - BAND)
            if in_band:
                if y0 > h * (1 - BAND):
                    footer.append(txt)
                if norm(txt) in drop_text:
                    continue            # running head / footer
                if in_band and len(norm(txt)) < 24 and re.search(r"\d", txt):
                    continue            # bare page number
            body.append(txt)

        imgs = []
        for info in page.get_images(full=True):
            if info[0] in drop_img:
                continue
            name = save_image(doc, info[0], img_dir)
            if name and name not in imgs:
                imgs.append(name)

        physical = i + 1
        printed = printed_page_number(footer) or ""
        chapter = chapter_for_page(toc, physical) or ""
        text = "\n\n".join(body).strip()

        rel = os.path.join(arch, "p%04d.md" % physical)
        with open(os.path.join(out_root, rel), "w", encoding="utf-8") as fh:
            fh.write("---\n")
            fh.write("pdf: %s\n" % os.path.basename(pdf_path))
            fh.write("arch: %s\n" % arch)
            fh.write("page_physical: %d\n" % physical)
            fh.write("page_printed: %s\n" % (printed or "-"))
            fh.write("chapter: %s\n" % chapter.replace("\n", " "))
            fh.write("---\n\n")
            fh.write(text + "\n")
            for name in imgs:
                fh.write("\n![figure](img/%s)\n" % name)
        pages.append({"physical": physical, "printed": printed,
                      "chapter": chapter, "path": rel, "text": text})

    return toc, pages, len(os.listdir(img_dir))


def write_toc(out_root, rows):
    with open(os.path.join(out_root, "toc.tsv"), "w", encoding="utf-8") as fh:
        fh.write("arch\tlevel\ttitle\tpage_physical\tpath\n")
        for arch, lvl, title, page in rows:
            fh.write("%s\t%d\t%s\t%d\t%s\n"
                     % (arch, lvl, norm(title), page, "%s/p%04d.md" % (arch, page)))
    return len(rows)


NAME_LINE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
LOWER_WORD = re.compile(r"\b[a-z]{2,}\b")


BARE_INT = re.compile(r"^\d{1,5}$")


def has_content(lines):
    """True if any line is neither a bare integer nor an all-caps mnemonic.

    An opcode table alternates strictly between instruction names and opcode
    numbers, so anything else -- a sentence, a pseudocode line like
    "tmp = MEM[ADDR];", even a "// 32bit" comment -- means the name is being
    documented rather than listed. This is the one signal that holds across all
    thirteen manuals; prose alone misses the older ones, whose definitions open
    with pseudocode, and opcode adjacency misses them too because their
    numbering does not always agree with the XML."""
    return any(not BARE_INT.match(x) and not NAME_LINE.match(x) for x in lines)


def write_instr_index(out_root, pages_by_arch, db):
    """Instruction -> page, distinguishing the definition from opcode tables.

    Matching the name alone is useless -- V_FMA_F32 is mentioned on dozens of
    pages. What identifies the page that *documents* an instruction is the name
    standing alone on a line with its own opcode immediately after: the
    definition page reads "V_FMA_F32 / 531 / <description> / <pseudocode>".
    An opcode table also puts the name at line start, but the number next to it
    belongs to the neighbouring entry, so requiring the instruction's *own*
    opcode separates the two cleanly.

    Bare mentions are deliberately not indexed -- grep the page tree for those.
    """
    if not os.path.exists(db):
        print("  (no isa.db -- skipping instruction index)")
        return 0
    conn = sqlite3.connect(db)
    rows = []
    for arch, pages in sorted(pages_by_arch.items()):
        opcodes = collections.defaultdict(set)
        for name, opc in conn.execute(
                "SELECT name, opcode FROM v_inst WHERE arch = ? AND opcode IS NOT NULL",
                (arch,)):
            opcodes[name].add(str(opc))
        if not opcodes:
            continue
        for p in pages:
            lines = [l.strip() for l in p["text"].splitlines()]
            for i, line in enumerate(lines):
                if not NAME_LINE.match(line) or line not in opcodes:
                    continue
                nxt = [x for x in lines[i + 1:i + 9] if x][:6]
                # The manuals disagree on layout -- modern ones put the
                # opcode right after the name, older ones lead with pseudocode
                # and number differently from the XML -- so classify by what
                # follows rather than by any one expected form.
                kind = "definition" if has_content(nxt) else "table"
                rows.append((arch, line, p["physical"], p["printed"] or "-",
                             kind, p["path"]))
    conn.close()
    # One row per (arch, instruction, kind, page); definitions sort first.
    rows = sorted(set(rows), key=lambda r: (r[0], r[1], r[4] != "definition", r[2]))
    with open(os.path.join(out_root, "instr-index.tsv"), "w", encoding="utf-8") as fh:
        fh.write("arch\tinstruction\tpage_physical\tpage_printed\tkind\tpath\n")
        for r in rows:
            fh.write("%s\t%s\t%d\t%s\t%s\t%s\n" % r)
    n_def = sum(1 for r in rows if r[4] == "definition")
    print("  instruction index: %d rows, %d definition pages" % (len(rows), n_def))
    return len(rows)


def write_format_index(out_root, pages_by_arch, db):
    """Encoding / microcode format -> page."""
    if not os.path.exists(db):
        return 0
    conn = sqlite3.connect(db)
    rows = []
    for arch, pages in sorted(pages_by_arch.items()):
        encs = [r[0] for r in conn.execute(
            "SELECT DISTINCT name FROM encoding WHERE arch = ?", (arch,))]
        if not encs:
            continue
        for p in pages:
            up = p["text"].upper()
            for e in encs:
                bare = e[4:] if e.startswith("ENC_") else e
                if e.upper() in up or re.search(r"\b%s\b" % re.escape(bare.upper()), up):
                    rows.append((arch, e, p["physical"], p["printed"] or "-", p["path"]))
    conn.close()
    with open(os.path.join(out_root, "format-index.tsv"), "w", encoding="utf-8") as fh:
        fh.write("arch\tencoding\tpage_physical\tpage_printed\tpath\n")
        for r in sorted(rows):
            fh.write("%s\t%s\t%d\t%s\t%s\n" % r)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", default=os.path.join(ROOT, "sources", "pdf"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "manual"))
    ap.add_argument("--db", default=os.path.join(ROOT, "build", "isa.db"))
    ap.add_argument("--only", help="convert one arch only")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    toc_rows, pages_by_arch, total_pages, total_imgs = [], {}, 0, 0

    for fname, arch in sorted(PDF_ARCH.items(), key=lambda kv: kv[1]):
        if args.only and arch != args.only:
            continue
        path = os.path.join(args.pdfs, fname)
        if not os.path.exists(path):
            print("  skip %-9s (%s not fetched)" % (arch, fname))
            continue
        toc, pages, n_img = convert(path, arch, args.out)
        toc_rows += [(arch, lvl, t, p) for lvl, t, p in toc]
        pages_by_arch[arch] = pages
        total_pages += len(pages)
        total_imgs += n_img
        print("  %-9s %4d pages  %3d figures  %3d outline entries"
              % (arch, len(pages), n_img, len(toc)))

    n_toc = write_toc(args.out, toc_rows)
    n_inst = write_instr_index(args.out, pages_by_arch, args.db)
    n_fmt = write_format_index(args.out, pages_by_arch, args.db)

    print("\n%d pages, %d figures (deduped) -> %s"
          % (total_pages, total_imgs, os.path.relpath(args.out, ROOT)))
    print("indexes: toc.tsv %d rows, instr-index.tsv %d rows, format-index.tsv %d rows"
          % (n_toc, n_inst, n_fmt))
    print("LOCAL ONLY -- PDF-derived; never publish build/manual/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
