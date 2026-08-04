#!/usr/bin/env python3
"""Brain-Claude — standalone external memory for Claude Code.

A single stdlib-only bridge between Claude Code (via hooks) and a portable
SQLite knowledge-graph store. Commands:

    brain.py init
    brain.py recall --query "<text>" [--k 5] [--hook]
    brain.py recall --recent [--pending] [--hook]
    brain.py recall --from-hook [--hook]          # reads user_prompt from stdin
    brain.py save --json <file|->
    brain.py ingest                               # reads Stop hook payload from stdin
    brain.py ingest --from-transcript [PATH]      # SessionEnd backstop (path or stdin)
    brain.py annotate <node_id> --metric name=value[:unit[:target[:dir]]]
    brain.py metrics [--tag X]
    brain.py merge
    brain.py status | doctor

Reliability contract (Phase 0):
  * Hook-facing commands NEVER crash the caller — on any error they emit an
    empty envelope (recall) or no-op (ingest) and exit 0. A SIGALRM budget
    guarantees they return fast even if the DB is stuck.
  * SQLite runs in WAL with a busy timeout, so overlapping events don't collide.
  * Rejected captures are dead-lettered (store/rejected.jsonl), never silently
    dropped; events are logged (store/events.log) for observability.

Storage is one SQLite file with an FTS5 index for recall (LIKE fallback).
No third-party dependencies.
"""

import argparse
import array
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Make the optional embeddings backend importable whether run as a script or
# imported by tests. Absent/broken → embeddings simply stay off.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import brain_embed
except Exception:
    brain_embed = None

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Paths are module globals so tests can point them at a temp dir.
STORE_DIR = os.environ.get("BRAIN_STORE", os.path.join(BASE, "store"))
DB_PATH = os.environ.get("BRAIN_DB", os.path.join(STORE_DIR, "brain.db"))
REJECTED_LOG = os.path.join(STORE_DIR, "rejected.jsonl")
EVENT_LOG = os.path.join(STORE_DIR, "events.log")

SCHEMA_VERSION = 5  # bump when init_db's schema changes

SEM_FLOOR = 0.6      # min cosine for a semantic-only (no keyword) match to count

# --- hygiene tuning --- #
MERGE_SEM, MERGE_KW = 0.75, 0.40      # "related" — loose, for merge suggestions
DEDUP_SEM, DEDUP_KW = 0.92, 0.75      # "near-duplicate" — tight, safe to consolidate
ARCHIVE_DAYS = 180

# Long-form text fields on a node (everything that isn't structured JSON).
TEXT_FIELDS = ("tags", "context", "actions_taken", "user_feedback", "lessons_learned")

# --- recall tuning (Phase 1) --- #
MIN_QUERY_TOKENS = 2     # skip recall on low-signal prompts ("yes", "continue")
REL_FLOOR = 0.34         # a node must match ≥ this fraction of query tokens (or a tag)
RECENCY_HALFLIFE_DAYS = 30.0
RECALL_MAX_CHARS = 8000  # budget injected context by whole nodes, not mid-node cuts


# --------------------------------------------------------------------------- #
# Small utilities (all best-effort — must never raise)
# --------------------------------------------------------------------------- #

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _store_dir():
    return os.path.dirname(DB_PATH) or "."


def log_event(kind, detail=""):
    try:
        os.makedirs(_store_dir(), exist_ok=True)
        with open(EVENT_LOG, "a") as f:
            f.write(f"{_now()}\t{kind}\t{detail}\n")
    except Exception:
        pass


def dead_letter(raw, reason):
    """Persist a rejected capture so a memory is never silently lost."""
    try:
        os.makedirs(_store_dir(), exist_ok=True)
        with open(REJECTED_LOG, "a") as f:
            f.write(json.dumps({"at": _now(), "reason": reason, "raw": raw}) + "\n")
    except Exception:
        pass


def dead_letter_count():
    try:
        with open(REJECTED_LOG) as f:
            return sum(1 for line in f if line.strip())
    except FileNotFoundError:
        return 0
    except Exception:
        return -1


# --------------------------------------------------------------------------- #
# Project scoping + redaction (privacy)
# --------------------------------------------------------------------------- #

def current_project():
    """Identify the project a session belongs to, cheaply and deterministically.

    Order: BRAIN_PROJECT override → $CLAUDE_PROJECT_DIR basename (set in hooks,
    no subprocess) → git toplevel basename → cwd basename.
    """
    override = os.environ.get("BRAIN_PROJECT")
    if override:
        return override.strip()
    proj_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if proj_dir:
        return os.path.basename(os.path.normpath(proj_dir)) or "default"
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=os.getcwd(), capture_output=True, text=True, timeout=1,
        )
        if top.returncode == 0 and top.stdout.strip():
            return os.path.basename(top.stdout.strip())
    except Exception:
        pass
    return os.path.basename(os.getcwd()) or "default"


def _scope_clause(col, project, scope):
    """SQL fragment + params that limit `nodes` to a project (globals always pass)."""
    if scope != "project" or not project:
        return "", []
    return f" AND ({col} = ? OR {col} IN ('', '*'))", [project]


# High-confidence secret patterns. Conservative by design — better to miss an
# exotic secret than to mangle ordinary lessons. Redaction is default-on so a
# secret never gets stored or re-injected into another session.
_SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
                re.DOTALL), "[REDACTED KEY]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"\bghp_[A-Za-z0-9]{36}\b"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b"), "[REDACTED]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
     "[REDACTED JWT]"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token|bearer)\b"
                r"(\s*[:=]\s*)(['\"]?)[^\s'\"]{6,}\3"), r"\1\2[REDACTED]"),
]


def redact(text):
    if not text:
        return text
    if os.environ.get("BRAIN_REDACT", "on").strip().lower() in ("off", "0", "false", "no"):
        return text
    for pat, repl in _SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def redaction_on():
    return os.environ.get("BRAIN_REDACT", "on").strip().lower() not in ("off", "0", "false", "no")


# --------------------------------------------------------------------------- #
# Connection / schema
# --------------------------------------------------------------------------- #

def _fts5_available(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def connect():
    os.makedirs(_store_dir(), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    # Concurrency: WAL lets readers and a writer coexist; busy_timeout makes
    # overlapping writers wait rather than raise 'database is locked'.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    return conn


def init_db(conn):
    # Hot-path guard: skip the FTS probe + migration once the schema is current.
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nodes (
            node_id         TEXT PRIMARY KEY,
            tags            TEXT DEFAULT '',
            context         TEXT DEFAULT '',
            actions_taken   TEXT DEFAULT '',
            user_feedback   TEXT DEFAULT '',
            metrics         TEXT DEFAULT '[]',   -- JSON array
            lessons_learned TEXT DEFAULT '',
            related_nodes   TEXT DEFAULT '[]',   -- JSON array of node_ids
            pending_items   TEXT DEFAULT '[]',   -- JSON array of strings
            status          TEXT DEFAULT 'active',
            project         TEXT DEFAULT '',     -- scoping key ('' or '*' = global)
            created_at      TEXT,
            updated_at      TEXT
        );

        CREATE TABLE IF NOT EXISTS metrics (
            node_id TEXT,
            name    TEXT,
            value   REAL,
            unit    TEXT,
            target  REAL,
            dir     TEXT DEFAULT 'min',   -- 'min': hit when value<=target; 'max': hit when value>=target
            source  TEXT DEFAULT 'self',  -- 'self': model-authored; 'measured': shell-computed
            FOREIGN KEY (node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_metrics_node ON metrics(node_id);
        CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name);

        CREATE TABLE IF NOT EXISTS node_vectors (
            node_id    TEXT PRIMARY KEY,
            model      TEXT,
            dim        INTEGER,
            vec        BLOB,      -- packed float32 (array('f'))
            updated_at TEXT,
            FOREIGN KEY (node_id) REFERENCES nodes(node_id) ON DELETE CASCADE
        );
        """
    )
    if _fts5_available(conn):
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5("
            "node_id UNINDEXED, tags, context, actions_taken, lessons_learned, user_feedback)"
        )
    # Migrations: add columns that post-date the original store.
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(metrics)")]
    if "dir" not in cols:
        conn.execute("ALTER TABLE metrics ADD COLUMN dir TEXT DEFAULT 'min'")
    if "source" not in cols:
        conn.execute("ALTER TABLE metrics ADD COLUMN source TEXT DEFAULT 'self'")
    node_cols = [r["name"] for r in conn.execute("PRAGMA table_info(nodes)")]
    if "project" not in node_cols:
        conn.execute("ALTER TABLE nodes ADD COLUMN project TEXT DEFAULT ''")
    # Create the index only now that the column is guaranteed to exist (works for
    # both fresh stores and ones migrated up from an earlier schema).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project)")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()


def has_fts(conn):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# Vectors (optional embeddings backend)
# --------------------------------------------------------------------------- #

def _embeddings_on():
    return brain_embed is not None and brain_embed.enabled()


def _node_text(row):
    """Concatenate a node's searchable text (works for a dict or a sqlite Row)."""
    parts = []
    for f in TEXT_FIELDS:
        try:
            val = row[f]
        except (KeyError, IndexError, TypeError):
            val = None
        if val:
            parts.append(val)
    return " ".join(parts)


def store_vector(conn, node_id, vec, model):
    conn.execute(
        "INSERT INTO node_vectors (node_id, model, dim, vec, updated_at) "
        "VALUES (?,?,?,?,?) "
        "ON CONFLICT(node_id) DO UPDATE SET model=excluded.model, dim=excluded.dim, "
        "vec=excluded.vec, updated_at=excluded.updated_at",
        (node_id, model, len(vec), array.array("f", vec).tobytes(), _now()),
    )
    conn.commit()


def _embed_node(conn, node_id, text):
    """Best-effort: compute + store a node's vector. Never raises."""
    if not _embeddings_on() or not text:
        return
    try:
        vec = brain_embed.embed(text)
        if vec:
            store_vector(conn, node_id, vec, brain_embed.model_name())
    except Exception as e:
        log_event("embed_error", f"{node_id}: {e}")


def _cosine(a, b):
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


def _semantic_scores(conn, qvec, limit):
    """cosine(query, node) for every stored vector; return the top `limit`."""
    scores = []
    for r in conn.execute("SELECT node_id, dim, vec FROM node_vectors"):
        try:
            v = array.array("f")
            v.frombytes(r["vec"])
        except Exception:
            continue
        if len(v) != len(qvec):
            continue
        scores.append((r["node_id"], _cosine(qvec, v)))
    scores.sort(key=lambda s: s[1], reverse=True)
    return dict(scores[:limit])


# --------------------------------------------------------------------------- #
# Write path
# --------------------------------------------------------------------------- #

def _coerce_metric(m):
    """Normalise one metric dict; returns (name, value, unit, target, dir) or None."""
    if not isinstance(m, dict) or "name" not in m:
        return None
    name = str(m["name"])
    try:
        value = float(m["value"]) if m.get("value") is not None else None
    except (TypeError, ValueError):
        value = None
    unit = str(m["unit"]) if m.get("unit") is not None else None
    try:
        target = float(m["target"]) if m.get("target") is not None else None
    except (TypeError, ValueError):
        target = None
    direction = m.get("dir")
    direction = direction if direction in ("min", "max") else "min"
    source = m.get("source")
    source = source if source in ("self", "measured") else "self"
    return (name, value, unit, target, direction, source)


def validate(node):
    """Lightweight validation. Returns list of error strings (empty == ok)."""
    errs = []
    if not isinstance(node, dict):
        return ["node must be a JSON object"]
    if not node.get("node_id"):
        errs.append("node_id is required")
    for f in ("metrics", "related_nodes", "pending_items"):
        if f in node and not isinstance(node[f], list):
            errs.append(f"{f} must be a JSON array")
    if isinstance(node.get("metrics"), list):
        for i, m in enumerate(node["metrics"]):
            if _coerce_metric(m) is None:
                errs.append(f"metrics[{i}] must be an object with at least a 'name'")
    return errs


def _resolve_node_id(conn, node, on_conflict):
    """Guard against accidental same-slug collisions.

    'update' (default): reuse node_id — legitimate for re-saves and annotate.
    'suffix': if a node with this id exists but describes a *different* task
    (different non-empty context), pick node_id-2/-3/... so we never clobber
    two distinct tasks that happened to share a slug on the same day.
    """
    node_id = node["node_id"]
    if on_conflict != "suffix":
        return node_id
    existing = conn.execute(
        "SELECT context FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    if not existing:
        return node_id
    inc = (node.get("context") or "").strip()
    cur = (existing["context"] or "").strip()
    if not inc or not cur or inc == cur:
        return node_id  # same task → update in place
    base, n = node_id, 2
    while conn.execute("SELECT 1 FROM nodes WHERE node_id=?", (f"{base}-{n}",)).fetchone():
        n += 1
    resolved = f"{base}-{n}"
    log_event("collision", f"{base} -> {resolved}")
    return resolved


def save_node(conn, node, on_conflict="update"):
    errs = validate(node)
    if errs:
        raise ValueError("; ".join(errs))

    node_id = _resolve_node_id(conn, node, on_conflict)
    now = _now()
    existing = conn.execute(
        "SELECT created_at FROM nodes WHERE node_id=?", (node_id,)
    ).fetchone()
    created_at = existing["created_at"] if existing else now

    metrics_list = node.get("metrics", []) or []
    # Provenance: anything not explicitly stamped 'measured' is model self-report.
    for m in metrics_list:
        if isinstance(m, dict) and "source" not in m:
            m["source"] = "self"
    # Scoping: honour an explicit project (incl. '' / '*' = global); else stamp
    # the current one. Privacy: redact secrets before anything is stored.
    if "project" in node and isinstance(node["project"], str):
        project = node["project"].strip()
    else:
        project = current_project()
    row = {
        "node_id": node_id,
        "tags": redact(node.get("tags", "") or ""),
        "context": redact(node.get("context", "") or ""),
        "actions_taken": redact(node.get("actions_taken", "") or ""),
        "user_feedback": redact(node.get("user_feedback", "") or ""),
        "metrics": json.dumps(metrics_list),
        "lessons_learned": redact(node.get("lessons_learned", "") or ""),
        "related_nodes": json.dumps(node.get("related_nodes", []) or []),
        "pending_items": json.dumps(node.get("pending_items", []) or []),
        "status": node.get("status", "active") or "active",
        "project": project,
        "created_at": created_at,
        "updated_at": now,
    }

    conn.execute(
        """
        INSERT INTO nodes (node_id, tags, context, actions_taken, user_feedback,
                           metrics, lessons_learned, related_nodes, pending_items,
                           status, project, created_at, updated_at)
        VALUES (:node_id, :tags, :context, :actions_taken, :user_feedback,
                :metrics, :lessons_learned, :related_nodes, :pending_items,
                :status, :project, :created_at, :updated_at)
        ON CONFLICT(node_id) DO UPDATE SET
            tags=excluded.tags, context=excluded.context,
            actions_taken=excluded.actions_taken, user_feedback=excluded.user_feedback,
            metrics=excluded.metrics, lessons_learned=excluded.lessons_learned,
            related_nodes=excluded.related_nodes, pending_items=excluded.pending_items,
            status=excluded.status, project=excluded.project, updated_at=excluded.updated_at
        """,
        row,
    )

    # Flatten metrics into the queryable table (replace, don't accumulate).
    conn.execute("DELETE FROM metrics WHERE node_id=?", (node_id,))
    for m in metrics_list:
        coerced = _coerce_metric(m)
        if coerced:
            conn.execute(
                "INSERT INTO metrics (node_id, name, value, unit, target, dir, source) "
                "VALUES (?,?,?,?,?,?,?)",
                (node_id, *coerced),
            )

    # Keep the FTS mirror in sync (manual replace — all writes flow through here).
    if has_fts(conn):
        conn.execute("DELETE FROM nodes_fts WHERE node_id=?", (node_id,))
        conn.execute(
            "INSERT INTO nodes_fts (node_id, tags, context, actions_taken, lessons_learned, user_feedback) "
            "VALUES (?,?,?,?,?,?)",
            (node_id, row["tags"], row["context"], row["actions_taken"],
             row["lessons_learned"], row["user_feedback"]),
        )

    conn.commit()
    # Semantic index (opt-in, best-effort — node write above is already durable).
    _embed_node(conn, node_id, _node_text(row))
    return node_id


def row_to_node(row):
    """Reconstruct a node dict from a DB row (parsing the JSON columns)."""
    def _arr(v):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return []
    return {
        "node_id": row["node_id"],
        "tags": row["tags"],
        "context": row["context"],
        "actions_taken": row["actions_taken"],
        "user_feedback": row["user_feedback"],
        "metrics": _arr(row["metrics"]),
        "lessons_learned": row["lessons_learned"],
        "related_nodes": _arr(row["related_nodes"]),
        "pending_items": _arr(row["pending_items"]),
        "status": row["status"],
        "project": row["project"],
    }


def load_node(conn, node_id):
    row = conn.execute("SELECT * FROM nodes WHERE node_id=?", (node_id,)).fetchone()
    return row_to_node(row) if row else None


def _num(s):
    """Best-effort numeric coercion; booleans map to 1/0; else None."""
    s = str(s).strip().lower()
    if s in ("true", "yes", "pass", "passed", "success", "green"):
        return 1.0
    if s in ("false", "no", "fail", "failed", "red"):
        return 0.0
    try:
        return float(s)
    except ValueError:
        return None


def parse_metric_spec(spec):
    """Parse 'name=value[:unit[:target[:dir]]]' into a metric dict.

    value/target accept true|false|yes|no (→ 1/0). Empty unit/target are dropped.
    dir is 'min' (default, lower is better) or 'max' (higher is better, e.g. coverage).
    Metrics written here are shell-computed, so they are stamped source='measured'.
    """
    if "=" not in spec:
        raise ValueError(f"metric '{spec}' must be name=value[:unit[:target[:dir]]]")
    name, rest = spec.split("=", 1)
    parts = rest.split(":")
    value = _num(parts[0])
    unit = parts[1] if len(parts) > 1 and parts[1] != "" else None
    target = _num(parts[2]) if len(parts) > 2 and parts[2] != "" else None
    direction = parts[3] if len(parts) > 3 and parts[3] in ("min", "max") else "min"
    return {"name": name.strip(), "value": value, "unit": unit,
            "target": target, "dir": direction, "source": "measured"}


def annotate_node(conn, node_id, metric_specs):
    """Attach/replace metrics on a node (creating a minimal node if absent)."""
    node = load_node(conn, node_id)
    created = node is None
    if node is None:
        node = {"node_id": node_id, "tags": "", "metrics": []}
    by_name = {m["name"]: m for m in node.get("metrics", []) if isinstance(m, dict)}
    for spec in metric_specs:
        m = parse_metric_spec(spec)
        by_name[m["name"]] = m           # replace-or-add by name
    node["metrics"] = list(by_name.values())
    save_node(conn, node)
    return node_id, created


# --------------------------------------------------------------------------- #
# Read path
# --------------------------------------------------------------------------- #

def _tokenize(text):
    return [t for t in re.findall(r"[A-Za-z0-9_]+", text or "") if len(t) >= 3]


def _candidates(conn, tokens, limit, project=None, scope="all"):
    """A wide pool of status='active' rows that match at least one token."""
    if has_fts(conn):
        match = " OR ".join(f'"{t}"' for t in tokens)
        sc, sp = _scope_clause("n.project", project, scope)
        try:
            return conn.execute(
                f"SELECT n.* FROM nodes_fts f JOIN nodes n ON n.node_id=f.node_id "
                f"WHERE nodes_fts MATCH ? AND n.status='active'{sc} ORDER BY rank LIMIT ?",
                (match, *sp, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            pass
    like_cols = " || ' ' || ".join(TEXT_FIELDS)
    clauses = " OR ".join(f"lower({like_cols}) LIKE ?" for _ in tokens)
    params = [f"%{t.lower()}%" for t in tokens]
    sc, sp = _scope_clause("project", project, scope)
    return conn.execute(
        f"SELECT * FROM nodes WHERE status='active' AND ({clauses}){sc} LIMIT ?",
        (*params, *sp, limit),
    ).fetchall()


def _relevance(row, tokens):
    """Fraction of query tokens present in the node's text, with a tag boost."""
    text = " ".join((row[f] or "") for f in TEXT_FIELDS).lower()
    tagset = {t.lower() for t in _tokenize(row["tags"])}
    matched = tag_hits = 0
    for t in tokens:
        tl = t.lower()
        if tl in text:
            matched += 1
            if tl in tagset:
                tag_hits += 1
    rel = matched / max(1, len(tokens)) + 0.1 * tag_hits
    return rel, matched, tag_hits


def _recency_weight(updated_at, now_dt):
    try:
        u = datetime.fromisoformat(updated_at)
    except (ValueError, TypeError):
        return 1.0
    if u.tzinfo is None:
        u = u.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now_dt - u).total_seconds() / 86400.0)
    return 0.5 ** (age_days / RECENCY_HALFLIFE_DAYS)


def _success_weight(conn, node_id):
    """Reward nodes whose MEASURED metrics hit target; neutral if none."""
    rows = conn.execute(
        "SELECT value, target, dir FROM metrics WHERE node_id=? AND source='measured' "
        "AND target IS NOT NULL AND value IS NOT NULL",
        (node_id,),
    ).fetchall()
    if not rows:
        return 1.0
    hits = sum(
        1 for r in rows
        if (r["dir"] == "max" and r["value"] >= r["target"])
        or (r["dir"] != "max" and r["value"] <= r["target"])
    )
    return 0.7 + 0.6 * (hits / len(rows))   # [0.7 .. 1.3]


def _semantic_candidates(conn, query, k):
    """{node_id: cosine} for semantically-close nodes, or {} if embeddings off/failed.

    Adds recall (paraphrase matching); never breaks recall — any failure yields {}.
    """
    if not _embeddings_on():
        return {}
    try:
        qvec = brain_embed.embed(query)
        if not qvec:
            return {}
        return _semantic_scores(conn, qvec, k * 4)
    except Exception as e:
        log_event("embed_error", f"query: {e}")
        return {}


def search(conn, query, k=5, project=None, scope="all"):
    """Hybrid recall: keyword ∪ semantic, ranked by relevance × recency × success.

    Relevance = max(keyword-overlap, semantic cosine) so semantic matches *add*
    paraphrase recall without displacing exact keyword hits. Keyword-weak nodes are
    dropped unless they clear the semantic floor; with embeddings off this reduces
    exactly to the Phase 1 keyword ranking. `scope='project'` limits to `project`
    (globals always included).
    """
    tokens = _tokenize(query)
    if not tokens:
        return []
    now_dt = datetime.now(timezone.utc)

    cand = {row["node_id"]: row for row in _candidates(conn, tokens, k * 4, project, scope)}
    sem = _semantic_candidates(conn, query, k)
    for nid in sem:
        if nid not in cand:
            sc, sp = _scope_clause("project", project, scope)
            row = conn.execute(
                f"SELECT * FROM nodes WHERE node_id=? AND status='active'{sc}",
                (nid, *sp),
            ).fetchone()
            if row:
                cand[nid] = row

    scored = []
    for nid, row in cand.items():
        kw_rel, matched, tag_hits = _relevance(row, tokens)
        sem_score = sem.get(nid, 0.0)
        keyword_ok = matched > 0 and (
            kw_rel >= REL_FLOOR or tag_hits > 0 or matched >= len(tokens)
        )
        if not keyword_ok and sem_score < SEM_FLOOR:
            continue  # neither a solid keyword nor a solid semantic match
        rel = max(kw_rel, sem_score)
        score = (
            rel
            * (0.4 + 0.6 * _recency_weight(row["updated_at"], now_dt))
            * _success_weight(conn, row["node_id"])
        )
        scored.append((score, row))
    scored.sort(key=lambda s: s[0], reverse=True)
    return [row for _, row in scored[:k]]


def recent(conn, k=5, project=None, scope="all"):
    sc, sp = _scope_clause("project", project, scope)
    return conn.execute(
        f"SELECT * FROM nodes WHERE status='active'{sc} ORDER BY updated_at DESC LIMIT ?",
        (*sp, k),
    ).fetchall()


def open_pending(conn, project=None, scope="all"):
    out = []
    sc, sp = _scope_clause("project", project, scope)
    for r in conn.execute(
        f"SELECT node_id, pending_items FROM nodes WHERE status='active' "
        f"AND pending_items != '[]'{sc} ORDER BY updated_at DESC", sp
    ):
        try:
            items = json.loads(r["pending_items"])
        except (ValueError, TypeError):
            items = []
        for it in items:
            out.append((r["node_id"], it))
    return out


# --------------------------------------------------------------------------- #
# Transcript backstop (SessionEnd guaranteed capture)
# --------------------------------------------------------------------------- #

BRAIN_BLOCK_RE = re.compile(r"<brain-update>\s*(\{.*?\})\s*</brain-update>", re.DOTALL)


def _assistant_text(obj):
    """Extract assistant text from a transcript JSONL record, tolerant to schema."""
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = msg.get("role") or obj.get("type")
    if role not in (None, "assistant"):
        return ""
    content = msg.get("content", obj.get("content"))
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(c.get("text", ""))
            elif isinstance(c, str):
                parts.append(c)
    return "\n".join(parts)


def extract_block_from_transcript(path):
    """Return the LAST <brain-update> block JSON string found in a transcript."""
    last = None
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                text = _assistant_text(obj)
                if text:
                    for m in BRAIN_BLOCK_RE.finditer(text):
                        last = m.group(1)
    except (FileNotFoundError, OSError):
        return None
    return last


def derive_transcript_metrics(path):
    """Cheap, objective metrics computed from the transcript (source='measured').

    These are facts the shell can see directly — how many assistant turns, how many
    file edits — so they never depend on the model's self-assessment.
    """
    turns = edits = 0
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                if (msg.get("role") or obj.get("type")) != "assistant":
                    continue
                turns += 1
                content = msg.get("content")
                if isinstance(content, list):
                    for c in content:
                        if (isinstance(c, dict) and c.get("type") == "tool_use"
                                and c.get("name") in ("Edit", "Write", "MultiEdit", "NotebookEdit")):
                            edits += 1
    except (FileNotFoundError, OSError):
        return []
    out = []
    if turns:
        out.append({"name": "assistant_turns", "value": turns, "source": "measured"})
    out.append({"name": "file_edits", "value": edits, "source": "measured"})
    return out


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #

def _fmt_metrics(metrics_json):
    try:
        metrics = json.loads(metrics_json)
    except (ValueError, TypeError):
        return ""
    parts = []
    for m in metrics:
        if not isinstance(m, dict):
            continue
        s = f"{m.get('name')}={m.get('value')}"
        if m.get("unit"):
            s += m["unit"]
        if m.get("target") is not None:
            cmp = "≥" if m.get("dir") == "max" else "≤"
            s += f" (target {cmp}{m['target']})"
        if m.get("source") == "self":
            s += " [self-reported]"
        parts.append(s)
    return "; ".join(parts)


def format_node(row, brief=False):
    lines = [f"### {row['node_id']}  [{row['updated_at']}]"]
    if row["tags"]:
        lines.append(f"- tags: {row['tags']}")
    if row["context"]:
        lines.append(f"- context: {row['context']}")
    if not brief and row["actions_taken"]:
        lines.append(f"- actions: {row['actions_taken']}")
    if row["lessons_learned"]:
        lines.append(f"- lesson: {row['lessons_learned']}")
    if row["user_feedback"]:
        lines.append(f"- feedback: {row['user_feedback']}")
    mt = _fmt_metrics(row["metrics"])
    if mt:
        lines.append(f"- metrics: {mt}")
    try:
        pend = json.loads(row["pending_items"])
    except (ValueError, TypeError):
        pend = []
    if pend:
        lines.append(f"- pending: {'; '.join(str(p) for p in pend)}")
    return "\n".join(lines)


def render_recall(nodes, pending=None, header="Relevant past experience",
                  max_chars=RECALL_MAX_CHARS):
    """Render ranked nodes within a char budget, dropping whole (lowest-ranked)
    nodes rather than truncating mid-node. `nodes` is assumed best-first."""
    if not nodes and not pending:
        return ""
    out = [f"## Brain-Claude — {header}"]
    used = len(out[0])
    for n in nodes:
        block = format_node(n)
        if used + len(block) + 2 > max_chars and len(out) > 1:
            break  # budget spent; keep what we have (at least one node)
        out.append(block)
        used += len(block) + 2
    if pending:
        lines = ["### Open pending items"]
        lines += [f"- [{node_id}] {item}" for node_id, item in pending]
        out.append("\n".join(lines))
    return "\n\n".join(out)


def emit_hook(context_text, event_name):
    """Print the Claude Code hook envelope that injects context."""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context_text[:10000],
        }
    }
    print(json.dumps(payload))


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def read_stdin_json():
    data = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not data.strip():
        return {}
    try:
        return json.loads(data)
    except ValueError:
        return {}


def cmd_init(args):
    conn = connect()
    init_db(conn)
    fts = "FTS5" if has_fts(conn) else "LIKE fallback (FTS5 unavailable)"
    print(f"Initialized {DB_PATH}\nRecall backend: {fts}")


def cmd_recall(args):
    conn = connect()
    init_db(conn)

    event = "UserPromptSubmit"
    nodes, pending, header = [], None, "Relevant past experience"

    project = current_project()
    scope = "all" if (args.all_projects
                      or os.environ.get("BRAIN_RECALL_SCOPE", "").strip().lower() == "all") \
        else "project"

    if args.recent:
        event = "SessionStart"
        nodes = recent(conn, args.k, project, scope)
        header = "Session start — recent experience"
        if args.pending:
            pending = open_pending(conn, project, scope)
    else:
        query = args.query
        if args.from_hook:
            hook = read_stdin_json()
            query = hook.get("user_prompt", "") or ""
            event = hook.get("hook_event_name", "UserPromptSubmit")
        # Skip low-signal prompts ("yes", "continue", "ok") — recall on them
        # only pollutes context and burns tokens.
        if len(_tokenize(query)) < MIN_QUERY_TOKENS:
            log_event("recall", f"{event} skipped=low-signal")
            if args.hook:
                emit_hook("", event)
            return
        nodes = search(conn, query, args.k, project, scope)

    log_event("recall", f"{event} project={project} scope={scope} "
                        f"nodes={len(nodes)} pending={len(pending or [])}")
    text = render_recall(nodes, pending, header)
    if args.hook:
        emit_hook(text, event)
    else:
        print(text if text else "(no matching memory)")


def cmd_save(args):
    conn = connect()
    init_db(conn)
    if args.json == "-":
        raw = sys.stdin.read()
    else:
        with open(args.json) as f:
            raw = f.read()
    node = json.loads(raw)
    node_id = save_node(conn, node)
    print(f"Saved node {node_id}")


def _ingest_raw(conn, raw, extra_metrics=None):
    """Parse + persist a single <brain-update> JSON string; dead-letter on failure.

    extra_metrics (objective, source='measured') are merged in without overriding
    any metric the model already named.
    """
    try:
        node = json.loads(raw)
    except ValueError as e:
        dead_letter(raw, f"malformed JSON: {e}")
        log_event("reject", "malformed json")
        return None
    if extra_metrics and isinstance(node, dict):
        existing = node.get("metrics")
        existing = existing if isinstance(existing, list) else []
        named = {m.get("name") for m in existing if isinstance(m, dict)}
        node["metrics"] = existing + [m for m in extra_metrics if m["name"] not in named]
    try:
        node_id = save_node(conn, node, on_conflict="suffix")
    except ValueError as e:
        dead_letter(raw, f"invalid node: {e}")
        log_event("reject", str(e))
        return None
    log_event("capture", node_id)
    sys.stderr.write(f"brain: captured node {node_id}\n")
    return node_id


def cmd_ingest(args):
    conn = connect()
    init_db(conn)
    extra = None
    if args.from_transcript is not None:
        path = args.from_transcript
        if path == "__stdin__":
            path = read_stdin_json().get("transcript_path", "")
        raw = extract_block_from_transcript(path) if path else None
        if raw and path:
            extra = derive_transcript_metrics(path)
    else:
        msg = read_stdin_json().get("last_assistant_message", "") or ""
        m = BRAIN_BLOCK_RE.search(msg)
        raw = m.group(1) if m else None
    if not raw:
        return  # clean no-op — nothing to capture this turn/session
    _ingest_raw(conn, raw, extra_metrics=extra)


def cmd_annotate(args):
    conn = connect()
    init_db(conn)
    if not args.metric:
        sys.stderr.write("brain: annotate needs at least one --metric\n")
        return
    node_id, created = annotate_node(conn, args.node_id, args.metric)
    where = "new node" if created else "existing node"
    print(f"Annotated {where} {node_id} with {len(args.metric)} metric(s)")


def cmd_metrics(args):
    conn = connect()
    init_db(conn)
    where, params = "", []
    if args.tag:
        where = "WHERE m.node_id IN (SELECT node_id FROM nodes WHERE tags LIKE ?)"
        params.append(f"%{args.tag}%")
    hit = ("((m.dir='max' AND m.value >= m.target) OR "
           "(m.dir!='max' AND m.value <= m.target))")
    rows = conn.execute(
        f"""
        SELECT m.name,
               COUNT(*)                                   AS n,
               ROUND(AVG(m.value), 3)                     AS avg_value,
               SUM(CASE WHEN m.source='measured' AND m.target IS NOT NULL AND m.value IS NOT NULL
                        AND {hit} THEN 1 ELSE 0 END)      AS m_hit,
               SUM(CASE WHEN m.source='measured' AND m.target IS NOT NULL THEN 1 ELSE 0 END) AS m_tot,
               SUM(CASE WHEN m.source!='measured' AND m.target IS NOT NULL AND m.value IS NOT NULL
                        AND {hit} THEN 1 ELSE 0 END)      AS s_hit,
               SUM(CASE WHEN m.source!='measured' AND m.target IS NOT NULL THEN 1 ELSE 0 END) AS s_tot
        FROM metrics m {where}
        GROUP BY m.name ORDER BY m.name
        """,
        params,
    ).fetchall()
    if not rows:
        print("(no metrics recorded yet)")
        return
    print("metric            n   avg      measured   self-reported")
    print("-" * 62)
    for r in rows:
        meas = f"{r['m_hit']}/{r['m_tot']}" if r["m_tot"] else "—"
        self_ = f"{r['s_hit']}/{r['s_tot']}" if r["s_tot"] else "—"
        print(
            f"{r['name'][:16]:<16}  {r['n']:<3} {str(r['avg_value']):<8} "
            f"{meas:<10} {self_}"
        )
    print("\nhit-rate = targets met (value≤target for dir:min, value≥target for dir:max)."
          "\nmeasured = shell-computed & trustworthy; self-reported = model-authored.")


# --------------------------------------------------------------------------- #
# Hygiene: clustering, merge, dedup, archive
# --------------------------------------------------------------------------- #

def _load_vectors(conn):
    vecs = {}
    for r in conn.execute("SELECT node_id, dim, vec FROM node_vectors"):
        try:
            a = array.array("f")
            a.frombytes(r["vec"])
        except Exception:
            continue
        vecs[r["node_id"]] = a
    return vecs


def _bucket(project):
    """Nodes only cluster within the same bucket — never across projects."""
    return "<global>" if project in ("", "*") else project


def _sim_text(row):
    # tags count double (strong signal), plus context + lessons
    return " ".join(filter(None, [row["tags"], row["tags"],
                                   row["context"], row["lessons_learned"]]))


def _jaccard(a, b):
    sa, sb = set(_tokenize(a)), set(_tokenize(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _similar_clusters(conn, sem_thr, kw_thr, project=None):
    """Union-find clusters of similar ACTIVE nodes, grouped per project bucket.

    Uses semantic cosine when both nodes have vectors, else token Jaccard.
    """
    scope = "project" if project else "all"
    sc, sp = _scope_clause("project", project, scope)
    rows = conn.execute(f"SELECT * FROM nodes WHERE status='active'{sc}", sp).fetchall()
    vecs = _load_vectors(conn)

    parent = {r["node_id"]: r["node_id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    groups = {}
    for r in rows:
        groups.setdefault(_bucket(r["project"]), []).append(r)
    for brows in groups.values():
        for i in range(len(brows)):
            for j in range(i + 1, len(brows)):
                a, b = brows[i], brows[j]
                ai, bi = a["node_id"], b["node_id"]
                if ai in vecs and bi in vecs and len(vecs[ai]) == len(vecs[bi]):
                    ok = _cosine(vecs[ai], vecs[bi]) >= sem_thr
                else:
                    ok = _jaccard(_sim_text(a), _sim_text(b)) >= kw_thr
                if ok:
                    union(ai, bi)

    clusters = {}
    for r in rows:
        clusters.setdefault(find(r["node_id"]), []).append(r["node_id"])
    return [ids for ids in clusters.values() if len(ids) > 1]


def _pick_canonical(conn, ids):
    """Best node to keep: measured success, then most content, then most recent."""
    best, best_key = None, None
    for nid in ids:
        n = load_node(conn, nid)
        succ = 1 if _success_weight(conn, nid) > 1.0 else 0
        text = len((n.get("context") or "") + (n.get("actions_taken") or "")
                   + (n.get("lessons_learned") or ""))
        upd = conn.execute("SELECT updated_at FROM nodes WHERE node_id=?",
                           (nid,)).fetchone()["updated_at"] or ""
        key = (succ, text, upd)
        if best_key is None or key > best_key:
            best, best_key = nid, key
    return best


def cmd_merge(args):
    """Suggest clusters of *related* nodes to consolidate (loose threshold)."""
    conn = connect()
    init_db(conn)
    clusters = _similar_clusters(conn, MERGE_SEM, MERGE_KW, args.project or None)
    if not clusters:
        print("(no related clusters found)")
        return
    print(f"{len(clusters)} cluster(s) of related nodes:")
    for ids in clusters:
        print(f"  - {', '.join(ids)}")
    print("\nConsolidate a cluster manually with `save`, or run `dedup --apply` "
          "to auto-merge the near-duplicates among them.")


def cmd_dedup(args):
    """Find near-duplicate nodes (tight threshold) and optionally consolidate them.

    --apply keeps one canonical node per cluster and marks the rest 'merged'
    (hidden from recall, retained in the store), folding their pending_items and
    links into the canonical. Never crosses a project boundary.
    """
    conn = connect()
    init_db(conn)
    clusters = _similar_clusters(conn, DEDUP_SEM, DEDUP_KW, args.project or None)
    if not clusters:
        print("(no near-duplicates found)")
        return
    if not args.apply:
        total = sum(len(c) - 1 for c in clusters)
        print(f"[dry-run] {len(clusters)} near-duplicate cluster(s); "
              f"{total} node(s) would be merged:")
        for ids in clusters:
            canon = _pick_canonical(conn, ids)
            dups = [i for i in ids if i != canon]
            print(f"  keep {canon}  ⇐  merge {', '.join(dups)}")
        print("\nre-run with --apply to consolidate.")
        return
    merged = 0
    for ids in clusters:
        canon_id = _pick_canonical(conn, ids)
        canon = load_node(conn, canon_id)
        rel = set(canon.get("related_nodes") or [])
        pend = list(canon.get("pending_items") or [])
        for d in [i for i in ids if i != canon_id]:
            dn = load_node(conn, d)
            rel.add(d)
            for p in (dn.get("pending_items") or []):
                if p not in pend:
                    pend.append(p)
            dn["status"] = "merged"
            dn["related_nodes"] = list(set(dn.get("related_nodes") or []) | {canon_id})
            save_node(conn, dn)
            merged += 1
        canon["related_nodes"] = list(rel)
        canon["pending_items"] = pend
        save_node(conn, canon)
    log_event("dedup", f"{merged} merged into {len(clusters)} canonical")
    print(f"Consolidated {merged} duplicate(s) into {len(clusters)} node(s); "
          "merged nodes hidden from recall.")


def cmd_archive(args):
    """Retire stale, low-value nodes from recall (reversible: sets status='archived')."""
    conn = connect()
    init_db(conn)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=args.older_than)).isoformat(timespec="seconds")
    scope = "project" if args.project else "all"
    sc, sp = _scope_clause("project", args.project or None, scope)
    rows = conn.execute(
        f"SELECT node_id FROM nodes WHERE status='active' AND updated_at < ? "
        f"AND project NOT IN ('', '*'){sc} ORDER BY updated_at", (cutoff, *sp)
    ).fetchall()
    victims = [r["node_id"] for r in rows
               if args.include_successful or _success_weight(conn, r["node_id"]) <= 1.0]
    if not victims:
        print(f"Nothing to archive (no active project nodes older than {args.older_than}d"
              f"{'' if args.include_successful else ', excluding measured-successful ones'}).")
        return
    if args.dry_run:
        print(f"[dry-run] would archive {len(victims)} node(s) older than {args.older_than}d:")
        for v in victims:
            print(f"  {v}")
        print("\nre-run without --dry-run to apply.")
        return
    conn.executemany("UPDATE nodes SET status='archived' WHERE node_id=?",
                     [(v,) for v in victims])
    conn.commit()
    log_event("archive", f"{len(victims)} nodes older than {args.older_than}d")
    print(f"Archived {len(victims)} node(s) — excluded from recall, retained in the store.")


def cmd_status(args):
    conn = connect()
    init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    active = conn.execute("SELECT COUNT(*) FROM nodes WHERE status='active'").fetchone()[0]
    mcount = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
    last = conn.execute("SELECT MAX(updated_at) FROM nodes").fetchone()[0] or "—"
    wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    vcount = conn.execute("SELECT COUNT(*) FROM node_vectors").fetchone()[0]
    emb = brain_embed.provider_info() if brain_embed else "unavailable"
    by_proj = conn.execute(
        "SELECT CASE WHEN project IN ('','*') THEN '(global)' ELSE project END AS p, "
        "COUNT(*) c FROM nodes WHERE status='active' GROUP BY p ORDER BY c DESC LIMIT 6"
    ).fetchall()
    proj_summary = ", ".join(f"{r['p']}:{r['c']}" for r in by_proj) or "—"
    print("Brain-Claude status")
    print(f"  store         {DB_PATH}")
    print(f"  project       {current_project()} (recall scoped here by default)")
    print(f"  by project    {proj_summary}")
    print(f"  recall        {'FTS5' if has_fts(conn) else 'LIKE fallback'}")
    print(f"  embeddings    {emb}")
    print(f"  vectors       {vcount} / {active} nodes indexed")
    print(f"  redaction     {'on' if redaction_on() else 'OFF'}")
    print(f"  journal       {wal}")
    archived = conn.execute("SELECT COUNT(*) FROM nodes WHERE status='archived'").fetchone()[0]
    merged = conn.execute("SELECT COUNT(*) FROM nodes WHERE status='merged'").fetchone()[0]
    print(f"  nodes         {active} active / {total} total")
    print(f"  hygiene       {archived} archived, {merged} merged (excluded from recall)")
    print(f"  metrics       {mcount}")
    print(f"  pending       {len(open_pending(conn))} open item(s)")
    print(f"  dead-letters  {dead_letter_count()} rejected")
    print(f"  last write    {last}")


def cmd_doctor(args):
    checks = []
    checks.append(("python", True, sys.version.split()[0]))
    conn = None
    try:
        conn = connect()
        init_db(conn)
        checks.append(("store writable", True, DB_PATH))
    except Exception as e:
        checks.append(("store writable", False, str(e)))
    if conn is not None:
        fts = has_fts(conn)
        checks.append(("fts5 recall", fts, "FTS5" if fts else "LIKE fallback (degraded)"))
        wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        checks.append(("WAL concurrency", wal.lower() == "wal", wal))
    if _embeddings_on():
        # A WARN here is non-fatal: recall just falls back to keyword.
        probe = None
        try:
            probe = brain_embed.embed("healthcheck")
        except Exception:
            probe = None
        detail = brain_embed.provider_info() + ("" if probe else " — unreachable, using keyword")
        checks.append(("embeddings", bool(probe), detail))
    else:
        checks.append(("embeddings", True, "off (keyword recall)"))
    checks.append(("redaction", redaction_on(),
                   "on" if redaction_on() else "OFF — secrets stored verbatim"))
    dl = dead_letter_count()
    checks.append(("dead-letters", dl == 0, f"{dl} rejected capture(s)"))
    ok_all = True
    for name, ok, detail in checks:
        ok_all = ok_all and ok
        print(f"[{'PASS' if ok else 'WARN'}] {name:<16} {detail}")
    print(f"\n{'all clear' if ok_all else 'see WARN lines above'}")


def cmd_reindex(args):
    """Rebuild the FTS mirror and (if embeddings are on) all node vectors.

    Use after enabling embeddings to backfill vectors for existing nodes, or to
    recover a drifted index.
    """
    conn = connect()
    init_db(conn)
    rows = conn.execute("SELECT * FROM nodes").fetchall()
    if has_fts(conn):
        conn.execute("DELETE FROM nodes_fts")
        for r in rows:
            conn.execute(
                "INSERT INTO nodes_fts (node_id, tags, context, actions_taken, "
                "lessons_learned, user_feedback) VALUES (?,?,?,?,?,?)",
                (r["node_id"], r["tags"], r["context"], r["actions_taken"],
                 r["lessons_learned"], r["user_feedback"]),
            )
        conn.commit()
    vecs = 0
    if _embeddings_on():
        for r in rows:
            before = conn.execute(
                "SELECT 1 FROM node_vectors WHERE node_id=?", (r["node_id"],)
            ).fetchone()
            _embed_node(conn, r["node_id"], _node_text(r))
            after = conn.execute(
                "SELECT 1 FROM node_vectors WHERE node_id=?", (r["node_id"],)
            ).fetchone()
            if after:
                vecs += 1
            del before
    emb = f"; {vecs}/{len(rows)} vectors" if _embeddings_on() else "; embeddings off"
    print(f"Reindexed {len(rows)} node(s): FTS rebuilt{emb}")


def cmd_embed(args):
    """Verify the embeddings setup by embedding a probe string."""
    if not _embeddings_on():
        print("embeddings: off — set BRAIN_EMBED=api|command (keyword recall in use).")
        return
    vec = None
    try:
        vec = brain_embed.embed(args.text)
    except Exception as e:
        print(f"embeddings: ERROR via {brain_embed.provider_info()}: {e}")
        return
    if not vec:
        print(f"embeddings: FAILED via {brain_embed.provider_info()} "
              f"(check config/network) — recall falls back to keyword.")
        return
    sample = [round(x, 4) for x in vec[:4]]
    print(f"embeddings: OK  provider={brain_embed.provider_info()}  "
          f"dim={len(vec)}  sample={sample}…")


# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(prog="brain.py", description="Brain-Claude memory bridge")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create the store").set_defaults(func=cmd_init)

    r = sub.add_parser("recall", help="retrieve past nodes")
    r.add_argument("--query", default="")
    r.add_argument("--recent", action="store_true", help="most-recent nodes (session start)")
    r.add_argument("--pending", action="store_true", help="also include open pending items")
    r.add_argument("--from-hook", action="store_true", help="read user_prompt from stdin JSON")
    r.add_argument("--hook", action="store_true", help="emit hookSpecificOutput JSON envelope")
    r.add_argument("--all-projects", action="store_true",
                   help="recall across every project, not just the current one")
    r.add_argument("--k", type=int, default=5)
    r.set_defaults(func=cmd_recall)

    s = sub.add_parser("save", help="upsert a node from JSON")
    s.add_argument("--json", required=True, help="path to JSON file, or - for stdin")
    s.set_defaults(func=cmd_save)

    i = sub.add_parser("ingest", help="capture <brain-update> from a hook payload")
    i.add_argument("--from-transcript", nargs="?", const="__stdin__", default=None,
                   metavar="PATH",
                   help="SessionEnd backstop: scan a transcript file (or, with no "
                        "value, the transcript_path in stdin JSON) for the last block")
    i.set_defaults(func=cmd_ingest)

    a = sub.add_parser("annotate", help="attach computed metrics to a node")
    a.add_argument("node_id")
    a.add_argument("--metric", action="append", default=[],
                   metavar="name=value[:unit[:target[:dir]]]",
                   help="repeatable; e.g. --metric coverage=73:%%:80:max --metric ci_passed=true")
    a.set_defaults(func=cmd_annotate)

    m = sub.add_parser("metrics", help="aggregate metric report")
    m.add_argument("--tag", default="")
    m.set_defaults(func=cmd_metrics)

    mg = sub.add_parser("merge", help="suggest clusters of related nodes")
    mg.add_argument("--project", default="", help="limit to one project")
    mg.set_defaults(func=cmd_merge)

    dd = sub.add_parser("dedup", help="find/consolidate near-duplicate nodes")
    dd.add_argument("--apply", action="store_true", help="consolidate (default: dry-run)")
    dd.add_argument("--project", default="", help="limit to one project")
    dd.set_defaults(func=cmd_dedup)

    ar = sub.add_parser("archive", help="retire stale nodes from recall")
    ar.add_argument("--older-than", type=int, default=ARCHIVE_DAYS, metavar="DAYS")
    ar.add_argument("--dry-run", action="store_true")
    ar.add_argument("--include-successful", action="store_true",
                    help="also archive nodes whose measured metrics hit target")
    ar.add_argument("--project", default="", help="limit to one project")
    ar.set_defaults(func=cmd_archive)

    sub.add_parser("status", help="store snapshot").set_defaults(func=cmd_status)
    sub.add_parser("doctor", help="health checks").set_defaults(func=cmd_doctor)
    sub.add_parser("reindex", help="rebuild FTS + vector indexes").set_defaults(func=cmd_reindex)

    e = sub.add_parser("embed", help="test the embeddings backend")
    e.add_argument("text", help="probe string to embed")
    e.set_defaults(func=cmd_embed)
    return p


def _hook_event_for(args):
    """The hook event name a --hook recall should answer with, else None."""
    if args.cmd == "recall" and getattr(args, "hook", False):
        return "SessionStart" if getattr(args, "recent", False) else "UserPromptSubmit"
    return None


def main(argv=None):
    args = build_parser().parse_args(argv)
    hook_event = _hook_event_for(args)
    is_hook = hook_event is not None or args.cmd == "ingest"

    # Wall-clock budget: a stuck DB must never stall the user's prompt.
    if is_hook and hasattr(signal, "SIGALRM"):
        budget = float(os.environ.get("BRAIN_HOOK_BUDGET", "2.0"))

        def _bail(signum, frame):
            raise TimeoutError(f"hook budget {budget}s exceeded")

        try:
            signal.signal(signal.SIGALRM, _bail)
            signal.setitimer(signal.ITIMER_REAL, budget)
        except Exception:
            pass

    try:
        args.func(args)
    except Exception as e:
        log_event("error", f"{args.cmd}: {type(e).__name__}: {e}")
        if is_hook:
            # Fail open: give Claude Code a valid (empty) response and exit clean.
            if hook_event:
                try:
                    emit_hook("", hook_event)
                except Exception:
                    pass
            sys.exit(0)
        sys.stderr.write(f"brain: {e}\n")
        sys.exit(1)
    finally:
        if is_hook and hasattr(signal, "SIGALRM"):
            try:
                signal.setitimer(signal.ITIMER_REAL, 0)
            except Exception:
                pass


if __name__ == "__main__":
    main()
