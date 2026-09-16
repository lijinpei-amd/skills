#!/usr/bin/env python3
"""Build isa.db from AMD's machine-readable ISA XML.

Everything this produces derives solely from the MIT-licensed, "AMD Public Use"
XML -- no PDF content reaches this database. That boundary is structural: PDF
material lives in a separate enrich.db that the query CLI attaches when present,
so shipping decisions are per-file rather than per-column.

Stdlib only (xml.etree + sqlite3), parsed with iterparse so a 17 MB spec does not
have to be held in memory as a tree.

Usage:  python3 builder/xml_to_db.py [--sources DIR] [--out FILE]
"""

import argparse
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))

SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous  = OFF;

CREATE TABLE arch (
    arch              TEXT PRIMARY KEY,   -- cdna4, rdna3_5, ...
    architecture_name TEXT,               -- "AMD RDNA 4"
    architecture_id   INTEGER,
    xml_file          TEXT,
    release_date      TEXT,
    schema_version    TEXT,
    copyright         TEXT,
    license           TEXT,
    sensitivity       TEXT
);

CREATE TABLE instruction (
    inst_id     INTEGER PRIMARY KEY,
    arch        TEXT NOT NULL REFERENCES arch(arch),
    name        TEXT NOT NULL,
    description TEXT,
    group_name  TEXT,
    is_branch              INTEGER,
    is_conditional_branch  INTEGER,
    is_indirect_branch     INTEGER,
    is_program_terminator  INTEGER,
    is_immediately_executed INTEGER
);

CREATE TABLE instruction_alias (
    inst_id INTEGER NOT NULL REFERENCES instruction(inst_id),
    alias   TEXT NOT NULL
);

CREATE TABLE instruction_subgroup (
    inst_id  INTEGER NOT NULL REFERENCES instruction(inst_id),
    subgroup TEXT NOT NULL
);

CREATE TABLE inst_encoding (
    ie_id         INTEGER PRIMARY KEY,
    inst_id       INTEGER NOT NULL REFERENCES instruction(inst_id),
    encoding_name TEXT NOT NULL,
    condition     TEXT,
    opcode        INTEGER
);

CREATE TABLE operand (
    ie_id        INTEGER NOT NULL REFERENCES inst_encoding(ie_id),
    ord          INTEGER,
    field_name   TEXT,
    data_format  TEXT,
    operand_type TEXT,
    size         INTEGER,
    is_input     INTEGER,
    is_output    INTEGER,
    is_implicit  INTEGER
);

CREATE TABLE encoding (
    arch            TEXT NOT NULL REFERENCES arch(arch),
    name            TEXT NOT NULL,
    bit_count       INTEGER,
    identifier_mask TEXT,
    description     TEXT,
    PRIMARY KEY (arch, name)
);

CREATE TABLE encoding_field (
    arch        TEXT NOT NULL,
    encoding    TEXT NOT NULL,
    ord         INTEGER,
    field_name  TEXT,
    bit_offset  INTEGER,
    bit_count   INTEGER,
    description TEXT
);

CREATE TABLE data_format (
    arch            TEXT NOT NULL REFERENCES arch(arch),
    name            TEXT NOT NULL,
    bit_count       INTEGER,
    component_count INTEGER,
    data_type       TEXT,
    description     TEXT,
    PRIMARY KEY (arch, name)
);

CREATE TABLE operand_type (
    arch        TEXT NOT NULL REFERENCES arch(arch),
    name        TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (arch, name)
);

CREATE TABLE functional_group (
    arch        TEXT NOT NULL REFERENCES arch(arch),
    name        TEXT NOT NULL,
    description TEXT,
    PRIMARY KEY (arch, name)
);

CREATE TABLE build_info (key TEXT PRIMARY KEY, value TEXT);
"""

INDEXES = """
CREATE INDEX idx_inst_name        ON instruction(name);
CREATE INDEX idx_inst_arch        ON instruction(arch, name);
CREATE INDEX idx_inst_group       ON instruction(arch, group_name);
CREATE INDEX idx_alias_name       ON instruction_alias(alias);
CREATE INDEX idx_alias_inst       ON instruction_alias(inst_id);
CREATE INDEX idx_subgroup_inst    ON instruction_subgroup(inst_id);
CREATE INDEX idx_ie_inst          ON inst_encoding(inst_id);
CREATE INDEX idx_ie_opcode        ON inst_encoding(encoding_name, opcode);
CREATE INDEX idx_operand_ie       ON operand(ie_id);
CREATE INDEX idx_encfield         ON encoding_field(arch, encoding);
"""

# Flattened views. Most real questions are answerable from these without a join,
# which is the main defence against an agent writing a subtly wrong one.
VIEWS = """
CREATE VIEW v_inst AS
SELECT i.arch          AS arch,
       i.name          AS name,
       i.group_name    AS "group",
       e.encoding_name AS encoding,
       e.opcode        AS opcode,
       e.condition     AS condition,
       i.description   AS description,
       i.inst_id       AS inst_id,
       e.ie_id         AS ie_id
FROM instruction i
JOIN inst_encoding e USING (inst_id);

-- One row per (instruction name, arch): the cross-architecture question.
CREATE VIEW v_presence AS
SELECT name,
       arch,
       "group",
       COUNT(*)     AS n_encodings,
       MIN(opcode)  AS opcode,
       MIN(inst_id) AS inst_id
FROM v_inst
GROUP BY name, arch;

-- Names including aliases, so a lookup of S_LOAD_DWORD finds S_LOAD_B32.
CREATE VIEW v_name AS
SELECT arch, name AS query_name, name AS canonical_name, 0 AS is_alias
FROM instruction
UNION ALL
SELECT i.arch, a.alias, i.name, 1
FROM instruction_alias a JOIN instruction i USING (inst_id);

CREATE VIEW v_operand AS
SELECT i.arch, i.name, e.encoding_name AS encoding, o.ord,
       o.field_name, o.operand_type, o.data_format, o.size,
       o.is_input, o.is_output, o.is_implicit
FROM operand o
JOIN inst_encoding e USING (ie_id)
JOIN instruction  i USING (inst_id);
"""

ARCH_FROM_FILE = re.compile(r"amdgpu_isa_(.+)\.xml$")


def text(el, tag, default=None):
    child = el.find(tag)
    return child.text if child is not None and child.text is not None else default


def flag(el, tag):
    v = text(el, tag)
    if v is None:
        return None
    return 1 if v.strip().upper() == "TRUE" else 0


def attr_bool(el, key):
    v = el.get(key)
    if v is None:
        return None
    return 1 if v.strip().lower() == "true" else 0


def int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def load_arch(conn, path):
    """Stream one architecture's XML into the database."""
    arch = ARCH_FROM_FILE.search(os.path.basename(path)).group(1)
    cur = conn.cursor()
    counts = {"instruction": 0, "inst_encoding": 0, "operand": 0,
              "encoding": 0, "encoding_field": 0}

    doc = {}
    arch_name = arch_id = None

    # iterparse and clear each top-level child as we finish with it, so peak
    # memory stays near one <Instruction> rather than the whole 17 MB tree.
    for event, el in ET.iterparse(path, events=("end",)):
        tag = el.tag

        if tag == "Document":
            for k in ("Copyright", "Sensitivity", "License", "ReleaseDate",
                      "SchemaVersion"):
                doc[k] = text(el, k)
            el.clear()

        elif tag == "Architecture":
            arch_name = text(el, "ArchitectureName")
            arch_id = int_or_none(text(el, "ArchitectureId"))
            cur.execute(
                "INSERT OR REPLACE INTO arch VALUES (?,?,?,?,?,?,?,?,?)",
                (arch, arch_name, arch_id, os.path.basename(path),
                 doc.get("ReleaseDate"), doc.get("SchemaVersion"),
                 doc.get("Copyright"), doc.get("License"),
                 doc.get("Sensitivity")))
            el.clear()

        elif tag == "FunctionalGroup" and el.find("Description") is not None:
            # The per-ISA catalogue entry (instructions carry their own
            # <FunctionalGroup> with a Name but no Description).
            cur.execute("INSERT OR REPLACE INTO functional_group VALUES (?,?,?)",
                        (arch, text(el, "Name"), text(el, "Description")))
            el.clear()

        elif tag == "DataFormat":
            cur.execute("INSERT OR REPLACE INTO data_format VALUES (?,?,?,?,?,?)",
                        (arch, text(el, "DataFormatName"),
                         int_or_none(text(el, "BitCount")),
                         int_or_none(text(el, "ComponentCount")),
                         text(el, "DataType"), text(el, "Description")))
            el.clear()

        elif tag == "OperandType":
            # Only the ISA-level catalogue has a name; operand references reuse
            # the tag as a plain value, which find() will not match.
            name = text(el, "OperandTypeName")
            if name:
                cur.execute("INSERT OR REPLACE INTO operand_type VALUES (?,?,?)",
                            (arch, name, text(el, "Description")))
                el.clear()

        elif tag == "Encoding":
            name = text(el, "EncodingName")
            if not name:
                continue
            cur.execute("INSERT OR REPLACE INTO encoding VALUES (?,?,?,?,?)",
                        (arch, name, int_or_none(text(el, "BitCount")),
                         text(el, "EncodingIdentifierMask"),
                         text(el, "Description")))
            counts["encoding"] += 1
            bitmap = el.find("./MicrocodeFormat/BitMap")
            if bitmap is not None:
                for ord_, field in enumerate(bitmap.findall("Field")):
                    rng = field.find("./BitLayout/Range")
                    cur.execute(
                        "INSERT INTO encoding_field VALUES (?,?,?,?,?,?,?)",
                        (arch, name, ord_, text(field, "FieldName"),
                         int_or_none(text(rng, "BitOffset")) if rng is not None else None,
                         int_or_none(text(rng, "BitCount")) if rng is not None else None,
                         text(field, "Description")))
                    counts["encoding_field"] += 1
            el.clear()

        elif tag == "Instruction" and el.find("InstructionName") is not None:
            name = text(el, "InstructionName")
            flags = el.find("InstructionFlags")
            group = el.find("FunctionalGroup")
            cur.execute(
                "INSERT INTO instruction (arch,name,description,group_name,"
                "is_branch,is_conditional_branch,is_indirect_branch,"
                "is_program_terminator,is_immediately_executed)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (arch, name, text(el, "Description"),
                 text(group, "Name") if group is not None else None,
                 flag(flags, "IsBranch") if flags is not None else None,
                 flag(flags, "IsConditionalBranch") if flags is not None else None,
                 flag(flags, "IsIndirectBranch") if flags is not None else None,
                 flag(flags, "IsProgramTerminator") if flags is not None else None,
                 flag(flags, "IsImmediatelyExecuted") if flags is not None else None))
            inst_id = cur.lastrowid
            counts["instruction"] += 1

            aliases = el.find("AliasedInstructionNames")
            if aliases is not None:
                for a in aliases.findall("InstructionName"):
                    if a.text:
                        cur.execute("INSERT INTO instruction_alias VALUES (?,?)",
                                    (inst_id, a.text))

            if group is not None:
                subs = group.find("FunctionalSubgroups")
                if subs is not None:
                    for s in subs.findall("Subgroup"):
                        if s.text:
                            cur.execute(
                                "INSERT INTO instruction_subgroup VALUES (?,?)",
                                (inst_id, s.text))

            encs = el.find("InstructionEncodings")
            for ie in (encs.findall("InstructionEncoding") if encs is not None else []):
                cur.execute(
                    "INSERT INTO inst_encoding (inst_id,encoding_name,condition,opcode)"
                    " VALUES (?,?,?,?)",
                    (inst_id, text(ie, "EncodingName"),
                     text(ie, "EncodingCondition"),
                     int_or_none(text(ie, "Opcode"))))
                ie_id = cur.lastrowid
                counts["inst_encoding"] += 1
                ops = ie.find("Operands")
                for op in (ops.findall("Operand") if ops is not None else []):
                    cur.execute(
                        "INSERT INTO operand VALUES (?,?,?,?,?,?,?,?,?)",
                        (ie_id, int_or_none(op.get("Order")),
                         text(op, "FieldName"), text(op, "DataFormatName"),
                         text(op, "OperandType"),
                         int_or_none(text(op, "OperandSize")),
                         attr_bool(op, "Input"), attr_bool(op, "Output"),
                         attr_bool(op, "IsImplicit")))
                    counts["operand"] += 1
            el.clear()

    conn.commit()
    return arch, arch_name, counts


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default=os.path.join(ROOT, "sources", "xml", "extracted"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "isa.db"))
    args = ap.parse_args()

    files = sorted(f for f in os.listdir(args.sources) if f.endswith(".xml"))
    if not files:
        sys.exit("no XML in %s -- run 'build.py fetch' first" % args.sources)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    if os.path.exists(args.out):
        os.remove(args.out)
    conn = sqlite3.connect(args.out)
    conn.executescript(SCHEMA)

    t0 = time.time()
    totals = {}
    for f in files:
        arch, arch_name, counts = load_arch(conn, os.path.join(args.sources, f))
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v
        print("  %-10s %-16s %5d instructions  %5d encodings  %6d operands"
              % (arch, arch_name or "?", counts["instruction"],
                 counts["inst_encoding"], counts["operand"]))

    conn.executescript(INDEXES)
    conn.executescript(VIEWS)

    # Full-text search over names and descriptions. Populated from the XML only.
    conn.executescript("""
        CREATE VIRTUAL TABLE fts_inst USING fts5(
            name, description, arch UNINDEXED, inst_id UNINDEXED);
    """)
    conn.execute("INSERT INTO fts_inst(name, description, arch, inst_id)"
                 " SELECT name, description, arch, inst_id FROM instruction")

    for k, v in sorted(totals.items()):
        conn.execute("INSERT OR REPLACE INTO build_info VALUES (?,?)",
                     ("count_" + k, str(v)))
    conn.execute("INSERT OR REPLACE INTO build_info VALUES ('built', ?)",
                 (time.strftime("%Y-%m-%d %H:%M:%S"),))
    conn.execute("INSERT OR REPLACE INTO build_info VALUES ('source', "
                 "'AMD machine-readable ISA XML (MIT, AMD Public Use)')")
    conn.commit()
    conn.executescript("PRAGMA journal_mode = DELETE;")
    conn.execute("VACUUM")
    conn.commit()
    conn.close()

    size = os.path.getsize(args.out)
    print("\n%d archs, %d instructions, %d encodings, %d operands"
          % (len(files), totals["instruction"], totals["inst_encoding"],
             totals["operand"]))
    print("%s  %.1f MB  in %.1fs"
          % (os.path.relpath(args.out, ROOT), size / 1e6, time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
