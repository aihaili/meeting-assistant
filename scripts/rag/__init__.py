"""plaud RAG module: SQLite (FTS5 over jieba + sqlite-vec) hybrid retrieval.

Embeddings come from ONNX INT8 models (``bge-small-zh-v1.5`` by default, ``bge-m3`` as a
multilingual alternative) — see ``embedder.py`` for why GGUF and WeMM-Embedding-2B were
both rejected. Multi-corpus search lives in ``multi_index.py``, incremental folder sync in
``sync.py``, the flat event table in ``events.py``/``event_store.py``/``extract.py``.
"""

__all__ = ["chunking", "embedder", "rag_core"]
