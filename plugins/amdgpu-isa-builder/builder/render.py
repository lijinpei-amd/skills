#!/usr/bin/env python3
"""Render the skill into dist/ from the templates plus the built database.

The output is self-contained -- isa.db, isa.py, SKILL.md, NOTICE.md -- with no
path back to this builder and no install path baked into the text.

Usage:  python3 builder/render.py [--out DIR]
"""

import argparse
import json
import os
import shutil
import sqlite3
import string
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
TEMPLATE_DIR = os.path.join(ROOT, "skill-template")
BUILD = os.path.join(ROOT, "build")

REBUILD_HINT = """# Claude Code (the builder ships disabled; enable it just for the rebuild)
claude plugin enable amdgpu-isa-builder
#   ...then ask: "refresh the AMD ISA corpus"

# or run it directly, no agent involved
python3 <builder>/build.py all"""

NOTICE = """Instruction data derives from AMD's machine-readable GPU ISA
specification (<https://gpuopen.com/machine-readable-isa/>), which each file
declares as:

    Copyright (c) 2026 Advanced Micro Devices, Inc., or its affiliates.
    Sensitivity: AMD Public Use.
    License: MIT

MIT permission notice, as required for redistribution:

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in
    all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

isa.py and the skill text are the work of this builder's author.
"""


def stats(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    info = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM build_info")}
    archs = [r["arch"] for r in conn.execute("SELECT arch FROM arch ORDER BY arch")]
    names = [r["architecture_name"] for r in
             conn.execute("SELECT architecture_name FROM arch ORDER BY arch")]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist", "amd-gpu-isa"))
    args = ap.parse_args()

    isa_db = os.path.join(BUILD, "isa.db")
    if not os.path.exists(isa_db):
        sys.exit("no build/isa.db -- run 'build.py xml' first")
    if os.path.exists(args.out):
        shutil.rmtree(args.out)
    os.makedirs(os.path.join(args.out, "data"))
    os.makedirs(os.path.join(args.out, "scripts"))

    shutil.copy2(isa_db, os.path.join(args.out, "data", "isa.db"))
    shutil.copy2(os.path.join(TEMPLATE_DIR, "scripts", "isa.py"),
                 os.path.join(args.out, "scripts", "isa.py"))
    os.chmod(os.path.join(args.out, "scripts", "isa.py"), 0o755)

    ref_src = os.path.join(TEMPLATE_DIR, "reference")
    if os.path.isdir(ref_src) and os.listdir(ref_src):
        shutil.copytree(ref_src, os.path.join(args.out, "reference"))

    info, archs, arch_names = stats(isa_db)
    db_size = os.path.getsize(isa_db)

    # Count the selftest cases rather than hardcoding a number that can drift.
    isa_py = open(os.path.join(TEMPLATE_DIR, "scripts", "isa.py"), encoding="utf-8").read()
    n_selftests = isa_py.count('", ["') if "SELFTESTS" in isa_py else 0

    # The manual pages are part of every install, so state it plainly rather
    # than hedging -- an agent that thinks the manuals might be missing will not
    # reach for them.
    has_manual = os.path.isdir(os.path.join(BUILD, "manual"))
    pdf_status = (
        "`manual/` holds the ISA reference manuals as one markdown file per "
        "page: `isa.py manual` searches them and `show` links an instruction's "
        "definition page. Treat them as the authority when the database and the "
        "manual disagree -- the database carries structure, the manual carries "
        "the pseudocode and the prose. These pages are built locally from AMD's "
        "PDFs, which grant review rights only, so they stay on this machine and "
        "are never copied into a shared artifact.")
    if not has_manual:
        pdf_status = (
            "`manual/` is MISSING from this build, so no pseudocode or prose is "
            "available and answers are structural only. Rebuild with the "
            "amdgpu-isa-builder skill before relying on this skill.")

    tmpl = string.Template(
        open(os.path.join(TEMPLATE_DIR, "SKILL.md.tmpl"), encoding="utf-8").read())
    skill_md = tmpl.safe_substitute(
        n_archs=len(archs),
        arch_list=compress_archs(arch_names),
        n_instructions=human(info.get("count_instruction", 0)),
        n_encodings=human(info.get("count_inst_encoding", 0)),
        n_operands=human(info.get("count_operand", 0)),
        db_size="%.0f MB" % (db_size / 1e6),
        built=info.get("built", "?").split()[0],
        source_desc=info.get("source", "AMD machine-readable ISA XML"),
        n_selftests=n_selftests,
        pdf_status=pdf_status,
        rebuild_hint=REBUILD_HINT,
        notice_line=("Instruction data: AMD machine-readable ISA XML, MIT / "
                     "\"AMD Public Use\". See NOTICE.md."),
        S="$S",
    )

    leftover = [line for line in skill_md.splitlines() if "$" in line
                and "$S" not in line and "isa.py" not in line]
    if leftover:
        sys.exit("render: unsubstituted placeholder(s):\n  " + "\n  ".join(leftover))

    open(os.path.join(args.out, "SKILL.md"), "w", encoding="utf-8").write(skill_md)
    open(os.path.join(args.out, "NOTICE.md"), "w", encoding="utf-8").write(
        NOTICE)

    manifest_path = os.path.join(ROOT, "sources", "manifest.json")
    sources = []
    if os.path.exists(manifest_path):
        sources = [{"file": e["file"], "url": e["url"], "sha256": e["sha256"]}
                   for e in json.load(open(manifest_path))["sources"]
                   if e["category"] == "machine-readable-isa"
]
    json.dump({
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        "archs": archs,
        "counts": {k[6:]: int(v) for k, v in info.items() if k.startswith("count_")},
        # isa.db is XML-derived whatever else happens, and that XML is MIT.
        "db_redistributable": True,
        # The install links the PDF-derived manual pages in, so the skill as a
        # whole is review-rights-only. Recorded rather than assumed: this is the
        # flag to read before copying an installed skill anywhere.
        "includes_pdf_derived": has_manual,
        "redistributable": not has_manual,
        "sources": sources,
    }, open(os.path.join(args.out, "build-info.json"), "w"), indent=2)

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _, fs in os.walk(args.out) for f in fs)
    print("  rendered %s  (%.1f MB, %s)"
          % (os.path.relpath(args.out, ROOT), total / 1e6,
             "manual pages linked at install: LOCAL ONLY" if has_manual
             else "XML-derived only -- no manual pages"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
