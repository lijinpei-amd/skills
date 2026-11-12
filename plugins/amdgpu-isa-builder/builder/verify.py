#!/usr/bin/env python3
"""Check the build and the generated plugin before either is trusted.

Four kinds of check:
  * counts   -- re-derive instruction/encoding/operand totals straight from the
                XML with an independent code path, and compare against the DB
  * licence  -- assert no AMD-derived file is tracked by the builder, that no
                PDF-derived file reaches the plugin, and that every architecture
                shipped is MIT / "AMD Public Use"
  * sync     -- with --plugin, regenerate into scratch and compare logical DB
                contents and generated text with the committed plugin
  * answers  -- run the query CLI's own selftest and regressions on the plugin

Usage:  python3 builder/verify.py [--quick] [--plugin] [--plugin-dir DIR]
"""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import plugin
from check_helpers import failure_detail, protect, run, validate_json

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


def verify_gfx_map(db, pinned=True):
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
    malformed = [r["gfx"] for r in conn.execute("SELECT gfx, triple_arch, kind FROM gfx_target")
                 if r["kind"] not in ("dGPU", "APU")
                 or not re.fullmatch(r"amdgpu\d+\.[0-9a-z]+", r["triple_arch"] or "")]
    metadata_ok = check("gfx kinds/triples contain no table markup", not malformed,
                        ", ".join(malformed))
    if not pinned:
        conn.close()
        return metadata_ok and not missing
    # The cases where LLVM's own grouping disagrees with the product arch.
    ok = True
    for gfx, want in (("gfx1250", "cdna5"), ("gfx950", "cdna4"),
                      ("gfx942", "cdna3"), ("gfx1201", "rdna4")):
        got = conn.execute("SELECT arch FROM gfx_target WHERE gfx=?", (gfx,)).fetchone()
        if not got or got[0] != want:
            ok = False
            check("gfx map %s -> %s" % (gfx, want), False,
                  "got %s" % (got[0] if got else "missing"))
    for gfx, triple, kind in (
            ("gfx803", "amdgpu8.03", "dGPU"), ("gfx950", "amdgpu9.50", "dGPU"),
            ("gfx1103", "amdgpu11.03", "APU"), ("gfx1250", "amdgpu12.50", "APU"),
            ("gfx1251", "amdgpu12.51", "APU")):
        got = conn.execute("SELECT triple_arch, kind FROM gfx_target WHERE gfx=?",
                           (gfx,)).fetchone()
        if not got or tuple(got) != (triple, kind):
            metadata_ok = False
            check("gfx metadata %s" % gfx, False, "got %s" % (tuple(got) if got else "missing",))
    check("gfx metadata matches LLVM processor rows", metadata_ok)
    conn.close()
    return check("gfx map: encoding-generation traps handled", ok)


def verify_gfx_parser():
    """Table borders and repeated sub-row scalars must not enter metadata."""
    from gfx_to_db import parse_table
    text = """.. table:: AMDGPU Processors
     =========== =============== =============== =====
     Processor   Alternative     Triple          Kind
     =========== =============== =============== =====
     **GCN GFX8**
     -------------------------------------------------
     ``gfx803``  - ``fiji``      ``amdgpu8.03``   dGPU
     \\           - ``polaris10`` ``amdgpu8.03``   dGPU
     **GCN GFX9**
     -------------------------------------------------
     ``gfx950``                  ``amdgpu9.50``   dGPU
     =========== =============== =============== =====
"""
    rows = list(parse_table(text))
    return check("gfx parser: borders/sub-rows preserve scalars", rows == [
        ["GCN GFX8", "gfx803", "- ``fiji`` - ``polaris10``", "``amdgpu8.03``", "dGPU"],
        ["GCN GFX9", "gfx950", "", "``amdgpu9.50``", "dGPU"],
    ], "" if len(rows) == 2 else "unexpected processor rows")


def verify_licence_boundary():
    """The .gitignore is the licence boundary; assert git is not tracking any
    AMD-derived artefact."""
    directory = Path(ROOT)
    if not any(os.path.lexists(parent / ".git") for parent in (directory, *directory.parents)):
        print("  skip no AMD-derived files tracked by git            standalone builder has no git metadata")
        return True
    try:
        # Git paths are bytes; undecodable bytes must not crash the check.
        result = run(["git", "-C", ROOT, "ls-files", "-z"],
                     encoding="utf-8", errors="backslashreplace")
        if result.returncode:
            return check("no AMD-derived files tracked by git", False, failure_detail(result))
        tracked = [path for path in result.stdout.split("\0") if path]
    except OSError as error:
        return check("no AMD-derived files tracked by git", False, str(error))
    bad = [f for f in tracked
           if f.endswith((".pdf", ".xml", ".db", ".zip"))
           or f.startswith(("sources/", "build/", "dist/"))]
    return check("no AMD-derived files tracked by git", not bad,
                 ("tracked: " + ", ".join(bad[:3])) if bad else "")


def verify_plugin_purity(skill):
    """The plugin is shared, so it must declare itself redistributable and carry
    no PDF-derived content: no manual pages, no source documents, nothing the
    generator does not own."""
    info_path = os.path.join(skill, "build-info.json")
    try:
        if os.path.islink(info_path):
            raise ValueError("build-info.json is a symlink")
        with open(info_path, encoding="utf-8") as stream:
            info = json.load(stream)
        if not isinstance(info, dict):
            raise ValueError("build-info.json must be an object")
        check("plugin build-info: redistributable, no PDF content",
              info.get("redistributable") is True
              and info.get("includes_pdf_derived") is False,
              "includes_pdf_derived=%s redistributable=%s"
              % (info.get("includes_pdf_derived"), info.get("redistributable")))
        check("plugin build-info hash matches its DB",
              info.get("db_sha256") == plugin.sha256(os.path.join(skill, "data", "isa.db")))
    except (OSError, ValueError, TypeError) as error:
        check("plugin declares its redistribution status", False, str(error))
    layout = Path(skill).parent.name == "skills"
    root = str(Path(skill).parent.parent) if layout else skill
    prefix = "skills/%s/" % Path(skill).name if layout else ""
    owned = {prefix + rel for rel in plugin.output_paths()}
    if layout:
        owned.update((".claude-plugin/plugin.json", ".codex-plugin/plugin.json"))
    extra = [rel for rel in sorted(owned)
             if not os.path.isfile(os.path.join(root, rel))
             or os.path.islink(os.path.join(root, rel))]
    for dirpath, dirnames, filenames in os.walk(root):
        for d in list(dirnames):
            if d == "__pycache__" and not os.path.islink(os.path.join(dirpath, d)):
                dirnames.remove(d)
                continue
            # A symlinked directory (a `manual` link) is a leak, not a subtree.
            if os.path.islink(os.path.join(dirpath, d)):
                extra.append(os.path.relpath(os.path.join(dirpath, d), root))
                dirnames.remove(d)
        extra += [r for r in (os.path.relpath(os.path.join(dirpath, f), root)
                              for f in filenames)
                  if r not in owned or os.path.islink(os.path.join(root, r))]
    check("plugin holds only generated files", not extra,
          ("missing, symlinked, or unexpected: " + ", ".join(sorted(set(extra))[:3])) if extra else "")
    with closing(sqlite3.connect(Path(skill, "data", "isa.db").resolve().as_uri() + "?mode=ro",
                                 uri=True)) as conn:
        lic = conn.execute("SELECT DISTINCT license, sensitivity FROM arch").fetchall()
    ok = all(l == "MIT" and s.startswith("AMD Public Use") for l, s in lic)
    return check("isa.db: every arch is MIT/Public Use", ok, str(lic[:1]))


def verify_plugin_in_sync(skill):
    """Regenerate and compare the plugin for the explicit --plugin check.

    Preserve its DB storage when the local corpus is logically unchanged, so
    size and hash text follow the bytes that would remain installed.
    """
    if not os.path.isfile(os.path.join(plugin.BUILD, "isa.db")):
        return check("plugin matches its generator", False,
                     "no build/isa.db -- run 'build.py xml' and 'build.py gfx'")
    with tempfile.TemporaryDirectory(prefix="amdgpu-isa-plugin-") as scratch:
        target = os.path.join(scratch, "amdgpu-isa")
        installed_db = os.path.join(skill, "data", "isa.db")
        if os.path.isfile(installed_db) and not os.path.islink(installed_db):
            os.makedirs(os.path.join(target, "data"))
            shutil.copyfile(installed_db, os.path.join(target, "data", "isa.db"))
        comparison = {}
        out = plugin.generate(target, comparison=comparison)
        stale = []
        for rel in plugin.output_paths():
            a, b = os.path.join(out, rel), os.path.join(skill, rel)
            if not os.path.isfile(b) or os.path.islink(b):
                stale.append(rel)
            elif rel == "data/isa.db":
                if not comparison["db_equivalent"]:
                    stale.append(rel)
            elif rel == "build-info.json":
                with open(a, encoding="utf-8") as stream:
                    generated = json.load(stream)
                with open(b, encoding="utf-8") as stream:
                    installed = json.load(stream)
                if not isinstance(generated, dict) or not isinstance(installed, dict):
                    stale.append(rel)
                    continue
                hashes_ok = (generated.pop("db_sha256", None)
                             == comparison["db_sha256"]
                             and installed.pop("db_sha256", None)
                             == comparison["previous_db_sha256"])
                if not hashes_ok or generated != installed:
                    stale.append(rel)
            else:
                with open(a, "rb") as source, open(b, "rb") as target:
                    if source.read() != target.read():
                        stale.append(rel)
    return check("plugin matches its generator", not stale,
                 ("stale: %s -- run 'build.py plugin'" % ", ".join(stale)) if stale else "")


def verify_manual_directory():
    """Manual pages must live in persistent storage outside an install."""
    from manual_link import MANUAL_ROOT
    indexes = ("toc.tsv", "instr-index.tsv", "format-index.tsv")
    missing = [name for name in indexes if not os.path.isfile(os.path.join(MANUAL_ROOT, name))]
    ok = os.path.isdir(MANUAL_ROOT) and not missing
    return check("manual pages stored persistently", ok,
                 "" if ok else "%s; missing %s -- run 'build.py all'"
                 % (MANUAL_ROOT, ", ".join(missing)))


def verify_answers(skill, quick=False):
    isa = os.path.join(skill, "scripts", "isa.py")
    if not os.path.exists(isa):
        return check("query CLI selftest", False, "no generated plugin")
    r = run([sys.executable, "-I", "-B", isa, "selftest"])
    last = (r.stdout.strip().splitlines() or [""])[-1]
    check("query CLI selftest", r.returncode == 0,
          last if r.returncode == 0 else failure_detail(r))
    all_ok = r.returncode == 0
    all_ok = protect(check, "installed manual queries", verify_manual_queries, isa) is not False and all_ok
    if quick:
        return all_ok
    groups = (verify_diff_direction, verify_query_options, verify_query_answers,
              verify_query_errors, verify_query_regressions, verify_other_names,
              verify_sql_queries, verify_search_patterns, verify_corpus_growth)
    for group in groups:
        all_ok = protect(check, group.__name__, group, isa) is not False and all_ok
    from regressions import run_answer_checks
    all_ok = protect(check, "answer integration regressions", run_answer_checks, isa, check) is not False and all_ok
    from query_regressions import run_checks
    return protect(check, "query review regressions", run_checks, isa, check) is not False and all_ok


def verify_manual_queries(isa):
    """Query each installed manual architecture independently of the XML corpus."""
    from manual_link import manual_archs
    cases = [("manual: %s remains queryable" % arch,
              ["manual", "--toc", "--arch", arch.upper(), "--json"],
              lambda result, arch=arch: result["shown"] > 0
              and all(row["arch"] == arch and os.path.isfile(row["path"])
                      for row in result["rows"])) for arch in sorted(manual_archs())]
    return verify_json_cases(isa, cases)


def verify_other_names(isa):
    """`which`/`show` answer for the exact spelling but must report the archs
    that have the same instruction under another name, in both alias
    directions. Expected sets come from the base tables, not the CLI's views."""
    db = Path(isa).resolve().parent.parent / "data" / "isa.db"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        def accepts(name):
            return {r[0] for r in conn.execute(
                "SELECT arch FROM instruction WHERE name = ?"
                " UNION SELECT i.arch FROM instruction_alias a"
                " JOIN instruction i USING (inst_id) WHERE a.alias = ?", (name, name))}
        # (spelling asked, the other spelling): renamed-to and renamed-from.
        pairs = [("V_XOR_ADD_U32", "V_XAD_U32"),
                 ("BUFFER_ATOMIC_ADD_U32", "BUFFER_ATOMIC_ADD")]
        expected = {q: {(a, other) for a in accepts(other) - accepts(q)}
                    for q, other in pairs}
    all_ok = True
    for q, _ in pairs:
        want = expected[q]
        result = run([sys.executable, "-I", "-B", isa, "which", q, "--json"])
        ok, detail = validate_json(result, lambda data:
                                  bool(want) and {(o["arch"], o["name"])
                                                  for o in data["also_present_as"]} == want)
        all_ok = check("which %s: also present under other names" % q, ok,
                       detail) and all_ok
        arch, other = min(want) if want else ("cdna4", "?")
        result = run([sys.executable, "-I", "-B", isa, "show", q, "--arch", arch])
        ok = (result.returncode == 1 and not result.stdout
              and "isa.py show %s --arch %s" % (other, arch) in result.stderr)
        all_ok = check("show %s --arch %s: points at %s" % (q, arch, other), ok) and all_ok
    return all_ok


def verify_query_options(isa):
    """Check JSON output, search filters and options before the subcommand."""
    cases = [
        ("JSON: alias lookup",
         ["show", "S_LOAD_DWORD", "--arch", "rdna4", "--json"],
         lambda result: result["shown"] > 0
         and all(row["arch"] == "rdna4"
                 and row["query_name"] == "S_LOAD_DWORD"
                 and row["canonical_name"] == "S_LOAD_B32"
                 and row["is_alias"] == 1 for row in result["rows"])),
        ("JSON: FTS arch/group filters",
         ["search", "--fts", "dot AND bf16", "--arch", "cdna4",
          "--group", "VALU", "-n", "0", "--json"],
         lambda result: result["total"] == result["shown"] > 0
         and all(row["arch"] == "cdna4" and row["group"] == "VALU"
                 for row in result["rows"])),
        ("JSON option before subcommand",
         ["--json", "which", "V_DOT2_F32_BF16"],
         lambda result: result["total"] == result["shown"] > 0),
        ("row limit before subcommand",
         ["--json", "-n", "5", "list", "--arch", "cdna5", "--group", "VALU"],
         lambda result: result["shown"] == 5 and result["total"] > 5),
        ("subcommand row limit overrides global limit",
         ["--json", "-n", "5", "list", "-n", "3", "--arch", "cdna5"],
         lambda result: result["shown"] == 3 and result["total"] > 3),
    ]
    return verify_json_cases(isa, cases)


def verify_json_cases(isa, cases):
    """Run labeled CLI commands and check their parsed JSON responses."""
    all_ok = True
    for label, argv, validate in cases:
        result = run([sys.executable, "-I", "-B", isa] + argv, timeout=5)
        ok, detail = validate_json(result, validate)
        all_ok = check(label, ok, detail) and all_ok
    return all_ok


def verify_sql_queries(isa):
    """SQL limits/comments must compose, and recursion must finish or fail."""
    cases = [
        ("SQL: user LIMIT composes with CLI limit",
         ["sql", "SELECT 7 AS value LIMIT 1", "--json"],
         lambda result: result["total"] == result["shown"] == 1
         and result["rows"] == [{"value": 7}]),
        ("SQL: trailing line comment is preserved",
         ["sql", "SELECT 7 AS value -- a trailing comment", "--json"],
         lambda result: result["total"] == result["shown"] == 1
         and result["rows"] == [{"value": 7}]),
        ("SQL: terminated statement with a trailing comment",
         ["sql", "SELECT 7 AS value; -- a trailing comment", "--json"],
         lambda result: result["total"] == result["shown"] == 1
         and result["rows"] == [{"value": 7}]),
        ("SQL: punctuation in literals stays unchanged",
         ["sql", "SELECT '; -- literal' AS value; -- a trailing comment", "--json"],
         lambda result: result["total"] == result["shown"] == 1
         and result["rows"] == [{"value": "; -- literal"}]),
        ("SQL: truncation reports an incomplete count",
         ["sql", "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL"
          " SELECT n+1 FROM seq WHERE n<9) SELECT n FROM seq", "-n", "3", "--json"],
         lambda result: result["total"] is None and result["shown"] == 3
         and result["has_more"] is True and result["total_at_least"] == 4
         and result["rows"] == [{"n": 1}, {"n": 2}, {"n": 3}]),
        ("SQL: unlimited finite query has an exact count",
         ["sql", "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL"
          " SELECT n+1 FROM seq WHERE n<9) SELECT n FROM seq", "-n", "0", "--json"],
         lambda result: result["total"] == result["shown"] == 9
         and [r["n"] for r in result["rows"]] == list(range(1, 10))),
        ("SQL: productive recursion needs no unbounded COUNT",
         ["sql", "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL"
          " SELECT n+1 FROM seq) SELECT n FROM seq", "-n", "3", "--json"],
         lambda result: result["total"] is None and result["shown"] == 3
         and result["has_more"] is True and result["total_at_least"] == 4
         and result["rows"] == [{"n": 1}, {"n": 2}, {"n": 3}]),
    ]
    all_ok = verify_json_cases(isa, cases)
    from regressions import expired_sql
    for label, query, limit in (
            ("nonproductive recursion", "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL"
             " SELECT n+1 FROM seq) SELECT n FROM seq WHERE n<0", "3"),
            ("unlimited recursion", "WITH RECURSIVE seq(n) AS (SELECT 1 UNION ALL"
             " SELECT n+1 FROM seq) SELECT n FROM seq", "0")):
        try:
            result = expired_sql(isa, ["sql", query, "-n", limit, "--json"])
            ok = (result.returncode == 1 and not result.stdout
                  and "SQL query exceeded" in result.stderr
                  and "terminating condition" in result.stderr
                  and "Traceback" not in result.stderr)
        except subprocess.TimeoutExpired:
            ok = False
        all_ok = check("SQL: bounds " + label, ok) and all_ok
    return all_ok


def verify_search_patterns(isa):
    """Invalid FTS/regex syntax must give a corrective error in either format."""
    cases = [
        (["search", "--fts", "packed 16-bit"], "full-text search error", "isa.py search --fts"),
        (["search", "V_PK_FMA_F32("], "invalid regular expression", "isa.py search"),
    ]
    all_ok = True
    with tempfile.TemporaryDirectory(prefix="amdgpu-isa-patterns-") as scratch:
        manual = Path(scratch)
        (manual / "rdna4").mkdir()
        (manual / "rdna4" / "page.md").write_text("wait( cycle\n", encoding="utf-8")
        (manual / "toc.tsv").write_text(
            "arch\tlevel\tpath\ttitle\nrdna4\t1\trdna4/page.md\tRegex fixture\n", encoding="utf-8")
        env = dict(os.environ, AMDGPU_ISA_MANUAL=scratch)
        cases.append((["manual", "--grep", "wait(", "--arch", "rdna4"],
                      "invalid regular expression", "isa.py manual --grep"))
        for argv, error, hint in cases:
            for output in ([], ["--json"]):
                result = run([sys.executable, "-I", "-B", isa] + argv + output,
                                        env=env, timeout=5)
                ok = (result.returncode == 1 and not result.stdout
                      and error in result.stderr and hint in result.stderr
                      and "try:" in result.stderr and "Traceback" not in result.stderr)
                all_ok = check("pattern error: %s%s" %
                               (" ".join(argv), " (JSON)" if output else ""), ok) and all_ok
        result = run([sys.executable, "-I", "-B", isa, "manual", "--grep", r"wait\(",
                                 "--arch", "rdna4", "--json"], env=env, timeout=5)
        ok, detail = validate_json(result, lambda data:
                                  data["total"] == 1 and data["rows"][0]["line"] == 1)
        all_ok = check("manual: escaped regex still matches literal text", ok, detail) and all_ok
    return verify_json_cases(isa, [
        ("FTS: a quoted hyphenated phrase still matches",
         ["search", "--fts", '"packed 16-bit"', "--json"],
         lambda result: result["total"] >= result["shown"] > 0),
    ]) and all_ok


def verify_corpus_growth(isa):
    """Query verification must survive new archs, changed opcodes and diffs."""
    db = Path(isa).resolve().parent.parent / "data" / "isa.db"
    before = len(failures)
    with tempfile.TemporaryDirectory(prefix="amdgpu-isa-growth-") as scratch:
        root = Path(scratch)
        (root / "scripts").mkdir()
        (root / "data").mkdir()
        cli = root / "scripts" / "isa.py"
        shutil.copyfile(isa, cli)
        copy = root / "data" / "isa.db"
        shutil.copyfile(db, copy)
        with closing(sqlite3.connect(copy)) as conn:
            conn.row_factory = sqlite3.Row
            arch = dict(conn.execute("SELECT * FROM arch ORDER BY arch LIMIT 1").fetchone())
            arch["arch"], arch["architecture_name"] = "verify_extra", "Verifier extra architecture"
            conn.execute("INSERT INTO arch (%s) VALUES (%s)" %
                         (", ".join(arch), ", ".join("?" for _ in arch)), list(arch.values()))

            def clone(name, destination, new_name=None):
                source = conn.execute("SELECT inst_id FROM instruction WHERE name=?"
                                      " ORDER BY arch LIMIT 1", (name,)).fetchone()[0]
                inst_id = conn.execute(
                    "INSERT INTO instruction (arch, name, group_name, description)"
                    " SELECT ?, ?, group_name, description FROM instruction WHERE inst_id=?",
                    (destination, new_name or name, source)).lastrowid
                conn.execute("INSERT INTO instruction_alias SELECT ?, alias"
                             " FROM instruction_alias WHERE inst_id=?", (inst_id, source))
                conn.execute("INSERT INTO inst_encoding (inst_id, encoding_name, condition, opcode)"
                             " SELECT ?, encoding_name, condition, opcode FROM inst_encoding"
                             " WHERE inst_id=?", (inst_id, source))

            for name in ("S_LOAD_DWORD", "V_ADD_F32", "V_XAD_U32"):
                clone(name, "verify_extra")
            # Ensure the extra arch accepts the alias even if the first V_XAD row does not.
            inst_id = conn.execute("SELECT inst_id FROM instruction WHERE arch='verify_extra'"
                                   " AND name='V_XAD_U32'").fetchone()[0]
            conn.execute("INSERT INTO instruction_alias VALUES (?, 'V_XOR_ADD_U32')", (inst_id,))
            clone("V_ADD_F32", "rdna3_5", "VERIFY_ADDED_INSTRUCTION")
            conn.execute("UPDATE inst_encoding SET opcode=opcode+1000 WHERE inst_id IN"
                         " (SELECT inst_id FROM instruction WHERE arch='cdna4' AND name='V_ADD_F32')")
            conn.commit()
        verify_query_answers(str(cli))
    return check("query checks handle corpus growth", len(failures) == before)


def verify_query_answers(isa):
    """Check alias identity, per-encoding opcodes and group diff membership."""
    db = Path(isa).resolve().parent.parent / "data" / "isa.db"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        # Base-table membership is independent of the CLI's alias and diff views.
        archs = {r[0] for r in conn.execute("SELECT arch FROM arch")}
        names = {arch: {r[0] for r in conn.execute(
            "SELECT name FROM instruction WHERE arch = ?", (arch,))}
            for arch in archs}
        branch = {arch: {r[0] for r in conn.execute(
            "SELECT name FROM instruction WHERE arch = ? AND group_name = 'BRANCH'",
            (arch,))} for arch in ("cdna4", "cdna5")}
        add_f32 = {(arch, encoding, opcode): count for arch, encoding, opcode, count
                   in conn.execute(
                       "SELECT arch, encoding_name, opcode, COUNT(*) FROM ("
                       " SELECT DISTINCT i.arch, e.encoding_name, e.opcode, e.condition"
                       " FROM instruction i JOIN inst_encoding e USING (inst_id)"
                       " WHERE i.name = 'V_ADD_F32')"
                       " GROUP BY arch, encoding_name, opcode")}
        identities = {name: set(conn.execute(
            "SELECT arch, name, 0 FROM instruction WHERE name=?"
            " UNION SELECT i.arch, i.name, 1 FROM instruction_alias a"
            " JOIN instruction i USING (inst_id) WHERE a.alias=?", (name, name)))
            for name in ("S_LOAD_DWORD", "V_XOR_ADD_U32")}
        vop_opcodes = dict(conn.execute(
            "SELECT e.encoding_name, e.opcode FROM instruction i"
            " JOIN inst_encoding e USING (inst_id) WHERE i.arch='cdna4' AND i.name='V_ADD_F32'"
            " AND e.encoding_name IN ('ENC_VOP2', 'ENC_VOP3')"))

    def load_identity(result):
        rows = result["rows"]
        return (result["total"] == result["shown"] == len(rows) > 0
                and {(r["arch"], r["canonical_name"], r["is_alias"]) for r in rows}
                == identities["S_LOAD_DWORD"]
                and all(r["query_name"] == "S_LOAD_DWORD"
                        for r in rows))

    def add_opcodes(result):
        rows = result["rows"]
        got = {(r["arch"], r["encoding"], r["opcode"]): r["n_variants"] for r in rows}
        return (result["total"] == result["shown"] == len(rows) == len(add_f32)
                and got == add_f32
                and all(r["query_name"] == r["canonical_name"] == "V_ADD_F32"
                        and r["is_alias"] == 0 for r in rows))

    cases = [
        ("show: aliases resolve per architecture",
         ["show", "s_load_dword", "-n", "0", "--json"], load_identity),
        ("which: aliases resolve per architecture",
         ["which", "S_LOAD_DWORD", "-n", "0", "--json"], load_identity),
        ("show: alias exists only in accepting archs",
         ["show", "V_XOR_ADD_U32", "-n", "0", "--json"],
         lambda result: result["total"] == result["shown"] > 0
         and {(r["arch"], r["canonical_name"], r["is_alias"]) for r in result["rows"]}
         == identities["V_XOR_ADD_U32"]
         and all(r["query_name"] == "V_XOR_ADD_U32"
                 for r in result["rows"])),
        ("which: opcodes and condition-variant counts",
         ["which", "V_ADD_F32", "-n", "0", "--json"], add_opcodes),
        ("diff: group migrations are not additions",
         ["diff", "cdna4", "cdna5", "--group", "BRANCH", "-n", "0", "--json"],
         lambda result: set(result["added"]) == branch["cdna5"] - names["cdna4"]
         and set(result["removed"]) == branch["cdna4"] - names["cdna5"]
         and "S_BRANCH" not in result["added"]),
        ("diff: group migrations are not removals",
         ["diff", "cdna5", "cdna4", "--group", "BRANCH", "-n", "0", "--json"],
         lambda result: set(result["added"]) == branch["cdna4"] - names["cdna5"]
         and set(result["removed"]) == branch["cdna5"] - names["cdna4"]
         and "S_BRANCH" not in result["removed"]),
        ("diff: architecture/group case is normalized",
         ["diff", "CDNA4", "CDNA5", "--group", "branch", "-n", "0", "--json"],
         lambda result: result["from"] == "cdna4" and result["to"] == "cdna5"
         and set(result["added"]) == branch["cdna5"] - names["cdna4"]
         and set(result["removed"]) == branch["cdna4"] - names["cdna5"]),
        ("diff: rdna3_5 uses the actual architecture",
         ["diff", "rdna3", "rdna3_5", "-n", "0", "--json"],
         lambda result: set(result["added"]) == names["rdna3_5"] - names["rdna3"]
         and set(result["removed"]) == names["rdna3"] - names["rdna3_5"]),
        ("groups: architecture case is normalized",
         ["groups", "--arch", "CDNA5", "--json"],
         lambda result: result["shown"] > 0
         and all(r["arch"] == "cdna5" for r in result["rows"])),
        ("SQL: distinct column aliases preserve values",
         ["sql", "SELECT a.opcode AS vop2, b.opcode AS vop3 FROM v_inst a"
          " JOIN v_inst b ON a.name=b.name AND a.arch=b.arch"
          " WHERE a.name='V_ADD_F32' AND a.arch='cdna4'"
          " AND a.encoding='ENC_VOP2' AND b.encoding='ENC_VOP3'", "--json"],
         lambda result: result["total"] == result["shown"] == 1
         and result["rows"] == [{"vop2": vop_opcodes["ENC_VOP2"],
                                 "vop3": vop_opcodes["ENC_VOP3"]}]),
    ]
    all_ok = verify_json_cases(isa, cases)
    result = run([sys.executable, "-I", "-B", isa, "which", "S_LOAD_DWORD", "-n", "1"])
    header = ("S_LOAD_DWORD: in %d/%d archs" %
              (len({arch for arch, _, _ in identities["S_LOAD_DWORD"]}), len(archs)))
    ok = (result.returncode == 0 and header in result.stdout
          and "showing 1. Use -n 0 for all." in result.stdout)
    return check("which: arch count survives row truncation", ok,
                 "" if ok else "expected %r with one row shown" % header) and all_ok


def verify_query_errors(isa):
    """Invalid filters must fail without output."""
    cases = [
        ("diff: reject misspelled destination arch",
         ["diff", "rdna3", "rdna3.5"], "architecture", "rdna3_5"),
        ("diff: reject unknown source arch",
         ["diff", "TYPO", "cdna5"], "architecture", "archs"),
        ("diff: reject unknown group",
         ["diff", "cdna4", "cdna5", "--group", "TYPO"], "group", "groups"),
        ("search: reject unknown group",
         ["search", "ADD", "--group", "TYPO"], "group", "groups"),
        ("list: reject unknown group",
         ["list", "--group", "TYPO"], "group", "groups"),
    ]
    for command, argv in (
            ("show", ["show", "V_ADD_F32"]), ("search", ["search", "ADD"]),
            ("list", ["list"]), ("encodings", ["encodings"]),
            ("groups", ["groups"]), ("gfx", ["gfx"]),
            ("manual", ["manual", "--toc"])):
        cases.append((command + ": reject unknown architecture",
                      argv + ["--arch", "TYPO"], "architecture", "archs"))
    all_ok = True
    for label, argv, field, hint in cases:
        for output in ([], ["--json"]):
            result = run([sys.executable, "-I", "-B", isa] + argv + output)
            ok = (result.returncode != 0 and not result.stdout
                  and field in result.stderr and hint in result.stderr
                  and "Traceback" not in result.stderr)
            all_ok = check(label + (" (JSON)" if output else ""), ok,
                           "" if ok else "expected failure with a corrective hint") and all_ok
    return all_ok


def verify_query_regressions(isa):
    """Check positional SQL values, full presence and canonical detail selection."""
    db = Path(isa).resolve().parent.parent / "data" / "isa.db"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        counts = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT i.name) FROM instruction i"
            " JOIN inst_encoding e USING (inst_id) WHERE i.arch='cdna4'").fetchone()
        add_rows = conn.execute(
            "SELECT i.arch, e.encoding_name, e.opcode FROM instruction i"
            " JOIN inst_encoding e USING (inst_id) WHERE i.name='V_ADD_F32'"
            " GROUP BY i.arch, e.encoding_name, e.opcode").fetchall()
        opcodes = dict(conn.execute(
            "SELECT e.encoding_name, e.opcode FROM instruction i"
            " JOIN inst_encoding e USING (inst_id)"
            " WHERE i.name='V_ADD_F32' AND i.arch='cdna4'"
            " AND e.encoding_name IN ('ENC_VOP2', 'ENC_VOP3')"))
        joined_columns = len(conn.execute(
            "SELECT * FROM v_inst a JOIN v_inst b ON 0").description)
    join = (" FROM v_inst a JOIN v_inst b ON a.name=b.name AND a.arch=b.arch"
            " WHERE a.name='V_ADD_F32' AND a.arch='cdna4'"
            " AND a.encoding='ENC_VOP2' AND b.encoding='ENC_VOP3'")
    cases = [
        ("SQL: case-distinct names preserve positional values",
         ["sql", "SELECT COUNT(*) AS n, COUNT(DISTINCT name) AS N FROM v_inst"
          " WHERE arch='cdna4'", "--json"],
         lambda result: result["rows"] == [{"n": counts[0], "N": counts[1]}]),
        ("SQL: duplicate opcodes get distinct names",
         ["sql", "SELECT a.opcode, b.opcode" + join, "--json"],
         lambda result: result["rows"] == [{"opcode": opcodes["ENC_VOP2"],
                                            "opcode_2": opcodes["ENC_VOP3"]}]),
        ("SQL: SELECT star joins preserve both sides",
         ["sql", "SELECT *" + join, "--json"],
         lambda result: result["shown"] == 1 and len(result["rows"][0]) == joined_columns
         and result["rows"][0]["arch"] == result["rows"][0]["arch_2"] == "cdna4"
         and result["rows"][0]["opcode"] == opcodes["ENC_VOP2"]
         and result["rows"][0]["opcode_2"] == opcodes["ENC_VOP3"]),
        ("SQL: suffixes preserve explicit column aliases",
         ["sql", "SELECT 1 AS x, 2 AS x, 3 AS x_2, 4 AS x", "--json"],
         lambda result: result["rows"] == [{"x": 1, "x_3": 2, "x_2": 3, "x_4": 4}]),
        ("SQL: empty duplicate-column queries succeed",
         ["sql", "SELECT 1 AS opcode, 2 AS opcode WHERE 0", "--json"],
         lambda result: result["total"] == result["shown"] == 0 and not result["rows"]),
        ("which: default output includes every encoding",
         ["which", "V_ADD_F32", "--json"],
         lambda result: result["total"] == result["shown"] == len(add_rows)
         and {(r["arch"], r["encoding"], r["opcode"]) for r in result["rows"]}
         == set(add_rows)),
    ]
    for argv in (["--json", "-n", "3", "which", "V_ADD_F32"],
                 ["which", "V_ADD_F32", "--json", "-n", "3"],
                 ["--json", "-n", "5", "which", "V_ADD_F32", "-n", "3"]):
        cases.append(("which: explicit limit (%s)" % " ".join(argv), argv,
                      lambda result: result["total"] == len(add_rows)
                      and result["shown"] == len(result["rows"]) == 3))
    all_ok = verify_json_cases(isa, cases)
    result = run([sys.executable, "-I", "-B", isa, "sql",
                             "SELECT 101 AS n, 202 AS N, 303 AS n"])
    lines = result.stdout.splitlines()
    ok = (result.returncode == 0 and len(lines) >= 3 and lines[0].split() == ["n", "N", "n_2"]
          and lines[2].split() == ["101", "202", "303"])
    all_ok = check("SQL: text preserves positional values", ok) and all_ok

    # A controlled manual fixture tests both absent pages and detail provenance.
    with tempfile.TemporaryDirectory(prefix="amdgpu-isa-regressions-") as scratch:
        os.mkdir(os.path.join(scratch, "scripts"))
        os.mkdir(os.path.join(scratch, "data"))
        cli = os.path.join(scratch, "scripts", "isa.py")
        shutil.copyfile(isa, cli)
        os.symlink(db, os.path.join(scratch, "data", "isa.db"))
        manual = Path(scratch) / "manual"
        # isa.py finds the manual through this variable, so the fixture never
        # depends on whether this machine has built the real pages.
        env = dict(os.environ, AMDGPU_ISA_MANUAL=str(manual))
        from manual_link import manual_archs
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            db_archs = {row[0] for row in conn.execute("SELECT arch FROM arch")}
        manual_only = sorted(manual_archs() - db_archs)
        for arch in manual_only:
            for output in ([], ["--json"]):
                result = run([sys.executable, "-I", "-B", cli, "manual", "--toc",
                                         "--arch", arch.upper()] + output,
                                        env=env)
                ok = (result.returncode != 0 and not result.stdout
                      and "no ISA manual pages at %s" % manual in result.stderr
                      and "try: isa.py" in result.stderr
                      and "isa.py show X --arch %s" % arch not in result.stderr
                      and "install" not in result.stderr
                      and "unknown architecture" not in result.stderr
                      and "Traceback" not in result.stderr)
                all_ok = check("manual: %s reports absent pages%s" %
                               (arch, " (JSON)" if output else ""), ok) and all_ok
        manual.mkdir()
        (manual / "vega").mkdir()
        (manual / "vega" / "toc.md").write_text("Vega ISA\n", encoding="utf-8")
        (manual / "instr-index.tsv").write_text(
            "instruction\tarch\tkind\tpath\tpage_printed\n"
            "BUFFER_ATOMIC_MAX_NUM_F32\tcdna5\tdefinition\talias-page.md\t100\n"
            "BUFFER_ATOMIC_MAX_F32\trdna3\tdefinition\tcanonical-page.md\t200\n",
            encoding="utf-8")
        (manual / "toc.tsv").write_text(
            "arch\tlevel\tpath\ttitle\nvega\t1\tvega/toc.md\tVega ISA\n",
            encoding="utf-8")
        (manual / "format-index.tsv").write_text("arch\tencoding\tpath\n", encoding="utf-8")
        for argv, page, field, operand in (
                (["show", "BUFFER_ATOMIC_MAX_F32", "-n", "1"],
                 "canonical-page.md", "SRSRC", "OPR_SREG_M0_INL"),
                (["show", "BUFFER_ATOMIC_MAX_F32", "--arch", "cdna5", "-n", "1"],
                 "alias-page.md", "RSRC", "OPR_SREG_M0")):
            result = run([sys.executable, "-I", "-B", cli] + argv,
                                    env=env)
            details = result.stdout.split("## operands", 1)[-1]
            ok = (result.returncode == 0 and page in details
                  and any(field in line.split() for line in details.splitlines())
                  and any(operand in line.split() for line in details.splitlines()))
            all_ok = check("show: correct detail source (%s)" % " ".join(argv), ok) and all_ok
        result = run([sys.executable, "-I", "-B", cli, "manual", "--format",
                                 "NOT_A_FORMAT", "--arch", "vega"],
                                env=env)
        ok = (result.returncode != 0 and not result.stdout
              and "isa.py manual --toc --arch vega" in result.stderr
              and "encodings --arch vega" not in result.stderr)
        all_ok = check("manual: format hint supports manual-only archs", ok) and all_ok
    return all_ok


def verify_diff_direction(isa):
    """`diff A B` must list B-only names as added and A-only names as removed,
    and `-n 0` must print every row of both lists."""
    def query(*argv):
        return run([sys.executable, "-I", "-B", isa, "diff", "cdna4", "cdna5"] + list(argv))
    result = query("--json", "-n", "0")
    ok, detail = validate_json(result, lambda data: isinstance(data["added"], list)
                               and isinstance(data["removed"], list))
    if not ok:
        return check("diff: added/removed direction", False, detail)
    d = json.loads(result.stdout)
    # BUFFER_ATOMIC_ADD_U32 is the GFX12-style name CDNA5 adopted; CDNA4 has
    # BUFFER_ATOMIC_ADD.
    ok = ("BUFFER_ATOMIC_ADD_U32" in d["added"]
          and "BUFFER_ATOMIC_ADD" in d["removed"])
    check("diff: added/removed direction", ok,
          "" if ok else "BUFFER_ATOMIC_ADD_U32 not in added, or BUFFER_ATOMIC_ADD not in removed")
    shown = {"added": 0, "removed": 0}
    section = None
    result = query("-n", "0")
    for line in result.stdout.splitlines():
        if line.startswith("## "):
            section = "added" if line.startswith("## added") else "removed"
        elif line.startswith("  ") and not line.startswith("  ... ") and section:
            shown[section] += 1
    want = {k: len(d[k]) for k in shown}
    return check("diff -n 0 prints every row", result.returncode == 0 and shown == want,
                 failure_detail(result) if result.returncode else
                 "" if shown == want else "shown %s, want %s" % (shown, want))


def verify_skill_text(skill):
    p = os.path.join(skill, "SKILL.md")
    if not os.path.exists(p):
        return check("SKILL.md rendered", False)
    t = open(p, encoding="utf-8").read()
    # $S is intentional (it is the placeholder the *agent* resolves at runtime).
    left = [l for l in t.splitlines() if re.search(r"\$(?!S\b)[a-z_]+", l)]
    check("no unsubstituted template placeholders", not left,
          left[0][:60] if left else "")
    hard = [l for l in t.splitlines()
            if re.search(r"~/\.(claude|codex|agents)/skills/amdgpu-isa", l)]
    return check("no install path hardcoded in SKILL.md", not hard,
                 hard[0][:60] if hard else "")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--plugin", action="store_true",
                    help="maintainer check: regenerate and compare the plugin against build/isa.db")
    ap.add_argument("--plugin-dir", default=plugin.DEFAULT_OUT,
                    help="generated plugin skill directory to check")
    ap.add_argument("--quick", action="store_true",
                    help="corpus-independent selftest, structural, licence and manual checks")
    ap.add_argument("--skip-gfx", action="store_true",
                    help="with --quick: warn about gfx checks after a failed all stage")
    args = ap.parse_args(argv)
    if args.skip_gfx and (not args.quick or args.plugin):
        ap.error("--skip-gfx requires --quick without --plugin")
    failures.clear()

    db = os.path.join(ROOT, "build", "isa.db")
    print("verify:")
    if args.plugin and not os.path.isfile(db):
        check("maintainer build database available", False,
              "no build/isa.db -- run 'build.py xml' and 'build.py gfx'")
    quick = args.quick
    if os.path.exists(db) and not quick:
        protect(check, "counts re-derived from XML", verify_counts, db)
    if os.path.exists(db):
        if args.skip_gfx:
            print("  WARN gfx map checks skipped: the gfx stage failed; run build.py gfx before releasing")
        else:
            protect(check, "gfx map checks", verify_gfx_map, db, pinned=not quick or args.plugin)
    protect(check, "gfx parser checks", verify_gfx_parser)
    protect(check, "licence boundary checks", verify_licence_boundary)
    protect(check, "manual directory checks", verify_manual_directory)

    def verify_skill(skill):
        protect(check, "plugin purity checks", verify_plugin_purity, skill)
        protect(check, "plugin skill text checks", verify_skill_text, skill)
        protect(check, "query answer checks", verify_answers, skill, quick=quick and not args.plugin)

    if os.path.isdir(args.plugin_dir):
        if args.plugin and os.path.isfile(db):
            protect(check, "plugin matches its generator", verify_plugin_in_sync, args.plugin_dir)
        verify_skill(args.plugin_dir)
    else:
        if args.plugin:
            check("plugin matches its generator", False, "generated plugin is missing")
        if args.plugin_dir != plugin.DEFAULT_OUT or not os.path.exists(db):
            check("query CLI available for verification", False,
                  "missing plugin or build/isa.db")
        elif args.skip_gfx:
            print("  WARN standalone query checks skipped until build.py gfx succeeds")
        else:
            def standalone():
                with tempfile.TemporaryDirectory(prefix="amdgpu-isa-verify-") as scratch:
                    skill = plugin.generate(os.path.join(scratch, "amdgpu-isa"))
                    verify_skill(skill)
            protect(check, "standalone CLI generation", standalone)
    if not quick:
        def builder_checks():
            from regressions import run_builder_checks
            return run_builder_checks(check)
        protect(check, "builder integration regressions", builder_checks)
        def manual_checks():
            from manual_regressions import run_checks
            return run_checks(check)
        protect(check, "manual review regressions", manual_checks)
        def release_checks():
            from release_regressions import run_checks
            return run_checks(check)
        protect(check, "release review regressions", release_checks)

    if failures:
        print("\n%d check(s) failed" % len(failures))
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
