"""Credit-risk preset nodes and template graph for the Studio palette.

The four presets mirror credit_risk/agentic_pipeline/agents.py
(detect -> investigate -> reflect -> decide). Prompts are adapted to the
Studio data-flow contract (inputs arrive by name; the pipeline's obligor/date
arguments become {company}/{window_end}, sourced from the credit_risk feed):

- taxonomy / rubric skill texts are inlined (braces escaped for str.format)
- investigate is a pure retrieval step in the pipeline; here it is an LLM
  summarizer over the node's long-term memory (use_long_term_memory=true),
  which accumulates its own IO history across runs
- decide keeps the pipeline's action/risk_level/score/rationale contract (the
  rubric skill's own output contract is dropped in favor of it)
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILLS_DIR = _REPO_ROOT / "credit_risk" / "skills"


def _skill_text(name: str) -> str:
    path = _SKILLS_DIR / name / "SKILL.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _esc(text: str) -> str:
    """Escape braces so the text survives str.format() in canvas prompts."""
    return text.replace("{", "{{").replace("}", "}}")


def _io(name, type_="str", description="", required=True):
    return {"name": name, "type": type_, "description": description, "required": required}


def credit_risk_presets() -> list[dict]:

    source_news_prompt = """You are the sourcing step of a credit-risk monitoring pipeline. For each news item below, decide which company is the MAIN SUBJECT of the item (not merely mentioned — counterparties, peers and list entries do not count).

Watch-list candidate: {company}
As-of date: {window_end}

News items:
{news_batch}

Return ONLY JSON: {{"companies": ["<main-subject company name>", ...]}}
Deduplicate; if no item has a clear company as main subject, return {{"companies": []}}."""

    source_8k_prompt = """You are the 8-K sourcing step of a credit-risk monitoring pipeline. The filings below are SEC 8-K submissions. Extract the registrant's CIK number (from the filing header/metadata text) and the reported 8-K item numbers.

Filings:
{filing_batch}

Return ONLY JSON: {{"cik": "<digits>", "items": ["Item 1.03", ...]}}
If no CIK can be found, return {{"cik": "", "items": []}}."""

    grounding_prompt = """You are the grounding step of a credit-risk monitoring pipeline. Only items whose company matches the internal obligor list proceed downstream — everything else is dropped.

Watch-list candidate: {company}
Main-subject companies extracted from news: {news_entities}
8-K filing metadata: {filing_entities}

Use the match_cik tool with the CIK from the filing metadata first (strongest evidence). If there is no CIK match, use the match_company_name tool for the candidate and each extracted company name.

Return ONLY JSON: {{"matched": true|false, "obligor": "<canonical name from the tool, or empty>", "cik": "<cik or empty>", "how": "exact|fuzzy|cik|none"}}
matched=true ONLY when a tool returned matched=true. When matched=false, downstream steps must drop this item."""

    detect_prompt = _esc(_skill_text("credit_risk_taxonomy")) + """

Obligor under watch: {company}
As-of date: {window_end}

Grounding result:
{grounding}

If the grounding result has "matched": false, the obligor is not on our watch list — return ONLY JSON: {{"is_main_subject": false, "topic": "none", "severity": "low", "confidence": 1.0, "summary": "obligor not covered, dropped at grounding"}} and do nothing else.

News items:
{news_batch}

Tasks:
1. Is {company} the MAIN SUBJECT of these items (not merely mentioned)?
2. Classify the alert topic using the taxonomy's event types (or "none").
3. Score risk severity (critical/high/medium/low/positive) and your
   confidence (0-1).

Return ONLY JSON: {{"is_main_subject": true|false, "topic": "...", "severity": "...", "confidence": 0.0, "summary": "one line, cite the fact"}}"""

    investigate_prompt = """You are the investigation step of a credit-risk monitoring pipeline. Assemble the context pack for the obligor from the detection below and the long-term memories appended at the end of this prompt (outputs of previous runs: past detections, context packs and decisions for related obligors).

Obligor: {company}

Detection:
{detection}

Produce a compact JSON context pack for the downstream agents:
{{"profile": "current risk picture of the obligor, or 'None'", "past_alerts": "relevant past alerts, or 'None'", "cases": "similar historical cases, or 'None'"}}

Return ONLY JSON."""

    reflect_prompt = """Obligor: {company}

Detection under review:
{detection}

Context pack (risk profile, past alerts, similar cases; 'None' if none yet):
{context}

Revalidate the detection:
- Does the obligor's history support this topic and severity? Escalation
  against unresolved confirmed events in the profile must be upheld;
  re-reports of already-alerted facts with no new content should be damped.
- Is the confidence honest given the evidence?

Return ONLY JSON: {{"topic": "...", "severity": "...", "confidence": 0.0, "adjusted": true|false, "rationale": "one line"}}"""

    decide_prompt = _esc(_skill_text("risk_scoring_rubric")) + """

Obligor: {company}

Detection (validated):
{detection}

Reflection:
{reflection}

Context pack:
{context}

Issue the action. Rules:
- "alert" only for material, credit-relevant developments; routine or
  duplicate content is "suppress".
- Confirmed unresolved events already in the profile dominate: a weaker new
  item must NOT lower the level below what they imply.
- If the detection found no relevant event, CARRY FORWARD the existing view
  (suppress the alert).

Return ONLY JSON: {{"action": "alert"|"suppress", "risk_level": "low"|"medium"|"high"|"critical", "score": 0-100, "rationale": "one line"}}"""

    return [
        {
            "type": "cr_source_news",
            "label": "CR Source News",
            "description": "Entity extraction: which companies are the main subject of each news item.",
            "defaults": {
                "description": "Extract main-subject companies from the news batch.",
                "inputs": [
                    _io("news_batch", description="News items, one per line"),
                    _io("company", description="Watch-list candidate name"),
                    _io("window_end", description="As-of date (YYYY-MM-DD)"),
                ],
                "outputs": [_io("news_entities", description='JSON: {"companies": [...]}')],
                "prompt": source_news_prompt,
                "system_prompt": "You are the sourcing agent of a credit-risk monitoring pipeline. Output valid JSON only.",
                "parse_mode": "str",
            },
        },
        {
            "type": "cr_source_8k",
            "label": "CR Source 8-K",
            "description": "Extract the registrant CIK and item numbers from 8-K filings.",
            "defaults": {
                "description": "Extract CIK and item numbers from 8-K filings.",
                "inputs": [
                    _io("filing_batch", description="8-K filings: date, form, items, text"),
                ],
                "outputs": [_io("filing_entities", description='JSON: {"cik": ..., "items": [...]}')],
                "prompt": source_8k_prompt,
                "system_prompt": "You are the 8-K sourcing agent of a credit-risk monitoring pipeline. Output valid JSON only.",
                "parse_mode": "str",
            },
        },
        {
            "type": "cr_grounding",
            "label": "CR Grounding",
            "description": "Match candidates against the internal obligor list (ObligorMatch tools); unmatched items are dropped.",
            "defaults": {
                "description": "Ground candidates against the internal obligor list.",
                "inputs": [
                    _io("company", description="Watch-list candidate name"),
                    _io("news_entities", description="JSON from the news sourcing step"),
                    _io("filing_entities", description="JSON from the 8-K sourcing step"),
                ],
                "outputs": [_io("grounding", description="JSON: matched, obligor, cik, how")],
                "prompt": grounding_prompt,
                "system_prompt": "You are the grounding agent of a credit-risk monitoring pipeline. Use the provided tools. Output valid JSON only.",
                "parse_mode": "str",
                "tool_names": ["ObligorMatchToolkit"],
            },
        },
        {
            "type": "cr_detect",
            "label": "CR Detect",
            "description": "Confirm the obligor is the main subject, classify the alert topic, score severity/confidence.",
            "defaults": {
                "description": "Detect credit-risk events about the obligor from the news batch.",
                "inputs": [
                    _io("company", description="The obligor under watch"),
                    _io("news_batch", description="News items, one per line"),
                    _io("window_end", description="As-of date (YYYY-MM-DD)"),
                    _io("grounding", description="Grounding JSON; matched=false drops the item"),
                ],
                "outputs": [_io("detection", description="JSON: is_main_subject, topic, severity, confidence, summary")],
                "prompt": detect_prompt,
                "system_prompt": "You are the detection agent of a credit-risk monitoring pipeline. Output valid JSON only.",
                "parse_mode": "str",
            },
        },
        {
            "type": "cr_investigate",
            "label": "CR Investigate",
            "description": "Assemble the context pack (profile / past alerts / similar cases) from long-term memory.",
            "defaults": {
                "description": "Gather the obligor's context pack from memory.",
                "inputs": [
                    _io("company", description="The obligor under watch"),
                    _io("detection", description="Detection JSON from the detect step"),
                ],
                "outputs": [_io("context", description="JSON: profile, past_alerts, cases")],
                "prompt": investigate_prompt,
                "system_prompt": "You are the investigation agent of a credit-risk monitoring pipeline. Output valid JSON only.",
                "parse_mode": "str",
                "use_long_term_memory": True,
            },
        },
        {
            "type": "cr_reflect",
            "label": "CR Reflect",
            "description": "Recalibrate topic/severity/confidence against the obligor's history.",
            "defaults": {
                "description": "Calibrate the detection against profile and history.",
                "inputs": [
                    _io("company", description="The obligor under watch"),
                    _io("detection", description="Detection JSON from the detect step"),
                    _io("context", description="Context pack JSON from the investigate step"),
                ],
                "outputs": [_io("reflection", description="JSON: topic, severity, confidence, adjusted, rationale")],
                "prompt": reflect_prompt,
                "system_prompt": "You are the reflection agent of a credit-risk monitoring pipeline. You calibrate, you do not invent facts. Output valid JSON only.",
                "parse_mode": "str",
            },
        },
        {
            "type": "cr_decide",
            "label": "CR Decide",
            "description": "Issue the final action: alert or suppress, with risk level and score. LTM store acts as the alert repository (step 5).",
            "defaults": {
                "description": "Decide alert or suppress following the scoring rubric.",
                "inputs": [
                    _io("company", description="The obligor under watch"),
                    _io("detection", description="Validated detection JSON"),
                    _io("reflection", description="Reflection JSON from the reflect step"),
                    _io("context", description="Context pack JSON from the investigate step"),
                ],
                "outputs": [_io("decision", description="JSON: action, risk_level, score, rationale")],
                "prompt": decide_prompt,
                "system_prompt": "You are the decision agent of a credit-risk monitoring pipeline — the chief credit officer's delegate. Output valid JSON only.",
                "parse_mode": "str",
                "use_long_term_memory": True,
            },
        },
    ]


def source_presets() -> list[dict]:
    """Canvas input-source presets (kind="source" — executed in Python, not LLM)."""
    from source_apis import SOURCE_TYPE_SCHEMAS

    presets = []
    for type_, schema in SOURCE_TYPE_SCHEMAS.items():
        presets.append({
            "type": f"source_{type_}",
            "label": schema["label"],
            "description": schema["description"],
            "defaults": {
                "kind": "source",
                "description": schema["description"],
                "source": {
                    "type": type_,
                    **{f["name"]: f.get("default", "") for f in schema["config"]},
                },
                "inputs": [],
                "outputs": [
                    _io(name, description=f"{schema['label']} output", required=False)
                    for name in schema["outputs"]
                ],
            },
        })
    return presets


# Template layout: (preset type, node name, x, y)
_TEMPLATE_NODES = [
    ("source_credit_risk", "feed", -200, 170),
    ("cr_source_news", "source_news", 60, 60),
    ("cr_source_8k", "source_8k", 60, 280),
    ("cr_grounding", "grounding", 330, 170),
    ("cr_detect", "detect", 580, 170),
    ("cr_investigate", "investigate", 830, 170),
    ("cr_reflect", "reflect", 1080, 170),
    ("cr_decide", "decide", 1330, 170),
]

_TEMPLATE_EDGES = [
    ("feed", "source_news"),
    ("feed", "source_8k"),
    ("source_news", "grounding"),
    ("source_8k", "grounding"),
    ("grounding", "detect"),
    ("detect", "investigate"),
    ("investigate", "reflect"),
    ("reflect", "decide"),
]


def credit_risk_template() -> dict:
    """The full 8-step credit-risk monitoring flow as a canvas graph.

    source_news + source_8k (step 1) -> grounding (step 2, ObligorMatch tools)
    -> detect -> investigate -> reflect -> decide (step 3) -> decision is the
    action (step 4). investigate's LTM store plays the risk profile and
    decide's the alert repository (step 5); evaluation is the Review panel
    (step 7) and prompt optimization the Evolve panel (step 8).
    """
    presets = {p["type"]: p for p in credit_risk_presets() + source_presets()}
    tasks = []
    for ptype, node_name, x, y in _TEMPLATE_NODES:
        defaults = presets[ptype]["defaults"]
        task = {
            "name": node_name,
            "description": defaults["description"],
            "inputs": defaults["inputs"],
            "outputs": defaults["outputs"],
            "x": x,
            "y": y,
        }
        if defaults.get("kind") == "source":
            task["kind"] = "source"
            task["source"] = dict(defaults["source"])
        else:
            task.update({
                "prompt": defaults["prompt"],
                "system_prompt": defaults["system_prompt"],
                "parse_mode": defaults["parse_mode"],
                "tool_names": list(defaults.get("tool_names") or []),
                "use_long_term_memory": bool(defaults.get("use_long_term_memory")),
            })
        tasks.append(task)
    edges = [{"source": s, "target": t} for s, t in _TEMPLATE_EDGES]
    return {
        "id": "credit-risk-monitoring",
        "name": "Credit Risk Monitoring",
        "goal": "Monitor credit risk end to end: source news and 8-K filings, ground against the obligor list, then detect, investigate, reflect and decide alert or suppress.",
        "tasks": tasks,
        "edges": edges,
    }


def templates() -> list[dict]:
    return [{"id": "credit-risk-monitoring", "name": "Credit Risk Monitoring",
             "description": "Full 8-step flow: news/8-K sourcing → obligor grounding → detect → investigate → reflect → decide, fed by the credit_risk data source. Gray-zone alerts go to the Review panel (step 7); prompts can be tuned in the Evolve panel (step 8).",
             "graph": credit_risk_template()}]
