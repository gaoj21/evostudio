"""
MIPRO prompt optimization for the credit-risk pipeline (plug-and-play mode).

Optimizes the three node instructions (extract_events / risk_analysis /
risk_verdict) against the v0.2 dataset's TRAIN split, keeping the full
pipeline semantics: per-sample daily replay with news dedup, short-term
memory, long-term company profile (curate + reflect) and the case library.

Why plug-and-play (`MiproOptimizer` + `MiproRegistry`) instead of
`WorkFlowMiproOptimizer`: the workflow-mode evaluator runs each example as a
single `workflow.execute()` call, but one of our examples is a 180-day news
window whose verdict depends on state evolving day by day. Plug-and-play lets
the program be the whole replay loop while MIPRO rewrites the three tracked
instruction attributes.

Zero-shot mode (max_*_demos=0): only instructions are optimized. The skills
(taxonomy / rubric) stay FIXED — they are the domain standard, not a tuning
target; the calibration rules live in the optimizable verdict instruction.

Scoring (benchmark.evaluate):
- positive: 0.8 * detection (max level over the window: >=high=1, medium=0.5,
  else 0) + 0.2 * lead_days/180 (early-warning bonus)
- negative: max level low=1.0 / medium=0.8 / high=0.2 / critical=0.0

Budget knobs: train subset 8 pos + 8 neg, MIPRO valset 3 pos + 3 neg, all
with <= 25 active news days; auto=None, num_candidates=4, max_steps=5
(trials), 6 threads, minibatch off (valset of 6 is too small to split).
Rough cost: ~$5-8, ~14 h wall time (1 eval wave per trial).

Run from the repository root:
    python examples/credit_risk/optimize_mipro.py --selftest   # one sample, no optimizer
    python examples/credit_risk/optimize_mipro.py              # full optimization
"""

import json
import os
import sys

os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
# bge-small-en-v1.5 is fully cached locally; without this every per-example
# LongTermMemory init fires ~10 HEAD requests to huggingface.co, which is
# unreachable from some networks (10s timeout × 5 retries each — dominates
# the entire runtime).
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import shutil
import tempfile
from datetime import datetime
from typing import Any, Tuple

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                "..")))  # repo root (llm/)

from credit_risk_demo import (  # noqa: E402
    SKILLS_DIR,
    ShortTermMemory,
    SkillManager,
    archive_news,
    dedup_news,
    recall_profile,
    reflect,
    retrieve_similar_cases,
    save_profile,
    seed_case_library,
    stage_news,
    update_profile,
)
from run_dataset_eval import _retry  # noqa: E402

from evoagentx.benchmark.benchmark import Benchmark  # noqa: E402
from evoagentx.core.module_utils import parse_json_from_llm_output  # noqa: E402
from evoagentx.memory.long_term_memory import LongTermMemory  # noqa: E402
from evoagentx.models import LiteLLM, LiteLLMConfig  # noqa: E402
from evoagentx.optimizers import MiproOptimizer  # noqa: E402
from evoagentx.rag.rag_config import (  # noqa: E402
    ChunkerConfig,
    EmbeddingConfig,
    IndexConfig,
    ReaderConfig,
    RetrievalConfig,
    RAGConfig,
)
from evoagentx.storages.base import StorageHandler  # noqa: E402
from evoagentx.storages.storages_config import (  # noqa: E402
    DBConfig,
    StoreConfig,
    VectorStoreConfig,
)
from evoagentx.utils.mipro_utils.register_utils import MiproRegistry  # noqa: E402

# --- dspy 3.3.0 compatibility shims ---------------------------------------
# evoagentx's MiproOptimizer was written against an older dspy whose
# MIPROv2._set_hyperparams_from_run_mode / _print_auto_run_settings took 2
# fewer arguments. Bridge the two call sites without forking the optimizer.
import dspy.teleprompt.mipro_optimizer_v2 as _dspy_mipro  # noqa: E402
from evoagentx.optimizers.mipro_optimizer import MiproOptimizer as _MiproOptimizer  # noqa: E402


def _compat_set_hyperparams(self, program, num_trials, minibatch, zeroshot_opt, valset):
    num_trials, valset, minibatch, n_instruct, n_fewshot = (
        _dspy_mipro.MIPROv2._set_hyperparams_from_run_mode(
            self, program, num_trials, minibatch, zeroshot_opt, valset,
            getattr(self, "num_instruct_candidates", None),
            getattr(self, "num_fewshot_candidates", None)))
    # dspy 3.3.0 computes candidate counts here (auto mode) and RETURNS them;
    # evoagentx's optimize() expects them on self (num_instruct_candidates).
    if self.auto is not None:
        self.num_instruct_candidates = n_instruct
        self.num_fewshot_candidates = n_fewshot
    return num_trials, valset, minibatch


def _compat_print_auto(self, num_trials, minibatch, valset):
    return _dspy_mipro.MIPROv2._print_auto_run_settings(
        self, num_trials, minibatch, valset,
        getattr(self, "num_fewshot_candidates", None) or 0,
        getattr(self, "num_instruct_candidates", None) or 0)


_MiproOptimizer._set_hyperparams_from_run_mode = _compat_set_hyperparams
_MiproOptimizer._print_auto_run_settings = _compat_print_auto


def _compat_propose_instructions(
    self,
    program,
    trainset,
    demo_candidates,
    view_data_batch_size,
    program_aware_proposer,
    data_aware_proposer,
    tip_aware_proposer,
    fewshot_aware_proposer,
):
    """evoagentx's _propose_instructions, adapted to dspy 3.3.0: temperature
    moved from the call (T=...) to GroundedProposer(init_temperature=...)."""
    from dspy.propose.grounded_proposer import GroundedProposer
    from dspy.teleprompt.utils import get_signature
    from evoagentx.core.logging import logger as _logger

    _logger.info("==> STEP 2: PROPOSE INSTRUCTION CANDIDATES <==")
    proposer = GroundedProposer(
        program=program,
        trainset=trainset,
        prompt_model=self.prompt_model,
        view_data_batch_size=view_data_batch_size,
        program_aware=program_aware_proposer,
        use_dataset_summary=data_aware_proposer,
        use_task_demos=fewshot_aware_proposer,
        num_demos_in_context=3,
        use_tip=tip_aware_proposer,
        set_tip_randomly=tip_aware_proposer,
        use_instruct_history=False,
        set_history_randomly=False,
        verbose=self.verbose,
        rng=self.rng,
        init_temperature=self.init_temperature,
    )
    _logger.info(f"Proposing N={self.num_instruct_candidates} instructions...")
    instruction_candidates = proposer.propose_instructions_for_program(
        trainset=trainset,
        program=program,
        demo_candidates=demo_candidates,
        trial_logs={},
        N=self.num_instruct_candidates,
    )
    for i, pred in enumerate(program.predicts):
        _logger.info(f"Proposed Instructions for Predictor {i}:\n")
        instruction_candidates[i][0] = get_signature(pred).instructions
        for j, instruction in enumerate(instruction_candidates[i]):
            _logger.info(f"{j}: {instruction}\n")
    return instruction_candidates


_MiproOptimizer._propose_instructions = _compat_propose_instructions


def _skip_bootstrap_in_zeroshot(self, program, trainset, seed, teacher=None):
    """Zero-shot mode (max_*_demos=0) must not bootstrap at all. evoagentx's
    override still runs 6 demo-set bootstraps (each = a full trainset replay
    through the program with a teacher) once num_fewshot_candidates is
    backfilled — many wasted hours under our per-example cost."""
    if self.max_bootstrapped_demos == 0 and self.max_labeled_demos == 0:
        from evoagentx.core.logging import logger as _logger
        _logger.info("zero-shot mode: skipping few-shot bootstrapping entirely")
        return None
    return _orig_bootstrap(self, program, trainset, seed, teacher)


_orig_bootstrap = _MiproOptimizer._bootstrap_fewshot_examples
_MiproOptimizer._bootstrap_fewshot_examples = _skip_bootstrap_in_zeroshot


# MiproLMWrapper stores the EvoAgentX LiteLLM OBJECT in `self.model`, but
# dspy 3.3.0's LM internals (supported_params, _provider_name) expect a
# model-NAME string there. Keep the string in .model for dspy and route
# forward() to the real model via .eax_model.
from evoagentx.optimizers.mipro_optimizer import MiproLMWrapper as _MiproLMWrapper  # noqa: E402

_orig_lm_init = _MiproLMWrapper.__init__


def _patched_lm_init(self, model, *args, **kwargs):
    _orig_lm_init(self, model, *args, **kwargs)
    self.eax_model = model
    self.model = getattr(getattr(model, "config", None), "model",
                         "deepseek/deepseek-v4-flash")


def _patched_lm_forward(self, prompt=None, messages=None, **kwargs):
    response = self.eax_model.generate(prompt=prompt, messages=messages)
    return [response.content]


def _patched_lm_copy(self, **kwargs):
    from copy import deepcopy as _deepcopy
    new_config = _deepcopy(self.eax_model.config)
    new_kwargs = {}
    for key, value in kwargs.items():
        if hasattr(new_config, key):
            setattr(new_config, key, value)
        if (key in self.kwargs) or (not hasattr(self, key)):
            new_kwargs[key] = value
    new_model = self.eax_model.__class__(config=new_config)
    return _MiproLMWrapper(new_model, **new_kwargs)


_MiproLMWrapper.__init__ = _patched_lm_init
_MiproLMWrapper.forward = _patched_lm_forward
_MiproLMWrapper.__call__ = lambda self, prompt=None, messages=None, **kw: self.forward(prompt, messages)
_MiproLMWrapper.copy = _patched_lm_copy


# Concurrent `SentenceTransformer(...)` construction from worker threads hits
# a transformers meta-tensor race; and reloading bge-small for every example
# is pure waste. Load it ONCE in the main thread and hand the same instance
# to every consumer (the memory layer's embedding wrapper and the dedup
# embedder). encode() is thread-safe enough for our read-only usage.
import sentence_transformers as _st_pkg  # noqa: E402
from evoagentx.rag.embeddings import huggingface_embedding as _hfe  # noqa: E402

_shared_st_model = None
_real_st_cls = _st_pkg.SentenceTransformer


def _st_singleton(model_name, *args, **kwargs):
    global _shared_st_model
    if _shared_st_model is None:
        _shared_st_model = _real_st_cls(model_name, *args, **kwargs)
    return _shared_st_model


def prewarm_embedder():
    """Call from the main thread BEFORE optimization starts."""
    import credit_risk_demo
    credit_risk_demo._get_embedder()  # dedup embedder (via package-level patch)
    _st_singleton("BAAI/bge-small-en-v1.5", device="cpu")


_st_pkg.SentenceTransformer = _st_singleton       # late binders (dedup embedder)
_hfe.SentenceTransformer = _st_singleton          # early-bound module attribute
# ---------------------------------------------------------------------------

TRAIN_JSONL = "credit_risk/dataset/v0.2/train.jsonl"
OUT_DIR = os.path.join("credit_risk", "output", "mipro")
STORES_DIR = os.path.join(OUT_DIR, "stores")

MAX_ACTIVE_DAYS = 25     # cost cap per sample (<=15 leaves only 2 samples)
N_TRAIN_POS, N_TRAIN_NEG = 8, 8
N_VAL_POS, N_VAL_NEG = 3, 3
SELECT_SEED = 42

LEVEL_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

# ---------------------------------------------------------------------------
# Seed instructions = the current production prompt cores (placeholders are
# the node's inputs; MIPRO rewrites everything else, including calibration)
# ---------------------------------------------------------------------------

EXTRACT_INSTRUCTION = """Extract all credit-risk-relevant events about {company} from today's news below.
The news has two sections: assess ONLY the items under 'Today's new items'; the 'Recent news from previous days' section is already-assessed context — never extract events from it.
Return ONLY a JSON list of objects with keys: event_type, severity, sentiment, date, confirmed, summary.
If no relevant event exists, return an empty list [].

News:
{news_batch}

(Context only — the company's current risk profile and similar historical cases follow; do NOT extract events from them:
{profile}
{cases}
)"""

ANALYSIS_INSTRUCTION = """Company: {company}

Events extracted from today's news batch:
{events}

The company's long-term risk profile ('None' means no profile yet):
{profile}

Similar historical cases retrieved from the case library ('None' means no relevant case):
{cases}

Analyze:
1. Which events are the most material for credit risk?
2. Compared with the profile's risk_focus and trajectory, is the risk theme new, recurring, escalating or improving?
3. Do any of the retrieved similar cases match the current situation? If so, what happened next in those cases, and what does that imply here? Also respect the profile's reflection (calibration notes and watch items) if present.
If the events list is empty, simply state that no credit-risk-relevant events were found in today's news and that the existing profile view is unchanged.
Be specific and cite the facts."""

VERDICT_INSTRUCTION = """Company: {company}

Extracted events:
{events}

Analysis:
{analysis}

The company's long-term risk profile, including its trajectory and reflection ('None' means no profile yet):
{profile}

Issue the final verdict. Return ONLY the JSON object specified in the rubric.
Calibration rules:
- Today's events update the EXISTING profile view; they do not replace it. Confirmed unresolved events already in the profile (e.g. an uncured default) dominate the verdict: a new unconfirmed or less severe event must NOT lower the score below what the unresolved confirmed events imply. The rubric's unconfirmed-rumor cap applies only when no confirmed critical event is outstanding.
- Respect the profile's reflection (calibration notes and watch items) when present.
If the analysis states that no credit-risk-relevant events were found, the verdict must CARRY FORWARD the existing profile view instead of resetting: reuse the risk_level, score and trend from the profile's latest trajectory entry, keep key_evidence empty, and give a one-line rationale that today's news was uneventful so the existing assessment is unchanged. Only when no profile exists yet (no prior trajectory entry), return the JSON object with risk_level "low", score 0, trend "stable", confidence "high", empty key_evidence, and a one-line rationale saying today's news was uneventful."""

EXTRACT_SYS = ("You are a credit analyst assistant that extracts structured "
               "risk events from news.")
ANALYSIS_SYS = ("You are a senior credit risk analyst. You compare current events "
                "with the company's risk profile and analogous historical cases to "
                "judge the trend.")
VERDICT_SYS = ("You are the chief credit officer. You issue the final risk verdict "
               "following the rubric strictly.")


# ---------------------------------------------------------------------------
# Per-example isolated long-term memory (threads must not share FAISS/SQLite)
# ---------------------------------------------------------------------------

def build_memory_at(store_dir: str) -> LongTermMemory:
    if os.path.exists(store_dir):
        shutil.rmtree(store_dir)
    os.makedirs(store_dir, exist_ok=True)
    store_config = StoreConfig(
        dbConfig=DBConfig(db_name="sqlite", path=os.path.join(store_dir, "memory.sql")),
        vectorConfig=VectorStoreConfig(vector_name="faiss", dimensions=384, index_type="flat_l2"),
        graphConfig=None,
        path=os.path.join(store_dir, "indexing"),
    )
    rag_config = RAGConfig(
        reader=ReaderConfig(recursive=False, exclude_hidden=True, errors="ignore", encoding="utf-8"),
        chunker=ChunkerConfig(strategy="simple", chunk_size=512, chunk_overlap=0, max_chunks=None),
        embedding=EmbeddingConfig(provider="huggingface", model_name="BAAI/bge-small-en-v1.5", device="cpu"),
        index=IndexConfig(index_type="vector"),
        retrieval=RetrievalConfig(retrivel_type="vector", postprocessor_type="simple",
                                  top_k=10, similarity_cutoff=0.0),
    )
    return LongTermMemory(
        storage_handler=StorageHandler(storageConfig=store_config),
        rag_config=rag_config,
        default_corpus_id="credit_risk_archive",
    )


# ---------------------------------------------------------------------------
# The program: full daily replay of one sample
# ---------------------------------------------------------------------------

class CreditRiskProgram:
    def __init__(self, llm, taxonomy: str, rubric: str):
        self.llm = llm
        self.taxonomy = taxonomy
        self.rubric = rubric
        # optimizable attributes (tracked in the MiproRegistry)
        self.extract_instruction = EXTRACT_INSTRUCTION
        self.analysis_instruction = ANALYSIS_INSTRUCTION
        self.verdict_instruction = VERDICT_INSTRUCTION

    def save(self, path: str):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "extract_instruction": self.extract_instruction,
                "analysis_instruction": self.analysis_instruction,
                "verdict_instruction": self.verdict_instruction,
            }, f, ensure_ascii=False, indent=2)

    def load(self, path: str):
        with open(path, encoding="utf-8") as f:
            params = json.load(f)
        self.extract_instruction = params["extract_instruction"]
        self.analysis_instruction = params["analysis_instruction"]
        self.verdict_instruction = params["verdict_instruction"]

    def _node(self, system: str, user: str) -> str:
        response = _retry(lambda: self.llm.generate(prompt=user, system_message=system))
        return str(response.content)

    def __call__(self, sample: str) -> Tuple[str, dict]:
        s = json.loads(sample)
        company = s["company"]["name"] or s["company"]["symbol"]

        store_dir = tempfile.mkdtemp(prefix=f"{s['sample_id']}_", dir=STORES_DIR)
        try:
            ltm = build_memory_at(store_dir)
            seed_case_library(ltm)
            stm = ShortTermMemory(max_size=12)
            archive = {}

            by_date = {}
            for row in s["news"]:
                by_date.setdefault(row["date"], []).append(row)

            max_level, final_level, final_score = 0, None, None
            first_flag_date = None
            last_io = {}
            for date in sorted(by_date):
                texts = [f"{r['date']} | {r['title']}" for r in by_date[date]]
                new_items, _ = dedup_news(archive, company, texts)
                if not new_items:
                    continue
                news_batch = stage_news(stm, company, new_items)
                profile, profile_id = recall_profile(ltm, company)
                profile_text = (json.dumps(profile, ensure_ascii=False, indent=2)
                                if profile else "None")
                cases_text = retrieve_similar_cases(ltm, query_text=news_batch)

                events_raw = self._node(
                    EXTRACT_SYS,
                    f"{self.taxonomy}\n\n" + self.extract_instruction.format(
                        company=company, news_batch=news_batch,
                        profile=profile_text, cases=cases_text))
                events = parse_json_from_llm_output(events_raw)
                events = events if isinstance(events, list) else []

                analysis = self._node(
                    ANALYSIS_SYS,
                    self.analysis_instruction.format(
                        company=company, events=events_raw,
                        profile=profile_text, cases=cases_text))

                verdict_raw = self._node(
                    VERDICT_SYS,
                    f"{self.rubric}\n\n" + self.verdict_instruction.format(
                        company=company, events=events_raw,
                        analysis=analysis, profile=profile_text))
                verdict = parse_json_from_llm_output(verdict_raw)
                verdict = verdict if isinstance(verdict, dict) else {}

                level = LEVEL_ORDER.get(str(verdict.get("risk_level")))
                if level is not None:
                    final_level, final_score = level, verdict.get("score")
                    max_level = max(max_level, level)
                    if level >= LEVEL_ORDER["high"] and first_flag_date is None:
                        first_flag_date = date

                # dedup archive is maintained inside dedup_news; also persist
                # accepted news for future-crawl fidelity
                archive_news(ltm, archive, company, date, new_items)

                if events:
                    new_profile = _retry(lambda: update_profile(
                        self.llm, company, date, profile, events, verdict))
                    reflection = _retry(lambda: reflect(self.llm, company, new_profile, verdict))
                    if reflection:
                        new_profile["reflection"] = reflection
                    save_profile(ltm, company, new_profile, profile_id)

                last_io = {
                    "company": company, "news_batch": news_batch,
                    "profile": profile_text, "cases": cases_text,
                    "events": events_raw, "analysis": analysis, "verdict": verdict_raw,
                }

            lead_days = None
            if first_flag_date and s["type"] == "positive":
                event = datetime.strptime(s["label"]["event_date"], "%Y-%m-%d")
                lead_days = (event - datetime.strptime(first_flag_date, "%Y-%m-%d")).days
            prediction = json.dumps({
                "final_level": final_level, "final_score": final_score,
                "max_level": max_level, "first_flag_date": first_flag_date,
                "lead_days": lead_days,
            })
            execution_data = last_io or {"company": company}
            return prediction, execution_data
        finally:
            shutil.rmtree(store_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Benchmark: v0.2 train split, cost-capped subsets
# ---------------------------------------------------------------------------

class CreditRiskBenchmark(Benchmark):

    def _load_data(self):
        import random
        with open(self.path, encoding="utf-8") as f:
            samples = [json.loads(line) for line in f]
        capped = [s for s in samples
                  if len({r["date"] for r in s["news"]}) <= MAX_ACTIVE_DAYS]
        pos = [s for s in capped if s["type"] == "positive"]
        neg = [s for s in capped if s["type"] == "negative"]
        random.seed(SELECT_SEED)
        random.shuffle(pos)
        random.shuffle(neg)

        def to_example(s):
            return {"sample": json.dumps(s, ensure_ascii=False),
                    "sample_id": s["sample_id"],
                    "label": {"type": s["type"], "event_date": s["label"]["event_date"]}}

        self._train_data = [to_example(s) for s in
                            pos[:N_TRAIN_POS] + neg[:N_TRAIN_NEG]]
        self._dev_data = [to_example(s) for s in
                          pos[N_TRAIN_POS:N_TRAIN_POS + N_VAL_POS]
                          + neg[N_TRAIN_NEG:N_TRAIN_NEG + N_VAL_NEG]]

    def get_input_keys(self):
        return ["sample"]

    def _get_id(self, example: Any) -> Any:
        return example["sample_id"]

    def _get_label(self, example: Any) -> Any:
        return example["label"]

    def evaluate(self, prediction: Any, label: Any) -> dict:
        try:
            pred = json.loads(prediction) if isinstance(prediction, str) else prediction
            max_level = int(pred.get("max_level") or 0)
        except (json.JSONDecodeError, TypeError, AttributeError, ValueError):
            return {"score": 0.0}
        if label["type"] == "positive":
            detect = 1.0 if max_level >= LEVEL_ORDER["high"] else (
                0.5 if max_level >= LEVEL_ORDER["medium"] else 0.0)
            lead = pred.get("lead_days") or 0
            return {"score": 0.8 * detect + 0.2 * min(max(lead, 0), 180) / 180,
                    "detection": detect}
        return {"score": {0: 1.0, 1: 0.8, 2: 0.2, 3: 0.0}[max_level],
                "detection": 0.0}


# ---------------------------------------------------------------------------

def main():
    selftest = "--selftest" in sys.argv

    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("DEEPSEEK_API_KEY not found.")

    os.makedirs(STORES_DIR, exist_ok=True)
    prewarm_embedder()  # load bge-small once, in the main thread
    from llm import get_evoagentx_llm  # noqa: E402
    llm = get_evoagentx_llm()  # default provider from llm/providers.json
    skills = SkillManager(skill_paths=SKILLS_DIR)
    taxonomy = skills.get_skill("credit_risk_taxonomy").content
    rubric = skills.get_skill("risk_scoring_rubric").content

    program = CreditRiskProgram(llm, taxonomy, rubric)
    benchmark = CreditRiskBenchmark(name="credit_risk", path=TRAIN_JSONL, mode="all")
    print(f"train subset: {len(benchmark._train_data)} examples, "
          f"val subset: {len(benchmark._dev_data)} examples")

    if selftest:
        example = benchmark._train_data[0]
        prediction, _ = program(sample=example["sample"])
        print("prediction:", prediction)
        print("metric:", benchmark.evaluate(prediction, example["label"]))
        return

    registry = MiproRegistry()
    registry.track(program, "extract_instruction",
                   input_names=["company", "news_batch", "profile", "cases"],
                   output_names=["events"])
    registry.track(program, "analysis_instruction",
                   input_names=["company", "events", "profile", "cases"],
                   output_names=["analysis"])
    registry.track(program, "verdict_instruction",
                   input_names=["company", "events", "analysis", "profile"],
                   output_names=["verdict"])

    optimizer = MiproOptimizer(
        registry=registry,
        program=program,
        optimizer_llm=llm,
        max_bootstrapped_demos=0,
        max_labeled_demos=0,
        auto=None,
        num_candidates=4,
        max_steps=5,
        minibatch=False,
        minibatch_size=10,
        num_threads=6,
        eval_rounds=1,
        seed=9,
        save_path=OUT_DIR,
    )
    optimizer.optimize(dataset=benchmark, metric_name="score")
    # optimize() returns None and resets the model; the best prompts live at:
    print(f"best prompts -> {os.path.join(OUT_DIR, 'best_program.json')}")


if __name__ == "__main__":
    main()
