# Brain-Claude

Persistent, measurable memory for **Claude Code**. It recalls relevant past work at
the start of a session, captures a structured record at the end, and lets Claude
compound experience across sessions and projects — wired in automatically via hooks,
no copy-paste.

- **Never start blind** — relevant past nodes are injected before work begins.
- **Measured, not self-graded** — outcomes are scored against targets, with
  provenance (`self` vs shell-`measured`).
- **Safe at scale** — recall is project-scoped, secrets are redacted on write, and
  hygiene commands keep the store healthy.
- **Dependency-free by default** — Python stdlib + SQLite. Semantic recall is optional.

## Setup

**Prerequisites:** Claude Code, `python3` (3.8+), and `git`. No other dependencies —
semantic recall and the autonomous loop have optional extras, noted where relevant.

### 1. Get the code

```sh
git clone <this repo> ~/dev/brain-claude && cd ~/dev/brain-claude
```

### 2. Install the hooks

Pick a scope. The installer wires four hooks, initialises the store, and runs a health
check. It's **idempotent** (re-run any time) and preserves any hooks you already have.

```sh
python3 bin/install.py --global      # every Claude Code session, any repo (recommended)
# or
python3 bin/install.py --project .   # only when working inside this repo
# preview first, writing nothing:
python3 bin/install.py --global --dry-run
```

`--global` writes to `~/.claude/settings.json` with an absolute path to `brain.py`;
`--project DIR` writes to `DIR/.claude/settings.json`.

### 3. Start a new session

Hooks load at session start, so **open a fresh Claude Code session** for them to take
effect. That's it — recall now runs automatically on each prompt, and a memory node is
captured when the session ends. You can force a capture any time with **`/brain-save`**.

### 4. Verify

```sh
python3 bin/brain.py doctor    # PASS/WARN on store, recall, WAL, redaction, dead-letters
python3 bin/brain.py status    # snapshot: nodes, vectors, project, hygiene counts
```

### 5. See it work (optional, no restart needed)

The store starts empty and fills as you work. To watch recall immediately:

```sh
python3 bin/brain.py save --json - <<'JSON'
{"node_id":"2026-01-01-oauth","tags":"oauth login jwt",
 "context":"Added OAuth login","lessons_learned":"Default to auth-code + PKCE for SPAs"}
JSON
python3 bin/brain.py recall --query "how should I do oauth login"
```

The node comes back — exactly what the `UserPromptSubmit` hook injects for you once installed.

### Optional configuration

- **Semantic recall** (paraphrase matching) — opt-in, still stdlib-only, falls back to
  keyword. See **[docs/embeddings.md](docs/embeddings.md)**.
- **Project scoping & redaction** — on by default; review the behaviour and the global
  setup notes in **[docs/privacy.md](docs/privacy.md)**.

### Uninstall

Remove the Brain-Claude hook entries from `~/.claude/settings.json` (or the project's
`.claude/settings.json`). Your `store/brain.db` is untouched — delete it to drop the memory.

## How it works

## How it works

```text
SessionStart   → recall recent nodes + open pending items      ┐
UserPromptSubmit → recall nodes matching your prompt (w/ metrics)│ injected in
                 ↓ Claude works, applying past lessons          │
Stop / SessionEnd → capture a <brain-update> node (+ metrics)   ┘ captured out
                 ↓
             store/brain.db   (SQLite: nodes · FTS5 · vectors · metrics)
```

Claude records the qualitative node (lessons, pending items) via a `<brain-update>`
block; the shell/hooks record hard metrics. See `CLAUDE.md` for the operating contract.

## Everyday commands

```sh
python3 bin/brain.py recall --query "oauth login"   # search the graph
python3 bin/brain.py recall --recent --pending      # recent + unfinished
python3 bin/brain.py metrics [--tag X]              # success over time (measured vs self)
python3 bin/brain.py status                         # snapshot
python3 bin/brain.py doctor                         # health checks
```

Force a capture any time with the **`/brain-save`** slash command.

## Keeping it healthy

```sh
python3 bin/brain.py dedup [--apply]                # consolidate near-duplicates
python3 bin/brain.py archive --older-than 180       # retire stale nodes
python3 bin/brain.py merge                          # suggest related clusters
python3 bin/brain.py reindex                        # rebuild FTS + vectors
```

All are project-safe (never merge across projects) and reversible (status flags,
no deletion). See **[docs/privacy.md](docs/privacy.md)**.

## Optional: semantic recall

Turn on embeddings for paraphrase matching (opt-in, stdlib-only, falls back to keyword).
See **[docs/embeddings.md](docs/embeddings.md)**.

## Autonomous mode

`bin/continuous-brain.sh` runs an iterate → PR → merge → measure loop over a target
repo, using brain.db as long-term memory. Run `bin/continuous-brain.sh --dry-run` first.

## Develop

```sh
make test        # run the suite (39 tests)
make install     # python3 bin/install.py --global
```

## Layout

| Path | What |
| --- | --- |
| `bin/brain.py` | the CLI + store engine |
| `bin/brain_embed.py` | optional embeddings backend |
| `bin/continuous-brain.sh` | autonomous loop |
| `bin/install.py` | hook installer |
| `.claude/` | hooks + `/brain-save` command |
| `store/brain.db` | the memory store |
| `schema/node.schema.json` | node format |
| `tests/` · `docs/` | tests · guides |

## License

[MIT](LICENSE)
