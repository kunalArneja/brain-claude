# Semantic recall (optional embeddings)

By default Brain-Claude uses **keyword** recall (FTS5) — no dependencies, fully
offline. You can optionally turn on **semantic** recall so paraphrases match
(a query about "authentication" finds a node about "OAuth login"). It's strictly
opt-in, stdlib-only, and **degrades back to keyword on any failure** — a slow or
broken provider never blocks a prompt.

## Turn it on

Pick one provider and set environment variables (e.g. in your shell profile, or a
project `.env` your hooks source).

**A) Hosted / local HTTP endpoint** (OpenAI-, Voyage-, or Ollama-compatible):

```sh
export BRAIN_EMBED=api
export BRAIN_EMBED_URL=https://api.voyageai.com/v1/embeddings   # or your endpoint
export BRAIN_EMBED_MODEL=voyage-3
export BRAIN_EMBED_KEY=sk-...                                    # omit for local servers
```

Examples of `BRAIN_EMBED_URL` / `BRAIN_EMBED_MODEL`:
- Voyage: `https://api.voyageai.com/v1/embeddings` · `voyage-3`
- OpenAI: `https://api.openai.com/v1/embeddings` · `text-embedding-3-small`
- Ollama (local): `http://localhost:11434/api/embed` · `nomic-embed-text`

**B) Local command** (fully offline; wire up any model, still zero Python deps):

```sh
export BRAIN_EMBED=command
export BRAIN_EMBED_CMD="python3 /path/to/your_embedder.py"
```

The command reads text on **stdin** and prints a **JSON array of floats** on stdout.

Optional: `BRAIN_EMBED_TIMEOUT` (seconds per request, default `1.5` — kept under the
hook budget so a slow endpoint falls back to keyword instead of stalling a prompt).

## After enabling

```sh
python3 bin/brain.py embed "a test sentence"   # verify the provider is reachable
python3 bin/brain.py reindex                    # backfill vectors for existing nodes
python3 bin/brain.py status                     # shows provider + "vectors N/N indexed"
python3 bin/brain.py doctor                     # PASS/WARN on the embeddings provider
```

New nodes are embedded automatically on save. Recall becomes **hybrid**: keyword and
semantic candidates are merged, so you gain paraphrase matching without losing exact
keyword hits. Turn it off any time by unsetting `BRAIN_EMBED` — recall returns to
keyword-only and existing vectors are simply ignored.
