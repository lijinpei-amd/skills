"""Exercise manual rebuilds with temporary PDFs, databases, and symlinks.

The verifier calls run_checks(check) with its normal result recorder. PDF
fixtures use the manual stage's virtual environment; this module is stdlib-only.
"""

import ast
import csv
import fcntl
import os
from pathlib import Path
import re
import selectors
import shutil
import sqlite3
import subprocess
import sys
import tempfile

from check_helpers import run, validate_json

ROOT = Path(__file__).resolve().parent.parent

MAKE_PDF = """import pymupdf, random, sys
path, count, title, text = sys.argv[1:]
doc = pymupdf.open()
for index in range(int(count)):
    page = doc.new_page()
    page.insert_text((72, 100), text)
    rng = random.Random(title + str(index))
    pixels = bytes(rng.getrandbits(8) for _ in range(80 * 80 * 3))
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, 80, 80, pixels, 0)
    page.insert_image(pymupdf.Rect(72, 150, 152, 230), pixmap=pixmap)
doc.set_toc([[1, title, 1]])
doc.save(path)
doc.close()
"""


def snapshot(path):
    """Return file contents and link targets without following symlinks."""
    output = {}
    for entry in sorted(path.rglob("*")):
        relative = str(entry.relative_to(path))
        if entry.is_symlink():
            output[relative] = ("link", os.readlink(entry))
        elif entry.is_file():
            output[relative] = ("file", entry.read_bytes())
        elif entry.is_dir():
            output[relative] = ("directory",)
    return output


def index_rows(manual, arch):
    """Return one arch's rows from all three indexes."""
    rows = {}
    for name in ("toc.tsv", "instr-index.tsv", "format-index.tsv"):
        with (manual / name).open(encoding="utf-8", newline="") as stream:
            rows[name] = [row for row in csv.DictReader(stream, delimiter="\t")
                          if row["arch"] == arch]
    return rows


def run_build_checks(check):
    """Check invalid input preservation and replacement of rebuilt archs."""
    interpreter = ROOT / ".venv/bin/python"
    if not interpreter.is_file():
        interpreter = Path(sys.executable)
    source = ROOT / "builder/pdf_to_manual.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    pdf_arch = next(ast.literal_eval(node.value) for node in tree.body
                    if isinstance(node, ast.Assign)
                    and any(getattr(target, "id", None) == "PDF_ARCH" for target in node.targets))
    (filename, arch), (other_filename, other_arch) = sorted(
        pdf_arch.items(), key=lambda item: item[1])[:2]
    ok = True
    with tempfile.TemporaryDirectory(prefix="isa-manual-lifecycle-") as scratch:
        root = Path(scratch)
        pdfs, manual, db = root / "pdfs", root / "manual", root / "isa.db"
        pdfs.mkdir()
        with sqlite3.connect(db) as conn:
            conn.executescript("CREATE TABLE v_inst(arch TEXT, name TEXT, opcode INTEGER);"
                               "CREATE TABLE encoding(arch TEXT, name TEXT);")
            for key in (arch, other_arch):
                conn.executemany("INSERT INTO v_inst VALUES (?, ?, ?)",
                                 [(key, "V_OLD_F32", 123), (key, "V_NEW_F32", 456)])
                conn.executemany("INSERT INTO encoding VALUES (?, ?)",
                                 [(key, "ENC_OLD"), (key, "ENC_NEW")])

        def make_pdf(name, count, title, text):
            return run([str(interpreter), "-B", "-c", MAKE_PDF,
                        str(pdfs / name), str(count), title, text])

        for name in (filename, other_filename):
            result = make_pdf(name, 2, "Original ISA",
                              "V_OLD_F32\n123\nOriginal definition\nENC_OLD")
            if result.returncode:
                return check("manual lifecycle: PDF fixtures available", False,
                             result.stderr.strip())
        command = [str(interpreter), "-B", str(source), "--pdfs", str(pdfs),
                   "--out", str(manual), "--db", str(db)]
        failed_first = root / "failed-first-conversion"
        saved_pdf = (pdfs / filename).read_bytes()
        (pdfs / filename).write_bytes(b"not a PDF")
        first_command = command[:]
        first_command[first_command.index("--out") + 1] = str(failed_first)
        result = run(first_command + ["--only", arch])
        ok = check("manual: first PDF conversion failure leaves no manual root",
                   result.returncode != 0 and not os.path.lexists(failed_first),
                   result.stderr.strip() if failed_first.exists() else "") and ok
        (pdfs / filename).write_bytes(saved_pdf)

        runner = """import os, sys
source, manual = sys.argv[1:3]
sys.path.insert(0, os.path.dirname(source))
import pdf_to_manual
original = os.replace
def fail_first_publication(src, dest):
    if str(dest) == manual or os.path.dirname(str(dest)) == manual:
        raise OSError('injected fixture first publication failure')
    return original(src, dest)
os.replace = fail_first_publication
sys.argv = [source] + sys.argv[3:]
raise SystemExit(pdf_to_manual.main())
"""
        failed_first = root / "failed-first-publication"
        first_command[first_command.index("--out") + 1] = str(failed_first)
        result = run([str(interpreter), "-B", "-c", runner, str(source), str(failed_first)]
                     + first_command[3:] + ["--only", arch])
        ok = check("manual: first publication failure leaves no manual root",
                   result.returncode != 0 and not os.path.lexists(failed_first)
                   and "injected fixture first publication failure" in result.stderr,
                   result.stderr.strip() if failed_first.exists() else "") and ok

        result = run(command)
        if not check("manual lifecycle: first successful publish creates the TOC marker",
                     result.returncode == 0 and (manual / "toc.tsv").is_file(),
                     result.stderr.strip()):
            return False

        staging_case = root / "staging-case"
        staging_case.mkdir()
        destination = staging_case / "manual"
        attempt = command[:]
        attempt[attempt.index("--out") + 1] = str(destination)
        runner = """import contextlib, os, shutil, sys, tempfile
source = sys.argv[1]
sys.path.insert(0, os.path.dirname(source))
import pdf_to_manual
@contextlib.contextmanager
def strict_temporary_directory(*args, **kwargs):
    path = tempfile.mkdtemp(*args, **kwargs)
    try:
        yield path
    finally:
        shutil.rmtree(path)
pdf_to_manual.tempfile.TemporaryDirectory = strict_temporary_directory
os.umask(0o027)
sys.argv = [source] + sys.argv[2:]
raise SystemExit(pdf_to_manual.main())
"""
        result = run([str(interpreter), "-B", "-c", runner] + attempt[2:] + ["--only", arch])
        ok = check("manual: first publish succeeds with strict moved-path cleanup",
                   result.returncode == 0 and (destination / "toc.tsv").is_file(),
                   result.stderr.strip() if result.returncode else "") and ok
        mode = destination.stat().st_mode & 0o777 if destination.exists() else None
        ok = check("manual: first root permissions honor umask 027",
                   mode == 0o750, "mode: %r" % mode) and ok
        modes = {path.name: path.stat().st_mode & 0o777
                 for path in destination.rglob("*") if path.is_file()
                 and path.name != ".amdgpu-isa-stage-owner"}
        ok = check("manual: first pages and indexes honor umask 027",
                   bool(modes) and all(mode == 0o640 for mode in modes.values()),
                   "modes: %r" % modes if any(mode != 0o640 for mode in modes.values()) else "") and ok
        destination.chmod(0o705)
        result = run(attempt + ["--only", arch])
        ok = check("manual: rebuilding preserves existing root permissions",
                   result.returncode == 0 and destination.stat().st_mode & 0o777 == 0o705) and ok

        helper = root / "cache-builder"
        (helper / "builder").mkdir(parents=True)
        (helper / "builder/manual_link.py").write_text("# Fixture builder marker.\n", encoding="utf-8")
        cache = helper / "build/manual"
        shutil.copytree(manual, cache)
        cache_link = root / "cache-manual-link"
        cache_link.symlink_to(cache, target_is_directory=True)
        link_command = command[:]
        link_command[link_command.index("--out") + 1] = str(cache_link)
        result = run(link_command + ["--only", arch])
        ok = check("manual: valid builder-cache symlinks stay intact and use their target",
                   result.returncode == 0 and cache_link.is_symlink()
                   and os.readlink(cache_link) == str(cache)
                   and (cache / "toc.tsv").is_file()) and ok
        file_target = root / "non-directory-target"
        file_target.write_text("preserved file\n", encoding="utf-8")
        for label, target in (("dangling cache", root / "deleted-builder/build/manual"),
                              ("dangling external", root / "missing/manual-pages"),
                              ("non-directory", file_target)):
            link = root / label.replace(" ", "-")
            link.symlink_to(target, target_is_directory=True)
            link_command[link_command.index("--out") + 1] = str(link)
            result = run(link_command + ["--only", arch])
            preserved = (target.read_text() == "preserved file\n" if target == file_target
                         else not target.exists() and not target.parent.exists())
            ok = check("manual: reports %s symlink without recreating or changing its target" % label,
                       result.returncode == 1 and "manual path links to" in result.stderr
                       and "Traceback" not in result.stderr and preserved
                       and link.is_symlink() and os.readlink(link) == str(target)) and ok

        alias_parent = root / "alias-parent"
        alias_parent.mkdir()
        alias = alias_parent / "manual-link"
        alias.symlink_to(destination, target_is_directory=True)
        alias_attempt = attempt[:]
        alias_attempt[alias_attempt.index("--out") + 1] = str(alias)

        runner = """import contextlib, io, os, sys
source = sys.argv[1]
sys.path.insert(0, os.path.dirname(source))
import pdf_to_manual
original = pdf_to_manual.convert
def pause_before_conversion(pdf, arch, staged):
    print(staged, file=sys.__stdout__, flush=True)
    sys.stdin.readline()
    return original(pdf, arch, staged)
pdf_to_manual.convert = pause_before_conversion
sys.argv = [source] + sys.argv[2:]
with contextlib.redirect_stdout(io.StringIO()):
    raise SystemExit(pdf_to_manual.main())
"""
        child = subprocess.Popen([str(interpreter), "-B", "-c", runner]
                                 + alias_attempt[2:] + ["--only", arch],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        staged = None
        try:
            with selectors.DefaultSelector() as ready:
                ready.register(child.stdout, selectors.EVENT_READ)
                if ready.select(timeout=15):
                    value = child.stdout.readline().strip()
                    staged = Path(value) if value else None
            ok = check("manual: active stage stays beside the live root",
                       staged is not None and staged.is_dir()
                       and staged.parent == staging_case
                       and staged.name.startswith(".amdgpu-isa-manual-stage-")) and ok
            with (staging_case / ".amdgpu-isa-manual.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    locked = True
                else:
                    locked = False
                    fcntl.flock(lock, fcntl.LOCK_UN)
            ok = check("manual: active build holds the cleanup/publication lock", locked) and ok
        finally:
            child.kill()
            child.communicate(timeout=5)
        stale = staging_case / ".amdgpu-isa-manual-stage-killed"
        stale.mkdir()
        (stale / "partial.md").write_text("interrupted build", encoding="utf-8")
        (stale / ".amdgpu-isa-stage-owner").write_text(str(stale.resolve()) + "\n", encoding="utf-8")
        (stale / "toc.tsv").write_text(
            "arch\tlevel\tpath\ttitle\ncdna1\t1\tcdna1/p0001.md\tInterrupted stage\n", encoding="utf-8")
        recovery = staging_case / ".amdgpu-isa-manual-backup-retained"
        recovery.mkdir()
        (recovery / "original.md").write_text("original pages", encoding="utf-8")
        result = run(attempt + ["--only", arch])
        ok = check("manual: next build removes killed sibling stages",
                   result.returncode == 0 and not stale.exists()
                   and (staged is None or not staged.exists())) and ok
        ok = check("manual: staged build through an alias preserves its link",
                   alias.is_symlink() and os.readlink(alias) == str(destination)) and ok
        ok = check("manual: stage cleanup preserves recovery backups",
                   (recovery / "original.md").read_text(encoding="utf-8") == "original pages") and ok

        named_root = staging_case / ".amdgpu-isa-manual-stage-live"
        shutil.copytree(manual, named_root)
        named_before = snapshot(named_root)
        saved_pdf = (pdfs / filename).read_bytes()
        (pdfs / filename).write_bytes(b"not a PDF")
        named_command = command[:]
        named_command[named_command.index("--out") + 1] = str(named_root)
        result = run(named_command + ["--only", arch])
        (pdfs / filename).write_bytes(saved_pdf)
        ok = check("manual: stage cleanup preserves the selected live root on conversion failure",
                   result.returncode != 0 and snapshot(named_root) == named_before) and ok

        published_root = staging_case / ".amdgpu-isa-manual-stage-published"
        named_command[named_command.index("--out") + 1] = str(published_root)
        result = run(named_command)
        published_before = snapshot(published_root)
        published_ok = result.returncode == 0 and (published_root / "toc.tsv").is_file()
        result = run(attempt + ["--only", arch])
        ok = check("manual: stage cleanup preserves a published sibling installation",
                   published_ok and result.returncode == 0
                   and snapshot(published_root) == published_before) and ok

        before = snapshot(manual)
        missing = root / "missing.db"
        empty = root / "empty.db"
        empty.touch()
        corrupt = root / "corrupt.db"
        corrupt.write_bytes(b"not a SQLite database")
        incomplete = root / "incomplete.db"
        with sqlite3.connect(incomplete) as conn:
            conn.executescript("CREATE TABLE v_inst(arch TEXT, name TEXT, opcode INTEGER);"
                               "CREATE TABLE encoding(name TEXT);")
        result = make_pdf(filename, 1, "Rejected ISA",
                          "V_NEW_F32\n456\nRejected definition\nENC_NEW")
        if result.returncode:
            return check("manual lifecycle: invalid DB PDF fixture available", False,
                         result.stderr.strip())
        for label, invalid in (("missing", missing), ("empty", empty), ("corrupt", corrupt),
                               ("incomplete schema", incomplete)):
            attempt = root / (label.replace(" ", "-") + "-manual")
            shutil.copytree(manual, attempt)
            attempt_command = command[:]
            attempt_command[attempt_command.index("--out") + 1] = str(attempt)
            result = run(attempt_command[:-1] + [str(invalid), "--only", arch])
            passed = (result.returncode != 0 and snapshot(attempt) == before
                      and "isa.db unavailable or invalid" in result.stderr
                      and "Traceback" not in result.stderr
                      and not missing.exists())
            ok = check("manual: %s DB fails before changing pages or indexes" % label,
                       passed, result.stderr.strip() if not passed else "") and ok
        absent = root / "absent-manual"
        result = run([str(interpreter), "-B", str(source), "--pdfs", str(pdfs),
                      "--out", str(absent), "--db", str(missing)])
        ok = check("manual: missing DB creates no empty manual directory",
                   result.returncode != 0 and not absent.exists()) and ok

        (manual / arch / "img/obsolete.png").write_bytes(b"obsolete figure")
        other_before = snapshot(manual / other_arch)
        other_indexes = index_rows(manual, other_arch)
        result = make_pdf(filename, 1, "Replacement ISA",
                          "V_NEW_F32\n456\nReplacement definition\nENC_NEW")
        if result.returncode:
            return check("manual lifecycle: replacement PDF fixture available", False,
                         result.stderr.strip()) and ok
        result = run(command + ["--only", arch])
        pages = list((manual / arch).glob("p*.md"))
        page_text = pages[0].read_text(encoding="utf-8") if len(pages) == 1 else ""
        referenced = set(re.findall(r"!\[figure\]\(img/([^\)]+)\)", page_text))
        images = {entry.name for entry in (manual / arch / "img").iterdir()}
        rebuilt = index_rows(manual, arch)
        passed = (result.returncode == 0 and len(pages) == 1 and referenced == images
                  and bool(images) and "Replacement definition" in page_text
                  and "Original definition" not in page_text
                  and all(row["page_physical"] == "1" for rows in rebuilt.values()
                          for row in rows)
                  and any(row["instruction"] == "V_NEW_F32"
                          for row in rebuilt["instr-index.tsv"]))
        ok = check("manual: shorter PDF prunes stale pages and unused figures", passed,
                   result.stderr.strip() if not passed else "") and ok
        ok = check("manual: rebuilding one arch preserves other pages and all index rows",
                   snapshot(manual / other_arch) == other_before
                   and index_rows(manual, other_arch) == other_indexes) and ok

        alias = root / "manual-alias"
        alias.symlink_to(manual, target_is_directory=True)
        alias_command = command[:]
        alias_command[alias_command.index("--out") + 1] = str(alias)
        result = run(alias_command + ["--only", arch])
        ok = check("manual: rebuild through an external root link preserves the link",
                   result.returncode == 0 and alias.is_symlink()
                   and os.readlink(alias) == str(manual),
                   result.stderr.strip() if result.returncode else "") and ok

        atomic_root = root / "atomic-index-parent/manual"
        shutil.copytree(manual, atomic_root)
        atomic_command = command[:]
        atomic_command[atomic_command.index("--out") + 1] = str(atomic_root)
        runner = """import os, sys
source, manual = sys.argv[1:3]
sys.path.insert(0, os.path.dirname(source))
import pdf_to_manual
original = os.replace
def observe_indexes(src, dest):
    original(src, dest)
    for name in pdf_to_manual.INDEX_FILES:
        if not os.path.isfile(os.path.join(manual, name)):
            raise OSError('published index disappeared: ' + name)
os.replace = observe_indexes
sys.argv = [source] + sys.argv[3:]
raise SystemExit(pdf_to_manual.main())
"""
        result = run([str(interpreter), "-B", "-c", runner, str(source), str(atomic_root)]
                     + atomic_command[3:] + ["--only", arch])
        ok = check("manual: rebuilding never removes a published index before its replacement",
                   result.returncode == 0, result.stderr.strip() if result.returncode else "") and ok

        before_kill = snapshot(manual / arch)
        runner = """import os, signal, sys
source, manual, arch = sys.argv[1:4]
sys.path.insert(0, os.path.dirname(source))
import pdf_to_manual
original = os.replace
def kill_after_move(src, dest):
    original(src, dest)
    if str(src) == os.path.join(manual, arch) and os.path.basename(os.path.dirname(dest)).startswith('.amdgpu-isa-manual-backup-'):
        os.kill(os.getpid(), signal.SIGKILL)
os.replace = kill_after_move
sys.argv = [source] + sys.argv[4:]
raise SystemExit(pdf_to_manual.main())
"""
        result = run([str(interpreter), "-B", "-c", runner, str(source), str(manual), arch]
                     + command[3:] + ["--only", arch])
        ok = check("manual: kill fixture stops between old and new arch publication",
                   result.returncode == -9 and not (manual / arch).exists()) and ok
        result = run(command + ["--only", other_arch])
        ok = check("manual: next build restores a killed arch before rebuilding another arch",
                   result.returncode == 0 and snapshot(manual / arch) == before_kill) and ok

        before_failure = snapshot(manual)
        result = make_pdf(filename, 2, "Failing replacement ISA",
                          "V_OLD_F32\n123\nFailed conversion content\nENC_OLD")
        if result.returncode:
            return check("manual lifecycle: failure PDF fixture available", False,
                         result.stderr.strip()) and ok

        later_failure = root / "later-pdf-failure"
        shutil.copytree(manual, later_failure)
        saved_pdf = (pdfs / other_filename).read_bytes()
        (pdfs / other_filename).write_bytes(b"not a PDF")
        attempt_command = command[:]
        attempt_command[attempt_command.index("--out") + 1] = str(later_failure)
        result = run(attempt_command)
        ok = check("manual: later PDF failure preserves every requested arch and index",
                   result.returncode != 0 and snapshot(later_failure) == before_failure) and ok
        (pdfs / other_filename).write_bytes(saved_pdf)

        malformed = root / "malformed-index"
        shutil.copytree(manual, malformed)
        (malformed / "toc.tsv").write_text("invalid header\n", encoding="utf-8")
        malformed_before = snapshot(malformed)
        attempt_command[attempt_command.index("--out") + 1] = str(malformed)
        result = run(attempt_command + ["--only", arch])
        ok = check("manual: invalid index header fails before replacing pages",
                   result.returncode != 0 and snapshot(malformed) == malformed_before) and ok
        runner = """import os, runpy, sys
source = sys.argv[1]
sys.path.insert(0, os.path.dirname(source))
import pdf_to_manual
original = pdf_to_manual.save_image
calls = 0
def fail_after_first_page(*args):
    global calls
    calls += 1
    if calls == 2:
        raise OSError('injected fixture conversion failure')
    return original(*args)
pdf_to_manual.save_image = fail_after_first_page
sys.argv = [source] + sys.argv[2:]
raise SystemExit(pdf_to_manual.main())
"""
        result = run([str(interpreter), "-B", "-c", runner] + command[2:] + ["--only", arch])
        passed = result.returncode != 0 and snapshot(manual) == before_failure
        ok = check("manual: failed one-arch conversion preserves pages and indexes",
                   passed, result.stderr.strip() if not passed else "") and ok

        runner = """import os, sys
source, manual = sys.argv[1:3]
sys.path.insert(0, os.path.dirname(source))
import pdf_to_manual
original = os.replace
def fail_last_index(src, dest):
    if str(dest) == os.path.join(manual, 'format-index.tsv') and 'backup' not in str(src):
        raise OSError('injected fixture final index replacement failure')
    return original(src, dest)
os.replace = fail_last_index
sys.argv = [source] + sys.argv[3:]
raise SystemExit(pdf_to_manual.main())
"""
        result = run([str(interpreter), "-B", "-c", runner, str(source), str(manual)]
                     + command[3:])
        ok = check("manual: failed index publication rolls back every arch and index",
                   result.returncode != 0 and snapshot(manual) == before_failure) and ok

        runner = """import os, sys
source, manual = sys.argv[1:3]
sys.path.insert(0, os.path.dirname(source))
import pdf_to_manual
original = os.replace
def fail_publication_and_restore(src, dest):
    if str(dest) == os.path.join(manual, 'format-index.tsv') and 'backup' not in str(src):
        raise OSError('injected fixture publication failure')
    if str(dest) == os.path.join(manual, 'toc.tsv') and 'backup' in str(src):
        raise OSError('injected fixture rollback failure')
    return original(src, dest)
os.replace = fail_publication_and_restore
sys.argv = [source] + sys.argv[3:]
raise SystemExit(pdf_to_manual.main())
"""
        result = run([str(interpreter), "-B", "-c", runner, str(source), str(manual)]
                     + command[3:])
        backups = list(manual.parent.glob(".amdgpu-isa-manual-backup-*"))
        live = snapshot(manual)
        recovery = snapshot(backups[0]) if len(backups) == 1 else {}
        passed = (result.returncode != 0 and len(backups) == 1
                  and "original files retained" in result.stderr
                  and all(recovery.get(name, live.get(name)) == content
                          for name, content in before_failure.items()))
        ok = check("manual: rollback failure retains all originals for recovery", passed) and ok
    return ok


def run_query_checks(isa, check):
    """Check grep and selftest against partial published manual trees."""
    ok = True

    def record(label, passed, result):
        nonlocal ok
        detail = (result.stderr.strip() or result.stdout[-700:]) if not passed else ""
        ok = check(label, passed, detail) and ok

    def query(manual, argv):
        return run([sys.executable, "-I", "-B", str(isa)] + argv,
                   env=dict(os.environ, AMDGPU_ISA_MANUAL=str(manual)))

    def pages(manual, archs, indexed=True):
        manual.mkdir()
        for arch in archs:
            (manual / arch).mkdir()
            (manual / arch / "p0001.md").write_text("Fixture manual text\n", encoding="utf-8")
        (manual / "toc.tsv").write_text(
            "arch\tlevel\tpath\ttitle\n" + "".join(
                "%s\t1\t%s/p0001.md\tFixture manual\n" % (arch, arch)
                for arch in archs), encoding="utf-8")
        if indexed:
            (manual / "instr-index.tsv").write_text(
                "instruction\tarch\tkind\tpath\tpage_printed\n" + "".join(
                    "V_FMA_F32\t%s\tdefinition\t%s/p0001.md\t1\n" % (arch, arch)
                    for arch in archs), encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="isa-manual-query-") as scratch:
        root = Path(scratch)
        missing = root / "missing-arch-folder"
        pages(missing, ["rdna4"])
        with (missing / "toc.tsv").open("a", encoding="utf-8") as stream:
            stream.write("missing_manual_arch\t1\tmissing_manual_arch/p0001.md\tMissing pages\n")
        result = query(missing, ["manual", "--grep", "Fixture", "--json"])
        passed, detail = validate_json(result, lambda data:
                                      len(data.get("rows", [])) == 1
                                      and data["rows"][0].get("arch") == "rdna4"
                                      and data["rows"][0].get("path") == str(missing / "rdna4/p0001.md"))
        record("manual: grep skips a missing TOC architecture folder", passed, result)
        result = query(missing, ["manual", "--grep", "Fixture", "--arch", "missing_manual_arch"])
        record("manual: grep of a missing indexed folder fails without traceback",
               result.returncode == 1 and not result.stdout
               and "Traceback" not in result.stderr, result)
        result = query(missing, ["manual", "--grep", "NEVER_MATCHES_THIS_MANUAL", "--arch", "rdna4"])
        record("manual: unmatched grep suggests the architecture TOC",
               result.returncode == 1 and not result.stdout
               and "try: isa.py manual --toc --arch rdna4" in result.stderr, result)
        for level in ("0", "-1"):
            for output in ([], ["--json"]):
                result = query(missing, ["manual", "--toc", "--level", level] + output)
                record("manual: rejects level %s%s" % (level, " (JSON)" if output else ""),
                       result.returncode == 2 and "level" in result.stderr
                       and not result.stdout and "Traceback" not in result.stderr, result)

        runner = """import builtins, os, runpy, sys
script, manual = sys.argv[1:3]
original = builtins.open
def disappear(path, *args, **kwargs):
    if str(path) == os.path.join(manual, 'toc.tsv'):
        raise FileNotFoundError('fixture TOC disappeared before open')
    return original(path, *args, **kwargs)
builtins.open = disappear
sys.argv = [script, 'manual', '--toc']
runpy.run_path(script, run_name='__main__')
"""
        result = run([sys.executable, "-I", "-B", "-c", runner, str(isa), str(missing)],
                     env=dict(os.environ, AMDGPU_ISA_MANUAL=str(missing)))
        record("manual: disappearing TOC has a clean missing-pages result",
               result.returncode == 1 and "no ISA manual pages" in result.stderr
               and "try:" in result.stderr and "Traceback" not in result.stderr, result)

        preferred = root / "preferred-arch"
        pages(preferred, ["cdna1", "rdna4"])
        result = query(preferred, ["selftest"])
        record("selftest: manual examples prefer installed rdna4 pages",
               result.returncode == 0
               and "manual --inst V_FMA_F32 --arch rdna4" in result.stdout
               and "manual --toc --arch rdna4" in result.stdout, result)

        partial = root / "partial-archs"
        pages(partial, ["cdna1"])
        result = query(partial, ["selftest"])
        record("selftest: manual examples use a partial build without rdna4",
               result.returncode == 0
               and "manual --inst V_FMA_F32 --arch cdna1" in result.stdout
               and "manual --toc --arch cdna1" in result.stdout, result)

        manual_only = root / "manual-only"
        pages(manual_only, ["manual_only_regression"], indexed=False)
        result = query(manual_only, ["selftest"])
        record("selftest: manual-only arch examples work without an instruction index",
               result.returncode == 0
               and "manual --grep . --arch manual_only_regression" in result.stdout
               and "manual --toc --arch manual_only_regression" in result.stdout, result)

        empty = root / "empty-marker"
        pages(empty, [])
        result = query(empty, ["selftest"])
        record("selftest: an empty TOC marker skips both manual examples",
               result.returncode == 0 and "skip manual definition page" in result.stdout
               and "skip manual toc" in result.stdout and ", 2 skipped" in result.stdout, result)
    return ok


def run_checks(check):
    """Record manual lifecycle results without interrupting other verification."""
    try:
        return run_build_checks(check)
    except Exception as error:
        return check("manual lifecycle: PDF rebuilds", False,
                     "%s: %s" % (type(error).__name__, error))
