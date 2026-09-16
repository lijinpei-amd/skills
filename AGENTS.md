# Working in this repo

This is a skills marketplace consumed by three agents at once. The skill content
exists exactly once; only the manifests are duplicated, because Claude Code and
Codex look in different places for them.

## Adding a skill

Five files, no more:

1. `plugins/<name>/skills/<name>/SKILL.md` — the skill.
2. `plugins/<name>/.claude-plugin/plugin.json`
3. `plugins/<name>/.codex-plugin/plugin.json` — same fields, plus
   `"skills": "./skills/"` and an `interface` block (`displayName`,
   `shortDescription`, `defaultPrompt`) that Codex renders in its UI.
4. An entry in `.claude-plugin/marketplace.json` (`"source": "./plugins/<name>"`).
5. An entry in `.agents/plugins/marketplace.json`
   (`"source": {"source": "local", "path": "./plugins/<name>"}`).

Then `python3 scripts/validate.py`. It fails the build if the two marketplaces
disagree, if the shared `plugin.json` fields have drifted apart, or if a
`SKILL.md` is missing `name`/`description`.

One skill per plugin — that keeps install granularity equal to skill
granularity.

## SKILL.md conventions

- Frontmatter needs `name` and `description`. **The description is the trigger**:
  it is the only thing the agent sees before deciding to load the skill, so spell
  out the situations and the vocabulary that should pull it in, not just what the
  skill is about. pi refuses to load a skill with no description.
- `name` should match the directory. pi does not require this; Claude Code and
  Codex effectively do.
- Keep `SKILL.md` short and push detail into `references/*.md` that the agent
  reads on demand. Bulk data belongs behind a script, not in a file an agent
  might `Read` whole.
- **Never hardcode an install path.** `~/.claude/skills/<name>` is wrong under
  Codex and wrong under a plugin install even in Claude Code. Write "`$S` = the
  directory containing this SKILL.md" and use `$S/...`; have scripts resolve
  their own data relative to `__file__`.
- Build tooling goes in `plugins/<name>/build/`, outside `skills/`, so agents
  never load it as skill content.

## Changing an existing skill

Bump `version` in **both** `plugin.json` files. Installs are copies, so without a
version bump the agents keep serving the old content.
