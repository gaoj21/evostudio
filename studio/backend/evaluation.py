"""Scoring a batch of runs against expected answers.

Evaluation here is a batch run with a metric attached: the same records, the
same runner, plus a score per item and an aggregate over the batch. Reusing
the batch path rather than building a second runner means node progress, the
result drawer, review routing and artifacts all keep working during an
evaluation.

Metrics come from two places:

- the built-ins defined here and reused by the optimizer, so a workflow
  scores the same way whether it is being evaluated or optimized;
- any custom tool taking `(prediction, label)`, which makes a metric an
  ordinary piece of this Studio's code — editable in the same editor, run in
  the same subprocess, bundled by the same export.
"""

import ast
import json
import re
import statistics

from . import custom_tools
_LEVEL_SCORE_POS = {"low": 0.0, "medium": 0.5, "high": 1.0, "critical": 1.0}
_LEVEL_SCORE_NEG = {"low": 1.0, "medium": 0.8, "high": 0.2, "critical": 0.0}

METRICS = {
    "exact_match": "1.0 when the normalized prediction equals the label string",
    "contains": "1.0 when the label string appears in the prediction (case-insensitive)",
    "numeric": "1.0 when the first number in the prediction equals the label number",
    "credit_risk": (
        "Reads risk_level from the prediction. Positive samples (label.type="
        "'positive') score 1.0/0.5/0.0 for high+/medium/low; negative samples "
        "score 1.0/0.8/0.2/0.0 for low/medium/high/critical."
    ),
}


def _prediction_text(prediction) -> str:
    """Workflow predictions arrive as str(dict); unwrap single-output dicts."""
    text = str(prediction)
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return text
    if isinstance(parsed, dict):
        values = [str(v) for v in parsed.values()]
        return values[0] if len(values) == 1 else "\n".join(values)
    return str(parsed)


def _score(metric: str, prediction, label) -> float:
    pred = _prediction_text(prediction)
    if metric == "exact_match":
        norm = lambda s: str(s).strip().lower().rstrip(".!")  # noqa: E731
        return float(norm(pred) == norm(label))
    if metric == "contains":
        return float(str(label).strip().lower() in pred.lower())
    if metric == "numeric":
        match = re.search(r"-?\d+(?:\.\d+)?", pred.replace(",", ""))
        try:
            return float(match is not None and abs(float(match.group()) - float(label)) < 1e-6)
        except (TypeError, ValueError):
            return 0.0
    if metric == "credit_risk":
        level_match = re.search(r"risk_level[\"'\s:]+(low|medium|high|critical)", pred, re.I)
        if not level_match:
            level_match = re.search(r"\b(low|medium|high|critical)\b", pred, re.I)
        if not level_match:
            return 0.0
        level = level_match.group(1).lower()
        label_type = (label or {}).get("type") if isinstance(label, dict) else str(label)
        table = _LEVEL_SCORE_POS if label_type == "positive" else _LEVEL_SCORE_NEG
        return table[level]
    raise ValueError(f"Unknown metric: {metric}")


class EvaluationError(Exception):
    """User-facing metric failure (HTTP 422)."""


METRIC_PARAMS = ("prediction", "label")


def custom_metrics() -> list[dict]:
    """Custom tools shaped like a metric: exactly (prediction, label)."""
    out = []
    for _toolkit, tool in custom_tools.iter_tools():
        names = tuple(p.get("name") for p in tool.get("params") or [])
        if names == METRIC_PARAMS:
            out.append({"name": tool["name"], "description": tool["description"],
                        "custom": True})
    return out


def available_metrics() -> list[dict]:
    builtin = [{"name": name, "description": description, "custom": False}
               for name, description in METRICS.items()]
    return builtin + custom_metrics()


def score_one(metric: str, prediction, label) -> dict:
    """Score one prediction. Returns {"score": float, **extra}.

    A metric that fails stops the evaluation: a scoring bug that silently
    reports zeros looks exactly like a workflow that got everything wrong.
    """
    if metric in METRICS:
        try:
            return {"score": float(_score(metric, prediction, label))}
        except Exception as e:
            raise EvaluationError(f"metric '{metric}' failed: {e}")

    if not any(m["name"] == metric for m in custom_metrics()):
        raise EvaluationError(
            f"Unknown metric '{metric}'. Built-ins: "
            + ", ".join(METRICS)
            + ". A custom metric is a tool taking exactly (prediction, label)."
        )

    result = custom_tools.run_custom_tool(
        metric, {"prediction": _as_text(prediction), "label": _as_text(label)}
    )
    if isinstance(result, dict) and "error" in result and "result" not in result:
        raise EvaluationError(f"metric '{metric}' failed: {result['error']}")
    value = result.get("result") if isinstance(result, dict) else result

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return {"score": float(value)}
    if isinstance(value, bool):
        return {"score": 1.0 if value else 0.0}
    if isinstance(value, dict):
        if "score" not in value:
            raise EvaluationError(
                f"metric '{metric}' returned an object without a 'score' key: "
                f"{sorted(value)[:6]}"
            )
        try:
            return {**value, "score": float(value["score"])}
        except (TypeError, ValueError):
            raise EvaluationError(f"metric '{metric}' returned a non-numeric score")
    raise EvaluationError(
        f"metric '{metric}' must return a number, a bool, or an object with a "
        f"'score'; it returned {type(value).__name__}"
    )


def _as_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                           default=str)


def split_labels(records: list[dict], label_key: str) -> tuple[list[dict], list]:
    """Separate the expected answer from the inputs of each record.

    The label must not reach the workflow: a node that happens to declare an
    input of the same name would be handed the answer.
    """
    if not label_key:
        raise EvaluationError("An evaluation needs the field holding the expected answer")
    inputs, labels = [], []
    for index, record in enumerate(records, start=1):
        if label_key not in record:
            raise EvaluationError(
                f"Record {index} has no '{label_key}' field. Available: "
                f"{sorted(record)[:8]}"
            )
        labels.append(record[label_key])
        inputs.append({k: v for k, v in record.items() if k != label_key})
    return inputs, labels


def summarise(items: list[dict]) -> dict:
    """Aggregate over the scored items, ignoring the ones that never ran."""
    scores = [i["score"] for i in items
              if isinstance(i.get("score"), (int, float))]
    scored, total = len(scores), len(items)
    return {
        "scored": scored,
        "total": total,
        "unscored": total - scored,
        "mean": round(statistics.fmean(scores), 4) if scores else None,
        "min": round(min(scores), 4) if scores else None,
        "max": round(max(scores), 4) if scores else None,
        # How many scored a perfect 1.0 — the number people actually quote.
        "perfect": sum(1 for s in scores if s >= 1.0),
    }
