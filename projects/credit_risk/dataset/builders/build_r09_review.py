"""Build a reproducible review release without changing the live dataset.

Annotations are rule-based candidates, never gold relevance/risk labels.
Only explicitly reviewed event dates may replace inherited dates. Output
separates model inputs from outcomes and quarantines unverified outcomes.
"""
import argparse
import copy
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
VERSION = 'review-0.1'


def norm(text):
    text = unicodedata.normalize('NFKD', text or '').encode('ascii', 'ignore').decode().lower()
    return ' '.join(re.findall(r'[a-z0-9]+', text))


def contains(text, phrase):
    return bool(phrase) and f' {norm(phrase)} ' in f' {norm(text)} '


# Narrow names for known collision-prone entities. Absence means review,
# never automatic rejection of potentially relevant news.
ALIASES = {
    '1731289': ['Nikola Corporation', 'Nikola Corp', 'Nikola Motor'],
    '1559998': ['Gaucho Group', 'Gaucho Holdings', 'Algodon'],
    '1446687': ['Silver Star Properties', 'Silver Star REIT', 'Hartman'],
    '1438901': ['Auto Parts 4Less', 'AutoParts4Less', '4Less Group'],
}
SPORTS = re.compile(r'\b(jokic|nuggets|nba)\b')
LEGAL = re.compile(r'\b(lawsuit|class action|lead plaintiff|securities fraud|rosen law|pomerantz|levi.*korsinsky)\b')
RISK = {
    'default_or_restructuring': r'\b(bankrupt\w*|chapter 11|chapter 7|default|restructur\w*)\b',
    'liquidity': r'\b(liquidity|cash crunch|going concern|missed payment|forbearance)\b',
    'rating_or_analyst_action': r'\b(downgrad\w*|upgrad\w*|price target)\b',
    'earnings': r'\b(earnings|loss|profit|revenue)\b',
    'financing_or_recovery': r'\b(refinanc\w*|financing|debt exchange|emerge\w*)\b',
}


def annotate(sample, row):
    c = sample['company']; cik = c['cik'].lstrip('0')
    title = row.get('title') or ''; head = title + ' ' + (row.get('text') or '')[:500]
    aliases = ALIASES.get(cik)
    if aliases is None:
        core = re.sub(r'\(.*?\)', '', c['name'])
        core = re.sub(r'\b(inc|corp|corporation|holdings|holding|co|ltd)\b\.?', '', core, flags=re.I)
        aliases = [core.strip(' ,.'), c.get('query_name') or '']
    matched = [a for a in aliases if contains(head, a)]
    # A ticker only counts with financial syntax; ME/W/M are ordinary words.
    tickers = [x.strip() for x in (c.get('symbol') or '').split(',') if x.strip()]
    ticker = any(re.search(r'(?:\$|NASDAQ\s*:\s*|NYSE\s*:\s*)' + re.escape(t) + r'\b', head, re.I) for t in tickers)
    collision = cik == '1731289' and bool(SPORTS.search(norm(title)))
    if collision and not (matched or ticker):
        entity, reason = 'off_target', 'sports_name_collision'
    elif matched or ticker:
        entity, reason = 'candidate_match', 'company_alias_or_explicit_ticker'
    else:
        entity, reason = 'needs_review', 'no_unambiguous_entity_evidence'
    normalized = norm(title)
    # Exact normalized title cluster; no claim that paraphrases are resolved.
    cluster = hashlib.sha256((sample['sample_id'] + '|' + normalized).encode()).hexdigest()[:20]
    tags = [k for k, pattern in RISK.items() if re.search(pattern, normalized)]
    if LEGAL.search(normalized): tags.append('legal_or_solicitation')
    return {'method': VERSION + '-rules', 'entity_status': entity, 'entity_reason': reason,
            'matched_aliases': matched, 'main_subject': 'unreviewed',
            'credit_relevance': 'unreviewed', 'risk_candidates': tags,
            'title_cluster_id': cluster, 'text_status': 'available' if len(row.get('text') or '') >= 200 else 'missing_or_thin',
            'publication_date': row.get('date'), 'event_date': None,
            'timestamp_precision': 'day', 'human_reviewed': False}


def load(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path, rows):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


def build(source, output, reviews_path):
    source = Path(source); output = Path(output)
    if output.resolve() == source.parent.resolve():
        raise ValueError('Review output must not overwrite the live dataset')
    samples = load(source); reviews = json.loads(Path(reviews_path).read_text())
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    # Refuse to silently rewrite an already frozen release.
    if output.exists() and any(output.iterdir()):
        raise ValueError('Output exists; choose a new release directory')
    output.mkdir(parents=True, exist_ok=True)
    records, labels, queue, safe, challenge = [], [], [], [], []
    totals = Counter()
    for sample in samples:
        sid = sample['sample_id']; c = sample['company']; review = reviews.get(c['cik'].lstrip('0'))
        input_id = hashlib.sha256(sid.encode()).hexdigest()[:20]
        audited = copy.deepcopy(sample)
        for n in audited['news']:
            n['review'] = annotate(sample, n)
            totals[n['review']['entity_status']] += 1
        counts = Counter(n['review']['title_cluster_id'] for n in audited['news'])
        for n in audited['news']:
            n['review']['title_cluster_size'] = counts[n['review']['title_cluster_id']]
        event = review['event_date'] if review else None
        window = copy.deepcopy(sample['window'])
        if event:
            end = date.fromisoformat(event) - timedelta(days=1)
            window = {'start': (end - timedelta(days=179)).isoformat(), 'end': end.isoformat()}
        retained = [n for n in audited['news'] if window['start'] <= n['date'][:10] <= window['end']]
        filings = [f for f in sample.get('filings', []) if window['start'] <= f['filing_date'][:10] <= window['end']]
        # Keep every original row in the annotated release; only derived inputs
        # are filtered, with counts and provenance in the sample audit.
        unique = {}; noise = []
        for n in sorted(retained, key=lambda n: (n['date'], n['news_id'])):
            if n['review']['entity_status'] != 'candidate_match':
                noise.append(n); continue
            k = n['review']['title_cluster_id']
            if k not in unique: unique[k] = n
            # Earliest publication wins so later text cannot leak backwards.
        old = sample['window']
        gaps = []
        if window['start'] < old['start']: gaps.append({'start': window['start'], 'end': (date.fromisoformat(old['start'])-timedelta(days=1)).isoformat()})
        if window['end'] > old['end']: gaps.append({'start': (date.fromisoformat(old['end'])+timedelta(days=1)).isoformat(), 'end': window['end']})
        reasons = []
        if not review: reasons.append('outcome_unverified' if sample['type']=='positive' else 'negative_followup_unverified')
        if gaps: reasons.append('window_requires_backfill')
        if len(unique) < 10: reasons.append('thin_entity_evidence')
        # Candidate clean inputs are not certified gold; all row judgments
        # still require a main-subject and risk-evidence review.
        outcome = {'sample_id': sid, 'input_id': input_id, 'inherited_label': sample['label'],
                   'reviewed_outcome': review, 'event_date': event,
                   'status': 'primary_source_reviewed' if review else 'pending',
                   'negative_followup_end': None, 'point_in_time_risk': 'unlabelled',
                   'split': sample['split'], 'evaluation_eligible': False,
                   'blocking_reasons': reasons + ['point_in_time_labels_unreviewed']}
        labels.append(outcome)
        audited['audit'] = {'source_sha256': digest, 'version': VERSION,
            'desired_window': window, 'unfetched_intervals': gaps,
            'candidate_clean_rows': len(unique), 'review_or_noise_rows': len(noise),
            'out_of_reviewed_window_rows': len(audited['news'])-len(retained),
            'duplicate_title_surplus': sum(v-1 for v in counts.values()),
            'outcome_status': outcome['status']}
        records.append(audited)
        model = {'input_id': input_id, 'company': c, 'window': window,
                 'news': [{k:v for k,v in n.items() if k != 'review'} for n in unique.values()],
                 'filings': filings, 'purpose': 'candidate_inputs_not_gold'}
        # Opaque IDs avoid encoding class/event dates. Review metadata such as
        # cluster size is excluded because it includes later publications.
        safe.append(model)
        if noise:
            challenge.append({'input_id': input_id, 'company': c, 'window': window,
                              'news': noise, 'purpose': 'entity_review_and_noise_challenge'})
        queue.append({'sample_id': sid, 'company': c['name'], 'outcome': outcome,
                      'unfetched_intervals': gaps, 'candidate_clean_rows': len(unique),
                      'news_to_review': len(noise), 'priority': 1 if reasons else 2})
    for name, rows in [('annotated_samples',records),('outcomes',labels),('review_queue',queue),
                       ('candidate_clean',safe),('noise_challenge',challenge)]:
        write_jsonl(output / (name + '.jsonl'), rows)
    manifest = {'version': VERSION, 'input_sha256': digest, 'samples':len(samples),
                'news_rows':sum(len(s['news']) for s in samples), 'entity_rules':dict(totals),
                'reviewed_outcomes':sum(x['status']=='primary_source_reviewed' for x in labels),
                'corrected_dates':sum(r['reviewed_outcome'] is not None and r['event_date'] != r['inherited_label']['event_date'] for r in labels),
                'gold_evaluation_samples':0, 'source_splits_preserved':True,
                'rules_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'event_reviews_sha256':hashlib.sha256(Path(reviews_path).read_bytes()).hexdigest(),
                'limitations':['Rule-based relevance is not gold.', 'No negative outcome is verified by absence of news.',
                              'Missing window coverage requires backfill.', 'Near-duplicate event clusters remain unreviewed.'],
                'files':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.glob('*.jsonl'))}}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, default=HERE/'contemporary/samples.jsonl')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--reviews', type=Path, default=HERE/'review/event_reviews.json')
    a = p.parse_args()
    print(json.dumps(build(a.source,a.output,a.reviews),ensure_ascii=False,indent=2))
