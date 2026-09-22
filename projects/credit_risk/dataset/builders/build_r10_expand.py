"""Expand cached monitoring evidence and add dated, primary-source events.

No model calls or network needed to rebuild. Labels remain sidecars. Cached
legacy outcome claims are not promoted to gold. A filing-only company is a
valid monitoring candidate; zero evidence is never used to meet a quota.
"""
import argparse
import copy
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from build_r09_review import annotate, norm

HERE = Path(__file__).resolve().parent.parent
VERSION = 'expansion-0.1'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()[:20]


def read_rows(path):
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def write_rows(path, rows):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))


def day(value):
    value = str(value or '')[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def window_ending(end):
    return {'start': (date.fromisoformat(end)-timedelta(days=179)).isoformat(), 'end': end}


def cached_window(candidate, prior, review):
    # Preserve R08's corrected boundary instead of resurrecting the original
    # candidate date. Only a newer explicit review overrides that boundary.
    if review:
        return window_ending((date.fromisoformat(review['event_date'])-timedelta(days=1)).isoformat())
    if prior and prior.get('window'):
        return dict(prior['window'])
    return window_ending((date.fromisoformat(candidate['ref_date'])-timedelta(days=1)).isoformat())


def raw_paths(base, candidate):
    key = re.sub(r'[^A-Za-z0-9_-]', '', candidate['symbol'] or candidate['cik'])
    stem = candidate['type'][:3] + '_' + key + '.jsonl'
    return [base/'raw'/folder/stem for folder in ['news','fulltext','filings']]


def merge_evidence(rows, kind):
    result = {}
    for r in rows:
        date_key = day(r.get('filing_date') if kind == 'filing' else r.get('date'))
        if not date_key:
            continue
        key = (r.get('adsh') or digest(r.get('text') or '')) if kind == 'filing' else (date_key, r.get('url'), norm(r.get('title')))
        # Richest cached representation; archival text revision is explicitly
        # unverified, so these are not point-in-time certified gold records.
        if key not in result or len(r.get('text') or '') > len(result[key].get('text') or ''):
            result[key] = r
    return list(result.values())


def documents(company, sid, news, filings, window):
    kept, excluded, clusters = [], [], set()
    fake_sample = {'sample_id': sid, 'company': company}
    for n in sorted(news, key=lambda x:(x.get('date') or '', x.get('url') or '')):
        when=day(n.get('date'))
        if not when or not window['start'] <= when <= window['end']:
            continue
        review=annotate(fake_sample,n)
        key=norm(n.get('title'))
        reason = None
        if review['entity_status'] != 'candidate_match': reason=review['entity_status']
        elif key in clusters: reason='duplicate_title'
        if reason:
            excluded.append({'date':when,'title':n.get('title'), 'url':n.get('url'), 'reason':reason})
            continue
        clusters.add(key)
        text=(n.get('text') or '').strip()
        kept.append({'document_id':digest('news|'+str((when,n.get('url'),key))),
                     'available_at':when,'kind':'news','title':n.get('title') or '',
                     'text':text,'source_url':n.get('url'),
                     'representation':'cached_article' if text else 'headline_only',
                     'temporal_status':'publication_day_only_content_revision_unverified'})
    for f in filings:
        when=day(f.get('filing_date'))
        if not when or not window['start'] <= when <= window['end'] or not (f.get('text') or '').strip():
            continue
        accession=f.get('adsh') or ''
        kept.append({'document_id':digest('filing|'+company['cik']+'|'+accession+'|'+when),
                     'available_at':when,'kind':'sec_filing','title':f.get('form') or '8-K',
                     'text':f['text'],'accession':accession,'items':f.get('items') or [],
                     'representation':'cached_filing_body',
                     'temporal_status':'filing_day_only',
                     'source_url':f'https://www.sec.gov/Archives/edgar/data/{int(company["cik"])}/{accession.replace("-", "")}/{accession}-index.html' if accession else None})
    return sorted(kept,key=lambda x:(x['available_at'],x['document_id'])),excluded


def passages(text, max_chars=5000):
    """Readable excerpts after substantive item headings, not cover pages.

    Excerpts are extractive and carry offsets; no generated findings are
    inserted into raw filings. All items can matter, so this is not an
    event-type labeler or a bankruptcy-only filter.
    """
    matches=list(re.finditer(r'\bItem\s+\d\.\d\d\b',text,re.I))
    offsets=[]
    for m in matches:
        if not offsets or m.start()-offsets[-1]>500:
            offsets.append(m.start())
    if not offsets: offsets=[0]
    pieces=[];remaining=max_chars
    for offset in offsets:
        if remaining <= 0: break
        end=min(len(text),offset+min(1800,remaining))
        excerpt=text[offset:end]
        pieces.append(f'[characters {offset}:{end}] {excerpt}')
        remaining-=len(excerpt)
    return '\n'.join(pieces)


def observation(case, available_at):
    # Each observation only contains documents dated on that day; running
    # the sorted episode builds memory without replaying every old article.
    docs=[d for d in case['documents'] if d['available_at']==available_at]
    news=[];filings=[]
    for d in docs:
        text=passages(d['text']) if d['kind']=='sec_filing' else d['text'][:3000]
        block=f"[{d['available_at']}] {d['title']}\nSource: {d.get('source_url') or 'cached source'}\n{text}"
        (filings if d['kind']=='sec_filing' else news).append(block)
    return {'case_id':case['case_id'],'sample_id':case['case_id'],
            'company':case['company']['name'],'cik':case['company']['cik'],
            'as_of':available_at,'window_start':case['window']['start'],
            # Never expose the final window endpoint to earlier observations.
            'window_end':available_at,
            'news_batch':'\n\n'.join(news) or '(no new news at this observation)',
            'filing_batch':f"Registrant CIK: {case['company']['cik']}\n"+'\n\n'.join(filings) if filings else '(no new SEC filing at this observation)',
            'document_ids':[d['document_id'] for d in docs]}


def reviewed_documents(evidence, cik, window):
    result=[]
    for e in evidence:
        if e['cik'] != cik:
            continue
        if e.get('review_status') != 'primary_source_checked' or day(e.get('published_at')) is None:
            raise ValueError('Backfill needs reviewed content and a publication date')
        if window['start'] <= e['published_at'] <= window['end']:
            result.append({'document_id':digest(e['source_url']),
                'available_at':e['published_at'],'kind':'primary_source_summary',
                'title':e['title'],'text':e['summary'],'source_url':e['source_url'],
                'representation':'assistant_paraphrase_of_primary_source',
                'temporal_status':'source_publication_day_checked'})
    return result


def build(base, curated_path, reviews_path, output, discovery_path=None, evidence_path=None, audit_path=None):
    base, output=Path(base),Path(output)
    if output.resolve()==base.resolve() or base.resolve() in output.resolve().parents:
        raise ValueError('Expansion must live outside the live dataset')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Use an empty release directory')
    originals=read_rows(base/'samples.jsonl')
    old={str(s['company']['cik']).lstrip('0'):s for s in originals}
    candidates=list(csv.DictReader((base/'candidates.csv').open()))
    curated=json.loads(Path(curated_path).read_text())
    reviews=json.loads(Path(reviews_path).read_text())
    paths={base/'samples.jsonl',base/'candidates.csv',Path(curated_path),Path(reviews_path)}
    evidence=json.loads(Path(evidence_path).read_text()) if evidence_path else []
    audit=json.loads(Path(audit_path).read_text()) if audit_path else []
    audit_by_id={r['case_id']:r for r in audit}
    paths.update(Path(p) for p in [evidence_path,audit_path] if p)
    cases=[];outcomes=[];inventory=[];noise=[];pools={}
    sectors={e['cik']:e['sector'] for e in curated}
    for c in candidates:
        cik=c['cik'].lstrip('0');prior=old.get(cik)
        files=raw_paths(base,c);paths.update(p for p in files if p.exists())
        n=read_rows(files[0])+read_rows(files[1])+(prior['news'] if prior else [])
        f=read_rows(files[2])+(prior.get('filings',[]) if prior else [])
        n=merge_evidence(n,'news');f=merge_evidence(f,'filing');pools[cik]=(n,f)
        company={'cik':cik,'name':re.sub(r'\s*\([^)]*\)','',c['name']).strip(),
                 'symbol':c['symbol'] or None,'query_name':c['query_name']}
        review=reviews.get(cik)
        w=cached_window(c,prior,review)
        key=digest('cached|'+cik+'|'+c['ref_date'])
        docs,rejected=documents(company,key,n,f,w)
        docs += reviewed_documents(evidence,cik,w)
        docs=sorted({d['document_id']:d for d in docs}.values(),key=lambda d:(d['available_at'],d['document_id']))
        entry={'cik':cik,'company':company['name'],'existing':bool(prior),'documents':len(docs),
               'raw_news':len(n),'raw_filings':len(f),'window':w,
               'decision':'included' if docs else 'excluded_no_usable_dated_evidence'}
        inventory.append(entry)
        noise.extend({'case_id':key,**x} for x in rejected)
        if not docs:continue
        case={'case_id':key,'group_id':'cik:'+cik,'company':company,'window':w,
              'cohort':'legacy' if prior else 'recovered_cache','documents':docs}
        cases.append(case)
        outcomes.append({'case_id':key,'legacy_sample_id':prior['sample_id'] if prior else None,
            'origin_label':prior['label'] if prior else {'candidate_type':c['type'],'reference_date':c['ref_date']},
            'reviewed_event':review,'event_type':review['event_type'] if review else 'unverified',
            'risk_level':None,'followup_status':'unverified','gold_eligible':False,
            'review_reasons':['point_in_time_risk_unlabelled','coverage_not_exhaustive']+([] if review else ['outcome_unverified']),
            'sector':sectors.get(cik,'unknown'),'sector_status':'coarse_manual' if cik in sectors else 'unreviewed'})
        if key in audit_by_id:
            outcome=outcomes[-1];review_audit=audit_by_id[key]
            outcome['outcome_review_status']=review_audit['review_status']
            outcome['outcome_audit']=review_audit
            if review_audit['review_status']=='negative_followup_insufficient':
                outcome['review_reasons'] += review_audit['remaining_requirements']
    for e in curated:
        key=digest('curated|'+e['key']);company={'cik':e['cik'],'name':e['name'],'symbol':e['symbol'],'query_name':e['name']}
        w=window_ending(e['published_at']);n,f=pools.get(e['cik'],([],[]))
        docs,rejected=documents(company,key,n,f,w)
        noise.extend({'case_id':key,**x} for x in rejected)
        for evidence in e.get('earlier_evidence',[])+[e]:
            when=evidence['published_at']
            if not w['start']<=when<=w['end']:continue
            docs.append({'document_id':digest(evidence['source_url']), 'available_at':when,
                         'kind':'primary_source_summary','title':'Dated company credit disclosure',
                         'text':evidence['summary'],'source_url':evidence['source_url'],
                         'representation':'assistant_paraphrase_of_primary_source',
                         'temporal_status':'source_publication_day_checked'})
        cases.append({'case_id':key,'group_id':'cik:'+e['cik'],'company':company,'window':w,
                      'cohort':'curated_monitoring','documents':sorted(docs,key=lambda d:(d['available_at'],d['document_id']))})
        outcomes.append({'case_id':key,'reviewed_event':e,'event_type':e['event_type'],
                         'risk_level':None,'followup_status':'unverified','gold_eligible':False,
                         'review_reasons':['risk_level_unlabelled','monitoring_not_pre_event_forecast'],
                         'sector':e['sector'],'sector_status':'coarse_manual'})
    groups={c['group_id'] for c in cases}
    # Old samples and their other episodes stay together; new groups can be
    # reserved for review. This is not a claim of a clean temporal test set.
    splits={g:('legacy_review' if g.split(':')[1] in old else
               'holdout_candidate' if int(digest(g),16)%5==0 else 'development_candidate') for g in groups}
    partitions=[{'case_id':c['case_id'],'group_id':c['group_id'],'partition':splits[c['group_id']]} for c in cases]
    observations=[observation(c,d) for c in cases for d in sorted({x['available_at'] for x in c['documents']})]
    labels={o['case_id']:o for o in outcomes}
    tasks=[]
    for c in cases:
        docs=c['documents'];filings=[d for d in docs if d['kind']=='sec_filing']
        known=[d['available_at'] for d in docs]
        tasks.append({'case_id':c['case_id'],'company':c['company']['name'],'cohort':c['cohort'],
                      'documents':len(docs),'filings':len(filings),'first_available':min(known),'last_available':max(known),
                      'days_before_first_evidence':(date.fromisoformat(min(known))-date.fromisoformat(c['window']['start'])).days,
                      'label_review':labels[c['case_id']]['review_reasons'],
                      'news_needed':not any(d['kind']=='news' for d in docs),
                      'query':f'"{c["company"]["name"]}" debt liquidity refinancing',
                      'priority':'sparse' if len(docs)<3 else 'normal'})
    output.mkdir(parents=True,exist_ok=True)
    if audit_path:
        (output/'outcome_audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    if evidence_path:
        (output/'review_backfill.json').write_text(json.dumps(evidence,indent=2)+'\n')
    if discovery_path:
        discovery_path=Path(discovery_path)
        discovery=json.loads(discovery_path.read_text())
        expected={c['company']['cik'] for c in cases}
        if {c['cik'] for c in discovery['coverage']} != expected:
            raise ValueError('Search coverage must match release companies')
        paths.add(discovery_path)
        # Discovery links never enter observations without dated content review.
        (output/'web_sources.json').write_text(json.dumps(discovery,indent=2)+'\n')
    for name,rows in [('cases',cases),('outcomes',outcomes),('partitions',partitions),('observations',observations),
                      ('inventory',inventory),('excluded_news',noise),('backfill_queue',tasks)]:
        write_rows(output/(name+'.jsonl'),rows)
    # The entity registry contains identities only, never outcomes.
    registry = {c['company']['cik']: c['company'] for c in cases}
    with (output/'obligors.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=['cik','name','symbol','query_name'])
        writer.writeheader()
        writer.writerows(registry[cik] for cik in sorted(registry))
    # A small sidecar-only smoke set exercises new event types without
    # sending the entire review pool into an expensive batch accidentally.
    curated_ids={c['case_id'] for c in cases if c['cohort']=='curated_monitoring'}
    write_rows(output/'curated_observations.jsonl',[o for o in observations if o['case_id'] in curated_ids])
    manifest={'version':VERSION,'case_count':len(cases),'company_count':len(groups),'old_company_count':len(old),
              'cohorts':dict(Counter(c['cohort'] for c in cases)),
              'document_references':sum(len(c['documents']) for c in cases),
              'unique_documents':len({d['document_id'] for c in cases for d in c['documents']}),
              'observations':len(observations),'reviewed_event_types':dict(Counter(o['event_type'] for o in outcomes)),
              'primary_summary_documents':len({d['document_id'] for c in cases for d in c['documents'] if d['kind']=='primary_source_summary'}),
              'unreviewed_sectors':sum(o['sector']=='unknown' for o in outcomes),'gold_samples':0,
              'excluded_companies':[x for x in inventory if x['decision']!='included'],
              'partition_counts':dict(Counter(p['partition'] for p in partitions)),
              'inputs':{str(p.relative_to(HERE) if p.is_relative_to(HERE) else p):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths)},
              'builder_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'annotation_rules_sha256':hashlib.sha256((Path(__file__).resolve().parent/'build_r09_review.py').read_bytes()).hexdigest(),
              'outputs':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(output.iterdir()) if p.is_file()}}
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')
    return manifest


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--base',type=Path,default=HERE/'contemporary')
    p.add_argument('--curated',type=Path,default=HERE/'expansion/curated_events.json')
    p.add_argument('--reviews',type=Path,default=HERE/'review/event_reviews.json')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--discovery',type=Path)
    p.add_argument('--evidence',type=Path)
    p.add_argument('--audit',type=Path)
    a=p.parse_args()
    m=build(a.base,a.curated,a.reviews,a.output,a.discovery,a.evidence,a.audit)
    print(json.dumps({k:v for k,v in m.items() if k not in ['inputs','outputs']},ensure_ascii=False,indent=2))
