---
name: credit_risk_taxonomy
description: Closed taxonomy of credit-risk-relevant corporate events, with severity definitions. Use when extracting risk events from news about a company.
---

# Credit Risk Event Taxonomy

Extract credit-risk-relevant events from news using ONLY the event types below.
Do not invent new types. If a news item contains no relevant event, skip it.

## Event types and severity

| event_type | description | typical severity |
|---|---|---|
| debt_default | missed bond/loan payment, bankruptcy filing, debt restructuring | critical |
| rating_downgrade | credit rating or outlook cut by a rating agency | high |
| liquidity_stress | cash crunch, asset fire-sales, credit line freeze, going-concern doubts | high |
| lawsuit_regulatory | major lawsuit, fraud probe, regulatory penalty, asset freeze | medium-high |
| earnings_warning | profit warning, large loss, covenant breach risk | medium |
| management_turmoil | CFO/CEO sudden departure, auditor resignation, board crisis | medium |
| operational_shock | plant shutdown, supply chain collapse, major contract loss | medium |
| positive_development | refinancing success, rating upgrade, strong earnings, strategic investment | positive |

## Extraction rules

- severity must be one of: critical, high, medium, low, positive.
- sentiment must be one of: negative, neutral, positive.
- Each event needs a one-line `summary` citing the concrete fact (numbers, dates, parties).
- `date` uses ISO format YYYY-MM-DD; use the news date if the event date is unstated.
- Rumors and unconfirmed reports count, but mark them with `"confirmed": false`.
