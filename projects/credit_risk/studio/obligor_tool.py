"""ObligorMatch toolkit for EvoAgentX Studio.

Studio-side wrapper around credit_risk's ObligorRegistry (step 2, grounding):
match_company_name / match_cik against the internal obligor list loaded from
projects/credit_risk/dataset/contemporary/candidates.csv (lazy, cached process-wide).
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from evoagentx.tools.tool import Tool, Toolkit

_REPO_ROOT = Path(__file__).resolve().parents[3]
CANDIDATES_CSV = Path(__file__).resolve().parents[1] / "dataset" / "contemporary" / "candidates.csv"

_registry = None
_registry_path = None


def _get_registry():
    global _registry, _registry_path
    configured = os.environ.get("EAX_STUDIO_OBLIGORS_FILE")
    path = Path(configured).expanduser() if configured else CANDIDATES_CSV
    if not path.is_absolute():
        path = _REPO_ROOT / path
    path = path.resolve()
    if _registry is None or _registry_path != path:
        from credit_risk.agentic_pipeline.obligors import ObligorRegistry

        _registry = ObligorRegistry.from_candidates_csv(str(path))
        _registry_path = path
    return _registry


def _obligor_dict(ob, how: str) -> Dict[str, Any]:
    return {
        "matched": True,
        "how": how,
        "obligor": ob.name,
        "obligor_id": ob.obligor_id,
        "cik": ob.cik,
        "symbol": ob.symbol,
    }


class MatchCompanyNameTool(Tool):
    name: str = "match_company_name"
    description: str = (
        "Match a company name against the internal obligor list "
        "(exact-normalized, alias, then token-containment fuzzy)."
    )
    inputs: Dict[str, Dict[str, str]] = {
        "name": {"type": "string", "description": "Company name to match"}
    }
    required: Optional[List[str]] = ["name"]

    def __call__(self, name: str) -> Dict[str, Any]:
        ob, how = _get_registry().match_name(name)
        if ob is None:
            return {"matched": False, "how": "none", "name": name}
        return _obligor_dict(ob, how)


class MatchCikTool(Tool):
    name: str = "match_cik"
    description: str = (
        "Match an SEC CIK number against the internal obligor list "
        "(direct lookup, strongest grounding evidence)."
    )
    inputs: Dict[str, Dict[str, str]] = {
        "cik": {"type": "string", "description": "CIK number (digits, leading zeros optional)"}
    }
    required: Optional[List[str]] = ["cik"]

    def __call__(self, cik: str) -> Dict[str, Any]:
        ob = _get_registry().match_cik(cik)
        if ob is None:
            return {"matched": False, "how": "none", "cik": cik}
        return _obligor_dict(ob, "cik")


class ObligorMatchToolkit(Toolkit):
    def __init__(self, name: str = "ObligorMatchToolkit"):
        super().__init__(name=name, tools=[MatchCompanyNameTool(), MatchCikTool()])
