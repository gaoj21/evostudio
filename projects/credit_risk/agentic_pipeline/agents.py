"""Step 3 — Reasoning: the four-agent loop.

Detect -> Investigate -> Reflect -> Decide, per (obligor, day) batch of
grounded items:

- **Detect**   confirms the obligor is the main subject, classifies the
  alert topic (the closed credit-risk taxonomy), scores severity + confidence.
- **Investigate**  pulls the obligor's risk profile, past alerts and similar
  historical cases from memory (pure retrieval — no LLM call).
- **Reflect**  revalidates topic/severity/confidence against that history
  (calibration: repeated low-signal noise gets damped; escalation against an
  unresolved confirmed event gets upheld).
- **Decide**   issues the action: alert or suppress, with risk level, score
  and rationale (the scoring rubric skill governs levels).

Prompts are module-level constants so the self-evolving layer (step 8) can
register and optimize them (see optimize_mipro.py for the 3-node ancestor).
"""

import json

from evoagentx.core.module_utils import parse_json_from_llm_output

# ---------------------------------------------------------------------------
# Detect
# ---------------------------------------------------------------------------

DETECT_SYS = ("You are the detection agent of a credit-risk monitoring "
              "pipeline. Output valid JSON only.")

DETECT_PROMPT = """{taxonomy}

Obligor under watch: {obligor} (industry: {industry})

Source items for {date} ({source}):
{items_text}

Tasks:
1. Is {obligor} the MAIN SUBJECT of these items (not merely mentioned)?
2. Classify the alert topic using the taxonomy's event types (or "none").
3. Score risk severity (critical/high/medium/low/positive) and your
   confidence (0-1).

Return ONLY JSON: {{"is_main_subject": true|false, "topic": "...", "
"severity": "...", "confidence": 0.0, "summary": "one line, cite the fact"}}"""


def detect(llm, taxonomy: str, obligor, date: str, source: str,
           items_text: str) -> dict:
    resp = llm.generate(
        prompt=DETECT_PROMPT.format(
            taxonomy=taxonomy, obligor=obligor.name,
            industry=obligor.industry or "unknown", date=date,
            source=source, items_text=items_text),
        system_message=DETECT_SYS)
    d = parse_json_from_llm_output(resp.content)
    return d if isinstance(d, dict) else {}


# ---------------------------------------------------------------------------
# Investigate (pure memory retrieval — deliberately no LLM call)
# ---------------------------------------------------------------------------

def investigate(ltm, alert_repo, obligor, query_text: str,
                recall_profile, retrieve_similar_cases) -> dict:
    """Gather the context pack: current profile + past alerts + cases."""
    profile, profile_id = recall_profile(ltm, obligor.name)
    past_alerts = alert_repo.alerts_for(obligor.obligor_id)
    cases_text = retrieve_similar_cases(ltm, query_text=query_text)
    return {
        "profile": profile,
        "profile_id": profile_id,
        "past_alerts": past_alerts,
        "cases_text": cases_text,
    }


# ---------------------------------------------------------------------------
# Reflect
# ---------------------------------------------------------------------------

REFLECT_SYS = ("You are the reflection agent of a credit-risk monitoring "
               "pipeline. You calibrate, you do not invent facts. "
               "Output valid JSON only.")

REFLECT_PROMPT = """Obligor: {obligor} (industry: {industry})
Date: {date}

Detection under review:
{detection}

Obligor risk profile ('None' if none yet):
{profile}

Past alerts for this obligor (most recent last; 'None' if none):
{past_alerts}

Similar historical cases:
{cases}

Revalidate the detection:
- Does the obligor's history support this topic and severity? Escalation
  against unresolved confirmed events in the profile must be upheld;
  re-reports of already-alerted facts with no new content should be damped.
- Is the confidence honest given the evidence?

Return ONLY JSON: {{"topic": "...", "severity": "...", "confidence": 0.0, "
"adjusted": true|false, "rationale": "one line"}}"""


def reflect(llm, obligor, date: str, detection: dict, context: dict) -> dict:
    profile = context.get("profile")
    past = context.get("past_alerts") or []
    resp = llm.generate(
        prompt=REFLECT_PROMPT.format(
            obligor=obligor.name, industry=obligor.industry or "unknown",
            date=date,
            detection=json.dumps(detection, ensure_ascii=False, indent=2),
            profile=(json.dumps(profile, ensure_ascii=False, indent=2)
                     if profile else "None"),
            past_alerts=(json.dumps(past[-5:], ensure_ascii=False, indent=2)
                         if past else "None"),
            cases=context.get("cases_text") or "None"),
        system_message=REFLECT_SYS)
    r = parse_json_from_llm_output(resp.content)
    return r if isinstance(r, dict) else {}


# ---------------------------------------------------------------------------
# Decide
# ---------------------------------------------------------------------------

DECIDE_SYS = ("You are the decision agent of a credit-risk monitoring "
              "pipeline — the chief credit officer's delegate. "
              "Output valid JSON only.")

DECIDE_PROMPT = """{rubric}

Obligor: {obligor} (industry: {industry})
Date: {date}

Detection (validated):
{detection}

Reflection:
{reflection}

Obligor risk profile ('None' if none yet):
{profile}

Issue the action. Rules:
- "alert" only for material, credit-relevant developments; routine or
  duplicate content is "suppress".
- Confirmed unresolved events already in the profile dominate: a weaker new
  item must NOT lower the level below what they imply.
- If the detection found no relevant event, the verdict CARRIES FORWARD the
  profile's latest trajectory level (suppress the alert, keep the level).

Return ONLY JSON: {{"action": "alert"|"suppress", "risk_level":
"low"|"medium"|"high"|"critical", "score": 0-100, "rationale": "one line"}}"""


def decide(llm, rubric: str, obligor, date: str, detection: dict,
           reflection: dict, context: dict) -> dict:
    profile = context.get("profile")
    resp = llm.generate(
        prompt=DECIDE_PROMPT.format(
            rubric=rubric, obligor=obligor.name,
            industry=obligor.industry or "unknown", date=date,
            detection=json.dumps(detection, ensure_ascii=False, indent=2),
            reflection=json.dumps(reflection, ensure_ascii=False, indent=2),
            profile=(json.dumps(profile, ensure_ascii=False, indent=2)
                     if profile else "None")),
        system_message=DECIDE_SYS)
    d = parse_json_from_llm_output(resp.content)
    return d if isinstance(d, dict) else {}
