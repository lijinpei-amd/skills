#!/usr/bin/env python3
"""Check the build before it is trusted or installed.

Three kinds of check:
  * counts   -- re-derive instruction/encoding/operand totals straight from the
                XML with an independent code path, and compare against the DB
  * licence  -- assert no AMD-derived file is tracked by git, and that every
                architecture shipped is MIT / "AMD Public Use"
  * answers  -- run the query CLI's own selftest against the rendered skill

Usage:  python3 builder/verify.py [--quick]
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
failures = []


def check(label, ok, detail=""):
    print("  %s %-46s %s" % ("ok  " if ok else "FAIL", label, detail))
    if not ok:
        failures.append(label)
    return ok


def verify_counts(db):
    """Independent re-derivation of the per-arch instruction count.

    Counts by regex rather than reusing the ElementTree loader on purpose: a bug
    shared by builder and verifier would otherwise cancel itself out.
    """
    xml_dir = os.path.join(ROOT, "sources", "xml", "extracted")
    if not os.path.isdir(xml_dir):
        return check("counts re-derived from XML", False, "no sources/xml/extracted")
    conn = sqlite3.connect(db)
    all_ok = True
    for f in sorted(os.listdir(xml_dir)):
        if not f.endswith(".xml"):
            continue
        arch = re.search(r"amdgpu_isa_(.+)\.xml", f).group(1)
        t = open(os.path.join(xml_dir, f), encoding="utf-8", errors="replace").read()
        # <Instruction> appears only as the instruction element; aliases use
        # <InstructionName> inside <AliasedInstructionNames>, so count the
        # element itself rather than names.
        xml_n = len(re.findall(r"<Instruction>", t))
        db_n = conn.execute("SELECT COUNT(*) FROM instruction WHERE arch = ?",
                            (arch,)).fetchone()[0]
        if xml_n != db_n:
            all_ok = False
            check("count %s" % arch, False, "XML %d != DB %d" % (xml_n, db_n))
    conn.close()
    return check("instruction counts match XML, all archs", all_ok)


def verify_gfx_map(db):
    """Every arch must be reachable from some gfx target, and the two mappings
    LLVM gets wrong must be right here."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    if "gfx_target" not in tables:
        conn.close()
        return check("gfx map present", False, "run 'build.py gfx'")
    missing = [r[0] for r in conn.execute(
        "SELECT arch FROM arch WHERE arch NOT IN"
        " (SELECT arch FROM gfx_target WHERE arch IS NOT NULL)")]
    check("every arch has a gfx target", not missing, ", ".join(missing))
    # The cases where LLVM's own grouping disagrees with the product arch.
    ok = True
    for gfx, want in (("gfx1250", "cdna5"), ("gfx950", "cdna4"),
                      ("gfx942", "cdna3"), ("gfx1201", "rdna4")):
        got = conn.execute("SELECT arch FROM gfx_target WHERE gfx=?", (gfx,)).fetchone()
        if not got or got[0] != want:
            ok = False
            check("gfx map %s -> %s" % (gfx, want), False,
                  "got %s" % (got[0] if got else "missing"))
    conn.close()
    return check("gfx map: encoding-generation traps handled", ok)


def verify_licence_boundary():
    """The .gitignore is the licence boundary; assert git is not tracking any
    AMD-derived artefact."""
    try:
        tracked = subprocess.run(["git", "-C", ROOT, "ls-files"],
                                 capture_output=True, text=True).stdout.split()
    except OSError:
        return check("no AMD-derived files tracked by git", False, "git unavailable")
    bad = [f for f in tracked
           if f.endswith((".pdf", ".xml", ".db", ".zip"))
           or f.startswith(("sources/", "build/", "dist/"))]
    return check("no AMD-derived files tracked by git", not bad,
                 ("tracked: " + ", ".join(bad[:3])) if bad else "")


def verify_dist_purity(dist):
    info_path = os.path.join(dist, "build-info.json")
    if not os.path.exists(info_path):
        return check("dist declares its redistribution status", False)
    info = json.load(open(info_path))
    check("dist declares itself redistributable, XML-derived only",
          info.get("redistributable") is True
          and info.get("includes_pdf_derived") is False)
    if info.get("redistributable"):
        conn = sqlite3.connect(os.path.join(dist, "data", "isa.db"))
        lic = conn.execute("SELECT DISTINCT license, sensitivity FROM arch").fetchall()
        conn.close()
        ok = all(l == "MIT" and s.startswith("AMD Public Use") for l, s in lic)
        check("redistributable build: every arch is MIT/Public Use", ok, str(lic[:1]))
    return True


def verify_export(dist):
    """The export is the licence boundary, so test it rather than trust it."""
    import tarfile, tempfile, subprocess as sp
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "export.tar.gz")
        r = sp.run([sys.executable, os.path.join(ROOT, "builder", "export.py"),
                    "--src", dist, "--out", out], capture_output=True, text=True)
        if r.returncode != 0:
            return check("export excludes PDF-derived content", False,
                         (r.stderr.strip().splitlines() or [""])[-1][:60])
        with tarfile.open(out) as tar:
            names = tar.getnames()
    bad = [n for n in names if "/manual/" in n or n.endswith((".pdf", ".xml"))]
    return check("export excludes PDF-derived content", not bad,
                 "%d files, none PDF-derived" % len(names) if not bad
                 else ", ".join(bad[:3]))


def verify_answers(dist):
    isa = os.path.join(dist, "scripts", "isa.py")
    if not os.path.exists(isa):
        return check("query CLI selftest", False, "no rendered skill")
    r = subprocess.run([sys.executable, isa, "selftest"],
                       capture_output=True, text=True)
    last = (r.stdout.strip().splitlines() or [""])[-1]
    return check("query CLI selftest", r.returncode == 0, last)


def verify_skill_text(dist):
    p = os.path.join(dist, "SKILL.md")
    if not os.path.exists(p):
        return check("SKILL.md rendered", False)
    t = open(p, encoding="utf-8").read()
    # $S is intentional (it is the placeholder the *agent* resolves at runtime).
    left = [l for l in t.splitlines() if re.search(r"\$(?!S\b)[a-z_]+", l)]
    check("no unsubstituted template placeholders", not left,
          left[0][:60] if left else "")
    hard = [l for l in t.splitlines()
            if re.search(r"~/\.(claude|codex|agents)/skills/amd-gpu-isa", l)]
    return check("no install path hardcoded in SKILL.md", not hard,
                 hard[0][:60] if hard else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist", "amd-gpu-isa"))
    ap.add_argument("--quick", action="store_true", help="skip XML re-derivation")
    args = ap.parse_args()

    db = os.path.join(ROOT, "build", "isa.db")
    print("verify:")
    if os.path.exists(db) and not args.quick:
        verify_counts(db)
    if os.path.exists(db):
        verify_gfx_map(db)
    verify_licence_boundary()
    if os.path.isdir(args.dist):
        verify_dist_purity(args.dist)
        verify_skill_text(args.dist)
        verify_export(args.dist)
        verify_answers(args.dist)

    if failures:
        print("\n%d check(s) failed" % len(failures))
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
