"""Publish only trajectories eligible under the current pre-event target rule.

Preserves all selected evidence, outcome records, IDs and split memberships.
Never writes the source release. Run from the repository with backend on PYTHONPATH.
"""
import argparse
import csv
import hashlib
import json
from collections import Counter
from pathlib import Path
from backend.features.data.datasets import label_for


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in values))


def build(source, output):
    source, output = Path(source), Path(output)
    if output.exists(): raise ValueError('Output must be a new directory')
    original = read(source/'cases.jsonl')
    by_id = {r['case_id']:r for r in original}
    outcomes = read(source/'outcomes.jsonl')
    accepted = {r['case_id'] for r in outcomes if label_for(r,by_id[r['case_id']]) is not None}
    if not accepted: raise ValueError('No eligible trajectories')
    cases = [r for r in original if r['case_id'] in accepted]
    companies = {str(r['company']['cik']) for r in cases}
    output.mkdir(parents=True)
    for name in ('cases','observations','outcomes','partitions','excluded_news','backfill_queue','curated_observations'):
        write(output/(name+'.jsonl'), [r for r in read(source/(name+'.jsonl')) if r['case_id'] in accepted])
    parts = read(output/'partitions.jsonl')
    partition = {r['case_id']:r['partition'] for r in parts}
    assert set(partition) == accepted
    memberships = {}
    for r in parts:
        memberships.setdefault(r['group_id'],set()).add(r['partition'])
    assert all(len(v)==1 for v in memberships.values())
    counts = {}
    for split in ('dev','test'):
        for name in ('cases','observations','outcomes','partitions'):
            rows=[r for r in read(output/(name+'.jsonl')) if partition[r['case_id']]==split]
            write(output/split/(name+'.jsonl'),rows)
        selected=read(output/split/'cases.jsonl')
        counts[split]={'companies':len({str(r['company']['cik']) for r in selected}),
                       'cases':len(selected),'observations':len(read(output/split/'observations.jsonl'))}
    with (source/'obligors.csv').open(newline='') as file:
        reader=csv.DictReader(file); fields=reader.fieldnames
        company_rows=[r for r in reader if str(r['cik']) in companies]
    with (output/'obligors.csv').open('w',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=fields);writer.writeheader();writer.writerows(company_rows)
    write(output/'inventory.jsonl',[r for r in read(source/'inventory.jsonl') if str(r['cik']) in companies])
    industry=[r for r in json.loads((source/'industry_annotations.json').read_text()) if str(r['cik']) in companies]
    audit=[r for r in json.loads((source/'outcome_audit.json').read_text()) if r['case_id'] in accepted]
    for name,value in [('industry_annotations.json',industry),('outcome_audit.json',audit)]:
        (output/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    web=json.loads((source/'web_sources.json').read_text())
    for field in ('coverage','sources'):
        web[field]=[r for r in web[field] if str(r.get('cik')) in companies]
    web.update(company_count=len({str(r['cik']) for r in web['coverage']}),source_count=len(web['sources']))
    (output/'web_sources.json').write_text(json.dumps(web,ensure_ascii=False,indent=2)+'\n')
    (output/'body_quality_rules.json').write_bytes((source/'body_quality_rules.json').read_bytes())
    docs=[d for r in cases for d in r['documents']]
    selected_outcomes=read(output/'outcomes.jsonl')
    manifest={'version':'eligible-trajectories-1.0','parent_release':source.name,
      'parent_manifest_sha256':hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest(),
      'case_count':len(cases),'company_count':len(companies),'observations':len(read(output/'observations.jsonl')),
      'document_references':len(docs),'unique_documents':len({d['document_id'] for d in docs}),
      'cohorts':dict(Counter(r['cohort'] for r in cases)),
      'reviewed_event_types':dict(Counter(r['event_type'] for r in selected_outcomes)),
      'representation_counts':dict(Counter(d['representation'] for d in docs)),
      'primary_summary_documents':sum(d['representation']=='assistant_paraphrase_of_primary_source' for d in docs),
      'sector_company_counts':dict(Counter(r['sector'] for r in industry)),
      'distinct_sic_codes':len({r.get('sic') for r in industry}),
      'unreviewed_sectors':sum(not r.get('sector') for r in industry),
      'partition_counts':dict(Counter(partition.values())), 'splits':counts,
      'split_rule':'Keep original dev/test membership after eligibility filtering; no resampling. Not an exact 60/40 split.',
      'eligibility':{'rule':'event_verified; chapter_11/chapter_7/canadian_bankruptcy_assignment; registrant scope; event_date > window.end',
                     'source_cases':len(original),'retained_cases':len(cases),'removed_cases':len(original)-len(cases),
                     'positive_only':True,'negative_cases':0},
      'limitations':['Eligibility is not an exhaustive audit of evidence quality; sparse trajectories remain.',
                     'All included trajectories are positives under the current target definition. Overall accuracy and false-positive rate are not measurable.',
                     'No daily risk labels are supplied. Existing legacy gold/risk flags are not the selection rule.'],
      'builder_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
      'eligibility_code_sha256':hashlib.sha256(Path(__import__('backend.features.data.datasets',fromlist=['__file__']).__file__).read_bytes()).hexdigest()}
    manifest['outputs']={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.rglob('*')) if p.is_file()}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();result=build(args.source,args.output)
    print(json.dumps({k:result[k] for k in ('case_count','company_count','observations','unique_documents','splits')},indent=2))
