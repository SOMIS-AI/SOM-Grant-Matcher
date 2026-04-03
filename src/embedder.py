"""
Semantic Embedding Engine
=========================
Generates dense vector embeddings for faculty keyword profiles and grant text,
then uses cosine similarity to find faculty whose research is semantically
relevant to a grant — even when exact keywords don't overlap.

Model: all-MiniLM-L6-v2
  - ~80 MB, fast on CPU, strong general semantic understanding
  - Good sub-specialty discrimination for biomedical research text
  - Note: pritamdeka/S-PubMedBert-MS-MARCO was tested (v4) but produced
    uniformly high scores (median 0.83+) with zero discrimination between
    sub-specialties — all faculty matched all grants. MiniLM's general-purpose
    embeddings produce much better spread (max 0.40-0.57, median 0.06-0.25)
    making it far more useful for distinguishing relevant matches.

Strategy:
  - Faculty embedding = their full keyword list joined as a sentence
  - Grant embedding = title + first 400 chars of synopsis
  - Similarity threshold configurable (default 0.45 — tunable in config.yaml)
  - Runs AFTER keyword matching as a second pass to catch missed faculty
  - Results labeled match_type="semantic" to distinguish from keyword matches

Graceful degradation:
  - If sentence-transformers is not installed, all functions are no-ops
  - If model download fails, scrape proceeds without embeddings
  - Embeddings cached in faculty_profiles.json — regenerated only when stale
"""

import logging
import os
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"
_model = None          # lazy-loaded singleton
_model_load_tried = False

# ── Model loading ──────────────────────────────────────────────────────────────

def _load_model():
    """
    Load the sentence-transformers model once per process, with two caching layers:

    Layer 1 — Module-level singleton (_model):
        Once loaded, the model object is reused for every matching cycle within
        the same running process. No reload between cycles.

    Layer 2 — Filesystem cache on Railway volume:
        MODEL_CACHE_DIR (default /mnt/data/model_cache, set via Railway Variable)
        persists the downloaded weights across deploys. First run downloads ~80 MB
        from HuggingFace; all subsequent runs load from disk in <1s.

    Layer 3 — HuggingFace env vars:
        TRANSFORMERS_CACHE and HF_HOME are pointed at the same volume directory
        so the HuggingFace Hub library also skips network HEAD requests when the
        model is already cached locally.
    """
    global _model, _model_load_tried
    if _model_load_tried:
        return _model
    _model_load_tried = True
    try:
        from sentence_transformers import SentenceTransformer

        # Resolve cache directory — prefer Railway volume mount, fall back to local
        cache_dir = os.environ.get("MODEL_CACHE_DIR", "/mnt/data/model_cache")
        Path(cache_dir).mkdir(parents=True, exist_ok=True)

        # Point HuggingFace Hub at the same directory so it uses the on-disk
        # cache and skips the dozens of HEAD requests seen in logs
        os.environ.setdefault("TRANSFORMERS_CACHE", cache_dir)
        os.environ.setdefault("HF_HOME", cache_dir)
        os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", cache_dir)

        # Determine whether model files are already on disk
        model_marker = Path(cache_dir) / "models--sentence-transformers--all-MiniLM-L6-v2"
        already_cached = model_marker.exists()

        logger.info(
            f"Loading embedding model '{MODEL_NAME}' "
            f"({'from disk cache' if already_cached else 'downloading ~80 MB — first run only'})..."
        )
        t0 = time.time()

        _model = SentenceTransformer(
            MODEL_NAME,
            cache_folder=cache_dir,
            # local_files_only=True prevents any HuggingFace network calls once
            # the model is cached — set only when we're confident it's on disk
            local_files_only=already_cached,
        )
        elapsed = time.time() - t0
        logger.info(
            f"Embedding model loaded in {elapsed:.1f}s "
            f"({'disk cache' if already_cached else 'downloaded and cached'})"
        )
    except ImportError:
        logger.warning(
            "sentence-transformers not installed — semantic matching disabled. "
            "Add 'sentence-transformers' to requirements.txt to enable."
        )
    except Exception as e:
        logger.warning(f"Could not load embedding model: {e} — semantic matching disabled.")
    return _model


def is_available() -> bool:
    """Return True if the embedding model loaded successfully."""
    return _load_model() is not None


# Embedding format version — bump this when faculty_to_text() changes so that
# stale embeddings (generated with an older text format) are auto-regenerated.
# The version is stored in each faculty profile alongside the embedding.
EMBEDDING_VERSION = 5    # v1 = pipe-separated, v2 = sentence-style, v3 = force regen, v4 = PubMedBert (too broad), v5 = back to MiniLM + threshold 0.40


# ── Text preparation ───────────────────────────────────────────────────────────

def faculty_to_text(faculty: dict) -> str:
    """
    Build a natural-language sentence from a faculty member's profile for embedding.

    v2 change: Previously joined keywords with " | " and "." separators, which
    produced embeddings in a very different vector space from narrative grant text.
    Diagnostic data showed max cosine similarity of only 0.40–0.50 as a result.

    Now produces a sentence like:
      "Dr. Jane Smith is a researcher in the Department of Biochemistry.
       Their research interests include cancer immunotherapy, T-cell biology,
       checkpoint inhibitors, and tumor microenvironment."

    This aligns with how grant descriptions are written, dramatically improving
    cosine similarity scores for genuine matches.
    """
    parts = []

    name = faculty.get("name", "")
    dept = faculty.get("department", "")

    # Opening sentence with name and department
    if name and dept:
        parts.append(f"{name} is a researcher in {dept}.")
    elif name:
        parts.append(f"{name} is a biomedical researcher.")
    elif dept:
        parts.append(f"A researcher in {dept}.")

    # Keywords as a natural-language research interests sentence
    keywords = faculty.get("keywords") or []
    if keywords:
        # Take up to 40 keywords — enough for semantic signal without flooding
        kws = keywords[:40]
        if len(kws) == 1:
            parts.append(f"Their research focuses on {kws[0]}.")
        elif len(kws) == 2:
            parts.append(f"Their research interests include {kws[0]} and {kws[1]}.")
        else:
            # "a, b, c, and d" format
            kw_str = ", ".join(kws[:-1]) + ", and " + kws[-1]
            parts.append(f"Their research interests include {kw_str}.")

    return " ".join(parts).strip()


def grant_to_text(grant: dict) -> str:
    """
    Build a text representation of a grant for embedding.
    Uses title + synopsis (truncated to keep inference fast).
    """
    title = grant.get("title", "")
    synopsis = grant.get("synopsis", "") or ""
    agency = grant.get("agency", "")
    # ~400 chars of synopsis is enough for semantic signal
    text = f"{title}. {agency}. {synopsis[:400]}"
    return text.strip()


# ── Embedding generation ───────────────────────────────────────────────────────

def embed_texts(texts: list[str]) -> "np.ndarray | None":
    """
    Generate embeddings for a list of texts.
    Returns numpy array of shape (N, dim), or None if model unavailable.
    Batches automatically — safe to call with thousands of texts.
    """
    model = _load_model()
    if model is None or not texts:
        return None
    try:
        # show_progress_bar=False keeps logs clean; batch_size=64 is efficient on CPU
        embeddings = model.encode(
            texts,
            batch_size=64,
            show_progress_bar=False,
            normalize_embeddings=True,   # L2-normalize → cosine sim = dot product
            convert_to_numpy=True,
        )
        return embeddings
    except Exception as e:
        logger.warning(f"Embedding generation failed: {e}")
        return None


def embed_faculty_batch(faculty_list: list[dict]) -> bool:
    """
    Generate and store embeddings for all faculty in-place.
    Embedding stored as faculty['embedding'] (list of floats for JSON serialisation).
    Also stores faculty['embedding_version'] to detect stale embeddings.

    If the embedding version has changed (e.g. faculty_to_text was updated),
    ALL embeddings are regenerated even if they already exist.

    Returns True if embeddings were generated, False if model unavailable.
    """
    model = _load_model()
    if model is None:
        return False

    # Check if any embeddings are stale (wrong version or missing)
    stale_count = sum(
        1 for f in faculty_list
        if not f.get("embedding") or f.get("embedding_version") != EMBEDDING_VERSION
    )
    total = len(faculty_list)

    if stale_count == 0:
        logger.info(
            f"All {total} faculty embeddings are current (v{EMBEDDING_VERSION}). "
            f"Skipping regeneration."
        )
        return True

    if stale_count < total:
        logger.info(
            f"Regenerating embeddings: {stale_count}/{total} faculty have stale or "
            f"missing embeddings (current version: v{EMBEDDING_VERSION})"
        )
    else:
        logger.info(
            f"Generating embeddings for all {total} faculty "
            f"(version v{EMBEDDING_VERSION})..."
        )

    texts = [faculty_to_text(f) for f in faculty_list]
    t0 = time.time()
    embeddings = embed_texts(texts)
    if embeddings is None:
        return False

    for faculty, emb in zip(faculty_list, embeddings):
        faculty["embedding"] = emb.tolist()
        faculty["embedding_version"] = EMBEDDING_VERSION

    elapsed = time.time() - t0
    logger.info(f"Embeddings generated in {elapsed:.1f}s ({elapsed/len(texts)*1000:.1f}ms/faculty)")
    return True


# ── Similarity search ──────────────────────────────────────────────────────────

def find_semantic_matches(
    grant: dict,
    faculty_list: list[dict],
    threshold: float = 0.45,
    already_matched_names: set | None = None,
) -> list[dict]:
    """
    Find faculty semantically similar to this grant.
    Only returns faculty NOT already found by keyword matching (additive pass).

    Args:
        grant: grant dict with title/synopsis
        faculty_list: all faculty (must have 'embedding' field)
        threshold: minimum cosine similarity to consider a match (0-1)
        already_matched_names: set of faculty names already found by keyword match

    Returns:
        list of dicts with keys:
          faculty_name, faculty_url, faculty_department, faculty_email,
          similarity_score, match_type="semantic"
    """
    model = _load_model()
    if model is None:
        return []

    already_matched_names = already_matched_names or set()

    # Only consider faculty with embeddings
    candidates = [f for f in faculty_list if f.get("embedding")]
    if not candidates:
        return []

    # Embed the grant text
    grant_text = grant_to_text(grant)
    grant_emb = embed_texts([grant_text])
    if grant_emb is None:
        return []
    grant_vec = grant_emb[0]  # shape (dim,)

    # Stack all faculty embeddings into a matrix for fast batch dot product
    faculty_matrix = np.array([f["embedding"] for f in candidates], dtype=np.float32)
    # Since embeddings are L2-normalised, dot product = cosine similarity
    similarities = faculty_matrix @ grant_vec

    matches = []
    for fac, sim in zip(candidates, similarities):
        if sim < threshold:
            continue
        if fac.get("name") in already_matched_names:
            continue   # already surfaced by keyword matching
        matches.append({
            "faculty_name":       fac.get("name", ""),
            "faculty_url":        fac.get("url", ""),
            "faculty_department": fac.get("department", ""),
            "faculty_email":      fac.get("email", ""),
            "similarity_score":   float(sim),
            "match_type":         "semantic",
        })

    # Sort by similarity descending
    matches.sort(key=lambda m: m["similarity_score"], reverse=True)
    return matches
