#!/usr/bin/env python3
"""Produce a shareable tarball of the skill, excluding anything PDF-derived.

This is the single enforcement point for the licence boundary. The local install
may well contain the ISA manual pages -- with `install --link` the installed
skill *is* dist/, so a `manual` symlink lands there -- and those pages must not
travel. Rather than thread a conditional through every stage, everything is built
freely and one command decides what may leave the machine.

What ships: isa.db (XML-derived, MIT / "AMD Public Use"), isa.py, SKILL.md,
NOTICE.md, build-info.json. What never ships: manual/ and any PDF, and the
tarball is inspected after writing to prove it.

Usage:  python3 builder/export.py [--out FILE]
"""

import argparse
import json
import os
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

# Anything matching these never leaves the machine.
EXCLUDE_DIRS = {"manual"}
EXCLUDE_EXT = (".pdf", ".xml", ".zip")


def excluded(rel):
    parts = rel.split(os.sep)
    return (any(p in EXCLUDE_DIRS for p in parts)
            or rel.lower().endswith(EXCLUDE_EXT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "dist", "amd-gpu-isa"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        sys.exit("nothing at %s -- run 'build.py render' first" % args.src)

    name = os.path.basename(args.src.rstrip("/"))
    out = args.out or os.path.join(
        ROOT, "dist", "%s-%s.tar.gz" % (name, time.strftime("%Y%m%d")))

    info_path = os.path.join(args.src, "build-info.json")
    info = json.load(open(info_path)) if os.path.exists(info_path) else {}
    if info.get("includes_pdf_derived"):
        sys.exit("refusing to export: build-info.json says this build includes "
                 "PDF-derived data")

    skipped, included = [], []
    with tarfile.open(out, "w:gz") as tar:
        for dirpath, dirnames, filenames in os.walk(args.src):
            # Do not descend into excluded directories, and do not follow the
            # manual symlink.
            dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
            for f in sorted(filenames):
                full = os.path.join(dirpath, f)
                rel = os.path.relpath(full, args.src)
                if excluded(rel):
                    skipped.append(rel)
                    continue
                tar.add(full, arcname=os.path.join(name, rel))
                included.append(rel)
        for d in sorted(set(os.listdir(args.src)) & EXCLUDE_DIRS):
            skipped.append(d + "/")

    # Prove it, rather than trust the walk.
    with tarfile.open(out) as tar:
        members = tar.getnames()
    bad = [m for m in members if excluded(os.path.relpath(m, name))]
    if bad:
        os.remove(out)
        sys.exit("export aborted: PDF-derived content reached the tarball: %s"
                 % ", ".join(bad[:5]))

    print("  exported %s  (%.1f MB, %d files)"
          % (os.path.relpath(out, ROOT), os.path.getsize(out) / 1e6, len(included)))
    for rel in included:
        print("    + %s" % rel)
    for rel in skipped:
        print("    - %s   (local only, not shared)" % rel)
    print("\n  XML-derived only; NOTICE.md carries the AMD copyright and MIT terms.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
