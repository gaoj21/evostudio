"""Steps 3–5 — orchestration: the agentic loop, action, and memory update.

AlertPipeline.ingest(items) runs one sourcing batch end to end:

    raw items ──(1 sourcing)──> candidates ──(2 grounding)──> grounded groups
        │                                             │ dropped: logged
        ▼                                             ▼
    per (obligor, date): dedup vs archive -> detect -> investigate
        -> reflect -> decide ──(4 action)──> Alert | suppression log
                                        │
                        (5 memory update): alert repository (LTM),
                        risk profile (focus + trajectory), news archive

The alert repository and the news archive live in the same long-term memory
as the risk profiles, so "related past alerts" are retrievable by similarity
(investigate) rather than only by exact obligor id.
"""

import json
from dataclasses import dataclass, field

from evoagentx.core.message import Message, MessageType

from . import agents
from .obligors import ObligorRegistry
from .sourcing import (CandidateItem, RawItem, candidates_from_filings,
                       candidates_from_news)

ALERT_RECORD_TYPE = "alert"


class AlertRepository:
    """Past alerts, stored in long-term memory (record_type="alert")."""

    def __init__(self, ltm):
        self.ltm = ltm

    def add(self, obligor, date: str, source: str, detection: dict,
            decision: dict) -> str:
        record = {
            "record_type": ALERT_RECORD_TYPE,
            "obligor_id": obligor.obligor_id,
            "obligor": obligor.name,
            "date": date,
            "source": source,
            "topic": detection.get("topic"),
            "severity": detection.get("severity"),
            "confidence": detection.get("confidence"),
            "risk_level": decision.get("risk_level"),
            "score": decision.get("score"),
            "summary": detection.get("summary"),
            "rationale": decision.get("rationale"),
        }
        message = Message(content=json.dumps(record, ensure_ascii=False),
                          action="alert_issued",
                          wf_goal=f"credit_risk:{obligor.obligor_id}",
                          agent="decide_agent",
                          msg_type=MessageType.RESPONSE)
        return self.ltm.add(message)[0]

    def alerts_for(self, obligor_id: str, n: int = 10) -> list:
        results = self.ltm.search(f"credit alert {obligor_id}", n=20)
        out = []
        for message, _ in results:
            try:
                rec = json.loads(str(message.content))
                if isinstance(rec, str):
                    rec = json.loads(rec)
            except (json.JSONDecodeError, TypeError):
                continue
            if (isinstance(rec, dict)
                    and rec.get("record_type") == ALERT_RECORD_TYPE
                    and rec.get("obligor_id") == obligor_id):
                out.append(rec)
        return sorted(out, key=lambda r: r.get("date", ""))[-n:]


@dataclass
class PipelineResult:
    alerts: list = field(default_factory=list)
    suppressed: list = field(default_factory=list)
    dropped_unmatched: list = field(default_factory=list)
    log: list = field(default_factory=list)


class AlertPipeline:
    """The full sourcing -> grounding -> loop -> action -> memory pipeline."""

    def __init__(self, llm, registry: ObligorRegistry, ltm, stm_factory,
                 taxonomy: str, rubric: str, memory_fns: dict,
                 woe_evaluator=None, human_feedback=None):
        """
        memory_fns: the reusable memory helpers from credit_risk_demo
        (dedup_news, archive_news, recall_profile, save_profile,
        update_profile, retrieve_similar_cases, reflect) — injected so this
        package stays decoupled from the demo module's import side effects.
        """
        self.llm = llm
        self.registry = registry
        self.ltm = ltm
        self.stm_factory = stm_factory
        self.taxonomy = taxonomy
        self.rubric = rubric
        self.mf = memory_fns
        self.alert_repo = AlertRepository(ltm)
        self.woe_evaluator = woe_evaluator          # step 7, optional
        self.human_feedback = human_feedback        # step 7, optional
        self.news_archive = {}                      # dedup state per company

    # -- step 2 --------------------------------------------------------------

    def _ground(self, cands: list, result: PipelineResult) -> dict:
        """Match candidates against the registry; group by (obligor, date)."""
        groups = {}
        for cand in cands:
            ob = None
            how = None
            if cand.candidate_cik:
                ob = self.registry.match_cik(cand.candidate_cik)
                how = "cik" if ob else None
            if ob is None and cand.candidate_name:
                ob, how = self.registry.match_name(cand.candidate_name)
            if ob is None:
                result.dropped_unmatched.append({
                    "title": cand.item.title[:80],
                    "candidate": cand.candidate_name or cand.candidate_cik,
                })
                continue
            groups.setdefault((ob, cand.item.date), []).append(cand)
        return groups

    # -- steps 3–5 -----------------------------------------------------------

    def _process_group(self, obligor, date: str, cands: list,
                       result: PipelineResult):
        source = cands[0].item.source
        lines = [f"{c.item.date} | {c.item.title}" for c in cands]
        if cands[0].item.text:    # 8-K bodies travel with the title line
            lines = [f"{ln}\n  body: {c.item.text[:2000]}"
                     for ln, c in zip(lines, cands)]

        new_items, _ = self.mf["dedup_news"](self.news_archive, obligor.name,
                                             lines)
        if not new_items:
            result.log.append(f"{obligor.name} {date}: all duplicates, skip")
            return

        detection = agents.detect(self.llm, self.taxonomy, obligor, date,
                                  source, "\n".join(new_items))
        if not detection.get("is_main_subject") or \
                detection.get("severity") in (None, "none"):
            result.suppressed.append({"obligor": obligor.name, "date": date,
                                      "reason": "not main subject / no event",
                                      "detection": detection})
            return

        context = agents.investigate(
            self.ltm, self.alert_repo, obligor, "\n".join(new_items),
            self.mf["recall_profile"], self.mf["retrieve_similar_cases"])
        reflection = agents.reflect(self.llm, obligor, date, detection,
                                    context)
        # reflection overrides detection where it adjusted
        validated = {**detection,
                     **{k: reflection[k] for k in
                        ("topic", "severity", "confidence")
                        if reflection.get(k) is not None}}
        decision = agents.decide(self.llm, self.rubric, obligor, date,
                                 validated, reflection, context)

        # step 4 — action
        if decision.get("action") == "alert":
            alert = {"obligor": obligor.name, "obligor_id": obligor.obligor_id,
                     "date": date, "source": source, "detection": validated,
                     "reflection": reflection, "decision": decision}
            # step 7 — weight of evidence + human feedback
            if self.woe_evaluator:
                alert["weight_of_evidence"] = self.woe_evaluator(
                    alert, corroboration=len(new_items), context=context)
            if self.human_feedback:
                fb = self.human_feedback(alert)
                if fb:
                    alert["human_feedback"] = fb
            self.alert_repo.add(obligor, date, source, validated, decision)
            result.alerts.append(alert)
        else:
            result.suppressed.append({"obligor": obligor.name, "date": date,
                                      "reason": decision.get("rationale"),
                                      "detection": validated})

        # step 5 — memory update: news archive + risk profile
        self.mf["archive_news"](self.ltm, self.news_archive, obligor.name,
                                date, new_items)
        events = [{"event_type": validated.get("topic"),
                   "severity": validated.get("severity"),
                   "summary": validated.get("summary")}]
        profile = self.mf["update_profile"](
            self.llm, obligor.name, date, context.get("profile"),
            events, {"risk_level": decision.get("risk_level"),
                     "score": decision.get("score"),
                     "trend": (context.get("profile") or {})
                     .get("trajectory", [{}])[-1].get("trend", "stable")})
        self.mf["save_profile"](self.ltm, obligor.name, profile,
                                context.get("profile_id"))

    # -- step 1 + driver ------------------------------------------------------

    def ingest(self, items: list) -> PipelineResult:
        result = PipelineResult()
        news_items = [it for it in items if it.source == "news"]
        filing_items = [it for it in items if it.source == "8k"]
        cands = []
        if news_items:
            cands += candidates_from_news(self.llm, news_items)   # step 1a
        if filing_items:
            cands += candidates_from_filings(filing_items)        # step 1b
        groups = self._ground(cands, result)                      # step 2
        for (obligor, date), group in sorted(groups.items(),
                                             key=lambda kv: kv[0][1]):
            self._process_group(obligor, date, group, result)     # steps 3–5
        return result


def build_pipeline(llm, registry: ObligorRegistry, store_dir: str = None,
                   fresh: bool = True, **kwargs) -> AlertPipeline:
    """Wire up LLM + LTM + skills + demo memory helpers into a pipeline."""
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(
        _os.path.dirname(_os.path.abspath(__file__))))
    import credit_risk_demo as demo
    from evoagentx.skills import SkillManager

    if store_dir:
        demo.STORE_DIR = store_dir
    ltm = demo.build_memory(fresh=fresh)
    demo.seed_case_library(ltm)
    skills = SkillManager(skill_paths=demo.SKILLS_DIR)
    memory_fns = {
        "dedup_news": demo.dedup_news,
        "archive_news": demo.archive_news,
        "recall_profile": demo.recall_profile,
        "save_profile": demo.save_profile,
        "update_profile": demo.update_profile,
        "retrieve_similar_cases": demo.retrieve_similar_cases,
        "reflect": demo.reflect,
    }
    return AlertPipeline(
        llm=llm, registry=registry, ltm=ltm,
        stm_factory=demo.ShortTermMemory,
        taxonomy=skills.get_skill("credit_risk_taxonomy").content,
        rubric=skills.get_skill("risk_scoring_rubric").content,
        memory_fns=memory_fns, **kwargs)
