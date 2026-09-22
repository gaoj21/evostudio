"""Step 1 — Sourcing: turn raw source items into obligor candidates.

Two source channels:

- **news**: headlines carry free text, so the obligor must be EXTRACTED.
  `extract_entities` asks the LLM for the company that is the grammatical
  subject of each headline (a company merely mentioned is not a candidate —
  that distinction is what filters out roundup wires and conference spam at
  the very first step).
- **8k**: filings arrive with EDGAR metadata, so the obligor is GIVEN —
  the registrant CIK is read off the submission header, no LLM needed.

Both channels emit `CandidateItem`s; grounding (obligors.py) decides which
candidates enter the reasoning loop.
"""

import json
from dataclasses import dataclass, field
from typing import Optional

from evoagentx.core.module_utils import parse_json_from_llm_output

ENTITY_EXTRACTION_PROMPT = """For each numbered news headline below, list the companies that are the MAIN
SUBJECT of the headline (the company the news is ABOUT). Companies only
mentioned in passing (as counterparties, in lists of peers, or as the venue
of an event) are NOT subjects.

Return ONLY a JSON object mapping each headline number to a list of company
names (usually zero or one), e.g. {{"1": ["Acme Corp"], "2": []}}.

Headlines:
{listing}"""

ENTITY_EXTRACTION_SYS = (
    "You are an entity extraction service for a credit-risk monitoring "
    "pipeline. Output valid JSON only."
)

MAX_ITEMS_PER_CALL = 30


@dataclass
class RawItem:
    """One raw source item (a news headline or an 8-K filing)."""
    source: str                     # "news" | "8k"
    date: str                       # YYYY-MM-DD
    title: str
    text: Optional[str] = None      # 8-K body; None for headlines
    url: Optional[str] = None
    publisher: Optional[str] = None
    cik: Optional[str] = None       # known for 8-K (EDGAR metadata)
    registrant_name: Optional[str] = None   # known for 8-K
    items: list = field(default_factory=list)   # 8-K item headings
    meta: dict = field(default_factory=dict)


@dataclass
class CandidateItem:
    """A raw item plus its extracted/given obligor candidate."""
    item: RawItem
    candidate_name: Optional[str] = None
    candidate_cik: Optional[str] = None


def extract_entities(llm, items: list) -> dict:
    """News channel: batch-extract subject company names per item.

    Returns {item_index: [names]}; indices align with the input list.
    Unparseable responses yield empty lists (the item is then dropped at
    grounding), never an exception.
    """
    out = {}
    for base in range(0, len(items), MAX_ITEMS_PER_CALL):
        chunk = items[base:base + MAX_ITEMS_PER_CALL]
        listing = "\n".join(f"{base + i + 1}. {it.title}"
                            for i, it in enumerate(chunk))
        resp = llm.generate(
            prompt=ENTITY_EXTRACTION_PROMPT.format(listing=listing),
            system_message=ENTITY_EXTRACTION_SYS)
        parsed = parse_json_from_llm_output(resp.content)
        if not isinstance(parsed, dict):
            parsed = {}
        for i in range(len(chunk)):
            names = parsed.get(str(base + i + 1)) or []
            if not isinstance(names, list):
                names = [names]
            out[base + i] = [str(n) for n in names if n]
    return out


def candidates_from_news(llm, items: list) -> list:
    """News channel end of step 1a: one CandidateItem per (item, name)."""
    entities = extract_entities(llm, items)
    cands = []
    for i, it in enumerate(items):
        for name in entities.get(i, []):
            cands.append(CandidateItem(item=it, candidate_name=name))
        if not entities.get(i):
            cands.append(CandidateItem(item=it))  # no subject -> will drop
    return cands


def candidates_from_filings(items: list) -> list:
    """8-K channel end of step 1b: CIK is read off the filing metadata."""
    return [CandidateItem(item=it,
                          candidate_name=it.registrant_name,
                          candidate_cik=it.cik)
            for it in items]
