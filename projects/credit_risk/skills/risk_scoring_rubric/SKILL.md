---
name: risk_scoring_rubric
description: Scoring rubric for judging a company's credit risk level from extracted events and historical context. Use when producing a final credit risk verdict.
---

# Credit Risk Scoring Rubric

## Risk levels

| risk_level | score band | meaning |
|---|---|---|
| low | 0-29 | no material negative events; financially sound signals |
| medium | 30-59 | isolated negative events, no confirmed default signal; monitor |
| high | 60-84 | multiple/severe negative events or confirmed critical event; elevated default risk |
| critical | 85-100 | confirmed default, bankruptcy, or imminent payment failure |

## Scoring rules

- A confirmed `critical` event (e.g. debt_default) alone justifies at least 80.
- An unconfirmed rumor caps the score at 70 until corroborated.
- **Trend matters**: if historical memory shows the same risk theme recurring or
  worsening over time (escalation), raise the score by 10-20 versus judging the
  current events in isolation. If history shows improvement, lower it.
- `confidence` is high when events are confirmed and consistent, low when based
  on rumors or a single source.
- `key_evidence` must quote only facts present in the input events or history.
  Never invent facts.

## Output contract

Return a single JSON object:

```json
{
  "company": "<name>",
  "risk_level": "low|medium|high|critical",
  "score": <0-100 integer>,
  "trend": "improving|stable|escalating|unknown",
  "key_evidence": ["<fact 1>", "<fact 2>"],
  "confidence": "low|medium|high",
  "rationale": "<2-3 sentences>"
}
```
