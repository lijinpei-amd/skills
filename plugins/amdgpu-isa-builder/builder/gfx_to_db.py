#!/usr/bin/env python3
"""Add the gfx-target -> architecture mapping to isa.db, derived from LLVM.

The XML names no gfx targets, so this is the missing link between "gfx950" and
the CDNA 4 instruction set. It comes from llvm/docs/AMDGPUUsage.rst (Apache-2.0
with LLVM exceptions), parsed out of the AMDGPU Processors table.

One trap this exists to avoid: LLVM's section headings are *encoding*
generations, not product architectures. LLVM lists gfx1250 under
"GCN GFX12 (RDNA 4)" because it uses GFX12-family encodings -- but gfx1250 is
CDNA 5 (MI450) hardware. Likewise "GCN GFX9 (Vega)" covers Vega *and* CDNA 1-4.
So the heading is taken as authoritative for the RDNA lines, where it is right,
and overridden for the GFX9 block and gfx125x, where it is not. Every override
carries its evidence.

Usage:  python3 builder/gfx_to_db.py [--rst FILE] [--db FILE]
"""

import argparse
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

SCHEMA = """
DROP TABLE IF EXISTS gfx_target;
CREATE TABLE gfx_target (
    gfx             TEXT PRIMARY KEY,  -- gfx950
    arch            TEXT,              -- cdna4; NULL when no XML spec exists
    llvm_generation TEXT,              -- "GCN GFX9 (Vega)" -- encoding family
    triple_arch     TEXT,              -- amdgpu9.50
    kind            TEXT,              -- dGPU / APU
    products        TEXT,              -- example products, ';'-separated
    basis           TEXT               -- how arch was decided
);
DROP VIEW IF EXISTS v_gfx;
CREATE VIEW v_gfx AS
SELECT g.gfx, g.arch, a.architecture_name, g.llvm_generation, g.kind,
       g.products, g.basis
FROM gfx_target g LEFT JOIN arch a USING (arch);
"""

# LLVM heading -> our arch key. Correct for every RDNA line.
HEADING_ARCH = {
    "RDNA 1": "rdna1", "RDNA 2": "rdna2", "RDNA 3": "rdna3",
    "RDNA 3.5": "rdna3_5", "RDNA 4": "rdna4",
}

# Where LLVM's encoding-generation heading is not the product architecture.
# Each entry records why, because these are exactly the rows a reader will
# doubt -- and the naive derivation gets them wrong.
OVERRIDES = {
    "gfx908": ("cdna1", "LLVM cites [AMD-GCN-GFX908-CDNA1] (MI100)"),
    "gfx90a": ("cdna2", "LLVM cites [AMD-GCN-GFX90A-CDNA2] (MI200)"),
    "gfx940": ("cdna3", "MI300 pre-production stepping of gfx942"),
    "gfx941": ("cdna3", "MI300 pre-production stepping of gfx942"),
    "gfx942": ("cdna3", "LLVM cites [AMD-GCN-GFX942-CDNA3] (MI300)"),
    "gfx950": ("cdna4", "MI350/CDNA4; grouped under GFX9 encodings by LLVM"),
    "gfx1250": ("cdna5", "MI450/CDNA5 hardware using GFX12-family encodings; "
                         "LLVM lists it under the GFX12 (RDNA 4) heading"),
    "gfx1251": ("cdna5", "MI450/CDNA5 hardware using GFX12-family encodings; "
                         "LLVM lists it under the GFX12 (RDNA 4) heading"),
}

# Vega and older: real targets, but AMD published no machine-readable XML for
# them, so they map to no arch in this corpus. Recorded rather than dropped, so
# `isa.py gfx gfx900` explains itself instead of just failing.
NO_SPEC = "no machine-readable ISA spec published for this generation"


def parse_table(text):
    """Yield (heading, columns) for each processor row of the AMDGPU table."""
    lines = text.splitlines()
    start = next(i for i, l in enumerate(lines) if ".. table:: AMDGPU Processors" in l)
    ruler_i = next(i for i in range(start, len(lines)) if lines[i].strip().startswith("==="))
    ruler = lines[ruler_i]

    # An RST simple table defines its columns by the runs of '=' in the ruler.
    spans = [(m.start(), m.end()) for m in re.finditer(r"=+", ruler)]
    spans[-1] = (spans[-1][0], 10 ** 6)  # last column runs to end of line

    heading = None
    row = None
    # Skip the header block: the second ruler closes it.
    body = ruler_i + 1
    body = next(i for i in range(body, len(lines)) if lines[i].strip().startswith("===")) + 1

    for line in lines[body:]:
        if line.strip().startswith("==="):
            break  # table ends
        h = re.match(r"\s*\*\*(.+?)\*\*", line)
        if h:
            heading = re.sub(r"\s*\[.*", "", h.group(1)).strip()
            continue
        cols = [line[s:e].strip() for s, e in spans]
        gfx = re.match(r"``(gfx[0-9a-z]+)``", cols[0])
        if gfx:
            if row:
                yield row
            row = [heading, gfx.group(1)] + cols[1:]
        elif row and any(cols):
            for i, c in enumerate(cols[1:], start=2):  # continuation lines
                if c:
                    row[i] = (row[i] + " " + c).strip()
    if row:
        yield row


def clean_products(s):
    if not s or "TBA" in s:
        return None
    s = re.sub(r"\.\.\s*TODO::.*", "", s)
    parts = [p.strip(" -") for p in re.split(r"\s*-\s(?=[A-Z0-9])", s) if p.strip(" -")]
    seen, out = set(), []
    for p in parts:
        p = re.sub(r"\s+", " ", p)
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return "; ".join(out) or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rst", default=os.path.join(ROOT, "sources", "llvm",
                                                  "AMDGPUUsage.rst"))
    ap.add_argument("--db", default=os.path.join(ROOT, "build", "isa.db"))
    args = ap.parse_args()

    if not os.path.exists(args.rst):
        sys.exit("no %s -- run 'build.py fetch' first" % args.rst)
    if not os.path.exists(args.db):
        sys.exit("no %s -- run 'build.py xml' first" % args.db)

    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)
    known = {r[0] for r in conn.execute("SELECT arch FROM arch")}

    n, mapped = 0, 0
    for heading, gfx, alt, triple, kind, feats, props, os_sup, products in (
            r + [""] * (9 - len(r)) for r in parse_table(
                open(args.rst, encoding="utf-8").read())):
        n += 1
        if gfx in OVERRIDES:
            arch, basis = OVERRIDES[gfx]
        else:
            key = next((k for k in HEADING_ARCH
                        if heading and heading.endswith("(%s)" % k)), None)
            if key:
                arch, basis = HEADING_ARCH[key], "LLVM heading %r" % heading
            else:
                arch, basis = None, NO_SPEC
        if arch and arch not in known:
            arch, basis = None, "arch %r not in this corpus" % arch
        if arch:
            mapped += 1
        conn.execute("INSERT OR REPLACE INTO gfx_target VALUES (?,?,?,?,?,?,?)",
                     (gfx, arch, heading, triple.strip("` "), kind,
                      clean_products(products), basis))
    conn.commit()

    covered = {r[0] for r in conn.execute(
        "SELECT DISTINCT arch FROM gfx_target WHERE arch IS NOT NULL")}
    missing = sorted(known - covered)
    for r in conn.execute("SELECT arch, COUNT(*) FROM gfx_target"
                          " WHERE arch IS NOT NULL GROUP BY arch ORDER BY arch"):
        print("  %-9s %d gfx target(s)" % r)
    if missing:
        print("  WARNING: no gfx target maps to %s" % ", ".join(missing))
    print("\n%d gfx targets parsed, %d mapped to an arch in this corpus" % (n, mapped))
    conn.close()
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
