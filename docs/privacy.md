# Project scoping & privacy

Brain-Claude is safe to run **globally** (hooks in `~/.claude/settings.json`, shared
across every repo) without one project's memory leaking into another's session.

## Project scoping

Every node records the **project** it was created in. The project is derived, in order:

1. `BRAIN_PROJECT` env override, else
2. `$CLAUDE_PROJECT_DIR` basename (set by Claude Code hooks — no subprocess), else
3. the `git` repo toplevel basename, else
4. the current directory's basename.

**Recall is scoped to the current project by default.** A session in repo A never sees
repo B's nodes. Two escape hatches:

- **Global nodes** — save with `"project": "*"` (or `""`). These surface in *every*
  project. Use for durable, cross-cutting lessons ("user prefers X", house style).
- **Cross-project recall** — `brain.py recall --all-projects`, or set
  `BRAIN_RECALL_SCOPE=all`, to search the whole store regardless of project.

`brain.py status` shows the current project and a per-project node count.

> Nodes created before scoping existed have an empty project and are treated as
> global (they surface everywhere) until re-saved under a project.

## Secret redaction

Because a memory can be injected into a *different* future session, secrets must never
be stored. On every write, Brain-Claude runs a **conservative, default-on** redaction
pass over the text fields, masking high-confidence secrets:

- AWS keys (`AKIA…`), GitHub tokens (`ghp_…`, `github_pat_…`), Slack (`xox…`),
  OpenAI-style (`sk-…`), JWTs, and `-----BEGIN … PRIVATE KEY-----` blocks;
- `password=` / `secret=` / `api_key=` / `token=` / `bearer …` key-value pairs.

Matches become `[REDACTED]`. The patterns are intentionally narrow — better to miss an
exotic secret than to mangle an ordinary lesson. Ordinary prose is never touched.

Disable with `BRAIN_REDACT=off` (not recommended). `brain.py status` / `doctor` report
whether redaction is on.

## Keeping the store healthy (hygiene)

As memory grows, run these occasionally (all respect project boundaries — nothing
is ever merged across projects):

```sh
python3 bin/brain.py merge            # suggest clusters of related nodes to consolidate
python3 bin/brain.py dedup            # dry-run: list near-duplicate clusters
python3 bin/brain.py dedup --apply    # keep one canonical per cluster; hide the rest (status='merged')
python3 bin/brain.py archive --older-than 180 --dry-run   # preview stale nodes
python3 bin/brain.py archive --older-than 180             # retire them (status='archived')
python3 bin/brain.py reindex          # rebuild FTS + vectors (recover drift)
```

`dedup --apply` and `archive` are **reversible** — they set a status flag and retain
the data; they never delete. Archived/merged nodes are excluded from recall. `archive`
spares nodes whose **measured** metrics hit target (pass `--include-successful` to
override). `status` shows archived/merged counts.

## Recommended global setup

1. Move the hooks from the project's `.claude/settings.json` into `~/.claude/settings.json`,
   replacing `$CLAUDE_PROJECT_DIR/bin/brain.py` with the absolute path to `brain.py`.
2. Keep redaction on (default).
3. Recall stays project-scoped automatically; reserve `"project":"*"` for truly global lessons.
