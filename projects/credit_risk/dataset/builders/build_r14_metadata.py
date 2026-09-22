"""Cache SEC entity classifications and filing coverage; never infer negative truth."""
import argparse
import hashlib
import json
import time
import urllib.request
from datetime import date,timedelta
from pathlib import Path


def read_json(url):
    request=urllib.request.Request(url,headers={'User-Agent':'EvoAgentX Dataset Research','Accept':'application/json'})
    with urllib.request.urlopen(request,timeout=25) as response:
        return json.load(response)


def classify(sic):
    n=int(sic)
    if 100<=n<1000:return 'agriculture'
    if 1000<=n<1500:return 'mining_oil_gas'
    if 1500<=n<1800:return 'construction'
    if 2000<=n<4000:return 'manufacturing'
    if 4000<=n<5000:return 'transport_communications_utilities'
    if 5000<=n<5200:return 'wholesale'
    if 5200<=n<6000:return 'retail'
    if 6000<=n<6800:return 'finance_insurance_real_estate'
    if 7000<=n<9000:return 'services'
    if 9100<=n<9800:return 'public_administration'
    return 'unclassified'


def build(release,output):
    release,output=Path(release),Path(output)
    output.mkdir(parents=True,exist_ok=True)
    cache=output/'sec_cache';cache.mkdir(exist_ok=True)
    cases=[json.loads(x) for x in (release/'cases.jsonl').read_text().splitlines()]
    entities={c['company']['cik']:c['company'] for c in cases}
    rows=[];errors=[]
    for cik,company in entities.items():
        p=cache/(cik+'.json');url=f'https://data.sec.gov/submissions/CIK{int(cik):010d}.json'
        try:
            if not p.exists():
                d=read_json(url);p.write_text(json.dumps(d));time.sleep(.15)
            d=json.loads(p.read_text())
            if int(d['cik'])!=int(cik):raise ValueError('CIK mismatch')
            sic=d.get('sic')
            if not sic:raise ValueError('Missing SIC')
            rows.append({'cik':cik,'company':company['name'],'sec_name':d['name'],'sic':sic,
                         'industry':d.get('sicDescription'),'sector':classify(sic),
                         'review_status':'sec_entity_metadata_matched','source_url':url,
                         'retrieved_at':str(date.today()),'temporal_scope':'current_registry_metadata_not_historical_predictor',
                         'source_sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
        except Exception as e:errors.append({'cik':cik,'error':str(e)})
    (output/'industry_annotations.json').write_text(json.dumps(rows,indent=2)+'\n')
    (output/'errors.json').write_text(json.dumps(errors,indent=2)+'\n')
    print(json.dumps({'classified':len(rows),'errors':errors}))


def publish(release, annotations_path, output):
    """Attach descriptive industry sidecars; never modify agent observations."""
    import shutil
    from collections import Counter
    release,annotations_path,output=map(Path,(release,annotations_path,output))
    if output.exists() and any(output.iterdir()):raise ValueError('Use an empty output directory')
    annotations=json.loads(annotations_path.read_text())
    by_cik={x['cik']:x for x in annotations}
    cases=[json.loads(x) for x in (release/'cases.jsonl').read_text().splitlines()]
    if {c['company']['cik'] for c in cases}!=set(by_cik):raise ValueError('Industry coverage must match companies')
    lookup={c['case_id']:by_cik[c['company']['cik']] for c in cases}
    outcomes=[json.loads(x) for x in (release/'outcomes.jsonl').read_text().splitlines()]
    for o in outcomes:
        a=lookup[o['case_id']]
        o.update(previous_sector=o.get('sector'),sector=a['sector'],industry=a['industry'],sic=a['sic'],
                 sector_status=a['review_status'],sector_temporal_scope=a['temporal_scope'])
    output.mkdir(parents=True,exist_ok=True)
    for p in release.iterdir():
        if p.is_file():shutil.copy2(p,output/p.name)
    shutil.copy2(annotations_path,output/'industry_annotations.json')
    (output/'outcomes.jsonl').write_text(''.join(json.dumps(o)+'\n' for o in outcomes))
    manifest=json.loads((release/'manifest.json').read_text())
    manifest.update(version='industry-0.1',parent_release=str(release.resolve()),
        parent_manifest_sha256=hashlib.sha256((release/'manifest.json').read_bytes()).hexdigest(),
        unreviewed_sectors=sum(o['sector']=='unclassified' for o in outcomes),
        sector_company_counts=dict(Counter(a['sector'] for a in annotations)),
        distinct_sic_codes=len({a['sic'] for a in annotations}),
        builder_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        limitations=['Current SEC SIC describes cohort composition, not historical industry membership.',
                     'Point-in-time risk labels and complete negative followup remain pending.'])
    manifest['outputs']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file() and p.name!='manifest.json'}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--release',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();build(a.release,a.output)
