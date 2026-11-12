#!/usr/bin/env python3
"""Query the AMD GPU ISA corpus.

Stdlib only -- no sqlite3 CLI binary required (it is often absent), no pip
install, same behaviour under Claude Code, Codex and pi.

The database is opened read-only.

Run `isa.py --help` for subcommands, or `isa.py schema` to write your own SQL.
"""

import argparse
import csv
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
SKILL_ROOT = os.path.dirname(HERE)
DB = os.path.join(SKILL_ROOT, "data", "isa.db")
DEFAULT_LIMIT = 50
SQL_TIMEOUT_SECONDS = 10.0
SELFTEST_TIMEOUT_SECONDS = 30.0

# The manual pages derive from AMD's PDFs, which may not be redistributed, so
# they never ship with this skill. amdgpu-isa-builder's `manual` stage builds
# them directly into persistent storage outside builder and plugin installs.
_data_home = os.environ.get("XDG_DATA_HOME", "")
MANUAL = os.path.abspath(os.path.expanduser(
    os.environ.get("AMDGPU_ISA_MANUAL") or os.path.join(
        _data_home if os.path.isabs(_data_home) else "~/.local/share", "amdgpu-isa", "manual")))


def connect():
    if not os.path.exists(DB):
        sys.exit("isa.py: corpus not found at %s\n"
                 "The amdgpu-isa install is incomplete; reinstall the plugin." % DB)
    conn = sqlite3.connect(Path(DB).as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def die(msg, hint=None):
    print("isa.py: %s" % msg, file=sys.stderr)
    if hint:
        print("  try: %s" % hint, file=sys.stderr)
    sys.exit(1)


def emit(rows, args, total=None, cols=None, extra=None, has_more=False):
    """Print rows with an exact total or an explicit truncation lower bound.

    `extra` holds additional top-level JSON keys; text output ignores it.
    """
    rows = list(rows)
    if rows and isinstance(rows[0], sqlite3.Row):
        columns = output_columns(rows[0].keys())
        # sqlite3.Row name lookup ignores case and returns the first duplicate.
        rows = [dict(zip(columns, tuple(r))) for r in rows]
    else:
        rows = [dict(r) for r in rows]
    if total is None and not has_more:
        total = len(rows)
    if args.json:
        out = {"total": total, "shown": len(rows), "rows": rows}
        if has_more:
            out.update(has_more=True, total_at_least=len(rows) + 1)
        out.update(extra or {})
        json.dump(out, sys.stdout, indent=2, default=str)
        print()
        return
    if not rows:
        print("-- 0 rows.")
        return
    cols = cols or list(rows[0].keys())
    width = {c: max(len(c), max(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    for c in cols:
        if c != "path":
            width[c] = min(width[c], 60)

    def fmt(vals):
        return "  ".join(str(v)[:width[c]].ljust(width[c]) for c, v in zip(cols, vals))

    print(fmt(cols))
    print(fmt(["-" * width[c] for c in cols]))
    for r in rows:
        print(fmt([r.get(c, "") for c in cols]))
    if has_more:
        print("\n-- at least %d rows match; showing %d. Use -n 0 for all."
              % (len(rows) + 1, len(rows)))
    elif len(rows) < total:
        print("\n-- %d rows match; showing %d. Use -n 0 for all." % (total, len(rows)))
    else:
        print("\n-- %d rows." % total)


def output_columns(columns):
    """Return unique names, keeping explicit labels and suffixing repeats."""
    columns = list(columns)
    reserved, used, result = set(columns), set(), []
    for name in columns:
        column, suffix = name, 2
        # Generated suffixes must not displace an explicitly named column.
        while column in used or (column != name and column in reserved):
            column = "%s_%d" % (name, suffix)
            suffix += 1
        used.add(column)
        result.append(column)
    return result


def manual_available():
    """Return whether the manual's published table of contents is present."""
    return os.path.isfile(os.path.join(MANUAL, "toc.tsv"))


def manual_archs():
    """Return the architectures indexed by the published table of contents."""
    path = os.path.join(MANUAL, "toc.tsv")
    if not os.path.isfile(path):
        return set()
    try:
        with open(path, encoding="utf-8", newline="") as stream:
            rows = csv.reader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
            header = next(rows, [])
            if "arch" not in header:
                return set()
            index = header.index("arch")
            return {row[index] for row in rows if len(row) > index and row[index]}
    except FileNotFoundError:
        return set()


def validate_filters(conn, args):
    """Normalize architecture/group case and reject unknown filter names."""
    archs = {r[0] for r in conn.execute("SELECT arch FROM arch")}
    if args.cmd == "manual":
        # Manual availability belongs to this machine, independently of XML
        # coverage. A missing TOC is reported by cmd_manual with build hints.
        archs = None
        if manual_available():
            archs = manual_archs()
        args._manual_archs = archs
    for field in ("arch", "arch_a", "arch_b"):
        value = getattr(args, field, None)
        if value is not None:
            normalized = value.lower()
            if archs is not None and normalized not in archs:
                if args.cmd == "manual":
                    die("no ISA manual pages for architecture %r at %s" % (value, MANUAL),
                        "isa.py manual --toc; installed manual archs: %s"
                        % (", ".join(sorted(archs)) or "none -- run build.py all"))
                die("unknown architecture: %r" % value,
                    "isa.py archs; valid names: %s" % ", ".join(sorted(archs)))
            setattr(args, field, normalized)
    value = getattr(args, "group", None)
    if value is not None:
        groups = {r[0] for r in conn.execute("SELECT DISTINCT name FROM functional_group")}
        normalized = value.upper()
        if normalized not in groups:
            die("unknown functional group: %r" % value,
                "isa.py groups; valid names: %s" % ", ".join(sorted(groups)))
        args.group = normalized


def limited(conn, sql, params, args, count_sql=None):
    """Run a query with a LIMIT, plus a COUNT so the total is never hidden."""
    total = conn.execute(count_sql or "SELECT COUNT(*) FROM (%s)" % sql,
                         params).fetchone()[0]
    if args.limit:
        sql += " LIMIT %d" % args.limit
    return conn.execute(sql, params).fetchall(), total


def resolve(conn, name, arch=None):
    """Return encoding variants with alias identity for each accepting arch."""
    q = ('SELECT n.arch, n.query_name, n.canonical_name, n.is_alias,'
         ' i."group", i.encoding, i.opcode, i.condition, i.description'
         ' FROM (SELECT DISTINCT arch, query_name, canonical_name, is_alias'
         '       FROM v_name) n'
         ' JOIN v_inst i ON i.arch = n.arch AND i.name = n.canonical_name'
         ' WHERE n.query_name = ?')
    p = [name.upper()]
    if arch:
        q += " AND n.arch = ?"
        p.append(arch)
    return conn.execute(q + " ORDER BY n.arch, n.canonical_name, i.encoding, i.condition", p).fetchall()


def other_names(conn, name):
    """Archs that lack this spelling but accept the same instruction as another.

    `which` and `show` answer for the exact spelling, as an assembler would. A
    renamed instruction is still present elsewhere under the name its
    canonical form has there, so report those archs instead of letting the
    spelling's absence read as the instruction's.
    """
    q = name.upper()
    rows = conn.execute(
        "WITH RECURSIVE edges(a,b) AS ("
        " SELECT a.alias,i.name FROM instruction_alias a JOIN instruction i USING (inst_id)"
        " UNION SELECT i.name,a.alias FROM instruction_alias a JOIN instruction i USING (inst_id)"
        "), linked(name) AS (SELECT ? UNION SELECT e.b FROM edges e"
        " JOIN linked l ON e.a=l.name)"
        " SELECT DISTINCT i.arch,i.name FROM instruction i JOIN linked l ON i.name=l.name"
        " WHERE i.arch NOT IN (SELECT arch FROM v_name WHERE query_name=?)"
        " ORDER BY i.arch,i.name", (q, q))
    by_arch = {}
    for arch, canonical in rows:
        by_arch.setdefault(arch, []).append(canonical)
    return [dict(arch=arch, name=names[0], names=names) for arch, names in by_arch.items()]


def print_other_names(others):
    by_name = {}
    for o in others:
        by_name.setdefault(" or ".join(o.get("names", [o["name"]])), []).append(o["arch"])
    for other, archs in by_name.items():
        print("# also present as %s in %d more arch%s: %s\n"
              % (other, len(archs), "" if len(archs) == 1 else "s", ", ".join(archs)))


# --------------------------------------------------------------------------- #

def cmd_show(conn, args):
    rows = resolve(conn, args.name, args.arch)
    if not rows:
        same = [o for o in other_names(conn, args.name) if o["arch"] == args.arch]
        if same:
            die("%s is not a name in %s; the same instruction is %s there"
                % (args.name.upper(), args.arch, same[0]["name"]),
                "isa.py show %s --arch %s" % (same[0]["name"], args.arch))
        die("no instruction named %r%s" % (args.name, " in " + args.arch if args.arch else ""),
            "isa.py which %s" % args.name.upper() if args.arch
            else "isa.py search '%s'" % re.escape(args.name.lower()))
    others = [] if args.arch else other_names(conn, args.name)
    if not args.json:
        print_other_names(others)
        aliases = {}
        for row in rows:
            if row["is_alias"]:
                aliases.setdefault(row["canonical_name"], set()).add(row["arch"])
        for canonical, archs in aliases.items():
            print("# %s is an alias of %s in %s\n"
                  % (args.name.upper(), canonical, ", ".join(sorted(archs))))
    total = len(rows)
    detail = next((r for r in rows if not r["is_alias"]), rows[0])
    if args.limit > 0:
        rows = rows[:args.limit]
    emit(rows, args, total, extra=None if args.arch else {"also_present_as": others})

    if args.json:
        return
    arch, canonical = detail["arch"], detail["canonical_name"]
    ops = conn.execute(
        "SELECT DISTINCT encoding, condition, ord, field_name, operand_type,"
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
        print("\n## operands (%s, %s)" % (canonical, arch))
        for key, encs in seen.items():
            print("\n  %s:" % ", ".join(encs))
            for line in key:
                print(line)

    page = manual_page_for(canonical, arch)
    if page:
        print("\n## manual (%s, %s)\n  %s  (printed p.%s)  -- semantics, pseudocode, notes"
              % (canonical, arch, page["path"],
                 page["page_printed"]))


def cmd_which(conn, args):
    hits = resolve(conn, args.name)
    if not hits:
        die("no instruction named %r" % args.name,
            "isa.py search '%s'" % re.escape(args.name.lower()))
    columns = ("arch", "query_name", "canonical_name", "is_alias", "group", "encoding", "opcode")
    by_encoding, conditions = {}, {}
    for hit in hits:
        key = tuple(hit[c] for c in columns)
        if key not in by_encoding:
            by_encoding[key] = dict(zip(columns, key))
            conditions[key] = set()
        # Duplicate source rows do not add a condition variant. None counts as
        # one condition too, unlike SQLite COUNT(DISTINCT condition).
        conditions[key].add(hit["condition"])
    for key, row in by_encoding.items():
        row["n_variants"] = len(conditions[key])
    rows = list(by_encoding.values())
    total = len(rows)
    n_arch = conn.execute("SELECT COUNT(*) FROM arch").fetchone()[0]
    others = other_names(conn, args.name)
    if not args.json:
        print("%s: in %d/%d archs\n" % (args.name.upper(), len({r["arch"] for r in rows}), n_arch))
        print_other_names(others)
    if args.limit > 0:
        rows = rows[:args.limit]
    text_columns = ["arch", "group", "encoding", "opcode", "n_variants"]
    if any(hit["canonical_name"] != args.name.upper() for hit in hits):
        text_columns.insert(1, "canonical_name")
    emit(rows, args, total, cols=text_columns, extra={"also_present_as": others})


def compile_regex(pattern, hint):
    """Compile a case-insensitive regex or exit with a corrective hint."""
    try:
        return re.compile(pattern, re.I)
    except re.error as exc:
        die("invalid regular expression: %r (%s)" % (pattern, exc), hint)


def cmd_search(conn, args):
    if args.fts:
        q = ("SELECT DISTINCT f.name AS name, f.arch AS arch,"
             " p.\"group\" AS \"group\", f.description AS description"
             " FROM fts_inst f JOIN v_presence p ON p.name = f.name AND p.arch = f.arch"
             " WHERE fts_inst MATCH ?")
        p = [args.pattern]
    else:
        pattern = compile_regex(args.pattern,
                                r"isa.py search 'V_PK_FMA_F32\('  # escape literal parentheses")
        q = ("SELECT name, arch, \"group\", opcode FROM v_presence"
             " WHERE name REGEXP ?")
        p = [args.pattern]
        conn.create_function("REGEXP", 2, lambda pat, s: bool(s is not None and pattern.search(s)))
    if args.arch:
        q += " AND %sarch = ?" % ("f." if args.fts else "")
        p.append(args.arch)
    if args.group:
        q += ' AND %s"group" = ?' % ("p." if args.fts else "")
        p.append(args.group)
    try:
        rows, total = limited(conn, q + " ORDER BY name, arch", p, args)
    except sqlite3.Error as exc:
        if args.fts:
            die("full-text search error: %s" % exc,
                "isa.py search --fts '\"packed 16-bit\"'  # quote phrases and hyphenated terms")
        die("search error: %s" % exc, "isa.py search '^V_PK_FMA_F32$'")
    if not rows:
        die("no match for %r" % args.pattern, "isa.py search --fts '<words>'")
    emit(rows, args, total)


def cmd_list(conn, args):
    q, p = 'SELECT name, arch, "group", encoding, opcode, condition FROM v_inst WHERE 1=1', []
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
    grp = [args.group] if args.group else []

    def only_in(x, y):
        """Names present in arch x but not in arch y."""
        return conn.execute(
            'SELECT name FROM v_presence WHERE arch = ?%s'
            ' EXCEPT SELECT name FROM v_presence WHERE arch = ? ORDER BY name'
            % where, [x] + grp + [y]).fetchall()

    added = only_in(b, a)
    removed = only_in(a, b)
    shown_added = added[:args.limit or None]
    shown_removed = removed[:args.limit or None]
    if args.json:
        json.dump({"from": a, "to": b,
                   "added": [r[0] for r in shown_added],
                   "removed": [r[0] for r in shown_removed],
                   "added_total": len(added), "added_shown": len(shown_added),
                   "removed_total": len(removed), "removed_shown": len(shown_removed),
                   "has_more": len(shown_added) < len(added)
                               or len(shown_removed) < len(removed)}, sys.stdout, indent=2)
        print()
        return
    print("%s -> %s: +%d added, -%d removed\n" % (a, b, len(added), len(removed)))
    n = args.limit or max(len(added), len(removed))
    for label, rows in (("added in " + b, added), ("only in " + a, removed)):
        print("## %s (%d)" % (label, len(rows)))
        for r in rows[:n]:
            print("  " + r[0])
        if len(rows) > n:
            print("  ... %d more (use -n 0)" % (len(rows) - n))
        print()


def manual_rows(name, want=None):
    """Read one of the manual TSV indexes. Plain files, so grep works too."""
    path = os.path.join(MANUAL, name)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            cols = fh.readline().rstrip("\n").split("\t")
            out = []
            for line in fh:
                row = dict(zip(cols, line.rstrip("\n").split("\t")))
                if row.get("path"):
                    row["path"] = os.path.abspath(os.path.join(MANUAL, row["path"]))
                if want and any(row.get(k) != v for k, v in want.items() if v):
                    continue
                out.append(row)
    except FileNotFoundError:
        return []
    return out


def manual_page_for(inst, arch):
    """Definition page of an instruction, if the manuals are installed."""
    if not manual_available():
        return None
    rows = [r for r in manual_rows("instr-index.tsv",
                                   {"instruction": inst, "arch": arch})
            if r["kind"] == "definition"]
    return rows[0] if rows else None


def manual_search_hint(args):
    """Suggest manual searches that work without machine-readable coverage."""
    arch = " --arch " + args.arch if args.arch else ""
    pattern = re.escape((args.inst or args.format or args.grep or "instruction").upper())
    return "isa.py manual --grep '%s'%s | isa.py manual --toc%s" % (pattern, arch, arch)


def cmd_manual(conn, args):
    if not manual_available():
        # Not a broken install: AMD's PDF licence forbids shipping the pages, so
        # each machine builds its own. XML coverage determines whether show is
        # a useful fallback for the requested architecture.
        inst = "%s%s" % (args.inst.upper(), " --arch " + args.arch if args.arch else "") \
            if args.inst else "V_FMA_F32 --arch rdna4"
        have_xml = not args.arch or conn.execute(
            "SELECT 1 FROM arch WHERE arch = ?", (args.arch,)).fetchone()
        hint = ("isa.py show %s   # the XML-derived answer" % inst if have_xml
                else manual_search_hint(args) + "   # after build.py all")
        die("no ISA manual pages at %s; they are built per machine with "
            "amdgpu-isa-builder (`build.py all`), or set AMDGPU_ISA_MANUAL"
            % MANUAL, hint)

    if args.toc:
        rows = manual_rows("toc.tsv", {"arch": args.arch})
        if not rows:
            die("no ISA manual pages in the TOC at %s" % MANUAL,
                "build the manuals with amdgpu-isa-builder (`build.py all`)")
        if args.level is not None:
            rows = [r for r in rows if int(r["level"]) <= args.level]
        emit(rows[:args.limit or None], args, len(rows))
        return

    if args.inst:
        rows = manual_rows("instr-index.tsv",
                           {"instruction": args.inst.upper(), "arch": args.arch})
        if not rows:
            have_xml = not args.arch or conn.execute(
                "SELECT 1 FROM arch WHERE arch = ?", (args.arch,)).fetchone()
            die("no manual page for %r%s" % (args.inst, " in " + args.arch if args.arch else ""),
                "isa.py which %s   # which archs have it at all" % args.inst.upper()
                if have_xml else manual_search_hint(args))
        emit(rows[:args.limit or None], args, len(rows))
        return

    if args.format:
        rows = manual_rows("format-index.tsv",
                           {"encoding": args.format.upper(), "arch": args.arch})
        if not rows:
            have_xml = conn.execute("SELECT 1 FROM arch WHERE arch = ?", (args.arch,)).fetchone()
            hint = ("isa.py manual --toc --arch %s" % args.arch
                    if args.arch and not have_xml
                    else "isa.py encodings --arch %s" % (args.arch or "rdna4"))
            die("no manual page for encoding %r" % args.format,
                hint)
        emit(rows[:args.limit or None], args, len(rows))
        return

    if args.grep:
        pat = compile_regex(args.grep,
                            r"isa.py manual --grep 'wait\('  # escape literal parentheses")
        archs = [args.arch] if args.arch else sorted(args._manual_archs or ())
        hits = []
        for a in archs:
            d = os.path.join(MANUAL, a)
            try:
                files = sorted(os.listdir(d))
            except FileNotFoundError:
                continue
            for f in files:
                if not f.endswith(".md"):
                    continue
                p = os.path.join(d, f)
                for n, line in enumerate(open(p, encoding="utf-8"), 1):
                    if pat.search(line):
                        hits.append({"arch": a, "path": p,
                                     "line": n, "text": line.strip()[:90]})
        if not hits:
            die("no match for %r in the manuals" % args.grep,
                "isa.py manual --toc%s; use a broader --grep pattern"
                % (" --arch " + args.arch if args.arch else ""))
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
    emit(rows[:args.limit or None], args, len(rows))


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
    deadline = time.monotonic() + SQL_TIMEOUT_SECONDS
    timed_out = False

    def progress():
        nonlocal timed_out
        timed_out = time.monotonic() >= deadline
        return timed_out

    conn.set_progress_handler(progress, 1000)
    try:
        # Fetch one extra row to detect truncation without rewriting the user's
        # SQL or scanning the entire result for a COUNT.
        cursor = conn.execute(q)
        try:
            rows = cursor.fetchmany(args.limit + 1) if args.limit > 0 else cursor.fetchall()
        finally:
            cursor.close()
    except (sqlite3.Error, sqlite3.Warning) as exc:
        if timed_out:
            die("SQL query exceeded the %.0f-second execution deadline" % SQL_TIMEOUT_SECONDS,
                "add a terminating condition to recursive queries or narrow the SELECT")
        die("SQL error: %s" % exc, "isa.py schema")
    finally:
        conn.set_progress_handler(None, 0)
    has_more = args.limit > 0 and len(rows) > args.limit
    if has_more:
        rows = rows[:args.limit]
    emit(rows, args, has_more=has_more)


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
    # These need the locally built manual pages; skipped when they are absent.
    ("manual definition page", ["manual", "--inst", "V_FMA_F32", "--arch", "rdna4"]),
    ("manual toc", ["manual", "--toc", "--arch", "rdna4", "--level", "1", "-n", "5"]),
    ("raw sql", ["sql", "SELECT arch, COUNT(*) FROM v_presence GROUP BY arch"]),
]


def cmd_selftest(conn, args):
    import subprocess
    archs = sorted(manual_archs())
    manual_arch = "rdna4" if "rdna4" in archs else archs[0] if archs else None
    corpus_archs = [row[0] for row in conn.execute("SELECT arch FROM arch ORDER BY arch")]
    ok = run = skipped = 0
    for label, argv in SELFTESTS:
        argv = list(argv)
        if argv[0] != "manual" and corpus_archs:
            if "--arch" in argv and argv[argv.index("--arch") + 1] not in corpus_archs:
                argv[argv.index("--arch") + 1] = corpus_archs[0]
            if argv[0] == "diff":
                argv[1:3] = [arch if arch in corpus_archs else corpus_archs[min(i, len(corpus_archs) - 1)]
                             for i, arch in enumerate(argv[1:3])]
            arch = argv[argv.index("--arch") + 1] if "--arch" in argv else None
            if argv[0] in ("show", "which"):
                names = [row[0] for row in conn.execute(
                    "SELECT DISTINCT query_name FROM v_name WHERE (? IS NULL OR arch=?)"
                    " ORDER BY query_name", (arch, arch))]
                if argv[1] not in names and names:
                    argv[1] = names[0]
                    if label == "alias lookup":
                        label = "alternate spelling lookup"
            elif argv[0] == "search":
                if "--fts" in argv:
                    if not conn.execute("SELECT 1 FROM fts_inst WHERE fts_inst MATCH ? LIMIT 1",
                                        (argv[2],)).fetchone():
                        name = conn.execute("SELECT name FROM fts_inst LIMIT 1").fetchone()
                        if name:
                            argv[2] = '"' + name[0].replace('"', '""') + '"'
                else:
                    names = [row[0] for row in conn.execute(
                        "SELECT name FROM instruction WHERE (? IS NULL OR arch=?) ORDER BY name",
                        (arch, arch))]
                    if names and not any(re.search(argv[1], name, re.I) for name in names):
                        argv[1] = "^" + re.escape(names[0]) + "$"
            elif argv[0] == "list" and "--group" in argv:
                groups = [row[0] for row in conn.execute(
                    "SELECT DISTINCT group_name FROM instruction WHERE arch=?"
                    " AND group_name IS NOT NULL ORDER BY group_name", (arch,))]
                if groups and argv[argv.index("--group") + 1] not in groups:
                    argv[argv.index("--group") + 1] = groups[0]
            elif argv[0] == "encodings" and "--encoding" in argv:
                encs = [row[0] for row in conn.execute(
                    "SELECT name FROM encoding WHERE arch=? ORDER BY name", (arch,))]
                if encs and argv[argv.index("--encoding") + 1] not in encs:
                    argv[argv.index("--encoding") + 1] = encs[0]
            elif argv[0] == "gfx":
                targets = [row[0] for row in conn.execute("SELECT gfx FROM gfx_target ORDER BY gfx")]
                if targets and argv[1] not in targets:
                    argv[1] = targets[0]
        if argv[0] == "manual":
            if manual_arch is None:
                print("skip %-24s (no indexed manual architectures at %s)" % (label, MANUAL))
                skipped += 1
                continue
            argv[argv.index("--arch") + 1] = manual_arch
            if "--inst" in argv and not manual_page_for(argv[argv.index("--inst") + 1], manual_arch):
                label = "manual text search"
                argv = ["manual", "--grep", ".", "--arch", manual_arch, "-n", "1"]
        run += 1
        command = [sys.executable, "-I", "-B", os.path.realpath(__file__)] + argv
        try:
            r = subprocess.run(command, capture_output=True, text=True,
                               timeout=SELFTEST_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired) as exc:
            r = subprocess.CompletedProcess(command, 1, "", str(exc))
        if r.returncode == 0:
            ok += 1
        print("%s %-24s isa.py %s"
              % ("ok  " if r.returncode == 0 else "FAIL", label, " ".join(argv)))
        if r.returncode != 0:
            print("     %s" % (r.stderr.strip().splitlines() or [""])[0])
    print("\n%d/%d examples passed%s"
          % (ok, run, ", %d skipped" % skipped if skipped else ""))
    return 0 if ok == run else 1


def positive_level(value):
    """Parse a positive manual outline depth."""
    level = int(value)
    if level < 1:
        raise argparse.ArgumentTypeError("manual TOC level must be at least 1")
    return level


def main():
    # Shared options, attached to both the top level and every subcommand, so
    # `isa.py -n 5 list` and `isa.py list -n 5` both work -- agents write either.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("-n", "--limit", type=int, default=argparse.SUPPRESS,
                        help="max rows (0 = unlimited; default: all for which, 50 otherwise);"
                             " truncated SQL queries report a lower bound instead of a total")

    ap = argparse.ArgumentParser(
        parents=[common],
        description="Query the AMD GPU ISA corpus (CDNA 1-5, RDNA 1-4).",
        epilog="Data: AMD machine-readable ISA XML (MIT, 'AMD Public Use').")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser(parents=[common], name="show", help="everything about one instruction")
    p.add_argument("name"); p.add_argument("--arch"); p.set_defaults(fn=cmd_show)

    p = sub.add_parser(parents=[common], name="which",
                       help="which archs have it, opcodes per encoding and condition-variant counts")
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
    p.add_argument("--level", type=positive_level, help="with --toc: max outline depth")
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

    args = ap.parse_args(namespace=argparse.Namespace(json=False, limit=None))
    if args.limit is None:
        args.limit = 0 if args.cmd == "which" else DEFAULT_LIMIT
    if args.limit < 0:
        ap.error("-n/--limit must be 0 or greater")
    conn = connect()
    validate_filters(conn, args)
    return args.fn(conn, args) or 0


if __name__ == "__main__":
    sys.exit(main())
