#!/usr/bin/env python3
"""Check that the two marketplace manifests and the per-plugin manifests agree.

Keeping a Claude Code manifest and a Codex manifest hand-synced is the obvious
failure mode of this repo, so check it mechanically. Stdlib only.

Exit 0 = consistent, 1 = problems found (printed to stderr).
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
CLAUDE_MARKET = os.path.join(ROOT, ".claude-plugin", "marketplace.json")
CODEX_MARKET = os.path.join(ROOT, ".agents", "plugins", "marketplace.json")
PLUGINS_DIR = os.path.join(ROOT, "plugins")

# plugin.json fields that must be byte-identical between the two flavors.
SHARED_FIELDS = ("name", "version", "description", "author", "license", "keywords")

problems = []


def fail(msg):
    problems.append(msg)


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        fail("missing file: %s" % os.path.relpath(path, ROOT))
    except json.JSONDecodeError as exc:
        fail("invalid JSON in %s: %s" % (os.path.relpath(path, ROOT), exc))
    return None


def frontmatter(path):
    """Return the raw YAML frontmatter block of a SKILL.md, or None."""
    with open(path, encoding="utf-8") as fh:
        if fh.readline().rstrip("\n") != "---":
            return None
        lines = []
        for line in fh:
            if line.rstrip("\n") == "---":
                return "".join(lines)
            lines.append(line)
    return None


def check_frontmatter(skill_md):
    rel = os.path.relpath(skill_md, ROOT)
    block = frontmatter(skill_md)
    if block is None:
        fail("%s: missing or unterminated YAML frontmatter" % rel)
        return
    # Top-level keys only: a continuation line of a folded scalar is indented.
    keys = {
        line.split(":", 1)[0]
        for line in block.splitlines()
        if line[:1] not in (" ", "\t", "#", "") and ":" in line
    }
    for required in ("name", "description"):
        if required not in keys:
            # pi refuses to load a skill with no description; Claude and Codex
            # use it as the trigger text.
            fail("%s: frontmatter is missing required key %r" % (rel, required))


def main():
    claude = load(CLAUDE_MARKET)
    codex = load(CODEX_MARKET)
    if claude is None or codex is None:
        return report()

    on_disk = sorted(
        d for d in os.listdir(PLUGINS_DIR)
        if os.path.isdir(os.path.join(PLUGINS_DIR, d)) and not d.startswith(".")
    )
    claude_names = sorted(p["name"] for p in claude.get("plugins", []))
    codex_names = sorted(p["name"] for p in codex.get("plugins", []))

    if claude_names != on_disk:
        fail(".claude-plugin/marketplace.json lists %s but plugins/ holds %s"
             % (claude_names, on_disk))
    if codex_names != on_disk:
        fail(".agents/plugins/marketplace.json lists %s but plugins/ holds %s"
             % (codex_names, on_disk))
    if claude.get("name") != codex.get("name"):
        fail("marketplace name differs: %r (claude) vs %r (codex)"
             % (claude.get("name"), codex.get("name")))

    for name in on_disk:
        pdir = os.path.join(PLUGINS_DIR, name)
        cj = load(os.path.join(pdir, ".claude-plugin", "plugin.json"))
        xj = load(os.path.join(pdir, ".codex-plugin", "plugin.json"))
        if cj is None or xj is None:
            continue

        if cj.get("name") != name:
            fail("%s: .claude-plugin/plugin.json name is %r, expected %r"
                 % (name, cj.get("name"), name))
        for field in SHARED_FIELDS:
            if cj.get(field) != xj.get(field):
                fail("%s: %r differs between the claude and codex plugin.json"
                     % (name, field))
        if xj.get("skills") != "./skills/":
            fail("%s: codex plugin.json must declare \"skills\": \"./skills/\"" % name)

        skills_dir = os.path.join(pdir, "skills")
        if not os.path.isdir(skills_dir):
            fail("%s: no skills/ directory" % name)
            continue
        found = [
            d for d in sorted(os.listdir(skills_dir))
            if os.path.isfile(os.path.join(skills_dir, d, "SKILL.md"))
        ]
        if not found:
            fail("%s: skills/ contains no <skill>/SKILL.md" % name)
        for skill in found:
            check_frontmatter(os.path.join(skills_dir, skill, "SKILL.md"))

    return report()


def report():
    if problems:
        for p in problems:
            print("error: %s" % p, file=sys.stderr)
        print("\n%d problem(s)" % len(problems), file=sys.stderr)
        return 1
    print("ok: marketplaces, plugin manifests and skill frontmatter are consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
