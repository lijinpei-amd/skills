#!/usr/bin/env python3
"""amdgpu-isa-builder -- build the amd-gpu-isa skill from public AMD sources.

Stages, each independently runnable so a slow one is never re-run for free:

    fetch     download public AMD sources    -> sources/   (network)
    xml       machine-readable ISA -> SQLite -> build/isa.db
    gfx       LLVM AMDGPUUsage -> gfx map    -> build/isa.db (gfx_target)
    manual    ISA PDFs -> markdown + indexes -> build/manual/  (LOCAL ONLY)
    render    templates + db -> the skill    -> dist/amd-gpu-isa
    install   dist -> agent skills directories, manual pages included
    verify    counts, licence boundary, selftest
    all       fetch + xml + gfx + manual + render + verify

The manual pages are not optional: an ISA skill without pseudocode and prose is
not authoritative, so every build produces them and every install links them.
That is why pymupdf is a hard dependency (.venv), and why the result is
local-only -- see the licence note below.

Nothing AMD-derived is ever committed, and nothing built here may be shared:
sources/, build/ and dist/ are gitignored, and `verify` fails the build if git
is tracking any of it.

    python3 build.py all
    python3 build.py install --link
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
    ap.add_argument("stage", choices=["fetch", "xml", "gfx", "manual", "render",
                                      "install", "verify", "all"])
    ap.add_argument("--link", action="store_true", help="install: symlink, don't copy")
    ap.add_argument("--agents", default="claude,codex,pi")
    ap.add_argument("--force", action="store_true", help="fetch: re-download everything")
    args, extra = ap.parse_known_args()

    if args.stage in ("fetch", "all"):
        run("fetch.py", (["--force"] if args.force else []) + extra,
            "fetch public AMD sources")
    if args.stage in ("xml", "all"):
        run("xml_to_db.py", extra, "machine-readable ISA -> isa.db")
    if args.stage in ("gfx", "all"):
        run("gfx_to_db.py", extra, "LLVM AMDGPUUsage -> gfx target map")
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
    if args.stage in ("render", "all"):
        run("render.py", extra, "render the skill")
    if args.stage == "install":
        run("install.py", (["--link"] if args.link else [])
            + ["--agents", args.agents] + extra, "install into agent skill dirs")
    if args.stage in ("verify", "all"):
        run("verify.py", extra, "verify")

    if args.stage == "all":
        print("\nBuilt dist/amd-gpu-isa. Install it with:"
              "\n    python3 build.py install [--link]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
