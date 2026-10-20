"""Finding the canvas object a chat operation names.

The assistant names nodes the way the person did: "Risk Detector" for the
task `risk_detector`, a Chat Agent by its title, a node by what it does ("the
风险检测 step"). An exact-match lookup turned every one of those into "no node
named ...", with nothing for the model (or the person) to go on. This module
resolves a name in tiers, most certain first, and says how it got there:

1. exact id/name (a Chat Agent's `chat:<id>` or bare id too)
2. exact display name (title / label / an Agent's name)
3. normalized: NFKC, case-folded, whitespace/-/_ collapsed; also the slug
4. alias: the query inside a name or description (CJK included), or a
   name inside the query
5. fuzzy: close spellings, shared words or shared character pairs

One hit at the first tier that has any is the answer; several there is an
ambiguity to report, never a guess. A destructive operation only acts on
tiers 1-3: anything looser comes back as candidates to confirm.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

EXACT, LABEL, NORMALIZED, ALIAS, FUZZY = 1, 2, 3, 4, 5
TIER_NAMES = {EXACT: "exact", LABEL: "display name", NORMALIZED: "normalized name",
              ALIAS: "description", FUZZY: "closest match"}
# Destructive operations auto-apply only on these tiers.
SAFE_TIERS = (EXACT, LABEL, NORMALIZED)
MAX_CANDIDATES = 5
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]")


def normalize(text) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold().strip()
    return re.sub(r"[\s\-_]+", " ", text).strip()


def _compact(text) -> str:
    return normalize(text).replace(" ", "")


def _slug(text) -> str:
    """Mirror of chat_api._slug / the frontend's sluggify()."""
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return s or "node"


def _pairs(text: str) -> set:
    """Character bigrams of the CJK text, for overlap without word breaks."""
    chars = [c for c in text if _CJK.match(c)]
    return {a + b for a, b in zip(chars, chars[1:])} or set(chars)


def candidate(id, kind, ref=None, labels=(), aliases=(), description="", names=()) -> dict:
    return {"id": str(id), "kind": kind, "ref": ref,
            "names": [str(id), *[str(n) for n in names if n]],
            "labels": [str(l) for l in labels if l],
            "aliases": [str(a) for a in aliases if a],
            "description": str(description or "")}


def task_candidates(tasks: list) -> list:
    out = []
    for t in tasks or []:
        if not isinstance(t, dict) or not t.get("name"):
            continue
        out.append(candidate(
            t["name"], t.get("kind") or "task", t,
            labels=[t.get(k) for k in ("title", "label", "display_name")],
            aliases=[t.get("description")], description=t.get("description")))
    return out


def agent_candidates(agents: list) -> list:
    out = []
    for a in agents or []:
        if not isinstance(a, dict) or not a.get("id"):
            continue
        raw = str(a["id"])
        bare = raw[len("chat:"):] if raw.startswith("chat:") else raw
        out.append(candidate(
            "chat:" + bare, "chat_agent", a, names=[bare], labels=[a.get("name")],
            aliases=[a.get("instructions")], description=a.get("instructions")))
    return out


def summary(c: dict) -> dict:
    """What a person needs to tell candidates apart."""
    name = c["labels"][0] if c["labels"] else c["id"]
    description = re.sub(r"\s+", " ", c["description"]).strip()
    return {"id": c["id"], "name": name, "kind": c["kind"],
            "description": description[:80] + ("…" if len(description) > 80 else "")}


def _score(query: str, c: dict) -> float:
    """How alike a query and a candidate are, 0..1, for fuzzy ranking."""
    q = normalize(query)
    if not q:
        return 0.0
    texts = [normalize(t) for t in c["names"] + c["labels"]]
    best = max((difflib.SequenceMatcher(None, q, t).ratio() for t in texts if t), default=0.0)
    words = set(q.split())
    for t in texts:
        other = set(t.split())
        if words and other:
            best = max(best, len(words & other) / len(words | other))
        if len(q) >= 3 and (q in t or (len(t) >= 3 and t in q)):
            best = max(best, 0.75)
    pairs = _pairs(q)
    if pairs:
        for t in texts + [normalize(a) for a in c["aliases"]]:
            other = _pairs(t)
            if other:
                best = max(best, len(pairs & other) / len(pairs))
    return best


def _tier_hits(query: str, candidates: list) -> tuple[int, list]:
    raw = str(query or "")
    stripped = raw.strip()
    q, compact = normalize(raw), _compact(raw)
    if not q:
        return 0, []
    tiers = [
        (EXACT, lambda c: raw in c["names"] or stripped in c["names"]),
        (LABEL, lambda c: raw in c["labels"] or stripped in c["labels"]),
        (NORMALIZED, lambda c: any(
            normalize(t) == q or _compact(t) == compact or t == _slug(raw)
            for t in c["names"] + c["labels"])),
        (ALIAS, lambda c: len(q) >= (2 if _CJK.search(q) else 3) and (
            any(q in normalize(t) for t in c["aliases"] + c["names"] + c["labels"])
            or any(len(normalize(t)) >= (2 if _CJK.search(t) else 3) and normalize(t) in q
                   for t in c["names"] + c["labels"]))),
    ]
    for tier, test in tiers:
        hits = [c for c in candidates if test(c)]
        if hits:
            return tier, hits
    scored = sorted(((_score(raw, c), c) for c in candidates), key=lambda x: -x[0])
    scored = [(s, c) for s, c in scored if s >= 0.6]
    if not scored:
        return 0, []
    top = scored[0][0]
    return FUZZY, [c for s, c in scored if top - s < 0.05]


def resolve(query, candidates: list, destructive: bool = False) -> dict:
    """{status, match, tier, candidates, note}.

    status: "ok" (match set), "ambiguous" (several equally good), "confirm"
    (destructive and only a loose match) or "missing".
    """
    tier, hits = _tier_hits(query, candidates)
    if not hits:
        ranked = sorted(candidates, key=lambda c: -_score(query, c))
        return {"status": "missing", "match": None, "tier": 0,
                "candidates": [summary(c) for c in ranked[:MAX_CANDIDATES]]}
    if len(hits) > 1:
        return {"status": "ambiguous", "match": None, "tier": tier,
                "candidates": [summary(c) for c in hits[:MAX_CANDIDATES]]}
    hit = hits[0]
    if destructive and tier not in SAFE_TIERS:
        return {"status": "confirm", "match": None, "tier": tier, "candidates": [summary(hit)]}
    note = None if tier == EXACT else f"resolved '{str(query).strip()}' → {hit['id']} (by {TIER_NAMES[tier]})"
    return {"status": "ok", "match": hit, "tier": tier, "candidates": [], "note": note}


def describe(candidates: list) -> str:
    return "; ".join(
        f"{c['id']} ({c['kind']}{', ' + repr(c['name']) if c['name'] != c['id'] else ''}"
        f"{': ' + c['description'] if c['description'] else ''})" for c in candidates)


def failure(op: str, query, result: dict) -> str:
    """The message a failed resolution reports, candidates included."""
    q = str(query or "").strip()
    found = describe(result["candidates"])
    if result["status"] == "ambiguous":
        return f"{op}: '{q}' matches several objects: {found}. Use the exact id."
    if result["status"] == "confirm":
        return (f"{op}: nothing is named '{q}'; the closest is {found}. Not applied: "
                "ask the user to confirm, then repeat with the exact id.")
    if found:
        return f"{op}: nothing on the canvas matches '{q}'. Closest: {found}."
    return f"{op}: nothing on the canvas matches '{q}' (the canvas is empty)."
