#!/usr/bin/env python3
"""Generate the redistributable amdgpu-isa plugin from the templates and the DB.

The output is the skill directory of the sibling `amdgpu-isa` plugin, which is
committed to the skills repo and shared, so it carries only what may be
redistributed: the XML- and LLVM-derived isa.db, isa.py, SKILL.md, NOTICE.md,
both licence files and build-info.json. The PDF-derived manual pages never go
in; isa.py finds them at a per-machine location instead.

The output is a pure function of build/isa.db, skill-template/, LICENSE and
sources/: no timestamps or absolute paths. Explicit maintainer verification
compares database contents rather than SQLite's version-specific storage bytes.

Usage:  python3 builder/plugin.py [--out DIR]
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import string
import sys
import tempfile

from database import content_digest
ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, "skill-template")
BUILD = os.path.join(ROOT, "build")
SOURCES = os.path.join(ROOT, "sources")
# Only present in a checkout of the skills repo, not in a plugin install.
DEFAULT_OUT = os.path.join(os.path.dirname(ROOT), "amdgpu-isa", "skills", "amdgpu-isa")

# Verbatim outputs need no corpus inputs and are also checked by repo validation.
STATIC_OUTPUTS = {
    "scripts/isa.py": "skill-template/scripts/isa.py",
    "LICENSE": "LICENSE",
    "LICENSE.LLVM.TXT": "skill-template/LICENSE.LLVM.TXT",
}

# Every file the generator owns. Anything else in the output directory is a
# leak or a leftover, and makes `verify` fail.
_CORE_OUTPUTS = ("LICENSE", "LICENSE.LLVM.TXT", "NOTICE.md", "SKILL.md",
                 "build-info.json", "data/isa.db", "scripts/isa.py")


def output_paths():
    """Return the generated file inventory for the active template directory."""
    references = Path(TEMPLATE_DIR) / "references"
    return tuple(dict.fromkeys(_CORE_OUTPUTS + tuple(STATIC_OUTPUTS))) + tuple(
        "references/" + path.name for path in sorted(references.glob("*.md")))


OUTPUTS = output_paths()


def static_output_sources():
    """Return verbatim output sources, including on-demand Markdown references."""
    sources = {
        output: Path(TEMPLATE_DIR, source[len("skill-template/"):])
        if source.startswith("skill-template/") else Path(ROOT, source)
        for output, source in STATIC_OUTPUTS.items()
    }
    sources.update({"references/" + path.name: path
                    for path in sorted(Path(TEMPLATE_DIR, "references").glob("*.md"))})
    return sources


def db_info(db):
    conn = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        try:
            have_gfx = conn.execute("SELECT 1 FROM gfx_target LIMIT 1").fetchone()
            conn.execute("SELECT * FROM v_gfx LIMIT 0")
        except sqlite3.Error:
            have_gfx = None
        if not have_gfx:
            sys.exit("plugin: gfx_target/v_gfx missing or empty -- run build.py gfx")
        info = dict(conn.execute("SELECT key, value FROM build_info"))
        archs = [r[0] for r in conn.execute("SELECT arch FROM arch ORDER BY arch")]
        names = [r[0] for r in conn.execute(
            "SELECT architecture_name FROM arch ORDER BY arch")]
    finally:
        conn.close()
    return info, archs, names


def human(n):
    return "{:,}".format(int(n))


def compress_archs(names):
    """['AMD CDNA 1', ..., 'AMD RDNA 4'] -> 'CDNA 1-5, RDNA 1-4'.

    The description is charged to every session, so spelling out ten names costs
    real tokens for no extra information.
    """
    fams = {}
    for n in names:
        parts = n.replace("AMD ", "").rsplit(" ", 1)
        if len(parts) == 2:
            fams.setdefault(parts[0], []).append(parts[1])
        else:
            fams.setdefault(n, [])
    out = []
    for fam, vers in fams.items():
        if len(vers) > 1:
            out.append("%s %s-%s" % (fam, vers[0], vers[-1]))
        elif vers:
            out.append("%s %s" % (fam, vers[0]))
        else:
            out.append(fam)
    return ", ".join(out)


def selftest_counts(isa_py):
    """(all, manual-only) selftest cases, read from isa.py's SELFTESTS literal."""
    tree = ast.parse(open(isa_py, encoding="utf-8").read())
    for node in tree.body:
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", None) == "SELFTESTS" for t in node.targets)):
            cases = ast.literal_eval(node.value)
            return len(cases), sum(1 for _, argv in cases if argv[0] == "manual")
    sys.exit("plugin: no SELFTESTS list in %s" % isa_py)


def source_entries():
    """Provenance of the two inputs isa.db is built from."""
    manifest = os.path.join(SOURCES, "manifest.json")
    if not os.path.exists(manifest):
        sys.exit("plugin: sources/manifest.json missing -- run 'build.py fetch' first")
    commit = None
    have_xml = False
    out = []
    for e in json.load(open(manifest, encoding="utf-8"))["sources"]:
        if e["category"] == "machine-readable-isa":
            have_xml = True
            out.append({"file": e["file"], "url": e["url"], "sha256": e["sha256"]})
        elif e["category"] == "llvm-doc" and e["file"].endswith(".rst"):
            commit = e.get("llvm_commit")
            if not commit:
                sys.exit("plugin: LLVM source entry has no llvm_commit -- run 'build.py fetch'")
            out.append({"file": e["file"], "url": e["url"], "sha256": e["sha256"],
                        "llvm_commit": commit,
                        "license": "Apache-2.0 WITH LLVM-exception",
                        "role": "gfx_target and v_gfx mappings"})
    if not commit:
        sys.exit("plugin: no LLVM RST source entry -- run 'build.py fetch'")
    if not have_xml:
        sys.exit("plugin: no machine-readable ISA source entry -- run 'build.py fetch'")
    return out, commit


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def write_if_changed(path, content, mode=0o644):
    """Atomically write changed content, preserving unchanged files and mtimes."""
    if os.path.isfile(path) and not os.path.islink(path):
        with open(path, "rb") as fh:
            if fh.read() == content:
                if os.stat(path).st_mode & 0o777 != mode:
                    os.chmod(path, mode)
                return
    fd, scratch = tempfile.mkstemp(prefix=".generated-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.chmod(scratch, mode)
        os.replace(scratch, path)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


def write_database(path, source):
    """Atomically copy a changed database without loading it into memory."""
    fd, scratch = tempfile.mkstemp(prefix=".generated-", dir=os.path.dirname(path))
    os.close(fd)
    try:
        shutil.copyfile(source, scratch)
        os.chmod(scratch, 0o644)
        os.replace(scratch, path)
    finally:
        if os.path.exists(scratch):
            os.unlink(scratch)


def generate(out, *, comparison=None):
    """Generate files, optionally returning database comparison facts in a dict."""
    isa_db = os.path.join(BUILD, "isa.db")
    if not os.path.exists(isa_db):
        sys.exit("plugin: no build/isa.db -- run 'build.py xml' and 'build.py gfx' first")
    sources, llvm_commit = source_entries()
    info, archs, arch_names = db_info(isa_db)

    output_db = os.path.join(out, "data", "isa.db")
    equivalent = False
    source_hash = sha256(isa_db)
    output_hash = None
    if os.path.isfile(output_db) and not os.path.islink(output_db):
        output_hash = sha256(output_db)
        if output_hash == source_hash:
            equivalent = True
        else:
            try:
                equivalent = content_digest(output_db) == content_digest(isa_db)
            except sqlite3.Error:
                equivalent = False
    effective_db = output_db if equivalent else isa_db
    isa_py = os.path.join(TEMPLATE_DIR, "scripts", "isa.py")
    outputs = {output: source.read_bytes() for output, source in static_output_sources().items()}

    n_selftests, n_manual_selftests = selftest_counts(isa_py)
    tmpl = string.Template(
        open(os.path.join(TEMPLATE_DIR, "SKILL.md.tmpl"), encoding="utf-8").read())
    skill_md = tmpl.substitute(
        n_archs=len(archs),
        arch_list=compress_archs(arch_names),
        n_instructions=human(info.get("count_instruction", 0)),
        n_encodings=human(info.get("count_inst_encoding", 0)),
        n_operands=human(info.get("count_operand", 0)),
        db_size="%.0f MB" % (os.path.getsize(effective_db) / 1e6),
        built=info.get("built", "?").split()[0],
        source_desc=info.get("source", "AMD machine-readable ISA XML"),
        n_selftests=n_selftests,
        n_manual_selftests=n_manual_selftests,
        S="$S",
    )
    outputs["SKILL.md"] = skill_md.encode("utf-8")

    notice = string.Template(
        open(os.path.join(TEMPLATE_DIR, "NOTICE.md.tmpl"), encoding="utf-8").read())
    outputs["NOTICE.md"] = notice.substitute(llvm_commit=llvm_commit).encode("utf-8")

    metadata = {
            # Source release date, independent of the wall-clock rebuild time.
            "built": info.get("built", "?"),
            "archs": archs,
            "counts": {k[6:]: int(v) for k, v in sorted(info.items())
                       if k.startswith("count_")},
            "db_redistributable": True,
            # The manual pages never enter this directory; verify enforces it.
            "includes_pdf_derived": False,
            "redistributable": True,
            "sources": sources,
            "db_sha256": output_hash if equivalent else source_hash,
            "db_license": "MIT AND (Apache-2.0 WITH LLVM-exception)",
        }
    outputs["build-info.json"] = (json.dumps(metadata, indent=2) + "\n").encode("utf-8")

    # Validate every input and render all text before changing any output.
    for relative in output_paths():
        path = os.path.join(out, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = 0o755 if relative == "scripts/isa.py" else 0o644
        if relative == "data/isa.db" and equivalent:
            if os.stat(path).st_mode & 0o777 != mode:
                os.chmod(path, mode)
        elif relative == "data/isa.db":
            write_database(path, isa_db)
        else:
            write_if_changed(path, outputs[relative], mode)
    if comparison is not None:
        comparison.update(db_equivalent=equivalent,
                          db_sha256=output_hash if equivalent else source_hash,
                          previous_db_sha256=output_hash)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="skill directory to (re)generate (default: the sibling "
                         "amdgpu-isa plugin in this checkout)")
    args = ap.parse_args()
    out = os.path.realpath(args.out)
    if args.out == DEFAULT_OUT and not os.path.isfile(
            os.path.join(os.path.dirname(os.path.dirname(out)), ".claude-plugin",
                         "plugin.json")):
        print("  skipped: no amdgpu-isa plugin next to this builder (not a skills-repo "
              "checkout); pass --out DIR to generate elsewhere")
        return 0
    generate(out)
    size = sum(os.path.getsize(os.path.join(dp, f))
               for dp, _, fs in os.walk(out) for f in fs)
    print("  generated %s  (%.1f MB, XML/LLVM-derived only -- redistributable)"
          % (out, size / 1e6))
    print("  bump 'version' in both amdgpu-isa plugin.json files if anything changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
