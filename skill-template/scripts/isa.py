#!/usr/bin/env python3
"""Query the AMD GPU ISA corpus.

Stdlib only -- no sqlite3 CLI binary required (it is often absent), no pip
install, same behaviour under Claude Code, Codex and pi.

The database is opened read-only.

Run `isa.py --help` for subcommands, or `isa.py schema` to write your own SQL.
"""

import argparse
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.realpath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
DB = os.path.join(SKILL_ROOT, "data", "isa.db")
DEFAULT_LIMIT = 50


def connect():
    if not os.path.exists(DB):
        sys.exit("isa.py: corpus not found at %s\n"
                 "Rebuild it with the amdgpu-isa-builder skill." % DB)
    conn = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def die(msg, hint=None):
    print("isa.py: %s" % msg, file=sys.stderr)
    if hint:
        print("  try: %s" % hint, file=sys.stderr)
    sys.exit(1)


def emit(rows, args, total=None, cols=None):
    """Print rows as a table or JSON, always stating the true total."""
    rows = [dict(r) for r in rows]
    if total is None:
        total = len(rows)
    if args.json:
        json.dump({"total": total, "shown": len(rows), "rows": rows},
                  sys.stdout, indent=2, default=str)
        print()
        return
    if not rows:
        return
    cols = cols or list(rows[0].keys())
    width = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    for c in cols:
        width[c] = min(width[c], 60)

    def fmt(vals):
        return "  ".join(str(v)[:width[c]].ljust(width[c]) for c, v in zip(cols, vals))

    print(fmt(cols))
    print(fmt(["-" * width[c] for c in cols]))
    for r in rows:
        print(fmt([r.get(c, "") for c in cols]))
    if len(rows) < total:
        print("\n-- %d rows match; showing %d. Use -n 0 for all." % (total, len(rows)))
    else:
        print("\n-- %d rows." % total)


def limited(conn, sql, params, args, count_sql=None):
    """Run a query with a LIMIT, plus a COUNT so the total is never hidden."""
    total = conn.execute(count_sql or "SELECT COUNT(*) FROM (%s)" % sql,
                         params).fetchone()[0]
    if args.limit:
        sql += " LIMIT %d" % args.limit
    return conn.execute(sql, params).fetchall(), total


def resolve(conn, name, arch=None):
    """Map a possibly-aliased name to its canonical name(s)."""
    q = "SELECT DISTINCT canonical_name, is_alias FROM v_name WHERE query_name = ?"
    p = [name.upper()]
    if arch:
        q += " AND arch = ?"
        p.append(arch)
    return conn.execute(q, p).fetchall()


# --------------------------------------------------------------------------- #

def cmd_show(conn, args):
    hits = resolve(conn, args.name, args.arch)
    if not hits:
        die("no instruction named %r%s" % (args.name, " in " + args.arch if args.arch else ""),
            "isa.py search '%s'" % re.escape(args.name.lower()))
    canonical = hits[0]["canonical_name"]
    if hits[0]["is_alias"]:
        print("# %s is an alias of %s\n" % (args.name.upper(), canonical))

    q = ("SELECT arch, \"group\", encoding, opcode, condition, description"
         " FROM v_inst WHERE name = ?")
    p = [canonical]
    if args.arch:
        q += " AND arch = ?"
        p.append(args.arch)
    rows, total = limited(conn, q + " ORDER BY arch, encoding", p, args)
    if not rows:
        die("%s not present in %s" % (canonical, args.arch), "isa.py which %s" % canonical)
    emit(rows, args, total)

    if args.json:
        return
    arch = args.arch or rows[0]["arch"]
    ops = conn.execute(
        "SELECT encoding, condition, ord, field_name, operand_type,"
        " data_format, size, is_input, is_output FROM v_operand"
        " WHERE name = ? AND arch = ? ORDER BY encoding, condition, ord",
        (canonical, arch)).fetchall()
    if ops:
        # One instruction usually has several encodings with identical operand
        # lists. Printing them back to back without the encoding name reads as
        # duplicated output, so group and label -- and collapse exact repeats.
        by_enc, order = {}, []
        for o in ops:
            key = (o["encoding"] if o["condition"] in (None, "default")
                   else "%s [%s]" % (o["encoding"], o["condition"]))
            by_enc.setdefault(key, []).append(o)
            if key not in order:
                order.append(key)

        def render(rows):
            return ["  %-2s %-10s %-18s %-14s %sb %s"
                    % (o["ord"], o["field_name"] or "-", o["operand_type"],
                       o["data_format"], o["size"],
                       ("in" if o["is_input"] else "")
                       + ("out" if o["is_output"] else ""))
                    for o in rows]

        seen = {}
        for enc in order:
            key = tuple(render(by_enc[enc]))
            seen.setdefault(key, []).append(enc)
        print("\n## operands (%s)" % arch)
        for key, encs in seen.items():
            print("\n  %s:" % ", ".join(encs))
            for line in key:
                print(line)

    page = manual_page_for(canonical, arch)
    if page:
        print("\n## manual\n  %s  (printed p.%s)  -- semantics, pseudocode, notes"
              % (page["path"], page["page_printed"]))


def cmd_which(conn, args):
    hits = resolve(conn, args.name)
    if not hits:
        die("no instruction named %r" % args.name,
            "isa.py search '%s'" % re.escape(args.name.lower()))
    canonical = hits[0]["canonical_name"]
    rows = conn.execute(
        "SELECT arch, \"group\", n_encodings, opcode FROM v_presence"
        " WHERE name = ? ORDER BY arch", (canonical,)).fetchall()
    n_arch = conn.execute("SELECT COUNT(*) FROM arch").fetchone()[0]
    if not args.json:
        print("%s: in %d/%d archs\n" % (canonical, len(rows), n_arch))
    emit(rows, args, len(rows))


def cmd_search(conn, args):
    if args.fts:
        q = ("SELECT DISTINCT f.name AS name, f.arch AS arch,"
             " p.\"group\" AS \"group\", f.description AS description"
             " FROM fts_inst f JOIN v_presence p ON p.name = f.name AND p.arch = f.arch"
             " WHERE fts_inst MATCH ?")
        p = [args.pattern]
    else:
        q = ("SELECT name, arch, \"group\", opcode FROM v_presence"
             " WHERE name REGEXP ?")
        p = [args.pattern]
        conn.create_function("REGEXP", 2, lambda pat, s: bool(
            s is not None and re.search(pat, s, re.I)))
    if args.arch:
        q += " AND arch = ?"
        p.append(args.arch)
    if args.group:
        q += ' AND "group" = ?'
        p.append(args.group)
    rows, total = limited(conn, q + " ORDER BY name, arch", p, args)
    if not rows:
        die("no match for %r" % args.pattern, "isa.py search --fts '<words>'")
    emit(rows, args, total)


def cmd_list(conn, args):
    q, p = 'SELECT name, arch, "group", encoding, opcode FROM v_inst WHERE 1=1', []
    for field, val in (("arch", args.arch), ('"group"', args.group),
                       ("encoding", args.encoding)):
        if val:
            q += " AND %s = ?" % field
            p.append(val)
    rows, total = limited(conn, q + " ORDER BY name", p, args)
    if not rows:
        die("nothing matches", "isa.py groups --arch %s" % (args.arch or "cdna5"))
    emit(rows, args, total)


def cmd_encodings(conn, args):
    if args.encoding:
        rows, total = limited(
            conn,
            "SELECT ord, field_name, bit_offset, bit_count, description"
            " FROM encoding_field WHERE arch = ? AND encoding = ? ORDER BY ord",
            [args.arch, args.encoding], args)
        if not rows:
            die("no encoding %r in %s" % (args.encoding, args.arch),
                "isa.py encodings --arch %s" % args.arch)
        meta = conn.execute("SELECT bit_count, identifier_mask, description"
                            " FROM encoding WHERE arch = ? AND name = ?",
                            (args.arch, args.encoding)).fetchone()
        if not args.json and meta:
            print("%s (%s): %d bits  mask %s\n"
                  % (args.encoding, args.arch, meta["bit_count"] or 0,
                     meta["identifier_mask"]))
        emit(rows, args, total)
    else:
        rows, total = limited(
            conn, "SELECT name, bit_count, description FROM encoding"
                  " WHERE arch = ? ORDER BY name", [args.arch], args)
        emit(rows, args, total)


def cmd_diff(conn, args):
    a, b = args.arch_a, args.arch_b
    where = ' AND "group" = ?' if args.group else ""
    pa = [a] + ([args.group] if args.group else [])
    pb = [b] + ([args.group] if args.group else [])
    added = conn.execute(
        'SELECT name FROM v_presence WHERE arch = ?%s'
        ' EXCEPT SELECT name FROM v_presence WHERE arch = ?%s ORDER BY name'
        % (where, where), pa[:1] + pa[1:] + pb[:1] + pb[1:]).fetchall()
    removed = conn.execute(
        'SELECT name FROM v_presence WHERE arch = ?%s'
        ' EXCEPT SELECT name FROM v_presence WHERE arch = ?%s ORDER BY name'
        % (where, where), pb[:1] + pb[1:] + pa[:1] + pa[1:]).fetchall()
    if args.json:
        json.dump({"from": a, "to": b,
                   "added": [r[0] for r in added],
                   "removed": [r[0] for r in removed]}, sys.stdout, indent=2)
        print()
        return
    print("%s -> %s: +%d added, -%d removed\n" % (a, b, len(added), len(removed)))
    n = args.limit or len(added)
    for label, rows in (("added in " + b, added), ("only in " + a, removed)):
        print("## %s (%d)" % (label, len(rows)))
        for r in rows[:n]:
            print("  " + r[0])
        if len(rows) > n:
            print("  ... %d more (use -n 0)" % (len(rows) - n))
        print()


MANUAL = os.path.join(SKILL_ROOT, "manual")


def manual_rows(name, want=None):
    """Read one of the manual TSV indexes. Plain files, so grep works too."""
    path = os.path.join(MANUAL, name)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as fh:
        cols = fh.readline().rstrip("\n").split("\t")
        out = []
        for line in fh:
            row = dict(zip(cols, line.rstrip("\n").split("\t")))
            if want and any(row.get(k) != v for k, v in want.items() if v):
                continue
            out.append(row)
    return out


def manual_page_for(inst, arch):
    """Definition page of an instruction, if the manuals are installed."""
    rows = [r for r in manual_rows("instr-index.tsv",
                                   {"instruction": inst, "arch": arch})
            if r["kind"] == "definition"]
    return rows[0] if rows else None


def cmd_manual(conn, args):
    if not os.path.isdir(MANUAL):
        die("the ISA manuals are not installed here",
            "build them locally: .venv/bin/python builder/pdf_to_manual.py"
            " && build.py install --manual")

    if args.toc:
        rows = manual_rows("toc.tsv", {"arch": args.arch})
        if args.level:
            rows = [r for r in rows if int(r["level"]) <= args.level]
        emit(rows[:args.limit or None], args, len(rows))
        return

    if args.inst:
        rows = manual_rows("instr-index.tsv",
                           {"instruction": args.inst.upper(), "arch": args.arch})
        if not rows:
            die("no manual page for %r%s" % (args.inst, " in " + args.arch if args.arch else ""),
                "isa.py which %s   # which archs have it at all" % args.inst.upper())
        emit(rows, args, len(rows))
        return

    if args.format:
        rows = manual_rows("format-index.tsv",
                           {"encoding": args.format.upper(), "arch": args.arch})
        if not rows:
            die("no manual page for encoding %r" % args.format,
                "isa.py encodings --arch %s" % (args.arch or "rdna4"))
        emit(rows, args, len(rows))
        return

    if args.grep:
        pat = re.compile(args.grep, re.I)
        archs = [args.arch] if args.arch else sorted(
            d for d in os.listdir(MANUAL) if os.path.isdir(os.path.join(MANUAL, d)))
        hits = []
        for a in archs:
            d = os.path.join(MANUAL, a)
            for f in sorted(os.listdir(d)):
                if not f.endswith(".md"):
                    continue
                p = os.path.join(d, f)
                for n, line in enumerate(open(p, encoding="utf-8"), 1):
                    if pat.search(line):
                        hits.append({"arch": a, "path": "%s/%s" % (a, f),
                                     "line": n, "text": line.strip()[:90]})
        if not hits:
            die("no match for %r in the manuals" % args.grep)
        emit(hits[:args.limit or None], args, len(hits))
        return

    die("nothing to do", "isa.py manual --inst V_FMA_F32 --arch rdna4"
                         " | --grep 'wait state' | --toc --arch rdna4")


def cmd_gfx(conn, args):
    """gfx target -> architecture, or the whole map."""
    if args.target:
        t = args.target.lower()
        if not t.startswith("gfx"):
            t = "gfx" + t
        r = conn.execute("SELECT * FROM v_gfx WHERE gfx = ?", (t,)).fetchone()
        if not r:
            near = conn.execute("SELECT gfx FROM gfx_target WHERE gfx LIKE ?"
                                " ORDER BY gfx LIMIT 5", (t[:6] + "%",)).fetchall()
            die("unknown gfx target %r" % args.target,
                ("did you mean %s?" % ", ".join(x[0] for x in near)) if near
                else "isa.py gfx   # lists every target")
        if args.json:
            emit([r], args, 1)
            return
        if r["arch"]:
            print("%s -> %s (%s)" % (r["gfx"], r["arch"], r["architecture_name"]))
        else:
            print("%s -> not in this corpus" % r["gfx"])
        print("  basis:            %s" % r["basis"])
        print("  LLVM generation:  %s   <- encoding family, not the product arch"
              % r["llvm_generation"])
        if r["products"]:
            print("  products:         %s" % r["products"])
        if r["arch"]:
            n = conn.execute("SELECT COUNT(*) FROM v_presence WHERE arch = ?",
                             (r["arch"],)).fetchone()[0]
            print("\n%d instructions. Try: isa.py list --arch %s --group VALU"
                  % (n, r["arch"]))
        return

    q = "SELECT gfx, arch, llvm_generation, products FROM v_gfx"
    if args.arch:
        q += " WHERE arch = '%s'" % args.arch.replace("'", "")
    rows, total = limited(conn, q + " ORDER BY gfx", [], args)
    emit(rows, args, total)


def cmd_archs(conn, args):
    rows = conn.execute(
        "SELECT a.arch, a.architecture_name, a.release_date, a.schema_version,"
        " (SELECT COUNT(*) FROM instruction i WHERE i.arch = a.arch) AS instructions"
        " FROM arch a ORDER BY a.arch").fetchall()
    emit(rows, args, len(rows))


def cmd_groups(conn, args):
    q = ('SELECT arch, "group", COUNT(*) AS instructions FROM v_presence'
         ' WHERE 1=1')
    p = []
    if args.arch:
        q += " AND arch = ?"
        p.append(args.arch)
    rows, total = limited(conn, q + ' GROUP BY arch, "group" ORDER BY arch, "group"',
                          p, args)
    emit(rows, args, total)


def cmd_sql(conn, args):
    q = args.query.strip().rstrip(";")
    if not re.match(r"(?is)^\s*(select|with)\b", q):
        die("only SELECT/WITH queries are allowed", "isa.py schema")
    try:
        total = conn.execute("SELECT COUNT(*) FROM (%s)" % q).fetchone()[0]
        rows = conn.execute(q + (" LIMIT %d" % args.limit if args.limit else "")).fetchall()
    except sqlite3.Error as exc:
        die("SQL error: %s" % exc, "isa.py schema")
    emit(rows, args, total)


def cmd_schema(conn, args):
    for r in conn.execute("SELECT type, name, sql FROM sqlite_master"
                          " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"
                          " AND name NOT LIKE 'fts_inst_%' ORDER BY type DESC, name"):
        print(r["sql"].strip() + ";\n")


SELFTESTS = [
    ("archs", ["archs"]),
    ("show one instruction", ["show", "V_FMA_F32", "--arch", "rdna4"]),
    ("alias lookup", ["show", "S_LOAD_DWORD", "--arch", "rdna4"]),
    ("cross-arch presence", ["which", "V_DOT2_F32_BF16"]),
    ("regex search", ["search", "^v_mfma.*f16$", "--arch", "cdna4"]),
    ("fts search", ["search", "--fts", "dot AND bf16"]),
    ("list a group", ["list", "--arch", "cdna5", "--group", "VALU", "-n", "5"]),
    ("encoding bit layout", ["encodings", "--arch", "rdna4", "--encoding", "ENC_VOP3"]),
    ("arch diff", ["diff", "cdna4", "cdna5", "-n", "5"]),
    ("gfx target lookup", ["gfx", "gfx950"]),
    ("gfx trap: gfx1250", ["gfx", "gfx1250"]),
    # These need the local-only manual pages; skipped on a shareable install.
    ("manual definition page", ["manual", "--inst", "V_FMA_F32", "--arch", "rdna4"]),
    ("manual toc", ["manual", "--toc", "--arch", "rdna4", "--level", "1", "-n", "5"]),
    ("raw sql", ["sql", "SELECT arch, COUNT(*) FROM v_presence GROUP BY arch"]),
]


def cmd_selftest(conn, args):
    import subprocess
    have_manual = os.path.isdir(MANUAL)
    ok = run = skipped = 0
    for label, argv in SELFTESTS:
        if argv[0] == "manual" and not have_manual:
            print("skip %-24s (manual pages not installed here)" % label)
            skipped += 1
            continue
        run += 1
        r = subprocess.run([sys.executable, os.path.realpath(__file__)] + argv,
                           capture_output=True, text=True)
        if r.returncode == 0:
            ok += 1
        print("%s %-24s isa.py %s"
              % ("ok  " if r.returncode == 0 else "FAIL", label, " ".join(argv)))
        if r.returncode != 0:
            print("     %s" % (r.stderr.strip().splitlines() or [""])[0])
    print("\n%d/%d examples passed%s"
          % (ok, run, ", %d skipped" % skipped if skipped else ""))
    return 0 if ok == run else 1


def main():
    # Shared options, attached to both the top level and every subcommand, so
    # `isa.py -n 5 list` and `isa.py list -n 5` both work -- agents write either.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    common.add_argument("-n", "--limit", type=int, default=DEFAULT_LIMIT,
                        help="max rows (0 = unlimited); the true total is always reported")

    ap = argparse.ArgumentParser(
        parents=[common],
        description="Query the AMD GPU ISA corpus (CDNA 1-5, RDNA 1-4).",
        epilog="Data: AMD machine-readable ISA XML (MIT, 'AMD Public Use').")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser(parents=[common], name="show", help="everything about one instruction")
    p.add_argument("name"); p.add_argument("--arch"); p.set_defaults(fn=cmd_show)

    p = sub.add_parser(parents=[common], name="which", help="which archs have it, opcode per arch")
    p.add_argument("name"); p.set_defaults(fn=cmd_which)

    p = sub.add_parser(parents=[common], name="search", help="find instructions by regex or full text")
    p.add_argument("pattern"); p.add_argument("--arch"); p.add_argument("--group")
    p.add_argument("--fts", action="store_true", help="full-text search descriptions")
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser(parents=[common], name="list", help="list instructions in a group / encoding")
    p.add_argument("--arch"); p.add_argument("--group"); p.add_argument("--encoding")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser(parents=[common], name="encodings", help="encoding list, or one encoding's bit layout")
    p.add_argument("--arch", required=True); p.add_argument("--encoding")
    p.set_defaults(fn=cmd_encodings)

    p = sub.add_parser(parents=[common], name="diff", help="what changed between two archs")
    p.add_argument("arch_a"); p.add_argument("arch_b"); p.add_argument("--group")
    p.set_defaults(fn=cmd_diff)

    p = sub.add_parser(parents=[common], name="manual",
                       help="the ISA manuals: definition pages, TOC, full text")
    p.add_argument("--inst", help="definition page of an instruction")
    p.add_argument("--format", help="pages documenting an encoding")
    p.add_argument("--grep", help="search the manual text (regex)")
    p.add_argument("--toc", action="store_true", help="table of contents")
    p.add_argument("--level", type=int, help="with --toc: max outline depth")
    p.add_argument("--arch")
    p.set_defaults(fn=cmd_manual)

    p = sub.add_parser(parents=[common], name="gfx",
                       help="gfx target -> architecture (gfx950 -> cdna4)")
    p.add_argument("target", nargs="?", help="e.g. gfx950; omit to list all")
    p.add_argument("--arch", help="list only targets of this arch")
    p.set_defaults(fn=cmd_gfx)

    sub.add_parser(parents=[common], name="archs", help="architectures in the corpus").set_defaults(fn=cmd_archs)

    p = sub.add_parser(parents=[common], name="groups", help="functional groups and their sizes")
    p.add_argument("--arch"); p.set_defaults(fn=cmd_groups)

    p = sub.add_parser(parents=[common], name="sql", help="run a read-only SELECT against the corpus")
    p.add_argument("query"); p.set_defaults(fn=cmd_sql)

    sub.add_parser(parents=[common], name="schema", help="print the schema, for writing SQL").set_defaults(fn=cmd_schema)
    sub.add_parser(parents=[common], name="selftest", help="verify the corpus answers correctly").set_defaults(fn=cmd_selftest)

    args = ap.parse_args()
    conn = connect()
    return args.fn(conn, args) or 0


if __name__ == "__main__":
    sys.exit(main())
