"""Offline preflight: do not spend model tokens on unscorable ground truth."""
import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path


def rows(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def build(release, output):
    release, output = Path(release), Path(output)
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use an empty output directory')
    cases = rows(release/'cases.jsonl')
    outcomes = {x['case_id']:x for x in rows(release/'outcomes.jsonl')}
    observations = rows(release/'observations.jsonl')
    by_id = {c['case_id']:c for c in cases}
    docs_by_case = {c['case_id']:{d['document_id']:d for d in c['documents']} for c in cases}
    errors=[]; followup=[]; risk_queue=[]; fingerprints=defaultdict(set)
    for c in cases:
        label=outcomes[c['case_id']]
        for d in c['documents']:
            if not c['window']['start']<=d['available_at']<=c['window']['end']:
                errors.append({'case_id':c['case_id'],'reason':'document_outside_window'})
            if d['text'].strip():
                fingerprints[hashlib.sha256(d['text'].strip().encode()).hexdigest()].add(c['company']['cik'])
        if label['event_type']=='unverified':
            end=date.fromisoformat(c['window']['end'])
            followup.append({'case_id':c['case_id'],'cik':c['company']['cik'],
                'company':c['company']['name'],'baseline':str(end),
                'followup_start':str(end+timedelta(days=1)),
                'followup_end':str(end+timedelta(days=90)),
                'target':'registrant_legal_insolvency_or_confirmed_payment_default',
                'status':'requires_interval_review','negative_verified':False,
                'excluded_from_target':['routine_refinancing','rating_change','subsidiary_only_insolvency']})
    for o in observations:
        cid=o['case_id'];docs=docs_by_case[cid]
        if o['as_of']!=o['window_end'] or any(k in o for k in ['label','event_type','outcome_audit','risk_level']):
            errors.append({'case_id':cid,'reason':'observation_metadata_leak'})
        if any(d not in docs or docs[d]['available_at']!=o['as_of'] for d in o['document_ids']):
            errors.append({'case_id':cid,'reason':'observation_evidence_date_mismatch'})
        risk_queue.append({'case_id':cid,'as_of':o['as_of'],'risk_level':None,
            'review_status':'pending','evidence_ids':o['document_ids'],
            'history_evidence_ids':[d['document_id'] for d in by_id[cid]['documents'] if d['available_at']<o['as_of']],
            'instruction':'Read only evidence available on or before as_of. Event outcome is not a risk label.'})
    summary={'company_count':len({c['company']['cik'] for c in cases}),
        'case_count':len(cases),'observations':len(observations),
        'temporal_structural_errors':len(errors),'pending_negative_followups':len(followup),
        'pending_risk_annotations':len(risk_queue),
        'unknown_sector_windows':sum(x.get('sector')=='unknown' for x in outcomes.values()),
        'cross_company_exact_text_clusters':sum(len(v)>1 for v in fingerprints.values()),
        'representation_counts':dict(Counter(d['representation'] for c in cases for d in c['documents'])),
        'full_evaluation_ready':False,
        'limitations':['publication-day checks do not certify historical article revisions',
                       'entity and relevance rules require manual sampling',
                       'matched CIK does not prove complete negative followup'],
        'input_hashes':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in release.iterdir() if p.is_file()}}
    output.mkdir(parents=True,exist_ok=True)
    for name,value in [('summary',summary),('temporal_errors',errors),('negative_followup',followup)]:
        (output/(name+'.json')).write_text(json.dumps(value,indent=2)+'\n')
    (output/'risk_review_queue.jsonl').write_text(''.join(json.dumps(x)+'\n' for x in risk_queue))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--release',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();print(json.dumps(build(a.release,a.output),indent=2))
