#!/usr/bin/env python3
"""Install the rendered skill into each agent's skills directory.

Copies by default. --link symlinks instead, which is what you want while
iterating on the builder: re-render and every agent sees it immediately.

Usage:  python3 builder/install.py [--agents claude,codex,pi] [--link] [--dry-run]
"""

import argparse
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HOME = os.path.expanduser("~")

# Where each agent looks for user-level skills. pi also reads ~/.agents/skills,
# the cross-agent convention, so installing there covers pi and anything else
# implementing the Agent Skills standard.
TARGETS = {
    "claude": os.path.join(HOME, ".claude", "skills"),
    "codex": os.path.join(HOME, ".codex", "skills"),
    "pi": os.path.join(HOME, ".agents", "skills"),
}


def install_one(agent, dest_dir, src, name, link, dry_run):
    dest = os.path.join(dest_dir, name)
    action = "link" if link else "copy"
    if dry_run:
        print("  %-7s would %s -> %s" % (agent, action, dest))
        return True

    os.makedirs(dest_dir, exist_ok=True)
    if os.path.islink(dest):
        os.unlink(dest)
    elif os.path.isdir(dest):
        shutil.rmtree(dest)

    if link:
        os.symlink(src, dest)
    else:
        shutil.copytree(src, dest)
    print("  %-7s %s -> %s" % (agent, action + "ed", dest))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.join(ROOT, "dist", "amd-gpu-isa"))
    ap.add_argument("--agents", default="claude,codex,pi",
                    help="comma-separated: claude, codex, pi (default: all)")
    ap.add_argument("--link", action="store_true",
                    help="symlink instead of copying (live edits, saves ~20 MB each)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(args.src):
        sys.exit("nothing to install at %s -- run 'build.py render' first" % args.src)
    name = os.path.basename(args.src.rstrip("/"))

    wanted = [a.strip() for a in args.agents.split(",") if a.strip()]
    unknown = [a for a in wanted if a not in TARGETS]
    if unknown:
        sys.exit("unknown agent(s): %s (known: %s)"
                 % (", ".join(unknown), ", ".join(TARGETS)))

    for agent in wanted:
        install_one(agent, TARGETS[agent], os.path.realpath(args.src), name,
                    args.link, args.dry_run)

    if not args.dry_run:
        print("\nInstalled as '%s'. Start a new agent session to pick it up." % name)
        if "pi" in wanted:
            print("pi: ensure its settings include ~/.agents/skills, or pass "
                  "--skill %s" % os.path.join(TARGETS["pi"], name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
