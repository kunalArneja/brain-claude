"""Optional embeddings backend for Brain-Claude.

Strictly opt-in and dependency-free (Python stdlib only). Disabled by default —
when off, Brain-Claude uses keyword (FTS5) recall exactly as before. When on, it
adds *semantic* recall (paraphrase matching) on top of keyword, and degrades back
to keyword on any error, so the reliability contract still holds.

Enable via environment:

  BRAIN_EMBED=api          POST to an OpenAI-compatible embeddings endpoint
    BRAIN_EMBED_URL          e.g. https://api.voyageai.com/v1/embeddings
                                  https://api.openai.com/v1/embeddings
                                  http://localhost:11434/api/embed   (Ollama)
    BRAIN_EMBED_MODEL        e.g. voyage-3 | text-embedding-3-small | nomic-embed-text
    BRAIN_EMBED_KEY          bearer token (omit for local servers)

  BRAIN_EMBED=command      run a local command that reads text on stdin and prints
    BRAIN_EMBED_CMD          a JSON array of floats (wire up sentence-transformers,
                             llama.cpp, etc. — fully offline, still zero Python deps)

  BRAIN_EMBED_TIMEOUT      seconds per request (default 1.5, deliberately under the
                          hook budget so a slow endpoint degrades to keyword recall
                          rather than stalling a prompt).

Every public function returns None / a safe default on failure and never raises.
"""

import json
import os
import subprocess
import urllib.request


def mode():
    return os.environ.get("BRAIN_EMBED", "off").strip().lower()


def enabled():
    return mode() in ("api", "command")


def model_name():
    return os.environ.get("BRAIN_EMBED_MODEL", "") or mode()


def _timeout():
    try:
        return float(os.environ.get("BRAIN_EMBED_TIMEOUT", "1.5"))
    except ValueError:
        return 1.5


def provider_info():
    if not enabled():
        return "off (keyword recall)"
    if mode() == "api":
        return f"api {os.environ.get('BRAIN_EMBED_URL', '?')} model={model_name()}"
    return f"command {os.environ.get('BRAIN_EMBED_CMD', '?')}"


def embed(text):
    """Return a list[float] embedding for text, or None on any failure."""
    if not enabled() or not text:
        return None
    try:
        if mode() == "api":
            return _embed_api(text)
        if mode() == "command":
            return _embed_command(text)
    except Exception:
        return None
    return None


def _extract_vector(obj):
    """Pull a single embedding out of the common response shapes."""
    if isinstance(obj, list):  # bare array of floats
        return [float(x) for x in obj]
    if isinstance(obj, dict):
        # OpenAI / Voyage: {"data": [{"embedding": [...]}]}
        data = obj.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            emb = data[0].get("embedding")
            if isinstance(emb, list):
                return [float(x) for x in emb]
        # Ollama /api/embeddings: {"embedding": [...]}
        if isinstance(obj.get("embedding"), list):
            return [float(x) for x in obj["embedding"]]
        # Ollama /api/embed: {"embeddings": [[...]]}
        embs = obj.get("embeddings")
        if isinstance(embs, list) and embs and isinstance(embs[0], list):
            return [float(x) for x in embs[0]]
    return None


def _embed_api(text):
    url = os.environ.get("BRAIN_EMBED_URL", "")
    if not url:
        return None
    body = json.dumps({"model": model_name(), "input": text}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    key = os.environ.get("BRAIN_EMBED_KEY", "")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    with urllib.request.urlopen(req, timeout=_timeout()) as resp:
        obj = json.loads(resp.read().decode())
    return _extract_vector(obj)


def _embed_command(text):
    cmd = os.environ.get("BRAIN_EMBED_CMD", "")
    if not cmd:
        return None
    p = subprocess.run(cmd, shell=True, input=text.encode(),
                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                       timeout=_timeout())
    if p.returncode != 0:
        return None
    return _extract_vector(json.loads(p.stdout.decode() or "null"))
