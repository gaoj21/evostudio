"""Publish dev/test partitions without moving any company or shared evidence across sets."""
import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows))


def publish(source, output, partitions=None, split_rule=None, extra_manifest=None):
    source, output = Path(source), Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use an empty output directory')
    cases = read(source / 'cases.jsonl')
    mapping = {'legacy_review': 'dev', 'development_candidate': 'dev', 'holdout_candidate': 'test', 'dev': 'dev', 'test': 'test'}
    if partitions is None:
        partitions = [{**row, 'partition': mapping[row['partition']]} for row in read(source / 'partitions.jsonl')]
    by_case = {row['case_id']: row['partition'] for row in partitions}
    assert len(by_case) == len(partitions) == len(cases)
    assert set(by_case) == {row['case_id'] for row in cases}
    # Preserve company, evidence component, document and nonempty body isolation.
    memberships = defaultdict(set)
    for row in partitions:
        memberships['group:' + row['group_id']].add(row['partition'])
    for case in cases:
        split = by_case[case['case_id']]
        memberships['cik:' + str(case['company']['cik'])].add(split)
        for doc in case['documents']:
            memberships['doc:' + doc['document_id']].add(split)
            if doc.get('text', '').strip():
                digest = hashlib.sha256(doc['text'].strip().encode()).hexdigest()
                memberships['body:' + digest].add(split)
    assert all(len(value) == 1 for value in memberships.values()), 'Cross-split evidence leakage'
    output.mkdir(parents=True, exist_ok=True)
    for path in source.iterdir():
        if path.is_file():
            shutil.copy2(path, output / path.name)
    write(output / 'partitions.jsonl', partitions)
    counts = {}
    for split in ('dev', 'test'):
        selected = [row for row in cases if by_case[row['case_id']] == split]
        counts[split] = {'companies': len({str(row['company']['cik']) for row in selected}), 'cases': len(selected)}
        for name in ('cases', 'observations', 'outcomes', 'partitions'):
            rows = partitions if name == 'partitions' else read(source / (name + '.jsonl'))
            rows = [row for row in rows if by_case[row['case_id']] == split]
            write(output / split / (name + '.jsonl'), rows)
            if name == 'observations':
                counts[split]['observations'] = len(rows)
    manifest = json.loads((source / 'manifest.json').read_text())
    manifest.update(version='dev-test-0.1', parent_release=str(source),
                    parent_manifest_sha256=hashlib.sha256((source / 'manifest.json').read_bytes()).hexdigest(),
                    partition_counts=dict(Counter(by_case.values())), splits=counts,
                    split_rule=split_rule or 'Merge legacy_review and development_candidate into dev; retain holdout_candidate as test. Preserve company and shared-evidence groups.',
                    builder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    manifest.update(extra_manifest or {})
    manifest['outputs'] = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
                           for path in sorted(output.rglob('*')) if path.is_file() and path.name != 'manifest.json'}
    (output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return counts


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    print(json.dumps(publish(args.source, args.output), ensure_ascii=False))
