# jinpei-skills

Personal agent skills, in one place, published through each agent's own
marketplace/discovery mechanism. Edit here, reinstall there.

| skill | what it is |
|---|---|
| [`working-on-llvm`](plugins/working-on-llvm) | upstream llvm-project workflow: build/test, reduce, debug passes, PR hygiene |
| [`amdgpu-isa`](plugins/amdgpu-isa) | AMD GPU instruction encodings, operands, aliases, gfx mappings and architecture differences; includes the XML- and LLVM-derived corpus |
| [`amdgpu-isa-builder`](plugins/amdgpu-isa-builder) | builds the ISA manual pages locally for `amdgpu-isa`, and regenerates its corpus |
| [`writing-python-docstrings-and-comments`](plugins/writing-python-docstrings-and-comments) | PEP 257 + Google-style Python docstrings, comments, TODOs, error messages |
| [`locking-gpus`](plugins/locking-gpus) | per-GPU `flock` so concurrent jobs and benchmarks never share a GPU |
| [`capture-att-trace`](plugins/capture-att-trace) | `rocprofv3 --att` thread traces of a GPU kernel, in versioned, indexed capture directories |

## Install

Each skill ships as its own plugin, so you install only what you want.

**Claude Code**

```
/plugin marketplace add ~/development/skills
/plugin install working-on-llvm@jinpei-skills
/plugin install amdgpu-isa@jinpei-skills
/plugin install amdgpu-isa-builder@jinpei-skills
/plugin install writing-python-docstrings-and-comments@jinpei-skills
/plugin install locking-gpus@jinpei-skills
/plugin install capture-att-trace@jinpei-skills
/plugin disable amdgpu-isa-builder          # see below
```

**Codex**

```bash
codex plugin marketplace add ~/development/skills
codex plugin add working-on-llvm@jinpei-skills
codex plugin add amdgpu-isa@jinpei-skills
codex plugin add amdgpu-isa-builder@jinpei-skills
codex plugin add writing-python-docstrings-and-comments@jinpei-skills
codex plugin add locking-gpus@jinpei-skills
codex plugin add capture-att-trace@jinpei-skills
```

**pi**

`package.json` makes the repo a pi package that exposes every skill except
`amdgpu-isa-builder`:

```bash
pi install ~/development/skills
```

### From the git remote

On another machine, add the marketplace by repo instead of by path:

```
/plugin marketplace add lijinpei-amd/skills                    # Claude Code
codex plugin marketplace add lijinpei-amd/skills --ref main    # Codex
pi install git:github.com/lijinpei-amd/skills                  # pi
```

Claude Code reads `.claude-plugin/marketplace.json`, Codex reads
`.agents/plugins/marketplace.json`; both point at the same `plugins/` tree.

## `amdgpu-isa-builder` is meant to stay disabled

It is a build tool, needed once per machine for the manual pages plus the odd
corpus refresh, so leave it disabled and pay nothing in ordinary sessions:

```
claude plugin enable amdgpu-isa-builder     # build, then disable again
codex  -c 'plugins."amdgpu-isa-builder@jinpei-skills".enabled=true'
```

It ships the builder, **not** the corpus. The `amdgpu-isa` plugin ships the
database (from AMD's MIT machine-readable ISA XML and LLVM's Apache-2.0 gfx
table), the query script and their licences. AMD's ISA reference manuals grant
review rights only, so their pages are never shipped: the builder converts them
on your machine and writes them into persistent storage at
`~/.local/share/amdgpu-isa/manual` (respects `$XDG_DATA_HOME`; override:
`$AMDGPU_ISA_MANUAL`), where the plugin's `manual` command finds them.
See the [builder README](plugins/amdgpu-isa-builder/README.md) for storage
requirements and rebuild behavior.

End-user `build.py all` builds local data and manuals without regenerating the
tracked plugin, then runs corpus-independent `verify --quick` (roughly 1–2
minutes with cached downloads). Maintainers regenerate explicitly with
`build.py plugin` and run `build.py verify --plugin` before releasing.

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
