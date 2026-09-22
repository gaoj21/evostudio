"""Public tool API with optional integrations loaded on first use.

Importing core EvoAgentX types must not require every browser, database,
search, and image integration.  The package therefore resolves each public
toolkit only when that attribute is requested.
"""

from __future__ import annotations

from importlib import import_module

from .tool import Tool, Toolkit

__all__ = [
    "Tool",
    "Toolkit",
    "DockerInterpreterToolkit",
    "PythonInterpreterToolkit",
    "GoogleSearchToolkit",
    "GoogleFreeSearchToolkit",
    "DDGSSearchToolkit",
    "WikipediaSearchToolkit",
    "BrowserToolkit",
    "MCPToolkit",
    "RequestToolkit",
    "ArxivToolkit",
    "BrowserUseToolkit",
    "GoogleMapsToolkit",
    "TelegramToolkit",
    "GmailToolkit",
    "MongoDBToolkit",
    "PostgreSQLToolkit",
    "FileStorageHandler",
    "LocalStorageHandler",
    "SupabaseStorageHandler",
    "StorageToolkit",
    "FluxImageGenerationEditTool",
    "FluxImageGenerationToolkit",
    "OpenAIImageToolkit",
    "OpenRouterImageAnalysisTool",
    "OpenRouterImageGenerationEditTool",
    "OpenRouterImageToolkit",
    "CMDToolkit",
    "RSSToolkit",
    "FileToolkit",
    "SerperAPIToolkit",
    "SerpAPIToolkit",
    "ExaSearchToolkit",
    "SkillToolkit",
    "ResearchToolkit",
]

_EXPORTS = {
    "DockerInterpreterToolkit": (".interpreter_docker", "DockerInterpreterToolkit"),
    "PythonInterpreterToolkit": (".interpreter_python", "PythonInterpreterToolkit"),
    "GoogleSearchToolkit": (".search_google", "GoogleSearchToolkit"),
    "GoogleFreeSearchToolkit": (".search_google_f", "GoogleFreeSearchToolkit"),
    "DDGSSearchToolkit": (".search_ddgs", "DDGSSearchToolkit"),
    "WikipediaSearchToolkit": (".search_wiki", "WikipediaSearchToolkit"),
    "BrowserToolkit": (".browser_tool", "BrowserToolkit"),
    "MCPToolkit": (".mcp", "MCPToolkit"),
    "RequestToolkit": (".request", "RequestToolkit"),
    "ArxivToolkit": (".request_arxiv", "ArxivToolkit"),
    "BrowserUseToolkit": (".browser_use", "BrowserUseToolkit"),
    "GoogleMapsToolkit": (".google_maps_tool", "GoogleMapsToolkit"),
    "TelegramToolkit": (".telegram_tools", "TelegramToolkit"),
    "GmailToolkit": (".gmail_tools", "GmailToolkit"),
    "MongoDBToolkit": (".database_mongodb", "MongoDBToolkit"),
    "PostgreSQLToolkit": (".database_postgresql", "PostgreSQLToolkit"),
    "FileStorageHandler": (".storage_handler", "FileStorageHandler"),
    "LocalStorageHandler": (".storage_handler", "LocalStorageHandler"),
    "SupabaseStorageHandler": (".storage_handler", "SupabaseStorageHandler"),
    "StorageToolkit": (".storage_file", "StorageToolkit"),
    "FluxImageGenerationEditTool": (
        ".image_tools.flux_image_tools.image_generation_edit",
        "FluxImageGenerationEditTool",
    ),
    "FluxImageGenerationToolkit": (
        ".image_tools.flux_image_tools.toolkit",
        "FluxImageGenerationToolkit",
    ),
    "OpenAIImageToolkit": (
        ".image_tools.openai_image_tools.toolkit",
        "OpenAIImageToolkit",
    ),
    "OpenRouterImageAnalysisTool": (
        ".image_tools.openrouter_image_tools.image_analysis",
        "ImageAnalysisTool",
    ),
    "OpenRouterImageGenerationEditTool": (
        ".image_tools.openrouter_image_tools.image_generation",
        "OpenRouterImageGenerationEditTool",
    ),
    "OpenRouterImageToolkit": (
        ".image_tools.openrouter_image_tools.toolkit",
        "OpenRouterImageToolkit",
    ),
    "CMDToolkit": (".cmd_toolkit", "CMDToolkit"),
    "RSSToolkit": (".rss_feed", "RSSToolkit"),
    "FileToolkit": (".file_tool", "FileToolkit"),
    "SerperAPIToolkit": (".search_serperapi", "SerperAPIToolkit"),
    "SerpAPIToolkit": (".search_serpapi", "SerpAPIToolkit"),
    "ExaSearchToolkit": (".search_exa", "ExaSearchToolkit"),
    "SkillToolkit": (".skill_tool", "SkillToolkit"),
    "ResearchToolkit": (".research_tools", "ResearchToolkit"),
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
