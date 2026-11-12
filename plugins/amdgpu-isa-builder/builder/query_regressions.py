"""Exercise query accuracy and local-manual boundaries through the CLI."""

from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

import plugin
from check_helpers import run, validate_json


def cli(isa, argv, **kwargs):
    """Query the generated script without importing it."""
    return run([sys.executable, "-I", "-B", str(isa)] + argv, **kwargs)


def fixture(isa, root):
    """Copy the generated script and DB, then add hand-counted query cases."""
    (root / "scripts").mkdir(parents=True)
    (root / "data").mkdir()
    script = root / "scripts/isa.py"
    shutil.copyfile(isa, script)
    db = Path(isa).resolve().parent.parent / "data/isa.db"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(root / "data/isa.db")) as conn:
            source.backup(conn)
            archs = [r[0] for r in conn.execute("SELECT arch FROM arch ORDER BY arch LIMIT 2")]
            if len(archs) < 2:
                raise ValueError("query regressions need two corpus architectures")
            name = "QUERY_REGRESSION_VARIANTS"
            inst = conn.execute(
                "INSERT INTO instruction(arch,name,description,group_name) VALUES(?,?,?,?)",
                (archs[0], name, "query regression fixture", "VALU")).lastrowid
            # Five rows contain three conditions. SQL COUNT(DISTINCT) alone
            # would drop NULL; counting rows would count both repeated values.
            for condition in (None, None, "default", "default", "has_literal"):
                ie = conn.execute(
                    "INSERT INTO inst_encoding(inst_id,encoding_name,condition,opcode)"
                    " VALUES(?,?,?,?)", (inst, "ENC_QUERY_REGRESSION", condition, 123)).lastrowid
                if condition == "default":
                    for field in ("REGRESSION_DST", "REGRESSION_DST", "REGRESSION_SIBLING"):
                        conn.execute(
                            "INSERT INTO operand(ie_id,ord,field_name,operand_type,data_format,"
                            "size,is_input,is_output,is_implicit) VALUES(?,?,?,?,?,?,?,?,?)",
                            (ie, 1, field, "OPR_VGPR", "FMT_NUM_F32", 32, 0, 1, 0))
            # Guarantee that both sides of a diff exceed -n 1 regardless of
            # which architectures or instruction names future sources contain.
            for arch, kind in zip(archs, ("REMOVED", "ADDED")):
                for number in range(3):
                    inst = conn.execute(
                        "INSERT INTO instruction(arch,name,group_name) VALUES(?,?,?)",
                        (arch, "QUERY_REGRESSION_%s_%d" % (kind, number), "VALU")).lastrowid
                    conn.execute(
                        "INSERT INTO inst_encoding(inst_id,encoding_name,opcode) VALUES(?,?,?)",
                        (inst, "ENC_QUERY_REGRESSION_DIFF", number))
            conn.commit()
    return script, archs, name


def manual_fixture(root, arch, manual_arch):
    """Write three pages and indexes so -n 1 has a visible truncation."""
    for item in (arch, manual_arch):
        (root / item).mkdir(parents=True)
        for page in range(1, 4):
            (root / item / ("p%04d.md" % page)).write_text(
                "QUERY_REGRESSION_VARIANTS manual text\n", encoding="utf-8")
    (root / "toc.tsv").write_text(
        "arch\tlevel\tpath\ttitle\n" + "".join(
            "%s\t1\t%s/p%04d.md\tManual section %d\n" % (item, item, page, page)
            for item in (arch, manual_arch) for page in range(1, 4)), encoding="utf-8")
    (root / "instr-index.tsv").write_text(
        "instruction\tarch\tkind\tpath\tpage_printed\n" + "".join(
            "QUERY_REGRESSION_VARIANTS\t%s\tdefinition\t%s/p%04d.md\t%d\n"
            % (arch, arch, page, page) for page in range(1, 4)), encoding="utf-8")
    (root / "format-index.tsv").write_text(
        "arch\tencoding\tpath\n" + "".join(
            "%s\tENC_QUERY_REGRESSION\t%s/p%04d.md\n" % (arch, arch, page)
            for page in range(1, 4)), encoding="utf-8")


def run_checks(isa, check):
    """Report accuracy regressions without aborting on failed CLI responses."""
    ok = True

    def record(label, passed, result=None):
        nonlocal ok
        detail = (result.stderr.strip() or result.stdout[:300]) if result and not passed else ""
        ok = check(label, passed, detail) and ok

    with tempfile.TemporaryDirectory(prefix="isa-query-regressions-") as scratch:
        root = Path(scratch)
        try:
            script, archs, name = fixture(isa, root / "plugin")
            manual_arch = "query_manual_only"
            manual = root / "manual-pages"
            manual_fixture(manual, archs[0], manual_arch)
        except (OSError, sqlite3.Error, ValueError) as exc:
            return check("query: fixture setup", False, str(exc))

        result = cli(script, ["which", name, "--json"])
        record("which: repeated NULL and named conditions count once",
               validate_json(result, lambda data: data.get("shown") == 1 and len(data.get("rows", [])) == 1
               and data["rows"][0].get("n_variants") == 3)[0],
               result)

        result = cli(script, ["show", name, "--arch", archs[0]])
        record("show: identical operands collapse while same-order siblings remain",
               result.returncode == 0 and result.stdout.count("REGRESSION_DST") == 1
               and result.stdout.count("REGRESSION_SIBLING") == 1, result)

        result = cli(script, ["list", "--arch", archs[0], "--encoding", "ENC_QUERY_REGRESSION",
                              "-n", "0", "--json"])
        record("list: conditions distinguish encoding rows",
               validate_json(result, lambda data: data.get("shown") == 5
               and {r.get("condition", "missing") for r in data.get("rows", [])}
               == {None, "default", "has_literal"})[0], result)

        result = cli(script, ["sql", "SELECT 1 WHERE 0"])
        record("SQL: zero rows have an explicit text result",
               result.returncode == 0 and "0 rows" in result.stdout, result)

        result = cli(script, ["archs", "-n", "1", "--json"])
        record("archs: limit truncates rows while retaining corpus total",
               validate_json(result, lambda data: isinstance(data.get("total"), int) and data["total"] >= 2
               and data.get("shown") == 1
               and len(data.get("rows", [])) == 1)[0], result)

        unlimited = cli(script, ["diff"] + archs + ["-n", "0", "--json"])
        full = {}
        full_ok, _ = validate_json(unlimited, lambda payload: full.update(payload) or True)
        result = cli(script, ["diff"] + archs + ["-n", "1", "--json"])
        record("diff: JSON limits each side and reports full counts",
               validate_json(result, lambda data: bool(full.get("added")) and bool(full.get("removed"))
               and data.get("added") == full["added"][:1]
               and data.get("removed") == full["removed"][:1]
               and data.get("added_total") == len(full["added"])
               and data.get("removed_total") == len(full["removed"])
               and data.get("added_shown") == data.get("removed_shown") == 1
               and data.get("has_more") is True)[0], result)
        record("diff: unlimited JSON shows exact untruncated totals",
               full_ok and bool(full.get("added")) and bool(full.get("removed"))
               and full.get("added_total") == full.get("added_shown") == len(full["added"])
               and full.get("removed_total") == full.get("removed_shown") == len(full["removed"])
               and full.get("has_more") is False, unlimited)

        # Every command shares -n. Reject it before any query or filesystem
        # lookup, both before and after the subcommand.
        commands = (["which", name], ["show", name], ["list"], ["sql", "SELECT 1"],
                    ["diff"] + archs, ["manual", "--toc"], ["archs"], ["groups"],
                    ["gfx"], ["schema"], ["selftest"], ["search", name],
                    ["encodings", "--arch", archs[0]])
        for argv in commands:
            for before in (True, False):
                command = ["-n", "-1"] + argv if before else argv + ["-n", "-1"]
                result = cli(script, command)
                record("%s: reject negative limit %s command" %
                       (argv[0], "before" if before else "after"),
                       result.returncode == 2 and "limit" in result.stderr.lower()
                       and not result.stdout and "Traceback" not in result.stderr, result)

        env = dict(os.environ, AMDGPU_ISA_MANUAL=str(manual))
        for mode, value in (("--inst", name), ("--format", "ENC_QUERY_REGRESSION")):
            result = cli(script, ["manual", mode, value, "--arch", archs[0],
                                  "-n", "1", "--json"], env=env)
            record("manual: %s honors -n and preserves exact total" % mode,
                   validate_json(result, lambda data: data.get("total") == 3 and data.get("shown") == 1
                   and len(data.get("rows", [])) == 1)[0], result)

        for argv in (["--grep", name], ["--toc"]):
            result = cli(script, ["manual"] + argv + ["--arch", archs[1]], env=env)
            record("manual: absent architecture fails cleanly for " + argv[0],
                   result.returncode == 1 and not result.stdout
                   and "Traceback" not in result.stderr
                   and "manual" in result.stderr.lower(), result)
            result = cli(script, ["manual"] + argv + ["--arch", manual_arch.upper(), "--json"],
                         env=env)
            record("manual: TOC-derived architecture works for " + argv[0],
                   validate_json(result, lambda data: data.get("shown") == 3
                   and all(r.get("arch") == manual_arch for r in data.get("rows", [])))[0], result)

        for stray in (".isa-manual-stage-leftover", ".isa-manual-backup-leftover", "stray_arch"):
            (manual / stray).mkdir()
            (manual / stray / "p0001.md").write_text(name + " stray page\n", encoding="utf-8")
            result = cli(script, ["manual", "--toc", "--arch", stray], env=env)
            record("manual: unindexed directory is rejected: " + stray,
                   result.returncode == 1 and "installed manual archs" in result.stderr
                   and stray not in result.stderr.split("installed manual archs:")[-1], result)
        result = cli(script, ["manual", "--grep", name, "--json"], env=env)
        data = {}
        valid, _ = validate_json(result, lambda payload: data.update(payload) or True)
        record("manual: grep scans only TOC architectures",
               valid and data.get("shown") == 6
               and {row.get("arch") for row in data.get("rows", [])} == {archs[0], manual_arch}, result)
        import manual_link
        helper = getattr(manual_link, "manual_archs", None)
        discovered = helper(str(manual)) if helper else set()
        record("manual: builder and CLI agree on the TOC architecture set",
               valid and discovered == {archs[0], manual_arch}
               and discovered == {row.get("arch") for row in data.get("rows", [])}, result)
        import verify
        for suite, expected_archs in ((verify.verify_manual_queries, {archs[0], manual_arch}),
                                      (verify.verify_query_regressions, {manual_arch})):
            recorded = []
            with patch.object(manual_link, "MANUAL_ROOT", str(manual)), \
                    patch.dict(os.environ, AMDGPU_ISA_MANUAL=str(manual)), \
                    patch.object(verify, "check", side_effect=lambda label, passed, detail="":
                                 recorded.append((label, passed)) or passed):
                suite(str(script))
            manual_cases = [(label, passed) for label, passed in recorded
                            if label.startswith("manual:") and ("remains queryable" in label
                                                               or "reports absent pages" in label)]
            record("manual: %s checks only indexed architectures" % suite.__name__,
                   {label.split()[1] for label, _ in manual_cases} == expected_archs
                   and all(passed for _, passed in manual_cases))

        result = cli(script, ["manual", "--inst", name, "--arch", manual_arch], env=env)
        record("manual: missing manual-only instruction suggests text search",
               result.returncode == 1 and "manual --grep" in result.stderr
               and "manual --toc" in result.stderr and "isa.py which" not in result.stderr,
               result)

        env = dict(os.environ, AMDGPU_ISA_MANUAL=str(root / "absent-manual"))
        result = cli(script, ["manual", "--inst", name, "--arch", manual_arch], env=env)
        record("manual: missing root keeps manual-only hints valid",
               result.returncode == 1 and "build.py all" in result.stderr
               and "manual --grep" in result.stderr and "manual --toc" in result.stderr
               and "isa.py show" not in result.stderr and "Traceback" not in result.stderr,
               result)

        total, manual_total = plugin.selftest_counts(script)
        expected = total - manual_total
        for label in ("empty", "partial"):
            incomplete = root / (label + "-manual")
            incomplete.mkdir()
            if label == "partial":
                (incomplete / manual_arch).mkdir()
                (incomplete / "instr-index.tsv").write_text(
                    "instruction\tarch\tkind\tpath\tpage_printed\n" + "".join(
                        "%s\t%s\tdefinition\t%s/orphan.md\t1\n" % (name, item, item)
                        for item in (archs[0], manual_arch)), encoding="utf-8")
            incomplete_env = dict(os.environ, AMDGPU_ISA_MANUAL=str(incomplete))
            available = getattr(manual_link, "manual_available", None)
            record("manual: builder treats %s TOC-less root as unavailable" % label,
                   available is not None and not available(str(incomplete)))
            for argv in (["--toc"], ["--toc", "--arch", archs[0]],
                         ["--inst", name, "--arch", manual_arch]):
                for output in ([], ["--json"]):
                    result = cli(script, ["manual"] + argv + output, env=incomplete_env)
                    record("manual: %s root without TOC reports missing pages for %s%s" %
                           (label, " ".join(argv), " (JSON)" if output else ""),
                           result.returncode == 1 and not result.stdout
                           and "no ISA manual pages at %s" % incomplete in result.stderr
                           and "build.py all" in result.stderr
                           and "Traceback" not in result.stderr, result)
            result = cli(script, ["show", name, "--arch", archs[0]], env=incomplete_env)
            record("show: %s root without TOC cannot supply a manual link" % label,
                   result.returncode == 0 and "## manual (" not in result.stdout, result)
            result = cli(script, ["selftest"], env=incomplete_env)
            record("selftest: %s root without TOC skips manual examples" % label,
                   result.returncode == 0
                   and "%d/%d examples passed, %d skipped" %
                   (expected, expected, manual_total) in result.stdout,
                   result)

        installed_empty = root / "installed-empty-manual"
        installed_empty.mkdir()
        (installed_empty / "toc.tsv").write_text("arch\tlevel\tpath\ttitle\n", encoding="utf-8")
        result = cli(script, ["manual", "--toc", "--arch", manual_arch],
                     env=dict(os.environ, AMDGPU_ISA_MANUAL=str(installed_empty)))
        record("manual: installed root without archs gives a complete-build hint",
               result.returncode == 1 and "none -- run build.py all" in result.stderr
               and "Traceback" not in result.stderr, result)

        runner = """import runpy, subprocess, sys
original = subprocess.run
first = True
kind = sys.argv[2]
def execute(command, **kwargs):
    global first
    if first:
        first = False
        if kind == 'timeout':
            raise subprocess.TimeoutExpired(command, kwargs.get('timeout', 1))
        raise OSError('fixture cannot start example')
    if not isinstance(kwargs.get('timeout'), (int, float)) or kwargs['timeout'] <= 0:
        raise RuntimeError('unbounded selftest example')
    return original(command, **kwargs)
subprocess.run = execute
path = sys.argv[1]
sys.argv = [path, 'selftest']
runpy.run_path(path, run_name='__main__')
"""
        for kind in ("timeout", "launch"):
            result = run([sys.executable, "-I", "-B", "-c", runner, str(script), kind], env=env)
            record("selftest: %s failure counts FAIL and continues bounded examples" % kind,
                   result.returncode == 1 and "FAIL" in result.stdout
                   and "%d/%d examples passed" % (expected - 1, expected) in result.stdout
                   and "Traceback" not in result.stderr, result)

        # URI-reserved characters must remain part of the DB's filename.
        unusual = root / "plugin ?query#fragment% ü"
        shutil.copytree(script.parent.parent, unusual)
        result = cli(unusual / "scripts/isa.py", ["sql", "SELECT 1 AS value", "--json"])
        record("query: database opens under URI-reserved and Unicode path characters",
               validate_json(result, lambda data: data.get("rows") == [{"value": 1}])[0], result)
    from manual_regressions import run_query_checks
    ok = run_query_checks(isa, check) and ok
    return ok
