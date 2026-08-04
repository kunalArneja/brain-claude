#!/usr/bin/env python3
"""Brain-Claude installer — wire the hooks and initialise the store.

    python3 bin/install.py --global            # every Claude Code session (recommended)
    python3 bin/install.py --project [DIR]     # just one project (default: this repo)
    python3 bin/install.py --global --dry-run  # show what would change

It is idempotent: re-running updates the Brain-Claude hooks in place and leaves any
other hooks/settings untouched. Uses only the Python standard library.
"""

import argparse
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAIN = os.path.join(BASE, "bin", "brain.py")

# (event, matcher, brain.py args). Absolute BRAIN path so hooks work from any repo.
HOOKS = [
    ("SessionStart", "startup|resume", "recall --recent --pending --hook"),
    ("UserPromptSubmit", "", "recall --from-hook --hook"),
    ("Stop", "", "ingest"),
    ("SessionEnd", "", "ingest --from-transcript"),
]


def command_for(args):
    return f'python3 "{BRAIN}" {args}'


def wire(settings):
    """Insert/refresh Brain-Claude's hooks; drop stale brain.py entries first."""
    hooks = settings.setdefault("hooks", {})
    for event, matcher, cmd_args in HOOKS:
        entries = hooks.setdefault(event, [])
        # strip any prior brain.py hook (from an earlier install / path change)
        for e in entries:
            e["hooks"] = [h for h in e.get("hooks", []) if BRAIN not in h.get("command", "")
                          and "brain.py" not in h.get("command", "")]
        entries[:] = [e for e in entries if e.get("hooks")]
        entries.append({
            "matcher": matcher,
            "hooks": [{"type": "command", "command": command_for(cmd_args)}],
        })
    return settings


def load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def main(argv=None):
    p = argparse.ArgumentParser(prog="install.py", description="Install Brain-Claude hooks")
    scope = p.add_mutually_exclusive_group()
    scope.add_argument("--global", dest="glob", action="store_true",
                       help="install into ~/.claude/settings.json (all sessions)")
    scope.add_argument("--project", nargs="?", const=BASE, metavar="DIR",
                       help="install into DIR/.claude/settings.json (default: this repo)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    if args.glob:
        target = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    else:
        proj = args.project or BASE
        target = os.path.join(os.path.abspath(proj), ".claude", "settings.json")

    if not os.path.exists(BRAIN):
        sys.exit(f"error: {BRAIN} not found — run this from the brain-claude repo.")

    settings = wire(load(target))
    rendered = json.dumps(settings, indent=2)

    if args.dry_run:
        print(f"[dry-run] would write {target}:\n{rendered}")
        return

    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w") as f:
        f.write(rendered + "\n")
    print(f"✓ wired {len(HOOKS)} hooks into {target}")

    # Initialise the store + report health (flush so our lines precede subprocess output).
    print("\n$ brain.py init", flush=True);   subprocess.run([sys.executable, BRAIN, "init"])
    print("\n$ brain.py doctor", flush=True); subprocess.run([sys.executable, BRAIN, "doctor"])

    where = "every Claude Code session" if args.glob else os.path.dirname(os.path.dirname(target))
    print(f"\nDone. Brain-Claude is active for {where} from the next session.")
    print("Optional: enable semantic recall (docs/embeddings.md) and review "
          "scoping/redaction (docs/privacy.md).")


if __name__ == "__main__":
    main()
