"""Random 60/40 company split with indivisible shared-evidence groups."""
import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path
from build_r16_dev_test import publish, read


def build(source, output, seed=42):
    source = Path(source)
    cases = {r['case_id']: r for r in read(source / 'cases.jsonl')}
    partitions = read(source / 'partitions.jsonl')
    groups = defaultdict(set)
    for row in partitions:
        groups[row['group_id']].add(str(cases[row['case_id']]['company']['cik']))
    companies = sorted({cik for values in groups.values() for cik in values})
    target = round(len(companies) * .4)
    rng = random.Random(seed)
    # Uniform company sampling, conditioned on keeping evidence groups intact.
    # No balancing by labels, observation counts, industry or prior partition.
    for attempt in range(1, 100001):
        test = set(rng.sample(companies, target))
        if all(not (values & test) or values <= test for values in groups.values()):
            break
    else:
        raise ValueError('Unable to draw an exact company split with intact evidence groups')
    result = [{**row, 'partition': 'test' if groups[row['group_id']] <= test else 'dev'} for row in partitions]
    return publish(source, output, partitions=result,
        split_rule='Random 60/40 by company with fixed seed; reject draws that split shared-evidence groups. No prior-partition locks or label/length/industry balancing.',
        extra_manifest={'version': 'random-dev-test-0.1', 'split_seed': seed,
                        'split_trials': attempt, 'protected_dev_groups': [], 'retained_test_groups': [],
                        'protected_review_release': None,
                        'split_builder_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.seed)))
