"""Versioned credit-risk releases: discovery, labels, and dated observations.

Outcomes stay out of workflow inputs: `label_for` is read only by evaluators.
"""
import json
import random
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / 'dataset' / 'expansion' / 'releases'


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def catalog():
    result = []
    for path in sorted(ROOT.glob('*'), reverse=True):
        if not all((path / name).is_file() for name in ('manifest.json', 'cases.jsonl', 'partitions.jsonl', 'observations.jsonl', 'outcomes.jsonl')):
            continue
        manifest = json.loads((path / 'manifest.json').read_text())
        splits = manifest.get('partition_counts', {})
        if not splits or not set(splits) <= {'dev', 'test'}:
            continue
        partitions = {r['case_id']: r['partition'] for r in rows(path / 'partitions.jsonl')}
        cases = {r['case_id']: r for r in rows(path / 'cases.jsonl')}
        eligible = Counter(partitions[r['case_id']] for r in rows(path / 'outcomes.jsonl') if label_for(r, cases[r['case_id']]) is not None)
        result.append({'id': path.name, 'label': path.name, 'splits': splits,
                       'companies': manifest.get('company_count'), 'observations': manifest.get('observations'),
                       'evolve_splits': {key: eligible[key] for key in splits},
                       'evolve_note': 'Evolve uses one end-of-window snapshot per eligible case, not sequential memory evaluation. Only verified registrant pre-event bankruptcy cases currently have usable labels; unverified negatives are excluded.'})
    return result


def release(dataset):
    from .feed import SourceError
    if not isinstance(dataset, str) or dataset not in {item['id'] for item in catalog()}:
        raise SourceError('Unknown dataset version. Choose an available dataset.')
    return ROOT / dataset


def label_for(outcome, case):
    event = outcome.get('reviewed_event') or {}
    # Never manufacture negative labels from missing/unverified outcomes.
    if (outcome.get('outcome_review_status') == 'event_verified'
        and outcome.get('event_type') in {'chapter_11', 'chapter_7', 'canadian_bankruptcy_assignment'}
        and str(event.get('scope', '')).startswith('registrant')
        and event.get('event_date', '') > case['window']['end']):
        return {'type': 'positive', 'event': outcome['event_type'], 'event_date': event['event_date'], 'case_id': case['case_id']}
    return None


def records(dataset, split=None, n=5, seed=42, with_labels=False, step=None):
    from .feed import SourceError, CREDIT_RISK_FIELDS, _sample_to_record
    path = release(dataset)
    partitions = {r['case_id']: r['partition'] for r in rows(path / 'partitions.jsonl')}
    if split and split not in set(partitions.values()):
        raise SourceError(f'Dataset has no {split!r} split')
    cases = [r for r in rows(path / 'cases.jsonl') if not split or partitions[r['case_id']] == split]
    outcomes = {r['case_id']: r for r in rows(path / 'outcomes.jsonl')} if with_labels else {}
    if with_labels:
        cases = [r for r in cases if label_for(outcomes[r['case_id']], r) is not None]
    if n and n > 0:
        random.Random(seed).shuffle(cases)
        cases = cases[:n]
    if not cases:
        raise SourceError('No eligible records in this dataset/split. Unverified outcomes are not treated as negative labels.')
    by_id = {r['case_id']: r for r in cases}
    def sample(case, docs):
        return {'sample_id': case['case_id'], 'company': case['company'], 'window': case['window'],
                'news': [{'date': d['available_at'], 'title': d['title'], 'text': d.get('text', ''), 'url': d.get('source_url', '')} for d in docs if d.get('representation') != 'cached_filing_body'],
                'filings': [{'filing_date': d['available_at'], 'form': d.get('form', '8-K'), 'items': d.get('items', []), 'text': d.get('text', '')} for d in docs if d.get('representation') == 'cached_filing_body']}
    if with_labels:
        return [{'id': c['case_id'], 'inputs': _sample_to_record(sample(c, c['documents'])), 'label': label_for(outcomes[c['case_id']], c)} for c in cases]
    grouped = {key: [] for key in by_id}
    for observation in rows(path / 'observations.jsonl'):
        case = by_id.get(observation['case_id'])
        if case is None:
            continue
        inputs = {key: '' for key in CREDIT_RISK_FIELDS}
        inputs.update(observation)
        inputs['symbol'] = case['company'].get('symbol', '')
        ids = set(observation['document_ids'])
        payload = sample(case, [d for d in case['documents'] if d['document_id'] in ids])
        payload['window'] = {**payload['window'], 'end': observation['as_of']}
        inputs['sample_json'] = json.dumps(payload, ensure_ascii=False)
        grouped[case['case_id']].append(inputs)
    return [r for case in cases for r in walk_window(sorted(grouped[case['case_id']], key=lambda r: r['as_of']), step, case['window'])]


def walk_window(observations, step=None, window=None):
    """Group new evidence into observation periods without dropping or looking ahead."""
    from bisect import bisect_left
    from datetime import date, timedelta
    from .feed import SourceError
    step = step or 'daily'
    if step not in ('none', 'daily', 'weekly', 'monthly'):
        raise SourceError('Choose none, daily, weekly or monthly for Walk each window.')
    if not observations or step == 'daily':
        return observations
    window = window or {'start': observations[0]['window_start'], 'end': max(r['window_end'] for r in observations)}
    start = date.fromisoformat(window['start'])
    end = date.fromisoformat(window['end'])
    marks = []
    if step == 'none':
        marks = [end.isoformat()]
    else:
        cursor = start
        while cursor <= end:
            following = (cursor + timedelta(days=7) if step == 'weekly' else
                         date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1))
            last = min(end, following - timedelta(days=1))
            marks.append(last.isoformat())
            cursor = last + timedelta(days=1)
    buckets = {}
    for row in observations:
        if not start.isoformat() <= row['as_of'] <= end.isoformat():
            raise SourceError('Observation date falls outside its trajectory window.')
        cutoff = marks[bisect_left(marks, row['as_of'])]
        buckets.setdefault(cutoff, []).append(row)
    result = []
    for cutoff, rows_ in sorted(buckets.items()):
        merged = {**rows_[0], 'as_of': cutoff, 'window_end': cutoff,
                  'document_ids': list(dict.fromkeys(d for row in rows_ for d in row['document_ids']))}
        for field in ('news_batch', 'filing_batch'):
            merged[field] = '\n\n'.join(row[field] for row in rows_ if row.get(field))
        samples = [json.loads(row['sample_json']) for row in rows_]
        merged['sample_json'] = json.dumps({**samples[0], 'window': {'start': start.isoformat(), 'end': cutoff},
            'news': [d for sample in samples for d in sample.get('news', [])],
            'filings': [d for sample in samples for d in sample.get('filings', [])]}, ensure_ascii=False)
        result.append(merged)
    return result

