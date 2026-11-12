# Query semantics

Read this when interpreting JSON, counting instruction presence or variants,
or writing SQL beyond the canned subcommands.

## Results and instruction identity

Every subcommand takes `--json` and `-n N` (`-n 0` = unlimited). Listings report
their full row count; truncated SQL queries report a lower bound instead of
running an unbounded count. `which` shows all rows by default; other commands default to 50.
An explicit `-n N` limits either. `diff` limits each side independently and
reports full totals in JSON. Negative limits are rejected. Empty SQL results
report zero rows; no-match lookups exit 1 with a suggested next command.

Aliases resolve per architecture: `S_LOAD_DWORD` is canonical on CDNA1-4 and
RDNA1-2, and aliases `S_LOAD_B32` on CDNA5 and RDNA3-4. JSON from `show` and
`which` reports `query_name`, `canonical_name` and `is_alias` for every row.
`which` and `show` count the archs that accept that exact spelling, as an
assembler would; archs that have the same instruction only under another name
are listed as "also present as" (JSON `also_present_as`), so `which
V_XOR_ADD_U32` shows 3/10 plus V_XAD_U32 in the other 7. Alias lookup follows
renamed and sibling spellings across architectures. Each `also_present_as`
entry represents one additional architecture; `names` lists its canonical
alternatives and `name` is the first. When asked whether the instruction exists,
count the union of architectures in the exact-spelling rows and
`also_present_as`, once per architecture.
Without `--arch`, `show` prefers a canonical spelling's architecture for
operands and manual links, and labels that detail source. `which` lists each
encoding/opcode pair with `n_variants`, the number of distinct conditions using
that pair (including one default/NULL condition). Duplicate source rows do not
increase it. An instruction can have different opcodes in different encodings.

## Writing SQL

When the subcommands do not fit, query directly. `isa.py schema` prints the full
schema; these flattened views answer most questions with no join:

```
v_inst      arch, name, "group", encoding, opcode, condition, description
v_presence  arch, name, "group", n_encodings, opcode      -- one row per (name, arch)
v_name      arch, query_name, canonical_name, is_alias    -- alias resolution
v_operand   arch, name, encoding, condition, ord, field_name, operand_type,
            data_format, size, is_input, is_output, is_implicit
v_gfx       gfx, arch, architecture_name, llvm_generation, kind, products, basis
```

`v_presence` is a legacy summary: `n_encodings` counts source rows, including duplicates,
and `opcode` is the minimum across encodings. Use `v_inst` or `which` for opcodes
of specific encodings. SQL preserves values by column position, including
case-distinct labels such as `n` and `N`. Repeated labels receive suffixes such
as `opcode_2`; existing explicit labels are preserved. Use `AS` for clearer names.

```bash
isa.py sql "SELECT arch, COUNT(*) FROM v_presence WHERE \"group\"='VALU' GROUP BY arch"
isa.py sql "SELECT name FROM v_presence WHERE arch='cdna5'
            EXCEPT SELECT name FROM v_presence WHERE arch='cdna4'"
```

Only `SELECT`/`WITH` are accepted and the connection is read-only, so a query
cannot damage the corpus. The CLI applies `-n` outside your query, so its own
`LIMIT` and trailing comments remain valid. For truncated SQL output, JSON has
`total: null`, `has_more: true` and `total_at_least`; `-n 0` reports an exact
total when the query finishes. SQL execution has a 10-second deadline, so recursive
queries need a terminating condition and expensive queries may need narrower filters.
Base tables — `instruction`, `inst_encoding`,
`operand`, `encoding`, `encoding_field`, `data_format`, `operand_type`,
`functional_group`, `arch` — are there when a view is not enough.
