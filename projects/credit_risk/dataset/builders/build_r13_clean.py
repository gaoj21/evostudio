"""Reproducible body quarantine and shared-evidence split isolation; no API calls."""
import argparse
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from pathlib import Path
from build_r10_expand import observation, read_rows, write_rows


def clean_document(document, rules):
    d = dict(document)
    fingerprint = hashlib.sha256(d['text'].strip().encode()).hexdigest()
    if fingerprint in rules and d['kind'] == 'news':
        d.update(text='', representation='headline_only', body_quality='quarantined',
                 quarantined_body_sha256=fingerprint, body_quality_reason=rules[fingerprint])
    return d


def isolated_partitions(cases, prior):
    parent = {c['group_id']:c['group_id'] for c in cases}
    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    seen = {}
    for c in cases:
        for d in c['documents']:
            keys = ['id:'+d['document_id']]
            if d['text'].strip():
                keys.append('body:'+hashlib.sha256(d['text'].strip().encode()).hexdigest())
            for key in keys:
                if key in seen:
                    a,b = sorted([root(c['group_id']),root(seen[key])]); parent[b]=a
                seen[key]=c['group_id']
    groups=defaultdict(list)
    for c in cases: groups[root(c['group_id'])].append(c['case_id'])
    prior={p['case_id']:p['partition'] for p in prior}
    result=[]
    for group, ids in groups.items():
        values={prior[x] for x in ids}
        # A component exposed to development/review cannot remain a holdout.
        partition=('legacy_review' if 'legacy_review' in values else
                   'development_candidate' if 'development_candidate' in values else 'holdout_candidate')
        result.extend({'case_id':x,'group_id':group,'partition':partition} for x in ids)
    return result


def build(source, output, rules_path):
    source,output,rules_path=map(Path,(source,output,rules_path))
    if output.exists() and any(output.iterdir()): raise ValueError('Use an empty output directory')
    rules=json.loads(rules_path.read_text())['body_hashes']
    cases=read_rows(source/'cases.jsonl'); affected=set(); refs=0
    for c in cases:
        c['documents']=[clean_document(d,rules) for d in c['documents']]
        for d in c['documents']:
            if d.get('body_quality')=='quarantined': affected.add(d['document_id']); refs+=1
    partitions=isolated_partitions(cases,read_rows(source/'partitions.jsonl'))
    observations=[observation(c,day) for c in cases for day in sorted({d['available_at'] for d in c['documents']})]
    output.mkdir(parents=True,exist_ok=True)
    for p in source.iterdir():
        if p.is_file(): shutil.copy2(p,output/p.name)
    for name,rows in [('cases',cases),('observations',observations),('partitions',partitions)]:
        write_rows(output/(name+'.jsonl'),rows)
    curated={c['case_id'] for c in cases if c['cohort']=='curated_monitoring'}
    write_rows(output/'curated_observations.jsonl',[o for o in observations if o['case_id'] in curated])
    shutil.copy2(rules_path,output/'body_quality_rules.json')
    manifest=json.loads((source/'manifest.json').read_text())
    manifest.update(version='clean-0.1', parent_release=str(source.resolve()),
        parent_manifest_sha256=hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest(),
        quarantined_unique_documents=len(affected),quarantined_document_references=refs,
        representation_counts=dict(Counter(d['representation'] for c in cases for d in c['documents'])),
        partition_counts=dict(Counter(p['partition'] for p in partitions)),
        builder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        limitations=['Headline dates remain publication-day only; historic body revisions unverified.',
                     'Sector and point-in-time risk annotations remain incomplete; not a gold evaluation set.'])
    manifest['outputs']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file() and p.name!='manifest.json'}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    return {k:manifest[k] for k in ['company_count','case_count','observations','quarantined_unique_documents','quarantined_document_references','partition_counts']}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);p.add_argument('--rules',required=True)
    a=p.parse_args();print(json.dumps(build(a.source,a.output,a.rules),indent=2))
