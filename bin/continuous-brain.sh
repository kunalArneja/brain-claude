#!/usr/bin/env bash
#
# continuous-brain.sh — continuous-claude's autonomous loop, wired into Brain-Claude.
#
# Each iteration:  RECALL past lessons → WORK (headless Claude) → capture qualitative
# node → SHIP (branch/PR/merge) → MEASURE hard metrics → annotate the same node.
# brain.db replaces continuous-claude's flat SHARED_TASK_NOTES.md: memory is
# searchable, cross-project, and carries measured metrics over time.
#
# Run from the ROOT of the TARGET repo:
#   cd ~/dev/some-legacy-app
#   TASK="Raise test coverage toward 80%, one module per iteration" \
#   MAX_ITERS=10 ~/dev/brain-claude/bin/continuous-brain.sh
#
# Requires: python3, git. Optional: gh (PRs/merge), timeout/gtimeout (per-iter cap).
# Provider-agnostic: swap run_agent() for another CLI.

set -Eeuo pipefail

# ----- config (env-overridable) -------------------------------------------- #
BRAIN="${BRAIN:-$HOME/dev/brain-claude/bin/brain.py}"
TASK="${TASK:-Improve the codebase in a small, safe, reviewable increment}"
MAX_ITERS="${MAX_ITERS:-5}"
RECALL_K="${RECALL_K:-5}"
CLAUDE_FLAGS="${CLAUDE_FLAGS:---permission-mode acceptEdits}"
AUTO_MERGE="${AUTO_MERGE:-1}"
ITER_TIMEOUT="${ITER_TIMEOUT:-0}"     # seconds per agent run; 0 = no cap
DRY_RUN="${DRY_RUN:-0}"
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

PROJECT="$(basename "$PWD")"
START_BRANCH=""
HAVE_GH=1

log()  { printf '\033[36m[brain]\033[0m %s\n' "$*"; }
warn() { printf '\033[33m[brain] warning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31m[brain] error:\033[0m %s\n' "$*" >&2; exit 1; }

# ----- timeout wrapper (macOS often lacks `timeout`) ----------------------- #
TIMEOUT_BIN=""
command -v timeout  >/dev/null 2>&1 && TIMEOUT_BIN=timeout
[ -z "$TIMEOUT_BIN" ] && command -v gtimeout >/dev/null 2>&1 && TIMEOUT_BIN=gtimeout
run_bounded() {  # $1=seconds (0=none), rest=command
  local secs="$1"; shift
  if [ -n "$TIMEOUT_BIN" ] && [ "$secs" -gt 0 ]; then "$TIMEOUT_BIN" "$secs" "$@"; else "$@"; fi
}

# ----- preflight ----------------------------------------------------------- #
preflight() {
  command -v python3 >/dev/null 2>&1 || die "python3 not found"
  [ -f "$BRAIN" ] || die "brain.py not found at $BRAIN (set BRAIN=...)"
  git rev-parse --git-dir >/dev/null 2>&1 || die "not a git repository"
  START_BRANCH="$(git symbolic-ref --short HEAD 2>/dev/null || echo main)"
  if [ -n "$(git status --porcelain)" ]; then
    die "working tree is not clean — commit or stash changes before an autonomous run"
  fi
  command -v gh >/dev/null 2>&1 || { HAVE_GH=0; warn "gh not found — PR/merge disabled, committing locally only"; }
  git remote get-url origin >/dev/null 2>&1 || { HAVE_GH=0; warn "no 'origin' remote — push/PR disabled"; }
  [ -z "$TIMEOUT_BIN" ] && [ "$ITER_TIMEOUT" -gt 0 ] && warn "no timeout/gtimeout — ITER_TIMEOUT ignored"
  python3 "$BRAIN" init >/dev/null
  log "preflight ok — project=$PROJECT base=$START_BRANCH gh=$HAVE_GH dry_run=$DRY_RUN"
}

# ----- cleanup: always return to the branch we started on ------------------ #
cleanup() { [ -n "$START_BRANCH" ] && git switch "$START_BRANCH" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# ----- customise me: how to MEASURE this repo (source='measured') ---------- #
measure() {
  if command -v coverage >/dev/null 2>&1; then
    local cov
    cov="$(coverage report 2>/dev/null | awk '/TOTAL/{gsub("%","",$NF); print $NF}')" || true
    [ -n "${cov:-}" ] && echo "coverage=${cov}:%:80:max"
  fi
}

# ----- WORK step (swap for another provider) ------------------------------- #
run_agent() { run_bounded "$ITER_TIMEOUT" claude -p "$1" $CLAUDE_FLAGS; }

# ----- one iteration (isolated: a failure here won't kill the loop) -------- #
run_iter() {
  local i="$1" stamp node_id prev_id memory prompt out branch cov ci merged
  stamp="$(date +%F)"; node_id="${PROJECT}-iter${i}-${stamp}"; prev_id="${PROJECT}-iter$((i-1))-${stamp}"

  git switch "$START_BRANCH" >/dev/null 2>&1 || true
  git pull --ff-only >/dev/null 2>&1 || true

  memory="$(python3 "$BRAIN" recall --query "$TASK" --k "$RECALL_K" || true)"

  prompt="$memory

TASK: $TASK

Autonomous iteration $i. Make ONE small, safe, reviewable change, then stop.
End your message with EXACTLY this block (valid JSON, node_id verbatim):
<brain-update>
{\"node_id\": \"$node_id\",
 \"tags\": \"$PROJECT autonomous\",
 \"context\": \"$TASK\",
 \"actions_taken\": \"<what you changed>\",
 \"lessons_learned\": \"<what to do differently next>\",
 \"pending_items\": [\"<still unfinished>\"],
 \"related_nodes\": [\"$prev_id\"]}
</brain-update>"

  if [ "$DRY_RUN" = 1 ]; then log "dry-run: would run agent + ship for $node_id"; return 0; fi

  out="$(run_agent "$prompt")" || { warn "agent run failed (iter $i)"; return 1; }
  printf '%s\n' "$out"

  # Capture the qualitative node (same path the Stop hook uses; idempotent).
  printf '%s' "$out" | python3 -c 'import json,sys; print(json.dumps({"last_assistant_message": sys.stdin.read()}))' \
    | python3 "$BRAIN" ingest || true

  if [ -z "$(git status --porcelain)" ]; then
    log "iter $i: no changes — recording no-op"
    python3 "$BRAIN" annotate "$node_id" --metric changes=0 --metric merged=0 || true
    return 0
  fi

  branch="auto/${node_id}"
  git switch -c "$branch" >/dev/null 2>&1 || git switch "$branch"
  git add -A && git commit -q -m "auto(iter $i): $TASK"

  merged=0
  if [ "$HAVE_GH" = 1 ]; then
    git push -u origin "$branch" >/dev/null 2>&1 || warn "push failed (iter $i)"
    gh pr create --fill --base "$START_BRANCH" --head "$branch" >/dev/null 2>&1 || warn "pr create failed"
    [ "$AUTO_MERGE" = 1 ] && { gh pr merge "$branch" --auto --squash >/dev/null 2>&1 || warn "auto-merge not enabled"; }
    ci="$(gh pr checks "$branch" --json state -q 'all(.[].state=="SUCCESS")' 2>/dev/null || echo unknown)"
    gh pr view "$branch" --json state -q '.state' 2>/dev/null | grep -qi merged && merged=1
  else
    ci="local"
  fi

  local metric_args=(--metric "changes=1" --metric "ci_passed=${ci}" --metric "merged=${merged}")
  while IFS= read -r line; do [ -n "$line" ] && metric_args+=(--metric "$line"); done < <(measure)
  python3 "$BRAIN" annotate "$node_id" "${metric_args[@]}" || true
  log "iter $i done — ci=$ci merged=$merged"
}

# ----- main ---------------------------------------------------------------- #
preflight
for i in $(seq 1 "$MAX_ITERS"); do
  log "==================== iteration $i / $MAX_ITERS ===================="
  run_iter "$i" || warn "iteration $i failed — continuing"
done
log "loop complete"
python3 "$BRAIN" metrics --tag "$PROJECT" || true
