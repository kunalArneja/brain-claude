"""Durability + concurrency tests for Brain-Claude.

Run:  python3 -m unittest discover -s tests   (from the repo root)
or:   python3 tests/test_brain.py

The module points brain.py's storage globals at a fresh temp dir per test, so
nothing here touches the real store.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest

# Import bin/brain.py as a module regardless of cwd.
HERE = os.path.dirname(os.path.abspath(__file__))
BRAIN_PATH = os.path.join(os.path.dirname(HERE), "bin", "brain.py")
_spec = importlib.util.spec_from_file_location("brain", BRAIN_PATH)
brain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(brain)


class BrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="brain-test-")
        brain.STORE_DIR = self.tmp
        brain.DB_PATH = os.path.join(self.tmp, "brain.db")
        brain.REJECTED_LOG = os.path.join(self.tmp, "rejected.jsonl")
        brain.EVENT_LOG = os.path.join(self.tmp, "events.log")
        # Pin the project (avoids a git subprocess per save) and default redaction on.
        os.environ["BRAIN_PROJECT"] = "proj-a"
        os.environ.pop("BRAIN_REDACT", None)
        self.conn = brain.connect()
        brain.init_db(self.conn)

        # A stub embeddings provider: maps text to a concept vector so we can test
        # the semantic path deterministically, offline. Tests opt in per-case.
        self._orig_embed = brain.brain_embed
        self.fake = types.SimpleNamespace(
            enabled=lambda: True, model_name=lambda: "fake",
            provider_info=lambda: "fake", embed=_concept_vec)

    def tearDown(self):
        brain.brain_embed = self._orig_embed
        os.environ.pop("BRAIN_PROJECT", None)
        os.environ.pop("BRAIN_REDACT", None)
        try:
            self.conn.close()
        except Exception:
            pass

    def node(self, **over):
        n = {"node_id": "2026-08-03-task", "tags": "alpha beta",
             "context": "ctx", "actions_taken": "did things",
             "lessons_learned": "a lesson"}
        n.update(over)
        return n

    # ---- core round-trip ------------------------------------------------- #
    def test_save_and_recall(self):
        brain.save_node(self.conn, self.node(tags="oauth login jwt"))
        rows = brain.search(self.conn, "how do I do oauth", k=5)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["node_id"], "2026-08-03-task")

    def test_wal_enabled(self):
        mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")

    def test_init_gated_by_user_version(self):
        self.assertEqual(
            self.conn.execute("PRAGMA user_version").fetchone()[0],
            brain.SCHEMA_VERSION,
        )

    # ---- metric direction + provenance ----------------------------------- #
    def test_metric_direction_scoring(self):
        brain.save_node(self.conn, self.node(node_id="n-cov", context="cov",
            metrics=[{"name": "coverage", "value": 73, "target": 80, "dir": "max"}]))
        brain.annotate_node(self.conn, "n-cov2", ["coverage=82:%:80:max"])
        rows = self.conn.execute(
            "SELECT node_id FROM metrics WHERE name='coverage' AND "
            "((dir='max' AND value>=target) OR (dir!='max' AND value<=target))"
        ).fetchall()
        hits = {r["node_id"] for r in rows}
        self.assertIn("n-cov2", hits)      # 82 >= 80 hits
        self.assertNotIn("n-cov", hits)    # 73 >= 80 misses

    def test_provenance_defaults(self):
        brain.save_node(self.conn, self.node(node_id="n-self",
            metrics=[{"name": "speed", "value": 5}]))
        brain.annotate_node(self.conn, "n-self", ["loc=10"])
        stored = {m["name"]: m["source"]
                  for m in brain.load_node(self.conn, "n-self")["metrics"]}
        self.assertEqual(stored["speed"], "self")       # model-authored
        self.assertEqual(stored["loc"], "measured")     # shell-annotated

    # ---- ingest: capture, no-op, dead-letter ----------------------------- #
    def _ingest_stdin(self, payload):
        node = json.dumps(payload)
        try:
            _in = sys.stdin
            sys.stdin = _FakeStdin(node)
            args = brain.build_parser().parse_args(["ingest"])
            brain.cmd_ingest(args)
        finally:
            sys.stdin = _in

    def test_ingest_captures_block(self):
        block = ('done. <brain-update>{"node_id":"2026-08-03-cap",'
                 '"tags":"x","context":"c","actions_taken":"a"}</brain-update>')
        self._ingest_stdin({"last_assistant_message": block})
        self.assertIsNotNone(brain.load_node(self.conn, "2026-08-03-cap"))

    def test_ingest_noop_without_block(self):
        self._ingest_stdin({"last_assistant_message": "just a normal reply"})
        self.assertEqual(brain.dead_letter_count(), 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 0)

    def test_ingest_malformed_is_dead_lettered(self):
        block = "<brain-update>{ not valid json }</brain-update>"
        self._ingest_stdin({"last_assistant_message": block})
        self.assertEqual(brain.dead_letter_count(), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 0)

    def test_ingest_invalid_node_is_dead_lettered(self):
        block = '<brain-update>{"tags":"no id here"}</brain-update>'  # missing node_id
        self._ingest_stdin({"last_assistant_message": block})
        self.assertEqual(brain.dead_letter_count(), 1)

    # ---- transcript backstop (SessionEnd) -------------------------------- #
    def test_transcript_backstop(self):
        tpath = os.path.join(self.tmp, "transcript.jsonl")
        lines = [
            {"type": "user", "message": {"role": "user", "content": "hi"}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": 'wrap. <brain-update>{"node_id":"2026-08-03-tr",'
                 '"context":"c"}</brain-update>'}]}},
        ]
        with open(tpath, "w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        raw = brain.extract_block_from_transcript(tpath)
        self.assertIsNotNone(raw)
        brain._ingest_raw(self.conn, raw)
        self.assertIsNotNone(brain.load_node(self.conn, "2026-08-03-tr"))

    # ---- collision guard ------------------------------------------------- #
    def test_collision_suffixes_distinct_task(self):
        brain.save_node(self.conn, self.node(node_id="dup", context="task ONE"),
                        on_conflict="suffix")
        rid = brain.save_node(self.conn, self.node(node_id="dup", context="task TWO"),
                              on_conflict="suffix")
        self.assertEqual(rid, "dup-2")  # not clobbered
        self.assertEqual(brain.load_node(self.conn, "dup")["context"], "task ONE")

    def test_same_task_updates_in_place(self):
        brain.save_node(self.conn, self.node(node_id="same", context="same ctx"),
                        on_conflict="suffix")
        rid = brain.save_node(self.conn,
                              self.node(node_id="same", context="same ctx",
                                        actions_taken="more"),
                              on_conflict="suffix")
        self.assertEqual(rid, "same")  # updated, not suffixed

    # ---- concurrency ----------------------------------------------------- #
    def test_concurrent_writers_no_lock(self):
        errors = []

        def writer(i):
            try:
                c = brain.connect()
                for j in range(5):
                    brain.save_node(c, self.node(node_id=f"c-{i}-{j}",
                                                 context=f"ctx {i} {j}"))
                c.close()
            except Exception as e:  # e.g. 'database is locked'
                errors.append(repr(e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"writer errors: {errors}")
        c = brain.connect()
        total = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        c.close()
        self.assertEqual(total, 25)

    # ---- Phase 1: blended ranking + floor -------------------------------- #
    def _all_updated_at(self, iso):
        self.conn.execute("UPDATE nodes SET updated_at=?", (iso,))
        self.conn.commit()

    def test_relevance_floor_drops_weak_match(self):
        # one non-tag token match against a 5-token query → below floor → dropped
        brain.save_node(self.conn, {"node_id": "weak",
                                    "context": "the quick brown fox banana"})
        rows = brain.search(self.conn, "alpha beta gamma delta banana", k=5)
        self.assertEqual(rows, [])

    def test_recency_breaks_ties(self):
        brain.save_node(self.conn, self.node(node_id="old", tags="python testing"))
        brain.save_node(self.conn, self.node(node_id="new", tags="python testing"))
        self.conn.execute("UPDATE nodes SET updated_at='2020-01-01T00:00:00+00:00' "
                          "WHERE node_id='old'")
        self.conn.commit()
        rows = brain.search(self.conn, "python testing", k=5)
        self.assertEqual(rows[0]["node_id"], "new")

    def test_measured_success_breaks_ties(self):
        brain.save_node(self.conn, self.node(node_id="win", tags="deploy release"))
        brain.save_node(self.conn, self.node(node_id="lose", tags="deploy release"))
        brain.annotate_node(self.conn, "win", ["speed=5:min:10"])    # 5<=10 hit
        brain.annotate_node(self.conn, "lose", ["speed=50:min:10"])  # 50<=10 miss
        self._all_updated_at("2026-08-03T00:00:00+00:00")            # equalize recency
        rows = brain.search(self.conn, "deploy release", k=5)
        self.assertEqual(rows[0]["node_id"], "win")

    def test_context_budget_drops_whole_nodes(self):
        big = "z " * 1200
        for i in range(6):
            brain.save_node(self.conn, {"node_id": f"b{i}", "tags": "common tag",
                                        "context": big})
        rows = brain.search(self.conn, "common tag", k=6)
        text = brain.render_recall(rows, max_chars=3000)
        self.assertLess(text.count("###"), 6)   # not all six fit
        self.assertGreaterEqual(text.count("###"), 1)

    def _recall_hook(self, prompt):
        args = brain.build_parser().parse_args(["recall", "--from-hook", "--hook"])
        buf, old = io.StringIO(), sys.stdin
        sys.stdin = _FakeStdin(json.dumps(
            {"hook_event_name": "UserPromptSubmit", "user_prompt": prompt}))
        try:
            with contextlib.redirect_stdout(buf):
                brain.cmd_recall(args)
        finally:
            sys.stdin = old
        return json.loads(buf.getvalue())["hookSpecificOutput"]["additionalContext"]

    def test_low_signal_prompt_skipped(self):
        brain.save_node(self.conn, self.node(tags="oauth login"))
        self.assertEqual(self._recall_hook("ok"), "")           # <2 tokens → skip
        self.assertNotEqual(self._recall_hook("oauth login help"), "")

    # ---- Phase 1: provenance report + objective capture ------------------ #
    def test_metrics_report_splits_by_source(self):
        brain.save_node(self.conn, self.node(node_id="ms",
            metrics=[{"name": "speed", "value": 5, "target": 10}]))  # self, hit
        brain.annotate_node(self.conn, "ms", ["coverage=90:%:80:max"])  # measured, hit
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            brain.cmd_metrics(brain.build_parser().parse_args(["metrics"]))
        out = buf.getvalue()
        self.assertIn("measured", out)
        self.assertIn("self-reported", out)
        self.assertIn("speed", out)
        self.assertIn("coverage", out)

    def test_derive_transcript_metrics(self):
        tpath = os.path.join(self.tmp, "t.jsonl")
        lines = [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "let me edit"},
                {"type": "tool_use", "name": "Edit", "input": {}}]}},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "done"}]}},
        ]
        with open(tpath, "w") as f:
            for ln in lines:
                f.write(json.dumps(ln) + "\n")
        m = {x["name"]: x for x in brain.derive_transcript_metrics(tpath)}
        self.assertEqual(m["assistant_turns"]["value"], 2)
        self.assertEqual(m["file_edits"]["value"], 1)
        self.assertTrue(all(x["source"] == "measured" for x in m.values()))

    # ---- Phase 2: optional embeddings backend ---------------------------- #
    def test_vector_stored_on_save(self):
        brain.brain_embed = self.fake
        brain.save_node(self.conn, {"node_id": "vn", "context": "deploy release"})
        cnt = self.conn.execute(
            "SELECT COUNT(*) FROM node_vectors WHERE node_id='vn'").fetchone()[0]
        self.assertEqual(cnt, 1)

    def test_semantic_recall_finds_paraphrase(self):
        brain.brain_embed = self.fake
        # node talks about OAuth/login; query uses 'authentication/signin' — ZERO
        # shared tokens, so only the semantic path can retrieve it.
        brain.save_node(self.conn, {"node_id": "n-oauth",
                        "context": "set up OAuth login flow for the web app"})
        rows = brain.search(self.conn, "how to add user authentication and signin", k=5)
        self.assertIn("n-oauth", [r["node_id"] for r in rows])

    def test_embeddings_off_misses_paraphrase(self):
        # same paraphrase, embeddings OFF (default) → not found (keyword-only)
        brain.save_node(self.conn, {"node_id": "n2",
                        "context": "set up OAuth login flow for the web app"})
        rows = brain.search(self.conn, "user authentication and signin", k=5)
        self.assertNotIn("n2", [r["node_id"] for r in rows])

    def test_embed_failure_falls_back_to_keyword(self):
        def boom(_):
            raise RuntimeError("provider down")
        brain.brain_embed = types.SimpleNamespace(
            enabled=lambda: True, model_name=lambda: "x",
            provider_info=lambda: "x", embed=boom)
        # save must still succeed (embed is best-effort) ...
        brain.save_node(self.conn, self.node(tags="oauth login"))
        # ... and keyword recall must still work despite the failing provider.
        rows = brain.search(self.conn, "oauth login help", k=5)
        self.assertIn("2026-08-03-task", [r["node_id"] for r in rows])

    def test_reindex_backfills_vectors(self):
        brain.brain_embed = self.fake
        brain.save_node(self.conn, {"node_id": "r1", "context": "deploy release"})
        self.conn.execute("DELETE FROM node_vectors")
        self.conn.commit()
        brain.cmd_reindex(brain.build_parser().parse_args(["reindex"]))
        c = brain.connect()
        cnt = c.execute("SELECT COUNT(*) FROM node_vectors").fetchone()[0]
        c.close()
        self.assertEqual(cnt, 1)

    # ---- Phase 2: project scoping ---------------------------------------- #
    def test_save_stamps_current_project(self):
        brain.save_node(self.conn, {"node_id": "p1", "context": "hi"})  # no project
        self.assertEqual(brain.load_node(self.conn, "p1")["project"], "proj-a")

    def test_recall_scoped_to_project(self):
        brain.save_node(self.conn, {"node_id": "a", "tags": "billing invoice",
                                    "context": "A work", "project": "proj-a"})
        brain.save_node(self.conn, {"node_id": "b", "tags": "billing invoice",
                                    "context": "B work", "project": "proj-b"})
        ids = [r["node_id"] for r in
               brain.search(self.conn, "billing invoice", project="proj-a", scope="project")]
        self.assertIn("a", ids)
        self.assertNotIn("b", ids)   # other project's node is invisible

    def test_global_nodes_surface_everywhere(self):
        brain.save_node(self.conn, {"node_id": "g", "tags": "billing invoice",
                                    "context": "shared", "project": "*"})
        ids = [r["node_id"] for r in
               brain.search(self.conn, "billing invoice", project="proj-a", scope="project")]
        self.assertIn("g", ids)

    def test_all_projects_scope_sees_everything(self):
        brain.save_node(self.conn, {"node_id": "a", "tags": "billing",
                                    "context": "A", "project": "proj-a"})
        brain.save_node(self.conn, {"node_id": "b", "tags": "billing",
                                    "context": "B", "project": "proj-b"})
        ids = [r["node_id"] for r in brain.search(self.conn, "billing", scope="all")]
        self.assertEqual({"a", "b"}, set(ids))

    def test_annotate_preserves_project(self):
        brain.save_node(self.conn, {"node_id": "z", "context": "x", "project": "proj-b"})
        brain.annotate_node(self.conn, "z", ["speed=5"])
        self.assertEqual(brain.load_node(self.conn, "z")["project"], "proj-b")

    # ---- Phase 2: redaction ---------------------------------------------- #
    def test_redaction_masks_secrets(self):
        brain.save_node(self.conn, {"node_id": "sec",
            "context": "deploy key AKIAIOSFODNN7EXAMPLE and password: hunter2secret here",
            "lessons_learned": "token sk-abcdefghijklmnopqrstuvwxyz012345 leaked"})
        n = brain.load_node(self.conn, "sec")
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", n["context"])
        self.assertNotIn("hunter2secret", n["context"])
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", n["lessons_learned"])
        self.assertIn("[REDACTED]", n["context"])

    def test_redaction_can_be_disabled(self):
        os.environ["BRAIN_REDACT"] = "off"
        brain.save_node(self.conn, {"node_id": "raw",
                                    "context": "key AKIAIOSFODNN7EXAMPLE"})
        self.assertIn("AKIAIOSFODNN7EXAMPLE",
                      brain.load_node(self.conn, "raw")["context"])

    def test_migration_adds_project_to_old_store(self):
        # Reproduce a pre-scoping (v4) store: nodes table without a project column.
        c = self.conn
        c.execute("DROP TABLE nodes")
        c.execute("CREATE TABLE nodes (node_id TEXT PRIMARY KEY, tags TEXT, "
                  "context TEXT, actions_taken TEXT, user_feedback TEXT, "
                  "metrics TEXT DEFAULT '[]', lessons_learned TEXT, "
                  "related_nodes TEXT DEFAULT '[]', pending_items TEXT DEFAULT '[]', "
                  "status TEXT DEFAULT 'active', created_at TEXT, updated_at TEXT)")
        c.execute("PRAGMA user_version=4")
        c.commit()
        brain.init_db(c)   # must migrate without 'no such column: project'
        cols = [r[1] for r in c.execute("PRAGMA table_info(nodes)")]
        self.assertIn("project", cols)
        self.assertEqual(c.execute("PRAGMA user_version").fetchone()[0],
                         brain.SCHEMA_VERSION)

    def test_redaction_keeps_ordinary_text(self):
        brain.save_node(self.conn, {"node_id": "ok",
                                    "context": "we refactored the auth module cleanly"})
        self.assertEqual(brain.load_node(self.conn, "ok")["context"],
                         "we refactored the auth module cleanly")

    # ---- Phase 2: memory hygiene ----------------------------------------- #
    def _dup(self, nid, project="proj-a"):
        return {"node_id": nid, "tags": "billing invoice reconcile ledger",
                "context": "reconcile invoices nightly by ledger", "project": project}

    def test_dedup_consolidates_near_duplicates(self):
        brain.save_node(self.conn, self._dup("d1"))
        brain.save_node(self.conn, self._dup("d2"))
        brain.cmd_dedup(brain.build_parser().parse_args(["dedup", "--apply"]))
        c = brain.connect()
        statuses = {r["node_id"]: r["status"] for r in
                    c.execute("SELECT node_id, status FROM nodes WHERE node_id IN ('d1','d2')")}
        ids = [r["node_id"] for r in
               brain.search(c, "billing invoice reconcile", project="proj-a", scope="project")]
        c.close()
        self.assertEqual(sorted(statuses.values()), ["active", "merged"])
        self.assertEqual(len(ids), 1)   # only the canonical survives recall

    def test_dedup_respects_project_boundary(self):
        brain.save_node(self.conn, self._dup("x", project="proj-a"))
        brain.save_node(self.conn, self._dup("y", project="proj-b"))  # identical, other project
        brain.cmd_dedup(brain.build_parser().parse_args(["dedup", "--apply"]))
        c = brain.connect()
        statuses = {r["node_id"]: r["status"] for r in
                    c.execute("SELECT node_id, status FROM nodes WHERE node_id IN ('x','y')")}
        c.close()
        self.assertEqual(statuses, {"x": "active", "y": "active"})  # never merged across projects

    def test_merge_suggests_related_clusters(self):
        brain.save_node(self.conn, {"node_id": "m1", "tags": "deploy release ci",
                                    "context": "deploy via pipeline", "project": "proj-a"})
        brain.save_node(self.conn, {"node_id": "m2", "tags": "deploy release ci",
                                    "context": "release through pipeline", "project": "proj-a"})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            brain.cmd_merge(brain.build_parser().parse_args(["merge"]))
        out = buf.getvalue()
        self.assertIn("m1", out)
        self.assertIn("m2", out)

    def test_archive_retires_old_and_hides_from_recall(self):
        brain.save_node(self.conn, {"node_id": "old", "tags": "legacy work",
                                    "context": "old", "project": "proj-a"})
        brain.save_node(self.conn, {"node_id": "fresh", "tags": "legacy work",
                                    "context": "recent", "project": "proj-a"})
        self.conn.execute("UPDATE nodes SET updated_at='2020-01-01T00:00:00+00:00' "
                          "WHERE node_id='old'")
        self.conn.commit()
        brain.cmd_archive(brain.build_parser().parse_args(["archive", "--older-than", "30"]))
        c = brain.connect()
        st = {r["node_id"]: r["status"] for r in
              c.execute("SELECT node_id, status FROM nodes WHERE node_id IN ('old','fresh')")}
        ids = [r["node_id"] for r in
               brain.search(c, "legacy work", project="proj-a", scope="project")]
        c.close()
        self.assertEqual(st, {"old": "archived", "fresh": "active"})
        self.assertNotIn("old", ids)

    def test_archive_keeps_measured_successful(self):
        brain.save_node(self.conn, {"node_id": "won", "tags": "legacy",
                                    "context": "old but good", "project": "proj-a"})
        brain.annotate_node(self.conn, "won", ["speed=5:min:10"])   # measured hit
        self.conn.execute("UPDATE nodes SET updated_at='2020-01-01T00:00:00+00:00' "
                          "WHERE node_id='won'")
        self.conn.commit()
        brain.cmd_archive(brain.build_parser().parse_args(["archive", "--older-than", "30"]))
        c = brain.connect()
        status = c.execute("SELECT status FROM nodes WHERE node_id='won'").fetchone()[0]
        c.close()
        self.assertEqual(status, "active")   # spared because it hit its target


def _concept_vec(text):
    """Deterministic stand-in for an embedding model: same concept → same vector,
    so paraphrases (different words, same idea) land close in cosine space."""
    t = (text or "").lower()
    auth = any(w in t for w in
               ("oauth", "auth", "login", "authentication", "signin", "credential"))
    deploy = any(w in t for w in ("deploy", "release", "ship", "rollout"))
    return [1.0 if auth else 0.0, 1.0 if deploy else 0.0, 0.1, 0.0]


class _FakeStdin:
    """Minimal stdin stand-in for read_stdin_json (non-tty, single read)."""
    def __init__(self, data):
        self._data = data
    def isatty(self):
        return False
    def read(self):
        return self._data


if __name__ == "__main__":
    unittest.main(verbosity=2)
