"""Shared embedding module. Used by both indexer.py and hook.py."""

from pathlib import Path
import numpy as np

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# Pin the model cache to a stable path next to this module so the weights persist
# (the fastembed default is $TMPDIR, which macOS can purge).
CACHE_DIR = Path(__file__).parent / ".fastembed_cache"

_model = None

def get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _model = TextEmbedding(MODEL_NAME, cache_dir=str(CACHE_DIR))
    return _model

def embed(texts):
    """Embed a list of strings. Returns an (N, 384) float32 ndarray, L2-normalized."""
    model = get_model()
    vectors = np.array(list(model.embed(texts)), dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1
    return vectors / norms
