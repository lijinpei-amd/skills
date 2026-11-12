"""Check explicit releases, source provenance and logical database equality."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import runpy
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

import database
import gfx_to_db
import plugin
import xml_to_db

ROOT = Path(__file__).resolve().parent.parent


def fixture_database(path):
    """Create a small corpus with duplicate rows, NULLs, blobs and FTS data."""
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE arch(arch TEXT PRIMARY KEY, architecture_name TEXT);
            CREATE TABLE build_info(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE sample(name TEXT, number INTEGER, payload BLOB);
            CREATE TABLE gfx_target(gfx TEXT PRIMARY KEY, arch TEXT);
            CREATE VIEW v_gfx AS SELECT gfx, arch FROM gfx_target;
            CREATE VIRTUAL TABLE fts_inst USING fts5(name, description);
            INSERT INTO arch VALUES ('test', 'AMD Test 1');
            INSERT INTO build_info VALUES ('built', '2026-01-01');
            INSERT INTO build_info VALUES ('source', 'test corpus');
            INSERT INTO build_info VALUES ('count_instruction', '3');
            INSERT INTO fts_inst VALUES ('TEST', 'test instruction');
            INSERT INTO gfx_target VALUES ('gfx_test', 'test');
        """)
        conn.executemany("INSERT INTO sample VALUES (?,?,?)", [
            ("alpha", 1, b"\x00\xff"), ("alpha", None, b""), ("alpha", None, b""),
        ])
        conn.execute("ANALYZE")


def fixture_xml(path):
    """Write one architecture with an instruction, encoding and operand."""
    path.mkdir()
    (path / "amdgpu_isa_test.xml").write_text("""
        <Spec>
          <Document><ReleaseDate>2026-01-01</ReleaseDate></Document>
          <Architecture><ArchitectureName>AMD Test 1</ArchitectureName></Architecture>
          <Instruction>
            <InstructionName>TEST</InstructionName><Description>test instruction</Description>
            <InstructionEncodings><InstructionEncoding>
              <EncodingName>TEST_ENCODING</EncodingName><Opcode>1</Opcode>
              <Operands><Operand Order="0" Input="true">
                <FieldName>TEST_FIELD</FieldName><OperandSize>32</OperandSize>
              </Operand></Operands>
            </InstructionEncoding></InstructionEncodings>
          </Instruction>
        </Spec>
    """, encoding="utf-8")


def run_checks(check):
    """Check release-only generation and database storage independence."""
    ok = True

    def case(label, test):
        nonlocal ok
        try:
            test()
        except (Exception, SystemExit) as exc:
            ok = check(label, False, str(exc) or type(exc).__name__) and ok
        else:
            ok = check(label, True) and ok

    def stage_flow():
        namespace = runpy.run_path(str(ROOT / "build.py"), run_name="build_fixture")
        globals_ = namespace["main"].__globals__
        calls = []
        globals_["run"] = lambda script, argv, label: calls.append((script, list(argv))) or 0

        def manual(argv, **kwargs):
            calls.append((Path(argv[1]).name, list(argv[2:])))
            return subprocess.CompletedProcess(argv, 0)

        with patch.object(sys, "argv", ["build.py", "all"]), \
                patch("os.path.exists", return_value=True), \
                patch("subprocess.run", side_effect=manual), redirect_stdout(io.StringIO()):
            assert namespace["main"]() == 0
        assert calls == [("fetch.py", []), ("xml_to_db.py", []), ("gfx_to_db.py", []),
                         ("pdf_to_manual.py", []), ("verify.py", ["--quick"])], calls
        calls.clear()
        with patch.object(sys, "argv", ["build.py", "plugin"]), redirect_stdout(io.StringIO()):
            assert namespace["main"]() == 0
        assert calls == [("plugin.py", [])], calls
        for arguments in ([], ["--plugin"], ["--quick"]):
            calls.clear()
            with patch.object(sys, "argv", ["build.py", "verify"] + arguments), \
                    redirect_stdout(io.StringIO()):
                assert namespace["main"]() == 0
            assert calls == [("verify.py", arguments)], calls

    case("release: all uses quick checks; explicit verify preserves full/flagged checks", stage_flow)

    def gfx_failure_flow():
        namespace = runpy.run_path(str(ROOT / "build.py"), run_name="build_fixture")
        calls = []

        def execute(argv, **kwargs):
            stage = Path(argv[1]).name
            calls.append(stage)
            return subprocess.CompletedProcess(argv, 1 if stage in ("gfx_to_db.py", "verify.py") else 0)

        error = io.StringIO()
        with patch.object(sys, "argv", ["build.py", "all"]), \
                patch("os.path.exists", return_value=True), \
                patch("subprocess.run", side_effect=execute), \
                redirect_stdout(io.StringIO()), redirect_stderr(error):
            try:
                namespace["main"]()
            except SystemExit as exc:
                assert "verify failed" in str(exc), str(exc)
            else:
                raise AssertionError("failed final verification was accepted")
        assert calls == ["fetch.py", "xml_to_db.py", "gfx_to_db.py", "pdf_to_manual.py", "verify.py"], calls
        assert "WARNING" in error.getvalue() and "manual" in error.getvalue()
        calls.clear()
        with patch.object(sys, "argv", ["build.py", "gfx"]), \
                patch("subprocess.run", side_effect=execute), redirect_stdout(io.StringIO()):
            try:
                namespace["main"]()
            except SystemExit as exc:
                assert "gfx target map failed" in str(exc)
            else:
                raise AssertionError("explicit gfx accepted a failing stage")
        assert calls == ["gfx_to_db.py"]

    case("release: failed gfx still builds manuals; verification and explicit gfx remain strict", gfx_failure_flow)
    with tempfile.TemporaryDirectory(prefix="isa-release-") as scratch:
        root = Path(scratch)

        def unmapped_manual_build():
            from manual_regressions import MAKE_PDF
            isolated = root / "unmapped-builder"
            shutil.copytree(ROOT, isolated, ignore=shutil.ignore_patterns(
                "sources", "build", "dist", ".venv", "__pycache__"))
            (isolated / ".venv").symlink_to(ROOT / ".venv", target_is_directory=True)
            (isolated / "builder/fetch.py").write_text("# Fixture sources are already present.\n", encoding="utf-8")
            sources = isolated / "sources"
            (sources / "xml").mkdir(parents=True)
            fixture_xml(sources / "xml/extracted")
            (sources / "llvm").mkdir()
            (sources / "llvm/AMDGPUUsage.rst").symlink_to(ROOT / "sources/llvm/AMDGPUUsage.rst")
            (sources / "pdf").mkdir()
            pdf = sources / "pdf/instinct-mi100-cdna1-shader-instruction-set-architecture.pdf"
            result = subprocess.run([str(ROOT / ".venv/bin/python"), "-B", "-c", MAKE_PDF,
                                     str(pdf), "1", "Fixture ISA", "TEST\n1\nInstruction definition"],
                                    capture_output=True, text=True, timeout=15)
            assert result.returncode == 0, result.stderr
            manual = root / "unmapped-manual"
            command = [sys.executable, "-B", str(isolated / "build.py")]
            env = dict(os.environ, AMDGPU_ISA_MANUAL=str(manual))
            result = subprocess.run(command + ["all"], capture_output=True, text=True, env=env, timeout=45)
            assert result.returncode == 0, result.stdout + result.stderr
            assert "WARN" in result.stdout + result.stderr
            assert (manual / "toc.tsv").is_file() and (manual / "cdna1/p0001.md").is_file()
            assert "Instruction definition" in (manual / "cdna1/p0001.md").read_text()
            result = subprocess.run(command + ["gfx"], capture_output=True, text=True, env=env, timeout=15)
            assert result.returncode != 0 and "no gfx target maps to test" in result.stdout
            import verify
            recorded = []
            with patch.object(verify, "check", side_effect=lambda label, passed, detail="":
                              recorded.append(passed) or passed):
                assert verify.verify_gfx_map(str(isolated / "build/isa.db"), pinned=True) is False
            assert False in recorded

        case("release: all publishes manuals successfully when a new XML arch has no gfx map", unmapped_manual_build)

        def incomplete_gfx_release(kind):
            build = root / ("incomplete-gfx-" + kind)
            build.mkdir()
            db = build / "isa.db"
            fixture_database(db)
            out = build / "published-plugin"
            with patch.object(plugin, "BUILD", str(build)):
                plugin.generate(str(out))
                before = {rel: (out / rel).read_bytes() for rel in plugin.output_paths()}
                with sqlite3.connect(db) as conn:
                    conn.execute({"absent": "DROP TABLE gfx_target", "empty": "DELETE FROM gfx_target",
                                  "view": "DROP VIEW v_gfx"}[kind])
                for target in (out, build / "new-plugin"):
                    try:
                        plugin.generate(str(target))
                    except SystemExit as exc:
                        assert "run build.py gfx" in str(exc), str(exc)
                    else:
                        raise AssertionError("plugin accepted %s gfx data" % kind)
                assert before == {rel: (out / rel).read_bytes() for rel in plugin.output_paths()}
                assert not (build / "new-plugin").exists()

        for kind in ("absent", "empty", "view"):
            case("plugin: refuses %s gfx data before changing any output" % kind,
                 lambda kind=kind: incomplete_gfx_release(kind))

        def quick_renamed_corpus():
            out = root / "renamed-plugin"
            shutil.copytree(plugin.DEFAULT_OUT, out)
            db = out / "data/isa.db"
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE instruction SET name='REVIEW_RENAMED_FMA' WHERE name='V_FMA_F32'")
                conn.execute("UPDATE instruction SET name='REVIEW_RENAMED_ATOMIC'"
                             " WHERE name='BUFFER_ATOMIC_MAX_F32'")
            info_path = out / "build-info.json"
            info = json.loads(info_path.read_text(encoding="utf-8"))
            info["db_sha256"] = plugin.sha256(db)
            info_path.write_text(json.dumps(info), encoding="utf-8")
            manual = root / "quick-manual"
            manual.mkdir()
            for name in ("toc.tsv", "instr-index.tsv", "format-index.tsv"):
                (manual / name).write_text("arch\tpath\n", encoding="utf-8")
            command = [sys.executable, "-B", str(ROOT / "builder/verify.py"),
                       "--quick", "--plugin-dir", str(out)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=45,
                                    env=dict(os.environ, AMDGPU_ISA_MANUAL=str(manual)))
            assert result.returncode == 0, result.stdout + result.stderr

        case("quick: upstream instruction renames do not fail local builds", quick_renamed_corpus)

        def gfx_failure(missing=False):
            target = root / ("gfx-missing.db" if missing else "gfx-failure.db")
            fixture_database(target)
            with sqlite3.connect(target) as conn:
                conn.executescript(gfx_to_db.SCHEMA)
                conn.execute("INSERT INTO gfx_target VALUES ('gfx_old','test',NULL,NULL,NULL,NULL,NULL)")
            before = target.read_bytes()

            def rows(text):
                yield ["Unknown generation", "gfx9999", "", "``amdgpu99.99``", "dGPU"]
                if not missing:
                    raise ValueError("injected gfx parse failure")

            with patch.object(gfx_to_db, "parse_table", side_effect=rows), \
                    patch.object(sys, "argv", ["gfx_to_db.py", "--db", str(target)]), \
                    redirect_stdout(io.StringIO()):
                if missing:
                    assert gfx_to_db.main() == 1
                else:
                    error = io.StringIO()
                    with redirect_stderr(error):
                        assert gfx_to_db.main() == 1
                    assert "injected gfx parse failure" in error.getvalue()
            assert target.read_bytes() == before, "failed gfx changed the published database"
            assert not list(root.glob(".isa-*.db")), "gfx failure left staging files"

        case("gfx: parse failure preserves the published database", gfx_failure)
        case("gfx: incomplete mappings preserve the published database", lambda: gfx_failure(True))

        def manual_paths():
            home = root / "home"
            env = {"HOME": str(home), "XDG_DATA_HOME": "relative-data"}
            with patch.dict(os.environ, env):
                os.environ.pop("AMDGPU_ISA_MANUAL", None)
                builder = runpy.run_path(str(ROOT / "builder/manual_link.py"))
                cli = runpy.run_path(str(ROOT / "skill-template/scripts/isa.py"))
            expected = str(home / ".local/share/amdgpu-isa/manual")
            assert builder["MANUAL_ROOT"] == expected, builder["MANUAL_ROOT"]
            assert cli["MANUAL"] == expected, cli["MANUAL"]

        case("manual: relative XDG_DATA_HOME uses the same default in builder and CLI", manual_paths)

        def interrupted_stages():
            directory = root / "interrupted"
            directory.mkdir()
            sources = directory / "sources"
            fixture_xml(sources)
            target = directory / "isa.db"
            stale = [directory / (prefix + "deadbeef.db")
                     for prefix in (".isa-xml-", ".isa-gfx-", ".isa-db-")]
            for path in stale:
                path.write_bytes(b"interrupted database")
                Path(str(path) + ".owner").write_text(str(path.resolve()) + "\n", encoding="utf-8")
            unowned = directory / ".isa-db-unowned1.db"
            unowned.write_bytes(b"unowned database")
            unrelated = directory / "keep.db"
            unrelated.write_bytes(b"unrelated database")
            link = directory / ".isa-db-symlink1.db"
            link.symlink_to(unrelated)
            with patch.object(sys, "argv", ["xml_to_db.py", "--sources", str(sources),
                                           "--out", str(target)]), redirect_stdout(io.StringIO()):
                assert xml_to_db.main() == 0
            assert not any(path.exists() for path in stale), "interrupted stages were retained"
            assert unowned.read_bytes() == b"unowned database", "unowned database was removed"
            assert unrelated.read_bytes() == b"unrelated database" and link.is_symlink()

        case("database: next stage removes interrupted DB files and preserves other files", interrupted_stages)

        def reference_validation():
            validator_path = ROOT.parent.parent / "scripts/validate.py"
            namespace = runpy.run_path(str(validator_path), run_name="validation_fixture")
            globals_ = namespace["check_mirrored"].__globals__
            checkout = root / "validation-checkout"
            for copy, source in namespace["mirrored_files"]():
                for relative in (copy, source):
                    path = checkout / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("same copied content\n", encoding="utf-8")
            builder = checkout / "plugins/amdgpu-isa-builder"
            (builder / "builder").mkdir()
            shutil.copyfile(ROOT / "builder/plugin.py", builder / "builder/plugin.py")
            source = builder / "skill-template/references/future.md"
            copy = checkout / "plugins/amdgpu-isa/skills/amdgpu-isa/references/future.md"
            source.parent.mkdir(exist_ok=True)
            copy.parent.mkdir(parents=True, exist_ok=True)
            source.write_text("maintained guidance\n", encoding="utf-8")
            copy.write_text("stale guidance\n", encoding="utf-8")
            globals_["ROOT"] = str(checkout)
            namespace["check_mirrored"]()
            assert any("references/future.md" in problem for problem in namespace["problems"]), \
                "new reference drift was ignored"
            namespace["problems"].clear()
            shutil.copyfile(source, copy)
            namespace["check_mirrored"]()
            assert not namespace["problems"], namespace["problems"]

        case("validation: new Markdown references are checked without build inputs", reference_validation)
        first, second = root / "first.db", root / "second.db"
        fixture_database(first)
        shutil.copyfile(first, second)
        with sqlite3.connect(second) as conn:
            conn.execute("PRAGMA page_size=8192")
            conn.execute("VACUUM")
            conn.execute("ANALYZE")

        def logical_equality():
            assert first.read_bytes() != second.read_bytes()
            assert database.content_digest(first) == database.content_digest(second)
            with sqlite3.connect(second) as conn:
                conn.execute("UPDATE sample SET number=7 WHERE number=1")
            assert database.content_digest(first) != database.content_digest(second)
            with sqlite3.connect(second) as conn:
                conn.execute("UPDATE sample SET number=1 WHERE number=7")
                conn.execute("ALTER TABLE sample ADD COLUMN added TEXT")
            assert database.content_digest(first) != database.content_digest(second)

        case("database: digest follows schema and rows, not storage", logical_equality)

        def planner_statistics():
            target = root / "statistics.db"
            fixture_database(target)
            with sqlite3.connect(target) as conn:
                conn.execute("DROP TABLE sqlite_stat1")
            unanalyzed = database.content_digest(target)
            with sqlite3.connect(target) as conn:
                conn.execute("ANALYZE")
            analyzed = database.content_digest(target)
            assert unanalyzed != analyzed, "ANALYZE statistics were ignored"

        case("database: digest includes new ANALYZE statistics", planner_statistics)

        def changed_statistics():
            target = root / "changed-statistics.db"
            fixture_database(target)
            analyzed = database.content_digest(target)
            with sqlite3.connect(target) as conn:
                conn.execute("UPDATE sqlite_stat1 SET stat='999' WHERE tbl='sample'")
            assert analyzed != database.content_digest(target), "changed statistics were ignored"

        case("database: digest includes changed ANALYZE statistics", changed_statistics)

        def fts_statistics():
            target = root / "fts-statistics.db"
            fixture_database(target)
            before = database.content_digest(target)
            with sqlite3.connect(target) as conn:
                changed = conn.execute("UPDATE sqlite_stat1 SET stat='999' WHERE tbl='fts_inst_data'")
                assert changed.rowcount, "fixture has no FTS shadow statistics"
            assert before == database.content_digest(target), "FTS storage statistics changed the digest"

        case("database: digest ignores FTS shadow planner statistics", fts_statistics)

        def reordered_statistics():
            target = root / "reordered-statistics.db"
            fixture_database(target)
            before = database.content_digest(target)
            with sqlite3.connect(target) as conn:
                rows = conn.execute("SELECT tbl,idx,stat FROM sqlite_stat1 ORDER BY rowid").fetchall()
                assert len(rows) > 1
                conn.execute("DELETE FROM sqlite_stat1")
                conn.executemany("INSERT INTO sqlite_stat1 VALUES (?,?,?)", reversed(rows))
                assert rows != conn.execute(
                    "SELECT tbl,idx,stat FROM sqlite_stat1 ORDER BY rowid").fetchall()
            assert before == database.content_digest(target)

        case("database: equal statistics ignore insertion order", reordered_statistics)

        def fallback():
            target = root / "old-sqlite.db"
            source = root / "unpublished-old-sqlite.db"
            fixture_database(source)
            target.write_bytes(b"previous published database")
            conn = sqlite3.connect(source)

            class OldConnection:
                def __getattr__(self, name):
                    return getattr(conn, name)

                def execute(self, sql, *args):
                    if sql.startswith("VACUUM INTO"):
                        raise sqlite3.OperationalError("near INTO: syntax error")
                    return conn.execute(sql, *args)

            with patch.object(sqlite3, "sqlite_version_info", (3, 26, 0)):
                database.finalize(OldConnection(), target)
            assert database.content_digest(first) == database.content_digest(target)
            assert target.stat().st_mode & 0o777 == 0o644

        case("database: SQLite 3.26 finalizes without VACUUM INTO", fallback)

        def run_xml(sources, target):
            with patch.object(sys, "argv", ["xml_to_db.py", "--sources", str(sources),
                                           "--out", str(target)]), redirect_stdout(io.StringIO()):
                return xml_to_db.main()

        def xml_failure(kind, published):
            fixture = root / ("xml-%s-%s" % (kind, "existing" if published else "first"))
            fixture.mkdir()
            sources = fixture / "sources"
            fixture_xml(sources)
            target = fixture / "output" / "isa.db"
            target.parent.mkdir()
            if published:
                fixture_database(target)
            before = (target.read_bytes(), target.stat().st_mtime_ns) if published else None
            if kind == "crash":
                script = """
import os
import sys
sys.path.insert(0, sys.argv.pop(1))
import xml_to_db
original = xml_to_db.load_arch
def crash(conn, path):
    original(conn, path)
    os._exit(23)
xml_to_db.load_arch = crash
xml_to_db.main()
"""
                result = subprocess.run(
                    [sys.executable, "-I", "-B", "-c", script, str(ROOT / "builder"),
                     "--sources", str(sources), "--out", str(target)],
                    capture_output=True, text=True, timeout=30)
                assert result.returncode == 23, result.stderr
            else:
                if kind == "parse":
                    (sources / "amdgpu_isa_zbad.xml").write_text("<Spec>", encoding="utf-8")
                    operation = nullcontext()
                    expected = xml_to_db.ET.ParseError
                elif kind == "publish":
                    operation = patch.object(database.os, "replace", side_effect=OSError("publish failed"))
                    expected = OSError
                else:
                    expected = KeyboardInterrupt if kind == "interrupt" else OSError
                    original = xml_to_db.load_arch

                    def fail(conn, path):
                        original(conn, path)
                        raise expected("build stopped")

                    operation = patch.object(xml_to_db, "load_arch", side_effect=fail)
                with operation:
                    try:
                        run_xml(sources, target)
                    except expected:
                        pass
                    else:
                        raise AssertionError("failed XML build succeeded")
            if published:
                assert before == (target.read_bytes(), target.stat().st_mtime_ns), \
                    "failed XML build changed the published database"
            else:
                assert not target.exists(), "failed XML build published a partial database"
            if kind != "crash":
                assert not list(target.parent.glob(".isa-*")), "failed build left staging files"

        for kind in ("parse", "error", "interrupt", "publish", "crash"):
            for published in (True, False):
                case("xml: %s preserves %s output" % (kind, "existing" if published else "absent"),
                     lambda kind=kind, published=published: xml_failure(kind, published))

        def xml_publication(legacy=False):
            suffix = "legacy" if legacy else "current"
            sources = root / ("xml-success-sources-" + suffix)
            fixture_xml(sources)
            target = root / ("xml-success-" + suffix + ".db")
            fixture_database(target)
            before = target.read_bytes()
            original = xml_to_db.load_arch

            def observe(conn, path):
                source = Path(conn.execute("PRAGMA database_list").fetchone()[2])
                assert source != target, "XML writes into the published destination"
                assert source.parent == target.parent, "XML stage is on a different filesystem"
                assert target.read_bytes() == before, "published database changed during XML loading"
                return original(conn, path)

            version = patch.object(sqlite3, "sqlite_version_info", (3, 26, 0)) if legacy else nullcontext()
            with version:
                with patch.object(xml_to_db, "load_arch", side_effect=observe):
                    assert run_xml(sources, target) == 0
                with sqlite3.connect(target) as conn:
                    assert conn.execute("SELECT name FROM instruction").fetchall() == [("TEST",)]
                    assert conn.execute("SELECT * FROM sqlite_stat1").fetchall()
                    assert conn.execute("PRAGMA quick_check").fetchone() == ("ok",)
                first_build = target.read_bytes()
                assert run_xml(sources, target) == 0
                assert first_build == target.read_bytes(), "repeat XML build changed database bytes"
                assert not list(target.parent.glob(".isa-*")), "successful build left staging files"

        case("xml: publishes an analyzed sibling stage reproducibly", xml_publication)
        case("xml: legacy SQLite publishes a sibling stage reproducibly",
             lambda: xml_publication(legacy=True))

        def provenance():
            sources = root / "sources"
            sources.mkdir()
            manifest = json.loads((ROOT / "sources/manifest.json").read_text(encoding="utf-8"))
            manifest["sources"] = [e for e in manifest["sources"]
                                   if e["category"] != "machine-readable-isa"]
            (sources / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(plugin, "SOURCES", str(sources)):
                try:
                    plugin.source_entries()
                except SystemExit as exc:
                    assert "machine-readable" in str(exc)
                else:
                    raise AssertionError("missing MIT corpus source was accepted")

        case("plugin: manifest requires the MIT corpus source", provenance)

        def unchanged_generation():
            build = root / "build"
            build.mkdir()
            db = build / "isa.db"
            shutil.copyfile(first, db)
            out = root / "plugin"
            with patch.object(plugin, "BUILD", str(build)):
                plugin.generate(str(out))
                before = {rel: ((out / rel).read_bytes(), (out / rel).stat().st_mtime_ns)
                          for rel in plugin.OUTPUTS}
                with sqlite3.connect(db) as conn:
                    conn.execute("PRAGMA page_size=8192")
                    conn.execute("VACUUM")
                    conn.execute("ANALYZE")
                assert db.read_bytes() != before["data/isa.db"][0]
                plugin.generate(str(out))
                assert before == {rel: ((out / rel).read_bytes(), (out / rel).stat().st_mtime_ns)
                                  for rel in plugin.OUTPUTS}
                info = json.loads((out / "build-info.json").read_text(encoding="utf-8"))
                assert info["db_sha256"] == plugin.sha256(out / "data/isa.db")

        case("plugin: storage-only rebuild preserves every output", unchanged_generation)

        def reference_generation():
            import verify
            templates = root / "reference-templates"
            shutil.copytree(ROOT / "skill-template", templates)
            references = templates / "references"
            references.mkdir(exist_ok=True)
            (references / "fixture.md").write_text("Fixture query guidance.\n", encoding="utf-8")
            out = root / "reference-plugin"
            with patch.object(plugin, "TEMPLATE_DIR", str(templates)):
                plugin.generate(str(out))
                assert (out / "references/fixture.md").read_text() == "Fixture query guidance.\n"
                saved = verify.failures[:]
                try:
                    verify.failures.clear()
                    with redirect_stdout(io.StringIO()):
                        verify.verify_plugin_purity(str(out))
                    assert not verify.failures, verify.failures
                    (out / "references/unexpected.md").write_text("unexpected\n", encoding="utf-8")
                    with redirect_stdout(io.StringIO()):
                        verify.verify_plugin_purity(str(out))
                    assert "plugin holds only generated files" in verify.failures
                finally:
                    verify.failures[:] = saved

        case("plugin: copies Markdown references and rejects unowned references", reference_generation)

        def strict_template():
            templates = root / "templates"
            shutil.copytree(ROOT / "skill-template", templates)
            path = templates / "SKILL.md.tmpl"
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\nisa.py $unknown_release_placeholder\n")
            with patch.object(plugin, "TEMPLATE_DIR", str(templates)):
                try:
                    plugin.generate(str(root / "bad-template"))
                except (KeyError, ValueError, SystemExit):
                    return
            raise AssertionError("unknown placeholder on an isa.py line was accepted")

        case("plugin: every template placeholder is validated", strict_template)

        def render_failure(filename):
            fixture = root / ("render-failure-" + filename)
            fixture.mkdir()
            build, out, templates = fixture / "build", fixture / "plugin", fixture / "templates"
            build.mkdir()
            db = build / "isa.db"
            shutil.copyfile(first, db)
            shutil.copytree(ROOT / "skill-template", templates)
            with patch.object(plugin, "BUILD", str(build)), \
                    patch.object(plugin, "TEMPLATE_DIR", str(templates)):
                plugin.generate(str(out))
                before = {rel: ((out / rel).read_bytes(), (out / rel).stat().st_mtime_ns,
                                (out / rel).stat().st_mode) for rel in plugin.OUTPUTS}
                with sqlite3.connect(db) as conn:
                    conn.execute("UPDATE build_info SET value='candidate corpus' WHERE key='source'")
                with (templates / "scripts/isa.py").open("a", encoding="utf-8") as stream:
                    stream.write("\n# Candidate script replacement.\n")
                (templates / "LICENSE.LLVM.TXT").write_text("candidate licence\n", encoding="utf-8")
                with (templates / filename).open("a", encoding="utf-8") as stream:
                    stream.write("\n$missing_render_placeholder\n")
                try:
                    plugin.generate(str(out))
                except (KeyError, ValueError, SystemExit):
                    pass
                else:
                    raise AssertionError("invalid template was accepted")
                after = {rel: ((out / rel).read_bytes(), (out / rel).stat().st_mtime_ns,
                               (out / rel).stat().st_mode) for rel in plugin.OUTPUTS}
                assert before == after, "template failure changed already-published files"
                absent = fixture / "absent-plugin"
                try:
                    plugin.generate(str(absent))
                except (KeyError, ValueError, SystemExit):
                    pass
                else:
                    raise AssertionError("invalid template was accepted for a fresh output")
                assert not absent.exists(), "template failure created a partial output"

        for filename in ("SKILL.md.tmpl", "NOTICE.md.tmpl"):
            case("plugin: %s failure preserves every published output" % filename,
                 lambda filename=filename: render_failure(filename))
    return ok
