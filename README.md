# jinpei-skills

Personal agent skills, in one place, published through each agent's own
marketplace/discovery mechanism. Edit here, reinstall there.

| skill | what it is |
|---|---|
| [`working-on-llvm`](plugins/working-on-llvm) | upstream llvm-project workflow: build/test, reduce, debug passes, PR hygiene |
| [`amdgpu-isa-builder`](plugins/amdgpu-isa-builder) | builds the `amd-gpu-isa` reference skill locally from public AMD sources |

## Install

Each skill ships as its own plugin, so you install only what you want.

**Claude Code**

```
/plugin marketplace add ~/development/skills
/plugin install working-on-llvm@jinpei-skills
/plugin install amdgpu-isa-builder@jinpei-skills
/plugin disable amdgpu-isa-builder          # see below
```

**Codex**

```bash
codex plugin marketplace add ~/development/skills
codex plugin add working-on-llvm@jinpei-skills
codex plugin add amdgpu-isa-builder@jinpei-skills
```

**pi**

pi discovers `SKILL.md` recursively, so one settings entry covers every skill in
the repo — present and future:

```json
{ "skills": ["~/development/skills/plugins"] }
```

### From the git remote

On another machine, add the marketplace by repo instead of by path:

```
/plugin marketplace add lijinpei-amd/skills                    # Claude Code
codex plugin marketplace add lijinpei-amd/skills --ref main    # Codex
```

For pi, clone it anywhere and point `skills` at the clone's `plugins/`.

Claude Code reads `.claude-plugin/marketplace.json`, Codex reads
`.agents/plugins/marketplace.json`; both point at the same `plugins/` tree.

## `amdgpu-isa-builder` is meant to stay disabled

It is a build tool, needed once per machine plus the odd refresh, so leave it
disabled and pay nothing in ordinary sessions:

```
claude plugin enable amdgpu-isa-builder     # rebuild, then disable again
codex  -c 'plugins."amdgpu-isa-builder@jinpei-skills".enabled=true'
```

It ships the builder, **not** the corpus. AMD's machine-readable ISA XML is MIT
and redistributable, but the ISA reference manuals grant review rights only, so
the reference data is generated on your machine rather than committed here.
`plugins/amdgpu-isa-builder/{sources,build,dist}` are gitignored for that reason,
and its `build.py verify` fails if git ever starts tracking them.

## Updating a skill

Installs are **copies**, not symlinks — editing a file here does not change what
an installed agent sees. After editing: bump `version` in both `plugin.json`
files, then re-run the install command (Claude Code: `/plugin`; Codex:
`codex plugin marketplace upgrade` then `codex plugin add ...`). pi reads the
repo directly and needs no step.

## Layout

```
.claude-plugin/marketplace.json     Claude Code marketplace
.agents/plugins/marketplace.json    Codex marketplace
plugins/<name>/
  .claude-plugin/plugin.json        Claude Code plugin manifest
  .codex-plugin/plugin.json         Codex plugin manifest ("skills": "./skills/")
  skills/<name>/SKILL.md            the skill itself — single source of truth
scripts/validate.py                 checks the manifests stay in sync
```

See [AGENTS.md](AGENTS.md) for how to add or change a skill.
