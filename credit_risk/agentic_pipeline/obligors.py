"""Step 2 — Grounding: obligor matching against the internal entity list.

Only items whose extracted company name (news) or CIK (8-K) match a known
obligor proceed to the reasoning loop; everything else is logged and dropped.
This is what keeps the pipeline from burning LLM calls on entities we do not
cover — and, symmetrically, what guarantees every downstream alert is attached
to a canonical, monitored counterparty.

The registry is deliberately storage-light: an in-memory list with a
normalized-name index. Load it from the contemporary dataset's
`candidates.csv` (company master simulation) or build it programmatically.
"""

import csv
import re
from dataclasses import dataclass, field
from typing import Optional

CORP_SUFFIXES = {
    "inc", "corp", "corporation", "group", "co", "company", "ltd", "llc",
    "lp", "llp", "plc", "sa", "nv", "se", "holdings", "holding", "trust",
    "partners", "acquisition", "intl", "international", "the", "de", "usa",
}


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation and corporate suffixes."""
    n = re.sub(r"\([^)]*\)", " ", name)          # "(SAVEQ)" ticker notes
    n = re.sub(r"[^a-z0-9 ]", " ", n.lower())
    toks = [t for t in n.split() if t not in CORP_SUFFIXES]
    return " ".join(toks)


@dataclass(eq=False)
class Obligor:
    """A monitored counterparty in the internal list."""
    obligor_id: str
    name: str                       # canonical (legal) name
    cik: Optional[str] = None
    symbol: Optional[str] = None
    industry: Optional[str] = None
    aliases: list = field(default_factory=list)

    def all_names(self):
        return [self.name] + list(self.aliases)


class ObligorRegistry:
    """The internal obligor list + matcher."""

    def __init__(self, obligors=()):
        self._by_id = {}
        self._by_cik = {}
        self._name_index = {}       # normalized name -> obligor_id
        for ob in obligors:
            self.add(ob)

    def add(self, ob: Obligor):
        self._by_id[ob.obligor_id] = ob
        if ob.cik:
            self._by_cik[str(ob.cik).lstrip("0")] = ob.obligor_id
        for n in ob.all_names():
            key = normalize_name(n)
            if key:
                self._name_index[key] = ob.obligor_id

    # -- matching -----------------------------------------------------------

    def match_cik(self, cik) -> Optional[Obligor]:
        """Direct CIK match — the strongest grounding evidence."""
        if not cik:
            return None
        oid = self._by_cik.get(str(cik).lstrip("0"))
        return self._by_id.get(oid) if oid else None

    def match_name(self, raw_name: str):
        """Name match: exact-normalized -> alias -> token-containment fuzzy.

        Returns (Obligor|None, how): how in {"exact", "fuzzy", None}.
        """
        if not raw_name:
            return None, None
        key = normalize_name(raw_name)
        if not key:
            return None, None
        oid = self._name_index.get(key)
        if oid:
            return self._by_id[oid], "exact"
        # fuzzy: token containment either way, min 2 tokens or 1 distinctive
        toks = set(key.split())
        for known, oid in self._name_index.items():
            ktoks = set(known.split())
            if not ktoks:
                continue
            if toks <= ktoks or ktoks <= toks:
                shorter = toks if len(toks) <= len(ktoks) else ktoks
                if len(shorter) >= 2 or any(len(t) >= 5 for t in shorter):
                    return self._by_id[oid], "fuzzy"
        return None, None

    # -- loading ------------------------------------------------------------

    @classmethod
    def from_candidates_csv(cls, path: str) -> "ObligorRegistry":
        """Load from the contemporary dataset's candidates.csv (columns:
        type, cik, name, query_name, symbol, ref_date). `query_name` (the
        newsworthy short name) becomes an alias."""
        reg = cls()
        with open(path, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                oid = f"OB-{row['cik'] or row['symbol']}"
                aliases = [row["query_name"]] if row.get("query_name") else []
                reg.add(Obligor(
                    obligor_id=oid,
                    name=re.sub(r"\s*\([^)]*\)\s*", "", row["name"]).strip(),
                    cik=row.get("cik") or None,
                    symbol=row.get("symbol") or None,
                    aliases=aliases,
                ))
        return reg

    def __len__(self):
        return len(self._by_id)
