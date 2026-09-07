from typing import Dict, Any

from evoagentx.core.logging import logger
from .base import EmbeddingProvider, BaseEmbeddingWrapper

__all__ = [
    'OpenAIEmbeddingWrapper',
    'AzureOpenAIEmbeddingWrapper',
    'HuggingFaceEmbeddingWrapper',
    'OllamaEmbeddingWrapper',
    'VoyageEmbeddingWrapper',
    'EmbeddingFactory',
    'BaseEmbedding',
    'EmbeddingProvider'
]

class EmbeddingFactory:
    """Factory for creating embedding models based on configuration."""
    
    def create(
        self,
        provider: EmbeddingProvider,
        model_config: Dict[str, Any] = None
    ) -> BaseEmbeddingWrapper:
        """Create an embedding model based on the provider and configuration.
        
        Args:
            provider (EmbeddingProvider): The embedding provider (e.g., OpenAI, HuggingFace, Ollama).
            model_config (Dict[str, Any], optional): Configuration for the embedding model.
            
        Returns:
            BaseEmbeddingWrapper: A LlamaIndex-compatible embedding model wrapper.
            
        Raises:
            ValueError: If the provider or configuration is invalid.
        """
        model_config = model_config or {}
        model_config.pop("provider")    # filter the provider key

        if provider == EmbeddingProvider.OPENAI:
            from .openai_embedding import OpenAIEmbeddingWrapper

            wrapper = OpenAIEmbeddingWrapper(**model_config)
        elif provider == EmbeddingProvider.AZURE_OPENAI:
            from .azure_openai_embedding import AzureOpenAIEmbeddingWrapper

            wrapper = AzureOpenAIEmbeddingWrapper(**model_config)
        elif provider == EmbeddingProvider.HUGGINGFACE:
            from .huggingface_embedding import HuggingFaceEmbeddingWrapper

            wrapper = HuggingFaceEmbeddingWrapper(**model_config)
        elif provider == EmbeddingProvider.OLLAMA:
            from .ollama_embedding import OllamaEmbeddingWrapper

            wrapper = OllamaEmbeddingWrapper(**model_config)
        elif provider == EmbeddingProvider.VOYAGE:
            from .voyage import VoyageEmbeddingWrapper

            wrapper = VoyageEmbeddingWrapper(**model_config)
        else:
            raise ValueError(f"Unsupported embedding provider: {provider}")
        
        logger.info(f"Created embedding model for provider: {provider}")
        return wrapper


_WRAPPERS = {
    "OpenAIEmbeddingWrapper": (".openai_embedding", "OpenAIEmbeddingWrapper"),
    "AzureOpenAIEmbeddingWrapper": (".azure_openai_embedding", "AzureOpenAIEmbeddingWrapper"),
    "HuggingFaceEmbeddingWrapper": (".huggingface_embedding", "HuggingFaceEmbeddingWrapper"),
    "OllamaEmbeddingWrapper": (".ollama_embedding", "OllamaEmbeddingWrapper"),
    "VoyageEmbeddingWrapper": (".voyage", "VoyageEmbeddingWrapper"),
}


def __getattr__(name: str):
    from importlib import import_module

    export = _WRAPPERS.get(name)
    if export is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = export
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value
