"""
Step 02b: verify that each candidate event is a REAL bankruptcy disclosure.

build_01 found 8-K filings via EDGAR full-text search for "Item 1.03" — but
FTS also matches filings that merely *cite* that item (agreements referencing
default clauses, parent companies disclosing a SUBSIDIARY's filing, etc.).
This step downloads each candidate's full submission text from EDGAR and
checks:

    V1  "Item 1.03" appears as an actual item heading in the 8-K body
    V2  bankruptcy keywords (chapter 7/11, voluntary petition, receivership,
        bankruptcy court) appear in the same document
    flag "subsidiary_mention" when the Item 1.03 section talks about a
        subsidiary filing rather than the registrant itself

Output: raw/event_verification.csv (cik, event_date, verdict, flags, snippet)
Events failing V1+V2 must be dropped before sampling (build_03 reads this).
"""

import csv
import re
import time
import urllib.request

EVENTS_CSV = "projects/credit_risk/dataset/raw/bankruptcy_events.csv"
CANDIDATES_CSV = "projects/credit_risk/dataset/candidates.csv"
OUT_CSV = "projects/credit_risk/dataset/raw/event_verification.csv"

UA = {"User-Agent": "EvoAgentX research (contact: research@example.com)"}
PAUSE = 0.2  # EDGAR fair-access: stay well under 10 req/s

ITEM_RE = re.compile(r"item\s*1\.03", re.I)
BANKRUPT_RE = re.compile(
    r"(chapter\s*(7|11)\b|voluntary\s+petition|receivership|bankruptcy\s+court|"
    r"title\s+11\s+of\s+the\s+united\s+states\s+code)", re.I)
SUBSIDIARY_RE = re.compile(r"subsidiar", re.I)
# co-debtor phrasing: the registrant filed TOGETHER with subsidiaries (a real
# bankruptcy of the registrant); without this, a subsidiary mention usually
# means only the subsidiary filed (not the registrant's own bankruptcy)
CODEBTOR_RE = re.compile(
    r"(together with|and (certain of )?(its|the\s+company.?s)\s+(direct\s+and\s+indirect\s+)?subsidiar"
    r"|majority of .*subsidiar|along with)", re.I)


def html_to_text(raw: str) -> str:
    import html
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    return re.sub(r"\s+", " ", text)


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def fetch_submission(cik: str, adsh: str) -> str:
    """Fetch the 8-K BODY (not exhibits — they routinely cite "Item 1.03"
    and mention bankruptcy in default clauses, which would false-positive
    V1/V2). Primary path: parse the SGML full-submission text and take the
    first <DOCUMENT> whose <TYPE> is 8-K — name heuristics misfire on
    exhibits like "mm11-2911_8ke991.htm"."""
    import json
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{adsh.replace('-', '')}"

    try:
        sgml = _get(f"{base}/{adsh}.txt").decode("utf-8", errors="replace")
        for doc in re.findall(r"<DOCUMENT>(.*?)</DOCUMENT>", sgml, re.S):
            type_m = re.search(r"<TYPE>\s*(\S+)", doc)
            if type_m and type_m.group(1).upper().startswith("8-K"):
                text_m = re.search(r"<TEXT>(.*?)</TEXT>", doc, re.S)
                return text_m.group(1) if text_m else doc
    except Exception:
        pass  # fall through to directory-index heuristics

    listing = json.loads(_get(f"{base}/index.json").decode("utf-8"))
    names = [item["name"] for item in listing["directory"]["item"]
             if "index" not in item["name"].lower()
             and item["name"].lower().endswith((".htm", ".txt"))
             and not item["name"].lower().startswith("ex")]
    # the 8-K body usually has "8k"/"8-k"/"e8vk" in its name; otherwise the
    # first non-exhibit document
    body = next((n for n in names
                 if re.search(r"8-?v?k", n, re.I)
                 and not re.search(r"e?x-?\d|ex\d|exhibit", n, re.I)), None)
    if body is None:
        body = names[0]
    return _get(f"{base}/{body}").decode("utf-8", errors="replace")


def main():
    wanted = {}
    with open(CANDIDATES_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            wanted[row["cik"]] = row["event_date"]

    events = []
    with open(EVENTS_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["cik"] in wanted:
                events.append(row)
    print(f"verifying {len(events)} candidate events against EDGAR full text")

    out_rows = []
    for i, ev in enumerate(events):
        try:
            text = html_to_text(fetch_submission(ev["cik"], ev["adsh"]))
        except Exception as e:  # network hiccup — mark unknown, keep out
            out_rows.append({**ev, "verdict": "ERROR", "subsidiary_mention": "",
                             "snippet": str(e)[:200]})
            print(f"  [{i+1}/{len(events)}] {ev['company_name'][:40]:40s} ERROR {e}")
            time.sleep(PAUSE)
            continue

        m = ITEM_RE.search(text)
        v1 = m is not None
        v2 = BANKRUPT_RE.search(text) is not None
        subsidiary = ""
        snippet = ""
        if m:
            section = text[m.start():m.start() + 3000]
            snippet = section[:400]
            if SUBSIDIARY_RE.search(section):
                subsidiary = "co-debtor" if CODEBTOR_RE.search(section) else "review"
        verdict = "REAL" if (v1 and v2) else "FALSE_MATCH"
        out_rows.append({**ev, "verdict": verdict,
                         "subsidiary_mention": subsidiary, "snippet": snippet})
        print(f"  [{i+1}/{len(events)}] {ev['company_name'][:40]:40s} {verdict:11s} sub={subsidiary}")
        time.sleep(PAUSE)

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["cik", "company_name", "event_date", "form",
                                               "adsh", "sics", "verdict",
                                               "subsidiary_mention", "snippet"])
        writer.writeheader()
        writer.writerows(out_rows)

    n_real = sum(1 for r in out_rows if r["verdict"] == "REAL")
    n_codebtor = sum(1 for r in out_rows if r["subsidiary_mention"] == "co-debtor")
    n_review = sum(1 for r in out_rows if r["subsidiary_mention"] == "review")
    print(f"\n{n_real}/{len(out_rows)} REAL bankruptcy disclosures "
          f"({n_codebtor} co-debtor filings, {n_review} subsidiary-only review) -> {OUT_CSV}")


if __name__ == "__main__":
    main()
