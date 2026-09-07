"""Public RAG API with implementations loaded on first use."""

from __future__ import annotations

from importlib import import_module

__all__ = [
    "RAGEngine",
    "LLamaIndexReader",
    "MultimodalReader",
    "SimpleChunker",
    "SemanticChunker",
    "HierarchicalChunker",
    "OpenAIEmbeddingWrapper",
    "VoyageEmbeddingWrapper",
    "VectorIndexing",
    "VectorRetriever",
    "SimpleReranker",
    "RAGConfig",
    "TextChunk",
    "ImageChunk",
    "Chunk",
    "Corpus",
    "RagResult",
    "ChunkMetadata",
    "Query",
]

_EXPORTS = {
    "RAGEngine": (".rag", "RAGEngine"),
    "LLamaIndexReader": (".readers", "LLamaIndexReader"),
    "MultimodalReader": (".readers", "MultimodalReader"),
    "SimpleChunker": (".chunkers", "SimpleChunker"),
    "SemanticChunker": (".chunkers", "SemanticChunker"),
    "HierarchicalChunker": (".chunkers", "HierarchicalChunker"),
    "OpenAIEmbeddingWrapper": (".embeddings", "OpenAIEmbeddingWrapper"),
    "VoyageEmbeddingWrapper": (".embeddings", "VoyageEmbeddingWrapper"),
    "VectorIndexing": (".indexings", "VectorIndexing"),
    "VectorRetriever": (".retrievers", "VectorRetriever"),
    "SimpleReranker": (".postprocessors", "SimpleReranker"),
    "RAGConfig": (".rag_config", "RAGConfig"),
    "TextChunk": (".schema", "TextChunk"),
    "ImageChunk": (".schema", "ImageChunk"),
    "Chunk": (".schema", "Chunk"),
    "Corpus": (".schema", "Corpus"),
    "RagResult": (".schema", "RagResult"),
    "ChunkMetadata": (".schema", "ChunkMetadata"),
    "Query": (".schema", "Query"),
}


def __getattr__(name: str):
    export = _EXPORTS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attribute = export
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
