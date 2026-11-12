#!/usr/bin/env python3
"""ISA reference PDFs -> one markdown file per page, plus three TSV indexes.

LOCAL ONLY. AMD's ISA reference PDFs grant review rights and forbid passing any
part to anyone else, so everything this writes stays on this machine. It lands in
the persistent per-machine manual directory, outside builder/plugin installs.

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
from contextlib import closing, contextmanager
import csv
import fcntl
import hashlib
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import tempfile
from manual_link import MANUAL_ROOT

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
INDEX_FILES = ("toc.tsv", "instr-index.tsv", "format-index.tsv")
STAGE_PREFIX = ".amdgpu-isa-manual-stage-"
BACKUP_PREFIX = ".amdgpu-isa-manual-backup-"
STAGE_OWNER = ".amdgpu-isa-stage-owner"
RECOVERY_OWNER = ".amdgpu-isa-recovery-owner"


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
    """Replace one arch's pages only after its PDF converts successfully."""
    with tempfile.TemporaryDirectory(prefix=".isa-arch-", dir=out_root) as scratch:
        with pymupdf.open(pdf_path) as doc:
            result = convert_pages(doc, pdf_path, arch, scratch)
        os.replace(os.path.join(scratch, arch), os.path.join(out_root, arch))
    return result


def convert_pages(doc, pdf_path, arch, out_root):
    """Write a PDF's pages and referenced figures into an empty staging tree."""
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


def open_index_database(db):
    """Return a valid read-only database for both manual index queries.

    Raise sqlite3.Error if the file is unavailable, corrupt, or lacks the
    required schema. Call this before preparing or replacing manual storage.
    """
    conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        integrity = conn.execute("PRAGMA quick_check").fetchall()
        if integrity != [("ok",)]:
            raise sqlite3.DatabaseError("database integrity check failed: %r" % integrity[:3])
        conn.execute("SELECT arch, name, opcode FROM v_inst LIMIT 0")
        conn.execute("SELECT arch, name FROM encoding LIMIT 0")
    except Exception:
        conn.close()
        raise
    return conn


def is_owned_manual_stage(path):
    """Return whether a directory identifies itself as an unpublished stage."""
    if not os.path.isdir(path) or os.path.islink(path):
        return False
    owner = Path(path) / STAGE_OWNER
    if owner.is_symlink():
        return False
    try:
        return owner.read_text(encoding="utf-8") == os.path.realpath(path) + "\n"
    except (OSError, UnicodeError):
        return False


def recover_manual_backups(out_root):
    """Restore missing published entries from this root's owned backups."""
    parent = os.path.dirname(out_root)
    for name in sorted(os.listdir(parent)):
        backup = os.path.join(parent, name)
        if (not name.startswith(BACKUP_PREFIX) or backup == out_root
                or not os.path.isdir(backup) or os.path.islink(backup)):
            continue
        owner = Path(backup) / RECOVERY_OWNER
        if owner.is_symlink():
            continue
        try:
            expected = os.path.realpath(out_root) + "\n" + os.path.realpath(backup) + "\n"
            if owner.read_text(encoding="utf-8") != expected:
                continue
        except (OSError, UnicodeError):
            continue
        for entry in os.listdir(backup):
            destination = os.path.join(out_root, entry)
            if entry != RECOVERY_OWNER and not os.path.lexists(destination):
                os.replace(os.path.join(backup, entry), destination)
        if os.listdir(backup) == [RECOVERY_OWNER]:
            shutil.rmtree(backup)


@contextmanager
def manual_build_directory(out_root):
    """Yield the resolved root and sibling stage under an exclusive build lock.

    Remove interrupted sibling stages before building. Keep recovery backups;
    they can contain the only surviving original pages after rollback failure.
    """
    out_root = os.path.abspath(os.path.expanduser(out_root))
    if os.path.islink(out_root) and not os.path.isdir(out_root):
        raise ValueError("manual path links to a missing or non-directory target: %r" % out_root)
    destination = os.path.realpath(out_root)
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    with open(os.path.join(parent, ".amdgpu-isa-manual.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        recover_manual_backups(destination)
        for name in os.listdir(parent):
            path = os.path.join(parent, name)
            if (name.startswith(STAGE_PREFIX) and path != destination
                    and is_owned_manual_stage(path)):
                shutil.rmtree(path)
        out_root = destination
        staged = tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=parent)
        try:
            # A renamed first publication retains this record of its old path,
            # so cleanup cannot mistake any published sibling for a stage.
            (Path(staged) / STAGE_OWNER).write_text(os.path.realpath(staged) + "\n",
                                                   encoding="utf-8")
            yield out_root, staged
        finally:
            if os.path.lexists(staged):
                shutil.rmtree(staged)


def publish_manual(staged, out_root, updated_archs):
    """Publish a first manual directory, or replace staged archs and indexes.

    The stage must share the output filesystem. Restore old files on rename
    failure; keep a recovery directory if rollback itself fails, so surviving
    original files remain available for recovery.
    Readers during an existing-root rebuild may see a partial publication.
    """
    if not os.path.lexists(out_root):
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(staged, 0o777 & ~mask)
        os.replace(staged, out_root)
        return
    backup = tempfile.mkdtemp(prefix=BACKUP_PREFIX, dir=os.path.dirname(out_root))
    replaced = []
    cleanup = False
    try:
        (Path(backup) / RECOVERY_OWNER).write_text(
            os.path.realpath(out_root) + "\n" + os.path.realpath(backup) + "\n", encoding="utf-8")
        for name in sorted(updated_archs) + list(INDEX_FILES):
            destination = os.path.join(out_root, name)
            had_old = os.path.lexists(destination)
            if had_old:
                if name in INDEX_FILES:
                    shutil.copy2(destination, os.path.join(backup, name))
                else:
                    os.replace(destination, os.path.join(backup, name))
            replaced.append((name, had_old))
            os.replace(os.path.join(staged, name), destination)
        cleanup = True
    except OSError as publication_error:
        try:
            for name, had_old in reversed(replaced):
                destination = os.path.join(out_root, name)
                if name in INDEX_FILES:
                    if had_old:
                        os.replace(os.path.join(backup, name), destination)
                    elif os.path.lexists(destination):
                        os.unlink(destination)
                    continue
                if os.path.lexists(destination):
                    os.replace(destination, os.path.join(staged, name))
                if had_old:
                    os.replace(os.path.join(backup, name), destination)
        except OSError as rollback_error:
            raise OSError("manual rollback failed; original files retained: %r (%s)"
                          % (backup, rollback_error)) from publication_error
        cleanup = True
        raise
    finally:
        if cleanup:
            shutil.rmtree(backup)


def write_index(out_root, filename, columns, rows, updated_archs):
    """Atomically replace rebuilt arch rows, retaining every untouched arch."""
    path = os.path.join(out_root, filename)
    retained = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
            if next(reader, None) != columns:
                raise ValueError("unexpected manual index header: %s" % path)
            retained = [row for row in reader if row and row[0] not in updated_archs]
    merged = retained + [[str(value) for value in row] for row in rows]
    # Sorting only by arch preserves each outline's order, including headings
    # sharing a page, and the instruction index's definition-first ordering.
    merged.sort(key=lambda row: row[0])
    fd, scratch = tempfile.mkstemp(prefix=".isa-index-", dir=out_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t", lineterminator="\n",
                                quoting=csv.QUOTE_NONE, quotechar=None)
            writer.writerow(columns)
            writer.writerows(merged)
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(scratch, os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o666 & ~mask)
        os.replace(scratch, path)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)
    return len(merged)


def write_toc(out_root, rows, updated_archs):
    """Merge rebuilt PDF outlines into the persistent table of contents."""
    rows = [(arch, lvl, norm(title), page, "%s/p%04d.md" % (arch, page))
            for arch, lvl, title, page in rows]
    return write_index(out_root, "toc.tsv", ["arch", "level", "title", "page_physical", "path"],
                       rows, updated_archs)


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


def write_instr_index(out_root, pages_by_arch, conn):
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
    # One row per (arch, instruction, kind, page); definitions sort first.
    rows = sorted(set(rows), key=lambda r: (r[0], r[1], r[4] != "definition", r[2]))
    count = write_index(out_root, "instr-index.tsv",
                        ["arch", "instruction", "page_physical", "page_printed", "kind", "path"],
                        rows, pages_by_arch)
    n_def = sum(1 for r in rows if r[4] == "definition")
    print("  instruction index: %d rows, %d definition pages in rebuilt archs" % (count, n_def))
    return count


def write_format_index(out_root, pages_by_arch, conn):
    """Encoding / microcode format -> page."""
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
    return write_index(out_root, "format-index.tsv",
                       ["arch", "encoding", "page_physical", "page_printed", "path"],
                       sorted(rows), pages_by_arch)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdfs", default=os.path.join(ROOT, "sources", "pdf"))
    ap.add_argument("--out", default=MANUAL_ROOT)
    ap.add_argument("--db", default=os.path.join(ROOT, "build", "isa.db"))
    ap.add_argument("--only", help="convert one arch only")
    args = ap.parse_args()

    inputs = []
    for fname, arch in sorted(PDF_ARCH.items(), key=lambda kv: kv[1]):
        if args.only and arch != args.only:
            continue
        path = os.path.join(args.pdfs, fname)
        if not os.path.isfile(path):
            print("  skip %-9s (%s not fetched)" % (arch, fname))
            continue
        inputs.append((path, arch))
    if not inputs:
        print("no matching ISA PDFs available; manual directory was not changed", file=sys.stderr)
        return 1

    try:
        conn = open_index_database(args.db)
    except (OSError, sqlite3.Error, ValueError) as error:
        print("isa.db unavailable or invalid: %r (%s); manual directory was not changed"
              % (args.db, error), file=sys.stderr)
        return 1

    toc_rows, pages_by_arch, total_pages, total_imgs = [], {}, 0, 0
    try:
        with closing(conn), manual_build_directory(args.out) as (out_root, staged):
            for name in INDEX_FILES:
                existing = os.path.join(out_root, name)
                if os.path.isfile(existing):
                    shutil.copy2(existing, os.path.join(staged, name))
            for path, arch in inputs:
                toc, pages, n_img = convert(path, arch, staged)
                toc_rows += [(arch, lvl, t, p) for lvl, t, p in toc]
                pages_by_arch[arch] = pages
                total_pages += len(pages)
                total_imgs += n_img
                print("  %-9s %4d pages  %3d figures  %3d outline entries"
                      % (arch, len(pages), n_img, len(toc)))

            n_toc = write_toc(staged, toc_rows, pages_by_arch)
            n_inst = write_instr_index(staged, pages_by_arch, conn)
            n_fmt = write_format_index(staged, pages_by_arch, conn)
            publish_manual(staged, out_root, pages_by_arch)
            args.out = out_root
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        print("manual rebuild failed: %s" % error, file=sys.stderr)
        return 1

    print("\n%d pages, %d figures (deduped) -> %s"
          % (total_pages, total_imgs, os.path.relpath(args.out, ROOT)))
    print("indexes: toc.tsv %d rows, instr-index.tsv %d rows, format-index.tsv %d rows"
          % (n_toc, n_inst, n_fmt))
    print("LOCAL ONLY -- PDF-derived; never publish the manual directory")
    return 0


if __name__ == "__main__":
    sys.exit(main())
