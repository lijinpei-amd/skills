"""Publish analyzed SQLite corpora and compare their logical contents."""

from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile


def content_digest(path):
    """Hash schema, rows and planner statistics, excluding FTS shadow storage."""
    digest = hashlib.sha256()

    def add(value):
        def encode(item):
            if isinstance(item, bytes):
                return {"blob": item.hex()}
            raise TypeError("unsupported SQLite value: %r" % (item,))

        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                             default=encode).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    def quote(name):
        return '"' + name.replace('"', '""') + '"'

    uri = Path(path).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        objects = [row for row in conn.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")
                   if (not row[1].startswith("sqlite_") or row[1] == "sqlite_stat1")
                   and row[3] is not None]
        virtual = {name for kind, name, _, sql in objects if kind == "table"
                   and re.search(r"\bUSING\s+fts5\b", sql, re.I)}
        shadow = {name + suffix for name in virtual
                  for suffix in ("_data", "_idx", "_content", "_docsize", "_config")}
        objects = [row for row in objects if row[1] not in shadow and row[2] not in shadow]
        add(("schema", objects))
        for kind, name, _, _ in objects:
            if kind != "table":
                continue
            columns = [row[1] for row in conn.execute("PRAGMA table_info(%s)" % quote(name))]
            if name in virtual:
                columns.insert(0, "rowid")
            add(("table", name, columns))
            selected = ",".join(quote(column) for column in columns)
            ordered = ",".join(quote(column) + " COLLATE BINARY" for column in columns)
            excluded = shadow | virtual if name == "sqlite_stat1" else set()
            where = " WHERE tbl NOT IN (%s)" % ",".join("?" for _ in excluded) if excluded else ""
            for row in conn.execute("SELECT %s FROM %s%s ORDER BY %s" %
                                    (selected, quote(name), where, ordered), sorted(excluded)):
                add(row)
    return digest.hexdigest()


@contextmanager
def build_stage(path, prefix, copy_source=False):
    """Yield a sibling database stage under the parent directory's build lock."""
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    with open(os.path.join(parent, ".amdgpu-isa-db.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for name in os.listdir(parent):
            stale = os.path.join(parent, name)
            if (re.fullmatch(r"\.isa-(?:xml|gfx|db)-[a-z0-9_]{8}\.db", name)
                    and stale != path and not os.path.islink(stale) and owned_stage(stale)):
                for suffix in ("", "-journal", "-wal", "-shm", ".owner"):
                    sidecar = stale + suffix
                    if os.path.isfile(sidecar) and not os.path.islink(sidecar):
                        os.unlink(sidecar)
        fd, staging = tempfile.mkstemp(prefix=prefix, suffix=".db", dir=parent)
        os.close(fd)
        try:
            Path(staging + ".owner").write_text(os.path.realpath(staging) + "\n", encoding="utf-8")
            if copy_source:
                with closing(sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True)) as source, \
                        closing(sqlite3.connect(staging)) as target:
                    source.backup(target)
            yield staging
        finally:
            for suffix in ("", "-journal", "-wal", "-shm", ".owner"):
                if os.path.exists(staging + suffix):
                    os.unlink(staging + suffix)


def owned_stage(path):
    """Return whether a stage's ownership record matches its current path."""
    owner = Path(path + ".owner")
    if owner.is_symlink():
        return False
    try:
        return owner.read_text(encoding="utf-8") == os.path.realpath(path) + "\n"
    except (OSError, UnicodeError):
        return False


def finalize(conn, path):
    """Analyze and close conn, then atomically publish its compact database.

    The file backing conn's main database may differ from the destination path.
    The caller owns that source file; conn is closed even if publication fails.
    """
    path = os.path.abspath(path)
    scratch = None
    try:
        conn.commit()
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("ANALYZE")
        conn.commit()
        fd, scratch = tempfile.mkstemp(prefix=".isa-db-", suffix=".db", dir=os.path.dirname(path))
        os.close(fd)
        Path(scratch + ".owner").write_text(os.path.realpath(scratch) + "\n", encoding="utf-8")
        legacy = sqlite3.sqlite_version_info < (3, 27, 0)
        if legacy:
            source = next(row[2] for row in conn.execute("PRAGMA database_list")
                          if row[1] == "main")
            conn.close()
            shutil.copyfile(source, scratch)
        else:
            conn.execute("VACUUM INTO ?", (scratch,))
            conn.close()
        output = sqlite3.connect(scratch)
        try:
            if legacy:
                output.execute("VACUUM")
            # This fresh, unpublished file has no readers, so its inherited
            # schema cookie can be normalized without invalidating caches.
            output.execute("PRAGMA schema_version=1")
            output.commit()
        finally:
            output.close()
        os.chmod(scratch, 0o644)
        os.replace(scratch, path)
    finally:
        conn.close()
        if scratch is not None and os.path.exists(scratch):
            os.unlink(scratch)
        if scratch is not None and os.path.exists(scratch + ".owner"):
            os.unlink(scratch + ".owner")
