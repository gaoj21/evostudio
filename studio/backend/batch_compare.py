"""Comparing two evaluations of the same workflow.

The loop metrics exist for is: change a prompt, run the set again, did it get
better? A mean on its own does not answer that — a mean that moved from 0.71 to
0.74 can hide six records that got worse. So a comparison reports the aggregate
*and* the records that moved, worst first.
"""

import json


def _key(item: dict) -> str:
    """What makes a record the same record across two runs.

    Its inputs, not its position: the two batches may have been drawn with a
    different sample size or order, and lining them up by index would then
    compare unrelated records and call the difference a regression.
    """
    inputs = item.get("inputs") or {}
    return json.dumps(inputs, sort_keys=True, ensure_ascii=False, default=str)


def _index(items: list[dict]) -> tuple[dict[str, dict], int]:
    """Records by identity, plus how many were dropped as ambiguous.

    A batch may legitimately contain the same inputs twice; such a record
    cannot be matched to one side of the other batch rather than the other, so
    it is left out and counted instead of guessed at.
    """
    seen: dict[str, dict] = {}
    duplicates: set[str] = set()
    for item in items:
        key = _key(item)
        if key in seen:
            duplicates.add(key)
        seen[key] = item
    for key in duplicates:
        seen.pop(key, None)
    return seen, len(duplicates)


def _label(item: dict) -> str:
    """A short, human-readable name for a record."""
    inputs = item.get("inputs") or {}
    for field in ("sample_id", "id", "name", "company", "question", "city"):
        if inputs.get(field):
            return str(inputs[field])
    if not inputs:
        return f"record {item.get('index')}"
    key, value = next(iter(inputs.items()))
    return f"{key}={str(value)[:40]}"


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def compare(baseline: dict, candidate: dict) -> dict:
    """How `candidate` differs from `baseline`, record by record."""
    base_index, base_dupes = _index(baseline.get("items") or [])
    cand_index, cand_dupes = _index(candidate.get("items") or [])
    shared = [k for k in cand_index if k in base_index]

    changes = []
    improved = regressed = unchanged = unscored = 0
    for key in shared:
        before, after = base_index[key], cand_index[key]
        score_a, score_b = before.get("score"), after.get("score")
        delta = None
        if score_a is None or score_b is None:
            unscored += 1
        else:
            delta = round(score_b - score_a, 4)
            if delta > 0:
                improved += 1
            elif delta < 0:
                regressed += 1
            else:
                unchanged += 1
        changes.append({
            "label": _label(after),
            "inputs": after.get("inputs"),
            "score_before": score_a,
            "score_after": score_b,
            "delta": delta,
            "status_before": before.get("status"),
            "status_after": after.get("status"),
            "run_before": before.get("run_id"),
            "run_after": after.get("run_id"),
            "output_before": before.get("output_summary"),
            "output_after": after.get("output_summary"),
        })

    # Regressions first: they are what the change broke, and what the person
    # running the comparison actually has to look at.
    changes.sort(key=lambda c: (c["delta"] is None, c["delta"] if c["delta"] is not None else 0))

    base_scores = [i["score"] for i in base_index.values() if i.get("score") is not None]
    cand_scores = [i["score"] for i in cand_index.values() if i.get("score") is not None]
    mean_before, mean_after = _mean(base_scores), _mean(cand_scores)

    # Over the shared records only: comparing a mean over 200 records with one
    # over the 20 that overlap would not be a comparison of anything.
    shared_before = [base_index[k]["score"] for k in shared
                     if base_index[k].get("score") is not None]
    shared_after = [cand_index[k]["score"] for k in shared
                    if cand_index[k].get("score") is not None]

    notes = []
    metric_a = baseline.get("metric")
    metric_b = candidate.get("metric")
    if metric_a != metric_b:
        notes.append(
            f"These were scored with different metrics ({metric_a or 'none'} vs "
            f"{metric_b or 'none'}); the scores are not comparable."
        )
    if not shared:
        notes.append(
            "No record appears in both batches — they were run over different "
            "inputs, so there is nothing to compare record by record."
        )
    elif len(shared) < min(len(base_index), len(cand_index)):
        notes.append(
            f"{len(shared)} of {len(base_index)} and {len(cand_index)} records "
            "appear in both; the rest are only in one."
        )
    if base_dupes or cand_dupes:
        notes.append(
            f"{base_dupes + cand_dupes} record(s) with duplicate inputs were "
            "left out: they cannot be matched up unambiguously."
        )

    return {
        "baseline": {
            "batch_id": baseline.get("batch_id"), "metric": metric_a,
            "created_at": baseline.get("created_at"), "mean": mean_before,
            "total": len(baseline.get("items") or []),
        },
        "candidate": {
            "batch_id": candidate.get("batch_id"), "metric": metric_b,
            "created_at": candidate.get("created_at"), "mean": mean_after,
            "total": len(candidate.get("items") or []),
        },
        "matched": len(shared),
        "mean_before": _mean(shared_before),
        "mean_after": _mean(shared_after),
        "delta": (round(_mean(shared_after) - _mean(shared_before), 4)
                  if shared_before and shared_after else None),
        "improved": improved,
        "regressed": regressed,
        "unchanged": unchanged,
        "unscored": unscored,
        "changes": changes,
        "notes": notes,
    }
