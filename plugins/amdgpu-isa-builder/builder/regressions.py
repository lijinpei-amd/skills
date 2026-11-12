"""Integration regressions for builder lifecycle and query boundaries.

Fixtures stay outside the generated plugin. Nested verification uses --quick
to exercise the queries without recursively rebuilding more fixtures.
"""

from contextlib import closing, redirect_stdout
import ast
import csv
import io
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from unittest.mock import patch

import plugin
from check_helpers import failure_detail, protect, run, validate_json

ROOT = Path(__file__).resolve().parent.parent
NESTED_VERIFY_TIMEOUT_SECONDS = 300



def cli(isa, argv, **kwargs):
    return run([sys.executable, "-I", "-B", str(isa)] + argv, **kwargs)


def verification_detail(result):
    """Preserve failed nested check labels when the build driver wraps stderr."""
    failed = [line.strip() for line in result.stdout.splitlines() if line.lstrip().startswith("FAIL")]
    return "; ".join(failed[:3]) or failure_detail(result)


def expired_sql(isa, argv):
    """Advance the clock after SQL starts to check deadlines without waiting."""
    runner = """import runpy, sys, time
ticks = iter([0.0])
time.monotonic = lambda: next(ticks, 1e6)
path = sys.argv[1]
sys.argv = [path] + sys.argv[2:]
runpy.run_path(path, run_name='__main__')
"""
    return run([sys.executable, "-I", "-B", "-c", runner, str(isa)] + argv)


def run_licence_path_checks(check):
    """Check tracked paths containing spaces and non-ASCII characters."""
    import verify
    ok = True
    with tempfile.TemporaryDirectory(prefix="isa-licence-paths-") as scratch:
        for index, (filename, forbidden, display) in enumerate((
                ("manual reference ä.pdf", True, "manual reference ä.pdf"),
                ("sources/reference notes 中文.txt", True, "sources/reference notes 中文.txt"),
                ("reference notes ü.md", False, "reference notes ü.md"),
                (os.fsdecode(b"manual-\xff.pdf"), True, r"manual-\xff.pdf"))):
            repository = Path(scratch) / str(index)
            repository.mkdir()
            path = repository / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("path fixture\n", encoding="utf-8")
            result = run(["git", "init", "--quiet", str(repository)])
            if result.returncode == 0:
                result = run(["git", "-C", str(repository), "add", "-f", "--", filename])
            if result.returncode:
                ok = check("licence: tracked path " + display, False, failure_detail(result)) and ok
                continue
            recorded = []
            try:
                with patch.object(verify, "ROOT", str(repository)), \
                        patch.object(verify, "check", side_effect=lambda label, passed, detail="":
                                     recorded.append((passed, detail)) or passed):
                    accepted = verify.verify_licence_boundary()
            except UnicodeError as error:
                ok = check("licence: tracked path " + display, False, str(error)) and ok
                continue
            passed = accepted is (not forbidden) and bool(recorded)
            if forbidden:
                passed = passed and display in recorded[0][1]
            ok = check("licence: tracked path " + display, passed,
                       recorded[0][1] if recorded and not passed else "") and ok
    return ok


def run_verifier_failure_checks(check):
    """Broken child commands must fail their checks while later checks run."""
    import verify
    ok = run_licence_path_checks(check)
    with tempfile.TemporaryDirectory(prefix="isa-verifier-failures-") as scratch:
        script = Path(scratch) / "child.py"
        script.write_text("print('{\"rows\": [null]}')\n", encoding="utf-8")
        recorded = []
        saved_check = verify.check
        try:
            verify.check = lambda label, passed, detail="": recorded.append((label, passed, detail)) or passed
            cases = [("malformed row", ["--json"], lambda data: data["rows"][0]["name"] == "x"),
                     ("later check", ["--json"], lambda data: True)]
            verify.verify_json_cases(str(script), cases)
        except Exception as error:
            recorded.append(("crashed", False, str(error)))
        finally:
            verify.check = saved_check
        ok = check("verify: malformed rows fail and later checks run",
                   [item[:2] for item in recorded] == [("malformed row", False), ("later check", False)]) and ok

        script.write_text("import sys\nprint('{\"rows\": []}')\nsys.exit(3)\n", encoding="utf-8")
        recorded = []
        try:
            verify.check = lambda label, passed, detail="": recorded.append((label, passed, detail)) or passed
            verify.verify_json_cases(str(script), [("failed child", [], lambda data: True)])
        finally:
            verify.check = saved_check
        ok = check("verify: valid JSON cannot hide child failure",
                   len(recorded) == 1 and recorded[0][1] is False) and ok

        from check_helpers import run as bounded_run
        result = bounded_run([sys.executable, "-I", "-B", "-c", "import time; time.sleep(10)"],
                             timeout=0.05)
        ok = check("verify: timed-out child returns a failure result",
                   result.returncode == 124 and "timed out" in result.stderr) and ok
        result = bounded_run([str(Path(scratch) / "missing-command")])
        ok = check("verify: child launch errors return a failure result",
                   result.returncode == 125 and "cannot run command" in result.stderr) and ok
        script.write_text("import time\ntime.sleep(10)\n", encoding="utf-8")
        recorded = []
        saved_run = verify.run
        try:
            verify.check = lambda label, passed, detail="": recorded.append((label, passed, detail)) or passed
            verify.run = lambda argv, **kwargs: bounded_run(argv, timeout=0.05,
                                                            **{k: v for k, v in kwargs.items() if k != "timeout"})
            verify.verify_search_patterns(str(script))
        finally:
            verify.check, verify.run = saved_check, saved_run
        ok = check("verify: pattern timeouts do not stop later checks",
                   len(recorded) > 1 and all(item[1] is False for item in recorded)
                   and recorded[-1][0].startswith("FTS:")) and ok

        bad = Path(scratch) / "bad-plugin"
        plugin.generate(str(bad))
        (bad / "scripts/isa.py").write_text("print('{\"rows\": [null]}')\n", encoding="utf-8")
        (bad / "build-info.json").write_text("{not JSON\n", encoding="utf-8")
        result = run([sys.executable, "-B", str(ROOT / "build.py"), "verify", "--quick",
                      "--plugin-dir", str(bad)], timeout=NESTED_VERIFY_TIMEOUT_SECONDS)
        passed = (result.returncode != 0 and "check(s) failed" in result.stdout
                  and "query CLI selftest" in result.stdout
                  and "FAIL plugin declares its redistribution status" in result.stdout
                  and "Traceback" not in result.stderr)
        ok = check("verify: failed child suite prints a nonzero summary", passed,
                   result.stderr.strip() if not passed else "") and ok
        import manual_link
        manual = Path(scratch) / "manual"
        manual.mkdir()
        indexes = ("toc.tsv", "instr-index.tsv", "format-index.tsv")
        for name in indexes:
            (manual / name).write_text("fixture\n", encoding="utf-8")
        linked = Path(scratch) / "manual-link"
        linked.symlink_to(manual, target_is_directory=True)
        saved_root, saved_failures = manual_link.MANUAL_ROOT, verify.failures[:]
        try:
            manual_link.MANUAL_ROOT = str(linked)
            with redirect_stdout(io.StringIO()):
                verify.failures.clear()
                accepted = verify.verify_manual_directory()
            ok = check("verify: external manual root symlinks are accepted", accepted) and ok
            for name in indexes:
                (manual / name).unlink()
                with redirect_stdout(io.StringIO()):
                    verify.failures.clear()
                    rejected = not verify.verify_manual_directory()
                ok = check("verify: requires manual " + name, rejected) and ok
                (manual / name).write_text("fixture\n", encoding="utf-8")
        finally:
            manual_link.MANUAL_ROOT = saved_root
            verify.failures[:] = saved_failures
    return ok


def run_verifier_storage_checks(check):
    """A storage-only local rebuild must preserve installed size and hash text."""
    import verify
    from release_regressions import fixture_database
    ok = True
    with tempfile.TemporaryDirectory(prefix="isa-verifier-storage-") as scratch:
        root = Path(scratch)
        build = root / "build"
        build.mkdir()
        db = build / "isa.db"
        fixture_database(db)
        skill = root / "skill"
        saved_build, saved_failures = plugin.BUILD, verify.failures[:]
        try:
            plugin.BUILD = str(build)
            plugin.generate(str(skill))
            before = {rel: (skill / rel).read_bytes() for rel in plugin.OUTPUTS}
            with sqlite3.connect(db) as conn:
                conn.execute("PRAGMA page_size=65536")
                conn.execute("VACUUM")
            ok = check("verify storage fixture: physical size crosses MB rounding",
                       len(before["data/isa.db"]) < 500000 <= db.stat().st_size) and ok
            with redirect_stdout(io.StringIO()):
                verify.failures.clear()
                accepted = verify.verify_plugin_in_sync(str(skill))
            ok = check("verify: storage-only rebuild preserves in-sync size/hash text", accepted) and ok
            ok = check("verify: in-sync check leaves installed outputs untouched",
                       before == {rel: (skill / rel).read_bytes() for rel in plugin.OUTPUTS}) and ok
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE sample SET number=2 WHERE number=1")
            with redirect_stdout(io.StringIO()):
                verify.failures.clear()
                rejected = not verify.verify_plugin_in_sync(str(skill))
            ok = check("verify: logical database changes still fail in-sync", rejected) and ok
        finally:
            plugin.BUILD = saved_build
            verify.failures[:] = saved_failures
    return ok


def run_answer_checks(isa, check):
    """Check indexed joins, sibling aliases, unique archs and manual paths."""
    ok = True
    query = ("SELECT a.name,a.encoding,a.opcode AS old_opcode,b.opcode AS new_opcode"
             " FROM v_inst a JOIN v_inst b ON a.name=b.name AND a.encoding=b.encoding"
             " AND a.condition IS b.condition WHERE a.arch='rdna3' AND b.arch='rdna4'"
             " AND a.opcode!=b.opcode")
    for limit in ("5", "0"):
        result = cli(isa, ["sql", query, "-n", limit, "--json"])
        passed, detail = validate_json(result, lambda data:
                                      bool(data["rows"]) and all(r["old_opcode"] != r["new_opcode"]
                                                                for r in data["rows"]))
        ok = check("SQL: ordinary opcode join -n " + limit, passed, detail) and ok

    db = Path(isa).resolve().parent.parent / "data/isa.db"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        expected = conn.execute("SELECT COUNT(*) FROM v_operand").fetchone()[0]
        edges = {}
        for alias, name in conn.execute("SELECT a.alias, i.name FROM instruction_alias a"
                                       " JOIN instruction i USING (inst_id)"):
            edges.setdefault(alias, set()).add(name)
            edges.setdefault(name, set()).add(alias)
        connected, pending = set(), ["BUFFER_ATOMIC_MAX_NUM_F32"]
        while pending:
            name = pending.pop()
            if name not in connected:
                connected.add(name)
                pending.extend(edges.get(name, set()) - connected)
        expected_archs = {arch for arch, name in conn.execute("SELECT arch, name FROM instruction")
                          if name in connected}
    result = cli(isa, ["sql", "SELECT * FROM v_operand ORDER BY arch, name, encoding, condition, ord",
                       "-n", "0", "--json"])
    passed, detail = validate_json(result, lambda data: data["total"] == data["shown"] == expected)
    ok = check("SQL: full sorted operand listing completes", passed, detail) and ok

    for name, arch, canonical in (
            ("V_MFMA_F32_16X16X16BF16", "cdna2", "V_MFMA_F32_16X16X16BF16_1K"),
            ("IMAGE_ATOMIC_MAX_NUM_FLT", "rdna1", "IMAGE_ATOMIC_FMAX"),
            ("IMAGE_ATOMIC_MAX_NUM_FLT", "rdna2", "IMAGE_ATOMIC_FMAX")):
        result = cli(isa, ["which", name, "--json"])
        passed, detail = validate_json(result, lambda data:
                                      any(o["arch"] == arch and canonical in o.get("names", [o["name"]])
                                          for o in data["also_present_as"]))
        ok = check("which: sibling alias %s in %s" % (name, arch), passed, detail) and ok
    result = cli(isa, ["which", "BUFFER_ATOMIC_MAX_NUM_F32", "--json"])
    def unique_other_archs(data):
        accepting = {row["arch"] for row in data["rows"]}
        others = data["also_present_as"]
        other_archs = {row["arch"] for row in others}
        return (len(others) == len(other_archs) and not accepting & other_archs
                and accepting | other_archs == expected_archs)
    passed, detail = validate_json(result, unique_other_archs)
    ok = check("which: other spellings count each arch once", passed, detail) and ok

    result = cli(isa, ["sql", "SELECT 1; SELECT 2", "--json"])
    passed = result.returncode == 1 and not result.stdout and "Traceback" not in result.stderr
    ok = check("SQL: multi-statement input has a clean error", passed) and ok
    # Python 3.8-3.10 used sqlite3.Warning for this boundary. Emulate that
    # exception while exercising the actual CLI against a read-only DB.
    runner = """import os, runpy, sqlite3, sys
original = sqlite3.connect
class Connection:
    def __init__(self, *args, **kwargs):
        self.conn = original(*args, **kwargs)
    def __getattr__(self, name):
        return getattr(self.conn, name)
    def execute(self, query, *args):
        if ';' in query:
            raise sqlite3.Warning('You can only execute one statement at a time.')
        return self.conn.execute(query, *args)
sqlite3.connect = Connection
path = sys.argv[1]
sys.argv = [path, 'sql', 'SELECT 1; SELECT 2', '--json']
runpy.run_path(path, run_name='__main__')
"""
    result = run([sys.executable, "-I", "-B", "-c", runner, str(isa)])
    passed = (result.returncode == 1 and not result.stdout and "SQL error" in result.stderr
              and "Traceback" not in result.stderr)
    ok = check("SQL: legacy Warning has a clean error", passed) and ok

    with tempfile.TemporaryDirectory(prefix="isa-manual-paths-") as scratch:
        root = Path(scratch)
        manual = root / "manual-pages-with-a-long-directory-name-for-text-results"
        (manual / "rdna4").mkdir(parents=True)
        (root / "work").mkdir()
        page = manual / "rdna4" / "page.md"
        page.write_text("wait state\n", encoding="utf-8")
        (manual / "toc.tsv").write_text(
            "arch\tlevel\tpath\ttitle\nrdna4\t1\trdna4/page.md\tISA\n", encoding="utf-8")
        (manual / "instr-index.tsv").write_text(
            "instruction\tarch\tkind\tpath\tpage_printed\n"
            "V_FMA_F32\trdna4\tdefinition\trdna4/page.md\t1\n", encoding="utf-8")
        (manual / "format-index.tsv").write_text(
            "arch\tencoding\tpath\nrdna4\tENC_VOP3\trdna4/page.md\n", encoding="utf-8")
        env = dict(os.environ, AMDGPU_ISA_MANUAL="../" + manual.name + "/")
        for argv in (["--toc"], ["--inst", "V_FMA_F32"], ["--format", "ENC_VOP3"],
                     ["--grep", "wait state"]):
            result = cli(Path(isa).resolve(), ["manual"] + argv + ["--arch", "rdna4", "--json"],
                         env=env, cwd=root / "work")
            passed, detail = validate_json(result, lambda data:
                                          bool(data["rows"]) and all(Path(row["path"]).resolve() == page.resolve()
                                                                     for row in data["rows"]))
            ok = check("manual: absolute paths for " + argv[0], passed, detail) and ok
            result = cli(Path(isa).resolve(), ["manual"] + argv + ["--arch", "rdna4"],
                         env=env, cwd=root / "work")
            passed = result.returncode == 0 and str(page.resolve()) in result.stdout
            ok = check("manual: text preserves long paths for " + argv[0], passed) and ok
    return ok


def run_manual_update_checks(check):
    """Check partial PDF builds retain other archs and empty runs change nothing."""
    interpreter = ROOT / ".venv/bin/python"
    if not interpreter.is_file():
        interpreter = Path(sys.executable)
    tree = ast.parse((ROOT / "builder/pdf_to_manual.py").read_text(encoding="utf-8"))
    pdf_arch = next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(getattr(target, "id", None) == "PDF_ARCH" for target in node.targets))
    filenames = {arch: filename for filename, arch in pdf_arch.items()}
    make_pdf = """import pymupdf, sys
doc = pymupdf.open()
page = doc.new_page()
page.insert_text((72, 100), sys.argv[2])
doc.set_toc([[1, sys.argv[3], 1]])
doc.save(sys.argv[1])
doc.close()
"""
    ok = True
    with tempfile.TemporaryDirectory(prefix="isa-manual-updates-") as scratch:
        root = Path(scratch)
        pdfs, manual = root / "pdfs", root / "manual"
        pdfs.mkdir()
        for arch in ("rdna3", "rdna4"):
            result = run([str(interpreter), "-B", "-c", make_pdf,
                          str(pdfs / filenames[arch]),
                          "V_FMA_F32\n123\nInstruction definition\nENC_VOP3", "Original ISA"])
            if result.returncode:
                return check("manual build fixtures are available", False, result.stderr.strip())
        command = [str(interpreter), "-B", str(ROOT / "builder/pdf_to_manual.py"),
                   "--pdfs", str(pdfs), "--out", str(manual), "--db", str(ROOT / "build/isa.db")]
        result = run(command)
        if not check("manual: initial fixture build", result.returncode == 0, result.stderr.strip()):
            return False
        names = ("toc.tsv", "instr-index.tsv", "format-index.tsv")

        def indexes():
            output = {}
            for name in names:
                with (manual / name).open(encoding="utf-8", newline="") as stream:
                    output[name] = list(csv.DictReader(stream, delimiter="\t"))
            return output

        original = indexes()
        result = run([str(interpreter), "-B", "-c", make_pdf, str(pdfs / filenames["rdna4"]),
                      "V_ADD_F32\n123\nUpdated instruction definition\nENC_VOP2", "Updated ISA"])
        if result.returncode:
            return check("manual: replacement fixture is available", False, result.stderr.strip())
        result = run(command + ["--only", "rdna4"])
        updated = indexes()
        passed = result.returncode == 0 and all(
            [r for r in updated[name] if r["arch"] == "rdna3"]
            == [r for r in original[name] if r["arch"] == "rdna3"] for name in names)
        ok = check("manual: --only preserves other archs in every index", passed) and ok
        passed = (any(r["arch"] == "rdna4" and r["title"] == "Updated ISA"
                      for r in updated["toc.tsv"])
                  and any(r["arch"] == "rdna4" and r["instruction"] == "V_ADD_F32"
                          for r in updated["instr-index.tsv"])
                  and not any(r["arch"] == "rdna4" and r["instruction"] == "V_FMA_F32"
                              for r in updated["instr-index.tsv"]))
        ok = check("manual: --only replaces the rebuilt arch's index rows", passed) and ok
        (pdfs / filenames["rdna3"]).unlink()
        result = run(command)
        remaining = indexes()
        ok = check("manual: missing PDFs preserve installed index rows",
                   result.returncode == 0 and remaining == updated and all(
                       [r for r in remaining[name] if r["arch"] == "rdna3"]
                       == [r for r in original[name] if r["arch"] == "rdna3"] for name in names)) and ok
        empty = root / "empty-pdfs"
        empty.mkdir()
        untouched = {name: (manual / name).read_bytes() for name in names}
        result = run(command[:command.index("--pdfs")] + ["--pdfs", str(empty), "--out", str(manual)])
        ok = check("manual: no PDFs leaves installed indexes unchanged",
                   result.returncode != 0 and untouched == {
                       name: (manual / name).read_bytes() for name in names}) and ok
        absent = root / "absent-manual"
        result = run([str(interpreter), "-B", str(ROOT / "builder/pdf_to_manual.py"),
                      "--pdfs", str(empty), "--out", str(absent)])
        ok = check("manual: no PDFs creates no empty install",
                   result.returncode != 0 and not absent.exists()) and ok
    return ok


def run_builder_checks(check):
    """Check manifest-only generation, deterministic rebuilds and persistence."""
    import verify
    ok = True
    with tempfile.TemporaryDirectory(prefix="isa-builder-regressions-") as scratch:
        root = Path(scratch)
        sources = root / "sources"
        sources.mkdir()
        shutil.copyfile(ROOT / "sources/manifest.json", sources / "manifest.json")
        old_sources = plugin.SOURCES
        try:
            plugin.SOURCES = str(sources)
            plugin.generate(str(root / "generated"))
            passed = True
        except SystemExit:
            passed = False
        finally:
            plugin.SOURCES = old_sources
        ok = check("plugin: manifest provenance needs no hand-created pin", passed) and ok
        out = root / "stable-plugin"
        plugin.generate(str(out))
        before = {rel: (out / rel).stat().st_mtime_ns for rel in plugin.OUTPUTS}
        plugin.generate(str(out))
        ok = check("plugin: unchanged generation preserves file mtimes",
                   before == {rel: (out / rel).stat().st_mtime_ns for rel in plugin.OUTPUTS}) and ok

        xml = root / "xml"
        xml.mkdir()
        sample = sorted((ROOT / "sources/xml/extracted").glob("*.xml"))[0]
        shutil.copyfile(sample, xml / sample.name)
        runner = """import os, runpy, sys, time
path, stamp, sources, target = sys.argv[1:]
sys.path.insert(0, os.path.dirname(path))
time.strftime = lambda *args, **kwargs: stamp
sys.argv = [path, '--sources', sources, '--out', target]
runpy.run_path(path, run_name='__main__')
"""
        databases = []
        for index, stamp in enumerate(("2030-01-01 00:00:00", "2040-02-02 00:00:00")):
            target = root / ("db%d.sqlite" % index)
            result = run([sys.executable, "-B", "-c", runner, str(ROOT / "builder/xml_to_db.py"),
                          stamp, str(xml), str(target)])
            if result.returncode == 0:
                result = run([sys.executable, "-B", str(ROOT / "builder/gfx_to_db.py"),
                              "--db", str(target), "--rst", str(ROOT / "sources/llvm/AMDGPUUsage.rst")])
            databases.append(target.read_bytes() if result.returncode == 0 else b"")
        ok = check("database: rebuild bytes ignore wall-clock time",
                   bool(databases[0]) and databases[0] == databases[1]) and ok
        target = root / "db0.sqlite"
        result = run([sys.executable, "-B", str(ROOT / "builder/gfx_to_db.py"), "--db", str(target),
                      "--rst", str(ROOT / "sources/llvm/AMDGPUUsage.rst")])
        ok = check("database: repeated gfx stage preserves bytes",
                   result.returncode == 0 and target.read_bytes() == databases[0]) and ok

        package = root / "package"
        skill = package / "skills/amdgpu-isa"
        plugin.generate(str(skill))
        for flavor in (".claude-plugin", ".codex-plugin"):
            (package / flavor).mkdir()
            (package / flavor / "plugin.json").write_text("{}", encoding="utf-8")
        cache = skill / "scripts/__pycache__"
        cache.mkdir()
        (cache / "isa.cpython-310.pyc").write_bytes(b"fixture")
        def purity_failed():
            saved = verify.failures[:]
            try:
                verify.failures.clear()
                with redirect_stdout(io.StringIO()):
                    verify.verify_plugin_purity(str(skill))
                return bool(verify.failures)
            finally:
                verify.failures[:] = saved

        ok = check("purity: ignores bytecode cache", not purity_failed()) and ok
        shutil.rmtree(cache)
        (package / "manual.pdf").write_bytes(b"fixture")
        ok = check("purity: catches extra files outside skills", purity_failed()) and ok
        (package / "manual.pdf").unlink()
        for rel in plugin.OUTPUTS:
            path = skill / rel
            held = root / "held-output"
            path.rename(held)
            path.symlink_to(held)
            rejected = purity_failed()
            path.unlink()
            held.rename(path)
            ok = check("purity: rejects symlink at " + rel, rejected) and ok
        for flavor in (".claude-plugin", ".codex-plugin"):
            path = package / flavor / "plugin.json"
            held = root / "held-manifest"
            path.rename(held)
            path.symlink_to(held)
            rejected = purity_failed()
            path.unlink()
            held.rename(path)
            ok = check("purity: rejects symlinked " + flavor + " manifest", rejected) and ok

        isolated = root / "isolated-builder"
        shutil.copytree(ROOT, isolated, ignore=shutil.ignore_patterns(
            "sources", "build", ".venv", "dist", "__pycache__"))
        (isolated / "sources").symlink_to(ROOT / "sources", target_is_directory=True)
        (isolated / "build").mkdir()
        local_db = isolated / "build/isa.db"
        shutil.copyfile(ROOT / "build/isa.db", local_db)
        result = run([sys.executable, "-B", str(isolated / "build.py"), "verify", "--quick"],
                     timeout=NESTED_VERIFY_TIMEOUT_SECONDS)
        from manual_link import manual_archs
        expected_tests, manual_tests = plugin.selftest_counts(str(ROOT / "skill-template/scripts/isa.py"))
        if not manual_archs():
            expected_tests -= manual_tests
        summary = "%d/%d examples passed" % (expected_tests, expected_tests)
        passed = (result.returncode == 0 and "query CLI selftest" in result.stdout
                  and summary in result.stdout)
        ok = check("verify: standalone builder exercises generated CLI", passed,
                   verification_detail(result) if not passed else "") and ok
        with closing(sqlite3.connect(local_db.as_uri() + "?mode=ro", uri=True)) as conn:
            db_archs = {row[0] for row in conn.execute("SELECT arch FROM arch")}
        installed = sorted(manual_archs() - db_archs)
        ok = check("verify: installed manual-only arch checks run",
                   result.returncode == 0 and all("manual: %s remains queryable" % arch in result.stdout
                                                  for arch in installed)) and ok
        with sqlite3.connect(local_db) as conn:
            conn.execute("UPDATE build_info SET value='local rebuild fixture' WHERE key='built'")
        command = [sys.executable, "-B", str(isolated / "build.py"), "verify", "--quick",
                   "--plugin-dir", str(skill)]
        result = run(command, timeout=NESTED_VERIFY_TIMEOUT_SECONDS)
        ok = check("verify: default accepts a plugin differing from local rebuild",
                   result.returncode == 0 and "plugin matches its generator" not in result.stdout,
                   verification_detail(result) if result.returncode else "") and ok
        result = run(command + ["--plugin"], timeout=NESTED_VERIFY_TIMEOUT_SECONDS)
        ok = check("verify: explicit plugin check rejects local rebuild differences",
                   result.returncode != 0 and "FAIL plugin matches its generator" in result.stdout
                   and "check(s) failed" in result.stdout and "Traceback" not in result.stderr) and ok
        local_db.unlink()
        result = run(command + ["--plugin"], timeout=NESTED_VERIFY_TIMEOUT_SECONDS)
        ok = check("verify: maintainer check reports a missing build database",
                   result.returncode != 0 and "no build/isa.db" in result.stdout
                   and "check(s) failed" in result.stdout and "Traceback" not in result.stderr) and ok
    ok = protect(check, "manual update regressions", run_manual_update_checks, check) is not False and ok
    ok = protect(check, "verifier failure regressions", run_verifier_failure_checks, check) is not False and ok
    return protect(check, "verifier storage regressions", run_verifier_storage_checks, check) is not False and ok
