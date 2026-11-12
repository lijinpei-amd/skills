#!/usr/bin/env python3
"""amdgpu-isa-builder -- build the amdgpu-isa corpus and the local ISA manuals.

Stages, each independently runnable so a slow one is never re-run for free:

    fetch     download public AMD sources    -> sources/   (network)
    xml       machine-readable ISA -> SQLite -> build/isa.db
    gfx       LLVM AMDGPUUsage -> gfx map    -> build/isa.db (gfx_target)
    manual    ISA PDFs -> markdown + indexes -> persistent data directory
              outside builder/plugin installs (LOCAL ONLY)
    plugin    templates + db -> the redistributable amdgpu-isa plugin
              (skills-repo checkout only; skipped elsewhere)
    verify    counts, licence boundary, manual pages and query answers
              --plugin also compares generated outputs (maintainers only)
    all       fetch -> xml -> gfx -> manual -> verify
              runs verify --quick; full checks require an explicit `verify`
              plugin generation requires an explicit `plugin` stage

Two outputs, two licence regimes. The plugin carries only what may be
redistributed: XML- and LLVM-derived data plus the query script. The manual
pages derive from AMD's PDFs, which may not be redistributed, so they stay in
the per-machine data directory, which plugin updates and removals preserve.

sources/ and build/ are gitignored, and `verify` fails the build if git
is tracking any of them or if a PDF-derived file reaches the plugin.

    python3 build.py all
"""

import argparse
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.realpath(__file__))
BUILDER = os.path.join(ROOT, "builder")
def run(script, argv, label):
    print("\n== %s" % label)
    t0 = time.time()
    r = subprocess.run([sys.executable, os.path.join(BUILDER, script)] + argv)
    if r.returncode != 0:
        sys.exit("\n%s failed (exit %d)" % (label, r.returncode))
    print("   (%.1fs)" % (time.time() - t0))
    return r.returncode


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["fetch", "xml", "gfx", "manual", "plugin",
                                      "verify", "all"])
    ap.add_argument("--force", action="store_true", help="fetch: re-download everything")
    args, extra = ap.parse_known_args()
    gfx_failed = False

    if args.stage in ("fetch", "all"):
        run("fetch.py", (["--force"] if args.force else []) + extra,
            "fetch public AMD sources")
    if args.stage in ("xml", "all"):
        run("xml_to_db.py", extra, "machine-readable ISA -> isa.db")
    if args.stage in ("gfx", "all"):
        try:
            run("gfx_to_db.py", extra, "LLVM AMDGPUUsage -> gfx target map")
        except SystemExit as error:
            if args.stage != "all":
                raise
            gfx_failed = True
            print("WARNING: %s; continuing with manual pages and verification" % error,
                  file=sys.stderr)
    if args.stage in ("manual", "all"):
        # The only stage that is not stdlib-only: pymupdf lives in .venv.
        venv = os.path.join(ROOT, ".venv", "bin", "python")
        if not os.path.exists(venv):
            sys.exit("the manual stage needs pymupdf, and it is not optional -- "
                     "the skill is not authoritative without the ISA manuals:\n"
                     "    python3 -m venv .venv && .venv/bin/pip install pymupdf")
        print("\n== ISA PDFs -> markdown pages")
        t0 = time.time()
        r = subprocess.run([venv, os.path.join(BUILDER, "pdf_to_manual.py")] + extra)
        if r.returncode:
            sys.exit("manual stage failed")
        print("   (%.1fs)" % (time.time() - t0))
    if args.stage == "plugin":
        run("plugin.py", extra, "generate the amdgpu-isa plugin")
    if args.stage in ("verify", "all"):
        flags = ["--quick"] if args.stage == "all" else []
        if gfx_failed:
            flags.append("--skip-gfx")
        run("verify.py", flags + extra, "verify")
    return 0


if __name__ == "__main__":
    sys.exit(main())
