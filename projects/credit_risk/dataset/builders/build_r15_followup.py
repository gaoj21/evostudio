"""Build interval-specific SEC review packets from cached registrant submissions."""
import argparse
import json
from datetime import date,timedelta
from pathlib import Path
from build_r14_metadata import read_json


def records(table):
    keys=list(table)
    return [{k:table[k][i] for k in keys} for i in range(len(table.get('accessionNumber',[])))]


def interval_packet(case, filings, today):
    start=date.fromisoformat(case['window']['end'])+timedelta(days=1)
    end=start+timedelta(days=89)
    selected=[]
    for f in filings:
        when=f.get('filingDate','')
        if str(start)<=when<=str(end):
            accession=f['accessionNumber'];document=f.get('primaryDocument','')
            selected.append({'filed_at':when,'form':f.get('form'),'items':f.get('items',''),
                'accession':accession,'source_url':f'https://www.sec.gov/Archives/edgar/data/{int(case["cik"])}/{accession.replace("-", "")}/{document}',
                'priority': 'default_or_insolvency_item' if any(x in f.get('items','') for x in ['1.03','2.04']) else 'periodic_report' if f.get('form') in ['10-Q','10-K','20-F'] else 'other'})
    selected=list({x['accession']:x for x in selected}.values())
    return {'case_id':case['case_id'],'cik':case['cik'],'company':case['company'],
            'followup_start':str(start),'followup_end':str(end),
            'status':'right_censored' if end>today else 'requires_content_review',
            'negative_verified':False,'filings':sorted(selected,key=lambda x:x['filed_at']),
            'limitation':'SEC filing list is a discovery index, not proof that payment defaults did not occur.'}


def build(checks,metadata,output):
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    packets=[]
    for case in json.loads(Path(checks).read_text()):
        d=json.loads((Path(metadata)/'sec_cache'/(case['cik']+'.json')).read_text())
        fs=records(d['filings']['recent'])
        start=str(date.fromisoformat(case['window']['end'])+timedelta(days=1));end=str(date.fromisoformat(case['window']['end'])+timedelta(days=90))
        errors=[]
        for archived in d['filings'].get('files',[]):
            if archived['filingTo']<start or archived['filingFrom']>end:continue
            name=archived['name'];cache=Path(metadata)/'sec_cache'/name
            try:
                if not cache.exists():cache.write_text(json.dumps(read_json('https://data.sec.gov/submissions/'+name)))
                fs.extend(records(json.loads(cache.read_text())))
            except Exception as e:errors.append({'file':name,'error':str(e)})
        packet=interval_packet(case,fs,date.today());packet['archive_errors']=errors
        if errors:packet['status']='incomplete_index'
        packets.append(packet)
    (output/'followup_packets.json').write_text(json.dumps(packets,indent=2)+'\n')
    summary={'companies':len(packets),'filings':sum(len(p['filings']) for p in packets),
        'priority_filings':sum(f['priority']=='default_or_insolvency_item' for p in packets for f in p['filings']),
        'right_censored':sum(p['status']=='right_censored' for p in packets),'negative_verified':0,
        'incomplete_indexes':sum(p['status']=='incomplete_index' for p in packets)}
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps(summary))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--checks',required=True);p.add_argument('--metadata',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();build(a.checks,a.metadata,a.output)
