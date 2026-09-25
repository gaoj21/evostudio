"""
Credit risk assessment demo: workflow + skills + two-layer memory, end to end.

Scenario: judge whether a company has credit risk from DAILY crawled news.

- News ingestion is daily: each day the crawler's batch for a company is
  first deduplicated against the historical news archive (exact match for
  re-crawls, embedding cosine similarity for reworded re-reports of the same
  event). Days whose crawl is entirely duplicates are skipped without any LLM
  call; surviving items are staged into short-term memory (an SEC 8-K filing
  source could be staged the same way). Accepted news is archived into
  long-term memory so future crawls compare against it.
- Workflow (3 nodes): extract_events -> risk_analysis -> risk_verdict.
  Skills `credit_risk_taxonomy` and `risk_scoring_rubric` are injected into the
  extraction / verdict prompts.
- Short-term memory (`ShortTermMemory`, one per company): a sliding window of
  the company's RECENT news (today plus the previous few days, bounded by
  `max_size`). The workflow input built from it separates today's new items
  (the assessment target) from the window's earlier items (context only), so
  old news informs judgment without being re-assessed.
- Long-term memory (`LongTermMemory`, FAISS + SQLite): ONE curated profile per
  company, updated in place after each assessment that found material events.
  The profile holds:
    - risk_focus: the company's current dominant risk themes
    - industry_risk_patterns: the industry-level pattern the company exhibits
    - similar_cases: analogous real-world cases (LLM general knowledge)
    - trajectory: day-by-day risk level / score / trend history
  A small "profile curator" LLM step synthesizes the updated profile from the
  old profile + latest events + verdict, and a reflection step reviews the
  trajectory for scoring calibration and derives watch items for the next
  assessments (stored back into the profile).
- Case library: well-known historical credit events (Evergrande, Arrium,
  British Steel, ...) are seeded into long-term memory. At each assessment,
  cases similar to the current news are retrieved and injected into the
  analysis, so judgments reference real precedents retrieved from memory
  instead of only the model's parametric knowledge.

The demo simulates daily crawls for "NexaSteel Group" (a deteriorating credit,
including noise days with no risk-relevant news), a contrast run of the default
day WITHOUT the long-term profile, and a stable company ("BrightHarvest Foods").

Prerequisites:
    export DEEPSEEK_API_KEY=<your-key>   # or put it in a .env file

Run from the repository root:

    python examples/projects/credit_risk/credit_risk_demo.py
"""

import json
import os
import re

# Must be set before torch/faiss load: their OpenMP runtimes conflict on macOS
# and segfault during embedding otherwise.
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

import shutil

from dotenv import load_dotenv

from evoagentx.agents import AgentManager
from evoagentx.core.message import Message, MessageType
from evoagentx.core.module_utils import parse_json_from_llm_output
from evoagentx.memory import ShortTermMemory
from evoagentx.memory.long_term_memory import LongTermMemory
from evoagentx.models import LiteLLM, LiteLLMConfig
from evoagentx.rag.rag_config import (
    ChunkerConfig,
    EmbeddingConfig,
    IndexConfig,
    ReaderConfig,
    RetrievalConfig,
    RAGConfig,
)
from evoagentx.skills import SkillManager
from evoagentx.storages.base import StorageHandler
from evoagentx.storages.storages_config import DBConfig, StoreConfig, VectorStoreConfig
from evoagentx.workflow import WorkFlow


def _embedding_model():
    """The project's local copy of bge-small when present (models/), else
    the Hugging Face id — the same rule Studio's memory uses."""
    try:
        from backend.memory.ltm import embedding_model
        return embedding_model()
    except Exception:
        return "BAAI/bge-small-en-v1.5"

from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

load_dotenv()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")
STORE_DIR = os.path.join("projects", "credit_risk", "output", "store")
LOG_PATH = os.path.join("projects", "credit_risk", "output", "assessment_log.json")

PROFILE_RECORD_TYPE = "company_profile"

# ---------------------------------------------------------------------------
# Mock daily news crawl: {company: [(date, [news items of that day]), ...]}
# Includes noise days with no risk-relevant content.
# ---------------------------------------------------------------------------

NEWS_FEED = {
    "NexaSteel Group": [
        ("2025-01-10", [
            "2025-01-10 | NexaSteel Group shares fell 6% after market rumors that the steelmaker "
            "faces tight liquidity and is seeking to extend upcoming bond maturities. The company declined to comment.",
        ]),
        ("2025-01-13", [
            "2025-01-13 | NexaSteel Group sponsored the city's annual marathon over the weekend; "
            "thousands of runners took part.",
        ]),
        ("2025-01-22", [
            "2025-01-22 | NexaSteel Group announced that CFO Li Wei has resigned for personal reasons, "
            "effective immediately. Controller Zhang Min will serve as interim CFO.",
        ]),
        # Reworded re-report of the 2025-01-22 CFO news -> semantic duplicate.
        ("2025-01-23", [
            "2025-01-23 | NexaSteel Group's finance chief departure: interim CFO Zhang Min takes over "
            "after Li Wei's sudden exit, raising questions about the steelmaker's financial oversight.",
        ]),
        ("2025-02-14", [
            "2025-02-14 | Zhongcheng Ratings revised NexaSteel Group's credit outlook to negative from stable, "
            "citing weakening operating cash flow and 4.2 billion yuan of debt maturing within 12 months.",
        ]),
        # Exact re-crawl of the 2025-02-14 outlook news -> exact duplicate.
        ("2025-02-15", [
            "2025-02-14 | Zhongcheng Ratings revised NexaSteel Group's credit outlook to negative from stable, "
            "citing weakening operating cash flow and 4.2 billion yuan of debt maturing within 12 months.",
        ]),
        ("2025-02-27", [
            "2025-02-27 | NexaSteel Group issued a profit warning, expecting a net loss of up to 800 million yuan "
            "for FY2024 amid falling steel prices and weak demand.",
        ]),
        ("2025-03-05", [
            "2025-03-05 | NexaSteel Group launched a new weather-resistant construction steel product line "
            "at an industry expo.",
        ]),
        ("2025-03-15", [
            "2025-03-15 | NexaSteel Group failed to pay a 45 million yuan coupon on its '22 NexaSteel MTN001' bond, "
            "according to a clearing house notice. The company has a 5-business-day grace period.",
        ]),
        # Reworded follow-up on the 2025-03-15 missed coupon -> semantic duplicate.
        ("2025-03-16", [
            "2025-03-16 | Clearing house records show NexaSteel Group did not pay the 45 million yuan coupon "
            "due on its MTN001 medium-term note; the steelmaker now has five business days to remedy the "
            "missed payment before a formal default.",
        ]),
        ("2025-03-20", [
            "2025-03-20 | A supplier sued NexaSteel Group for 120 million yuan in unpaid invoices, and a court "
            "froze one of the company's bank accounts, local media reported.",
        ]),
    ],
    "BrightHarvest Foods": [
        ("2025-03-08", [
            "2025-03-08 | BrightHarvest Foods reported FY2024 net profit up 18% to 320 million yuan, "
            "its fifth consecutive year of growth.",
        ]),
        ("2025-03-19", [
            "2025-03-19 | BrightHarvest Foods secured a 500 million yuan syndicated credit line to fund a new "
            "plant; the deal was oversubscribed by participating banks.",
        ]),
    ],
}


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

def build_graph(taxonomy: str, rubric: str) -> SequentialWorkFlowGraph:
    """3-node pipeline: extract events -> analyze with profile -> final verdict."""
    # Node prompts go through str.format() at execution time, so literal braces
    # in the injected skill content (e.g. the rubric's JSON example) must be escaped.
    taxonomy = taxonomy.replace("{", "{{").replace("}", "}}")
    rubric = rubric.replace("{", "{{").replace("}", "}}")
    return SequentialWorkFlowGraph.from_dict({
        "goal": "Assess a company's credit risk from today's crawled news and its long-term risk profile.",
        "tasks": [
            {
                "name": "extract_events",
                "description": "Extract structured credit-risk events from today's news batch.",
                "inputs": [
                    {"name": "company", "type": "str", "required": True, "description": "The company under review."},
                    {"name": "news_batch", "type": "str", "required": True, "description": "Today's crawled news items, one per line."},
                    # Declared here so `profile`/`cases` become workflow inputs and can
                    # feed risk_analysis; the prompt shows them as context only.
                    {"name": "profile", "type": "str", "required": True, "description": "The company's long-term risk profile, or 'None'."},
                    {"name": "cases", "type": "str", "required": True, "description": "Similar historical cases retrieved from memory, or 'None'."},
                ],
                "outputs": [
                    {"name": "events", "type": "str", "required": True, "description": "JSON list of extracted events."},
                ],
                "system_prompt": "You are a credit analyst assistant that extracts structured risk events from news.",
                "prompt": (
                    f"{taxonomy}\n\n"
                    "Extract all credit-risk-relevant events about {company} from today's news below.\n"
                    "The news has two sections: assess ONLY the items under 'Today's new items'; the "
                    "'Recent news from previous days' section is already-assessed context — never extract "
                    "events from it.\n"
                    "Return ONLY a JSON list of objects with keys: "
                    "event_type, severity, sentiment, date, confirmed, summary.\n"
                    "If no relevant event exists, return an empty list [].\n\n"
                    "News:\n{news_batch}\n\n"
                    "(Context only — the company's current risk profile and similar historical cases "
                    "follow; do NOT extract events from them:\n{profile}\n{cases}\n)"
                ),
                "parse_mode": "str",
            },
            {
                "name": "risk_analysis",
                "description": "Analyze the extracted events against the company's long-term risk profile.",
                "inputs": [
                    {"name": "company", "type": "str", "required": True, "description": "The company under review."},
                    {"name": "events", "type": "str", "required": True, "description": "Events extracted from today's news."},
                    {"name": "profile", "type": "str", "required": True, "description": "The company's long-term risk profile, or 'None'."},
                    {"name": "cases", "type": "str", "required": True, "description": "Similar historical cases retrieved from memory, or 'None'."},
                ],
                "outputs": [
                    {"name": "analysis", "type": "str", "required": True, "description": "Reasoned analysis including the risk trend."},
                ],
                "system_prompt": "You are a senior credit risk analyst. You compare current events with the company's risk profile and analogous historical cases to judge the trend.",
                "prompt": (
                    "Company: {company}\n\n"
                    "Events extracted from today's news batch:\n{events}\n\n"
                    "The company's long-term risk profile ('None' means no profile yet):\n{profile}\n\n"
                    "Similar historical cases retrieved from the case library ('None' means no relevant case):\n{cases}\n\n"
                    "Analyze:\n"
                    "1. Which events are the most material for credit risk?\n"
                    "2. Compared with the profile's risk_focus and trajectory, is the risk theme new, "
                    "recurring, escalating or improving?\n"
                    "3. Do any of the retrieved similar cases match the current situation? If so, what "
                    "happened next in those cases, and what does that imply here? Also respect the "
                    "profile's reflection (calibration notes and watch items) if present.\n"
                    "If the events list is empty, simply state that no credit-risk-relevant events were "
                    "found in today's news and that the existing profile view is unchanged.\n"
                    "Be specific and cite the facts."
                ),
                "parse_mode": "str",
            },
            {
                "name": "risk_verdict",
                "description": "Produce the final structured credit risk verdict.",
                "inputs": [
                    {"name": "company", "type": "str", "required": True, "description": "The company under review."},
                    {"name": "events", "type": "str", "required": True, "description": "Events extracted from today's news."},
                    {"name": "analysis", "type": "str", "required": True, "description": "The risk analysis."},
                    {"name": "profile", "type": "str", "required": True, "description": "The company's long-term risk profile, or 'None'."},
                ],
                "outputs": [
                    {"name": "verdict", "type": "str", "required": True, "description": "Final verdict as a JSON object."},
                ],
                "system_prompt": "You are the chief credit officer. You issue the final risk verdict following the rubric strictly.",
                "prompt": (
                    f"{rubric}\n\n"
                    "Company: {company}\n\n"
                    "Extracted events:\n{events}\n\n"
                    "Analysis:\n{analysis}\n\n"
                    "The company's long-term risk profile, including its trajectory and reflection "
                    "('None' means no profile yet):\n{profile}\n\n"
                    "Issue the final verdict. Return ONLY the JSON object specified in the rubric.\n"
                    "Calibration rules:\n"
                    "- Today's events update the EXISTING profile view; they do not replace it. "
                    "Confirmed unresolved events already in the profile (e.g. an uncured default) "
                    "dominate the verdict: a new unconfirmed or less severe event must NOT lower the "
                    "score below what the unresolved confirmed events imply. The rubric's unconfirmed-rumor "
                    "cap applies only when no confirmed critical event is outstanding.\n"
                    "- Respect the profile's reflection (calibration notes and watch items) when present.\n"
                    "If the analysis states that no credit-risk-relevant events were found, the "
                    "verdict must CARRY FORWARD the existing profile view instead of resetting: "
                    "reuse the risk_level, score and trend from the profile's latest trajectory "
                    "entry, keep key_evidence empty, and give a one-line rationale that today's "
                    "news was uneventful so the existing assessment is unchanged. Only when no "
                    "profile exists yet (no prior trajectory entry), return the JSON object with "
                    "risk_level \"low\", score 0, trend \"stable\", confidence \"high\", empty "
                    "key_evidence, and a one-line rationale saying today's news was uneventful."
                ),
                "parse_mode": "str",
            },
        ],
    })


# ---------------------------------------------------------------------------
# Short-term memory: a sliding window of RECENT news (today + previous days)
# ---------------------------------------------------------------------------

def stage_news(stm: ShortTermMemory, company: str, new_items: list) -> str:
    """Add today's deduped news to the short-term memory and build the
    workflow's news input from what the memory holds.

    The STM is a sliding window of recent days (bounded by `max_size`), not
    just today's batch. To avoid re-assessing old news, the workflow input
    separates today's new items (the assessment target) from the window's
    earlier items (context only).
    """
    prior = [str(msg.content) for msg in stm.get()]
    stm.add_messages([
        Message(content=item, msg_type=MessageType.INPUT, wf_goal=f"credit_risk:{company}")
        for item in new_items
    ])
    sections = ["Today's new items (assess ONLY these):"]
    sections += [f"- {item}" for item in new_items]
    if prior:
        sections += [
            "",
            "Recent news from previous days (already assessed; context only — "
            "do NOT extract events from these):",
        ]
        sections += [f"- {item}" for item in prior]
    return "\n".join(sections)


# ---------------------------------------------------------------------------
# News dedup: today's crawl vs. the historical news archive
# ---------------------------------------------------------------------------

NEWS_ITEM_RECORD_TYPE = "news_item"
# Calibrated on the credit_risk_dataset dev split (v0.2): reworded re-reports
# of the same event score >= ~0.90 cosine, so 0.88 keeps recall high; the
# template-headline false positives that similarity alone cannot separate
# (e.g. recurring "Option Alert" notices at ~0.95) are rejected by the
# number-mismatch guard below instead of a higher threshold.
NEWS_DUP_THRESHOLD = 0.88

_embedder = None


def _get_embedder():
    """Shared local embedding model for semantic news dedup (same model the
    long-term memory uses for retrieval)."""
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(_embedding_model(), device="cpu")
    return _embedder


def _numbers(text: str) -> set:
    """Standalone numeric tokens (amounts, dates, strikes, counts)."""
    return set(re.findall(r"\b\d[\d.,]*\b", text))


def _numbers_mismatch(a: str, b: str) -> bool:
    """Guard against template-headline false duplicates: two items that both
    carry numbers but share few of them report DIFFERENT events (different
    amounts/dates/contracts), however similar the wording. Items with no
    numbers on either side are unaffected."""
    na, nb = _numbers(a), _numbers(b)
    if not na or not nb:
        return False
    return len(na & nb) / min(len(na), len(nb)) < 0.5


def dedup_news(news_archive: dict, company: str, items: list):
    """Compare today's crawl with the company's news archive AND with the
    items already accepted from today's own batch (wire stories are typically
    re-posted by several publishers on the same day — archive-only
    comparison misses those).

    Returns (new_items, duplicates); duplicates are (item, first_seen_date)
    pairs. Two levels: exact text match (re-crawls) and embedding cosine
    similarity (reworded re-reports of the same event).
    """
    import numpy as np

    seen = news_archive.setdefault(company, [])
    embedder = _get_embedder()
    new_items, duplicates = [], []
    for item in items:
        exact = next((e for e in seen if e["text"] == item), None)
        if exact is not None:
            duplicates.append((item, exact["date"]))
            continue
        match, emb = None, None
        if seen:
            emb = embedder.encode(item, normalize_embeddings=True)
            sims = [float(emb @ e["emb"]) for e in seen]
            best = int(np.argmax(sims))
            if sims[best] >= NEWS_DUP_THRESHOLD and not _numbers_mismatch(item, seen[best]["text"]):
                match = seen[best]
        if match is not None:
            duplicates.append((item, match["date"]))
        else:
            new_items.append(item)
            # accepted items join the comparison set immediately, so a
            # near-identical story later in the SAME batch is caught too
            seen.append({"date": item.split(" | ", 1)[0], "text": item,
                         "emb": emb if emb is not None
                         else embedder.encode(item, normalize_embeddings=True)})
    return new_items, duplicates


def archive_news(memory: LongTermMemory, news_archive: dict, company: str, date: str, items: list):
    """Persist today's accepted news items to long-term memory, so future runs
    can reload the archive. The in-run dedup archive (`news_archive`) is
    already maintained by dedup_news itself: accepted items join the
    comparison set as soon as they survive, which is what makes intra-batch
    dedup work."""
    for item in items:
        memory.add(Message(
            content=json.dumps(
                {"record_type": NEWS_ITEM_RECORD_TYPE, "company": company, "date": date, "text": item},
                ensure_ascii=False,
            ),
            action="news_archive",
            wf_goal=f"credit_risk:{company}",
            agent="news_crawler",
            msg_type=MessageType.INPUT,
        ))


# ---------------------------------------------------------------------------
# Long-term memory: one curated profile per company, updated in place
# ---------------------------------------------------------------------------

CORPUS_ID = "credit_risk_archive"  # stable across runs so save()/load() round-trips


def build_memory(fresh: bool = True) -> LongTermMemory:
    """FAISS + SQLite backed long-term memory, embedded with a local bge-small model.

    Args:
        fresh: If True, wipe the store first (demo behaviour). If False, keep the
            existing store; call ``memory.load()`` afterwards to restore the archive.
    """
    if fresh and os.path.exists(STORE_DIR):
        shutil.rmtree(STORE_DIR)
    os.makedirs(STORE_DIR, exist_ok=True)
    store_config = StoreConfig(
        dbConfig=DBConfig(db_name="sqlite", path=os.path.join(STORE_DIR, "memory.sql")),
        vectorConfig=VectorStoreConfig(vector_name="faiss", dimensions=384, index_type="flat_l2"),
        graphConfig=None,
        path=os.path.join(STORE_DIR, "indexing"),
    )
    rag_config = RAGConfig(
        reader=ReaderConfig(recursive=False, exclude_hidden=True, errors="ignore", encoding="utf-8"),
        chunker=ChunkerConfig(strategy="simple", chunk_size=512, chunk_overlap=0, max_chunks=None),
        embedding=EmbeddingConfig(provider="huggingface", model_name=_embedding_model(), device="cpu"),
        index=IndexConfig(index_type="vector"),
        retrieval=RetrievalConfig(
            retrivel_type="vector", postprocessor_type="simple",
            top_k=10, similarity_cutoff=0.0,
        ),
    )
    return LongTermMemory(
        storage_handler=StorageHandler(storageConfig=store_config),
        rag_config=rag_config,
        default_corpus_id=CORPUS_ID,
    )


def _parse_record(content) -> dict:
    """Parse a memory message's content into a dict (content is JSON-encoded
    once by us and once more by LongTermMemory's chunking)."""
    try:
        record = json.loads(str(content))
        if isinstance(record, str):
            record = json.loads(record)
    except (json.JSONDecodeError, TypeError):
        return {}
    return record if isinstance(record, dict) else {}


def recall_profile(memory: LongTermMemory, company: str):
    """Retrieve the company's curated profile. Returns (profile_dict|None, memory_id|None)."""
    results = memory.search(f"{company} company credit risk profile risk focus trajectory", n=10)
    for message, memory_id in results:
        record = _parse_record(message.content)
        if record.get("record_type") == PROFILE_RECORD_TYPE and record.get("company") == company:
            return record, memory_id
    return None, None


def save_profile(memory: LongTermMemory, company: str, profile: dict, memory_id=None) -> str:
    """Persist the profile: insert on first sight, update in place afterwards."""
    message = Message(
        content=json.dumps(profile, ensure_ascii=False),
        action="company_profile_update",
        wf_goal=f"credit_risk:{company}",
        agent="profile_curator",
        msg_type=MessageType.RESPONSE,
    )
    if memory_id is None:
        return memory.add(message)[0]
    memory.update((memory_id, message))
    return memory_id


# ---------------------------------------------------------------------------
# Case library: well-known credit events, retrieved by similarity at judgment time
# ---------------------------------------------------------------------------

CASE_RECORD_TYPE = "historical_case"

CASE_LIBRARY = [
    {
        "name": "China Evergrande (2021)",
        "industry": "real estate",
        "pattern": "Highly leveraged developer facing a wall of near-term maturities; liquidity rumors, "
                   "rating downgrades, then missed bond payments and restructuring.",
        "outcome": "Defaulted on offshore bonds; ordered into liquidation in 2024.",
    },
    {
        "name": "Arrium (2016)",
        "industry": "steel",
        "pattern": "Capital-intensive steelmaker in a commodity downcycle: margin compression, weakening "
                   "cash flow and large debt it could not refinance.",
        "outcome": "Collapsed into administration after failing to refinance its debt.",
    },
    {
        "name": "British Steel (2019)",
        "industry": "steel",
        "pattern": "Steel producer hit by a severe liquidity squeeze amid weak demand and uncertainty; "
                   "required emergency funding to keep operating.",
        "outcome": "Entered insolvency and was rescued via a government-backed sale.",
    },
    {
        "name": "Luckin Coffee (2020)",
        "industry": "consumer / retail",
        "pattern": "Rapid-growth company with governance red flags; fabricated sales revealed after "
                   "management turmoil.",
        "outcome": "Accounting scandal, delisting and bankruptcy proceedings.",
    },
    {
        "name": "Wirecard (2020)",
        "industry": "fintech / payments",
        "pattern": "Auditor refused to sign off, missing cash balances, executive departures — governance "
                   "and oversight failure rather than a market downturn.",
        "outcome": "Insolvency after 1.9 billion euros of cash proved non-existent.",
    },
]


def seed_case_library(memory: LongTermMemory):
    """Load the historical case library into long-term memory (idempotent per run)."""
    for case in CASE_LIBRARY:
        memory.add(Message(
            content=json.dumps({"record_type": CASE_RECORD_TYPE, **case}, ensure_ascii=False),
            action="case_library_seed",
            wf_goal="credit_risk:case_library",
            agent="case_library",
            msg_type=MessageType.RESPONSE,
        ))


def retrieve_similar_cases(memory: LongTermMemory, query_text: str, k: int = 2, min_similarity: float = 0.45) -> str:
    """Retrieve historical cases similar to the current situation.

    Candidates come from long-term memory (vector search); relevance is gated
    by local embedding cosine similarity because the memory search API does
    not expose scores.
    """
    import numpy as np

    results = memory.search(f"historical credit risk case similar to: {query_text}", n=10)
    embedder = _get_embedder()
    query_emb = embedder.encode(query_text, normalize_embeddings=True)
    scored = []
    for message, _ in results:
        record = _parse_record(message.content)
        if record.get("record_type") != CASE_RECORD_TYPE:
            continue
        case_text = f"{record.get('name')} {record.get('industry')} {record.get('pattern')} {record.get('outcome')}"
        sim = float(query_emb @ embedder.encode(case_text, normalize_embeddings=True))
        if sim >= min_similarity:
            scored.append((sim, record))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return "None"
    lines = []
    for sim, case in scored[:k]:
        lines.append(
            f"- {case['name']} ({case.get('industry')}): pattern — {case.get('pattern')} "
            f"Outcome — {case.get('outcome')} (similarity {sim:.2f})"
        )
    return "\n".join(lines)


PROFILE_CURATOR_PROMPT = """You maintain the long-term credit profile of a company. Update the profile JSON based on the latest daily assessment.

Company: {company}
Assessment date: {date}

Current profile JSON ("None" means this is the first assessment):
{old_profile}

Latest extracted events:
{events}

Latest verdict:
{verdict}

Update rules:
- industry: infer it from the evidence if not set; keep it stable afterwards.
- risk_focus: the 1-5 dominant risk themes for this company RIGHT NOW (e.g. "refinancing risk: 4.2bn yuan debt maturing within 12 months"). Drop resolved themes, add new ones, reword them if they evolve.
- industry_risk_patterns: 1-3 sentences on the typical risk pattern of this industry that the company is currently exhibiting (e.g. a downcycle liquidity spiral in steel).
- similar_cases: 1-2 well-known real-world analogous cases or historical episodes from your general knowledge, clearly marked as analogies; use "none identified" if none fits.
- trajectory: keep the existing entries and append one object {{"date": "{date}", "risk_level": ..., "score": ..., "trend": ...}} from the latest verdict.
- trajectory_summary: 1-2 sentences on how the company's risk has evolved so far.

Return ONLY the updated profile as a single JSON object with keys: company, industry, risk_focus, industry_risk_patterns, similar_cases, trajectory, trajectory_summary.
"""


def update_profile(llm, company: str, date: str, old_profile, events: list, verdict: dict) -> dict:
    """Profile curator: synthesize the updated long-term profile with the LLM.

    Falls back to a deterministic Python update if the LLM output cannot be
    parsed, so the memory layer never breaks the assessment loop.
    """
    prompt = PROFILE_CURATOR_PROMPT.format(
        company=company,
        date=date,
        old_profile=json.dumps(old_profile, ensure_ascii=False, indent=2) if old_profile else "None",
        events=json.dumps(events, ensure_ascii=False, indent=2),
        verdict=json.dumps(verdict, ensure_ascii=False, indent=2),
    )
    response = llm.generate(prompt=prompt)
    profile = parse_json_from_llm_output(response.content)
    if not isinstance(profile, dict) or "trajectory" not in profile:
        # Deterministic fallback: keep the old profile, append the trajectory entry.
        profile = dict(old_profile or {})
        profile.update({
            "record_type": PROFILE_RECORD_TYPE,
            "company": company,
            "industry": profile.get("industry", "unknown"),
            "risk_focus": profile.get("risk_focus", []),
            "industry_risk_patterns": profile.get("industry_risk_patterns", ""),
            "similar_cases": profile.get("similar_cases", ""),
            "trajectory_summary": profile.get("trajectory_summary", ""),
        })
        trajectory = list(profile.get("trajectory") or [])
        trajectory.append({"date": date, "risk_level": verdict.get("risk_level"),
                           "score": verdict.get("score"), "trend": verdict.get("trend")})
        profile["trajectory"] = trajectory
        return profile
    profile["record_type"] = PROFILE_RECORD_TYPE
    profile["company"] = company
    # The curator only rewrites the profile fields it owns; carry over the
    # previous reflection until a new one replaces it.
    if old_profile and "reflection" in old_profile and "reflection" not in profile:
        profile["reflection"] = old_profile["reflection"]
    return profile


REFLECTION_PROMPT = """You are an independent reviewer calibrating the credit risk assessments of a company over time.

Company: {company}

The company's long-term profile, including its assessment trajectory:
{profile}

Latest verdict:
{verdict}

Reflect on the trajectory and the latest judgment:
1. calibration: were the score moves between consecutive assessments justified by the new information each day? Flag any over-reaction or under-reaction, and say what the score should have been if so.
2. watch_items: list 1-3 concrete facts or events the NEXT assessments should look for to confirm or refute the current view.
3. lesson: one sentence on what this trajectory teaches about assessing this company or industry.

Return ONLY a JSON object with keys: calibration, watch_items (a list), lesson.
"""


def reflect(llm, company: str, profile: dict, verdict: dict) -> dict:
    """Trajectory reflection: review scoring calibration across the trajectory
    and derive watch items for future assessments. Stored into the profile so
    the next judgment sees it. Returns {} on parse failure (non-fatal)."""
    prompt = REFLECTION_PROMPT.format(
        company=company,
        profile=json.dumps(profile, ensure_ascii=False, indent=2),
        verdict=json.dumps(verdict, ensure_ascii=False, indent=2),
    )
    response = llm.generate(prompt=prompt)
    reflection = parse_json_from_llm_output(response.content)
    return reflection if isinstance(reflection, dict) else {}


# ---------------------------------------------------------------------------
# Assessment loop
# ---------------------------------------------------------------------------

def assess(workflow: WorkFlow, company: str, news_batch: str, profile_text: str, cases_text: str = "None") -> dict:
    result = workflow.execute(inputs={"company": company, "news_batch": news_batch,
                                      "profile": profile_text, "cases": cases_text})
    if result.status != "success":
        raise RuntimeError(f"Workflow failed: {result.displayable_error or result.error_msg}")
    outputs = result.result
    # `result.result` only contains the END node's outputs (verdict). Intermediate
    # node outputs live in the workflow environment's execution data.
    events_raw = workflow.environment.execution_data.get("events", "[]")
    events = parse_json_from_llm_output(str(events_raw))
    verdict = parse_json_from_llm_output(str(outputs.get("verdict", "{}")))
    return {"events": events if isinstance(events, list) else [], "verdict": verdict}


def print_verdict(company: str, date: str, verdict: dict, tag: str = ""):
    print(f"\n[{date}] {company} {tag}")
    print(f"  risk_level: {verdict.get('risk_level')}  score: {verdict.get('score')}  "
          f"trend: {verdict.get('trend')}  confidence: {verdict.get('confidence')}")
    print(f"  rationale: {verdict.get('rationale')}")


def print_profile(profile: dict):
    print("  profile updated:")
    print(f"    industry: {profile.get('industry')}")
    for focus in profile.get("risk_focus", []):
        print(f"    risk_focus: {focus}")
    print(f"    industry_risk_patterns: {profile.get('industry_risk_patterns')}")
    print(f"    similar_cases: {profile.get('similar_cases')}")
    print(f"    trajectory_summary: {profile.get('trajectory_summary')}")


def run_day(llm, workflow, long_term, short_term, news_archive, company, date, news_items, log):
    """One daily cycle: dedup today's crawl against the news archive -> stage
    the survivors into short-term memory -> assess with the profile -> curate
    and persist the updated profile (only on material events) -> archive the
    accepted news for future dedup."""
    new_items, duplicates = dedup_news(news_archive, company, news_items)
    for item, first_seen in duplicates:
        print(f"\n[{date}] {company} — duplicate news skipped (first seen {first_seen}): {item[:90]}...")
    if not new_items:
        print(f"[{date}] {company} — nothing new today; assessment skipped.")
        log.append({"company": company, "date": date, "with_memory": True,
                    "events": [], "verdict": None, "duplicates": len(duplicates)})
        return
    news_batch = stage_news(short_term, company, new_items)
    profile, profile_id = recall_profile(long_term, company)
    profile_text = json.dumps(profile, ensure_ascii=False, indent=2) if profile else "None"
    cases_text = retrieve_similar_cases(long_term, query_text=news_batch)
    outcome = assess(workflow, company, news_batch, profile_text, cases_text)
    archive_news(long_term, news_archive, company, date, new_items)
    if not outcome["events"]:
        print(f"\n[{date}] {company} — no credit-risk-relevant events today; profile unchanged.")
        log.append({"company": company, "date": date, "with_memory": True,
                    "events": [], "verdict": None})
        return
    print_verdict(company, date, outcome["verdict"], tag=f"(profile: {'none' if profile is None else 'loaded'})")
    print(f"  similar cases used:\n    {cases_text.replace(chr(10), chr(10) + '    ')}")
    new_profile = update_profile(llm, company, date, profile, outcome["events"], outcome["verdict"])
    reflection = reflect(llm, company, new_profile, outcome["verdict"])
    if reflection:
        new_profile["reflection"] = reflection
        print(f"  reflection: {reflection.get('calibration')}")
        for item in reflection.get("watch_items", []):
            print(f"    watch: {item}")
    save_profile(long_term, company, new_profile, profile_id)
    print_profile(new_profile)
    log.append({"company": company, "date": date, "with_memory": True,
                "events": outcome["events"], "verdict": outcome["verdict"], "profile": new_profile})


def main():
    if not DEEPSEEK_API_KEY:
        raise ValueError("DEEPSEEK_API_KEY not found. Set it via environment variable or .env file.")

    llm = LiteLLM(config=LiteLLMConfig(model="deepseek/deepseek-v4-flash", deepseek_key=DEEPSEEK_API_KEY))

    # Skills: judgment standards live in SKILL.md files, injected into prompts.
    skills = SkillManager(skill_paths=SKILLS_DIR)
    print("Loaded skills:", [s["name"] for s in skills.list_skills()])
    taxonomy = skills.get_skill("credit_risk_taxonomy").content
    rubric = skills.get_skill("risk_scoring_rubric").content

    graph = build_graph(taxonomy, rubric)
    agent_manager = AgentManager()
    agent_manager.add_agents_from_workflow(graph, llm.config)
    workflow = WorkFlow(graph=graph, agent_manager=agent_manager, llm=llm)

    long_term = build_memory()
    seed_case_library(long_term)
    # One short-term memory per company: a sliding window of its recent news
    # (bounded by max_size; roughly the last few days at this news volume).
    short_terms = {"NexaSteel Group": ShortTermMemory(max_size=12),
                   "BrightHarvest Foods": ShortTermMemory(max_size=12)}
    news_archive = {}  # company -> [{date, text, emb}], for news dedup
    log = []

    # --- NexaSteel Group: daily crawl, each day assessed WITH the evolving profile ---
    company = "NexaSteel Group"
    print(f"\n{'=' * 70}\nDaily assessment of {company} (memory-enabled)\n{'=' * 70}")
    for date, news_items in NEWS_FEED[company]:
        run_day(llm, workflow, long_term, short_terms[company], news_archive, company, date, news_items, log)

    # --- Contrast: the default day WITHOUT the long-term profile (same news) ---
    print(f"\n{'=' * 70}\nContrast: 2025-03-15 assessed WITHOUT the long-term profile\n{'=' * 70}")
    news_batch = stage_news(short_terms[company], company, dict(NEWS_FEED[company])["2025-03-15"])
    outcome = assess(workflow, company, news_batch, profile_text="None", cases_text="None")
    print_verdict(company, "2025-03-15", outcome["verdict"], tag="(no profile)")
    log.append({"company": company, "date": "2025-03-15", "with_memory": False,
                "events": outcome["events"], "verdict": outcome["verdict"]})

    # --- BrightHarvest Foods: a stable company should NOT be flagged ---
    company = "BrightHarvest Foods"
    print(f"\n{'=' * 70}\nDaily assessment of {company}\n{'=' * 70}")
    for date, news_items in NEWS_FEED[company]:
        run_day(llm, workflow, long_term, short_terms[company], news_archive, company, date, news_items, log)

    # --- Summary ---
    print(f"\n{'=' * 70}\nRisk trajectory summary\n{'=' * 70}")
    for entry in log:
        v = entry.get("verdict")
        if not v:
            note = (f"all {entry['duplicates']} item(s) were duplicates"
                    if entry.get("duplicates") else "no material events")
            print(f"  {entry['company']:22s} {entry['date']}  (with memory ) -> {note}")
            continue
        mem = "with memory" if entry["with_memory"] else "NO memory"
        print(f"  {entry['company']:22s} {entry['date']}  ({mem:12s}) -> "
              f"{str(v.get('risk_level')):9s} score={v.get('score')}  trend={v.get('trend')}")

    final_profile, _ = recall_profile(long_term, "NexaSteel Group")
    if final_profile:
        print("\nNexaSteel Group final long-term profile trajectory:")
        for point in final_profile.get("trajectory", []):
            print(f"  {point.get('date')}: {point.get('risk_level')} score={point.get('score')} trend={point.get('trend')}")

    # Persist the long-term memory (FAISS index + memory table) to disk; without
    # this the archive only lives in the current process.
    long_term.save()

    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print(f"\nFull log saved to {LOG_PATH}")


if __name__ == "__main__":
    main()
