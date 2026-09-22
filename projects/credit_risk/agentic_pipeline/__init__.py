"""Agentic credit-risk alert pipeline — see README.md in this package."""

from .obligors import Obligor, ObligorRegistry
from .sourcing import CandidateItem, RawItem
from .pipeline import AlertPipeline, PipelineResult, build_pipeline
from .evaluate import AutoApprover, make_hitl_hook, weight_of_evidence

__all__ = [
    "Obligor", "ObligorRegistry",
    "RawItem", "CandidateItem",
    "AlertPipeline", "PipelineResult", "build_pipeline",
    "weight_of_evidence", "AutoApprover", "make_hitl_hook",
]
