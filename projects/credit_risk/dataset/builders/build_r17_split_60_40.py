"""Seeded company-group 60/40 split; keep legacy review and previous holdout fixed."""
import argparse
import hashlib
import random
from collections import Counter, defaultdict
from pathlib import Path
from build_r16_dev_test import publish, read


def build(source, prior, output, seed=42):
    source, prior = Path(source), Path(prior)
    cases = {r['case_id']: r for r in read(source / 'cases.jsonl')}
    outcomes = {r['case_id']: r for r in read(source / 'outcomes.jsonl')}
    counts = Counter(r['case_id'] for r in read(source / 'observations.jsonl'))
    partitions = read(source / 'partitions.jsonl')
    old = {r['case_id']: r['partition'] for r in read(prior / 'partitions.jsonl')}
    groups = defaultdict(list)
    for row in partitions:
        groups[row['group_id']].append(row['case_id'])
    locked_dev = {g for g, ids in groups.items() if any(old[x] == 'legacy_review' for x in ids)}
    locked_test = {r['group_id'] for r in partitions if r['partition'] == 'test'}
    assert not locked_dev & locked_test
    sizes = {g: len({str(cases[x]['company']['cik']) for x in ids}) for g, ids in groups.items()}
    target = round(sum(sizes.values()) * .4)
    remaining = target - sum(sizes[g] for g in locked_test)
    free = sorted(set(groups) - locked_dev - locked_test)
    features = {}
    for g, ids in groups.items():
        f = Counter()
        companies = set()
        for x in ids:
            cik = str(cases[x]['company']['cik'])
            if cik not in companies:
                f['sector:' + outcomes[x].get('sector', 'unknown')] += 1
                companies.add(cik)
            f['event:' + outcomes[x]['event_type']] += 1
            f['length:' + ('1' if counts[x] == 1 else '2-9' if counts[x] < 10 else '10-49' if counts[x] < 50 else '50+')] += 1
            f['observations'] += counts[x]
        features[g] = f
    total = sum(features.values(), Counter())
    fixed = sum((features[g] for g in sorted(locked_test)), Counter())
    def score(chosen):
        f = fixed + sum((features[g] for g in sorted(chosen)), Counter())
        # Balance observable data composition, never model predictions or scores.
        loss = sum((f[k] - .4*v)**2 / max(v, 1) for k, v in total.items() if k != 'observations')
        return loss + 100 * (f['observations'] / total['observations'] - .4)**2
    rng = random.Random(seed)
    best = None
    for _ in range(3000):
        order = free.copy()
        rng.shuffle(order)
        solutions = {0: ()}
        for g in order:
            for n, chosen in list(solutions.items()):
                if n + sizes[g] <= remaining and n + sizes[g] not in solutions:
                    solutions[n + sizes[g]] = (*chosen, g)
        if remaining not in solutions:
            raise ValueError('Exact 60/40 is impossible without splitting protected groups')
        chosen = solutions[remaining]
        candidate = (score(chosen), tuple(sorted(chosen)))
        if best is None or candidate < best:
            best = candidate
    test = locked_test | set(best[1])
    result = [{**r, 'partition': 'test' if r['group_id'] in test else 'dev'} for r in partitions]
    assert all(r['partition'] == 'dev' for r in result if r['group_id'] in locked_dev)
    summary = publish(source, output, partitions=result,
        split_rule='60/40 by company, indivisible shared-evidence groups; legacy-review groups remain dev, existing test groups remain test; seeded composition balancing.',
        extra_manifest={'version': 'dev-test-60-40-0.1', 'split_seed': seed, 'split_trials': 3000,
                        'protected_review_release': str(prior), 'protected_dev_groups': sorted(locked_dev),
                        'retained_test_groups': sorted(locked_test),
                        'split_builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
    return summary


if __name__ == '__main__':
    import json
    p = argparse.ArgumentParser()
    p.add_argument('--source', required=True)
    p.add_argument('--prior', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    print(json.dumps(build(a.source, a.prior, a.output)))
