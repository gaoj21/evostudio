"""Verified outcomes of a credit-risk release, as evaluator label records.

The versioned releases keep outcomes out of the workflow's inputs. This turns
a release's reviewed outcomes into rows the monitoring_report evaluator
reads through its "Separate labels": {case_id, type, event, event_date,
scope, reason}. Only a reviewer-verified, registrant-level insolvency after
the observation window is positive; everything else is "unverified", never
a negative: no case in these releases has a verified "nothing happened".

    python -m projects.credit_risk.evaluators.labels 2026-09-10-random-dev-test-v1
prints the id of a new data resource holding the labels.
"""
import sys

INSOLVENCY_EVENTS = {
    'chapter_11', 'chapter_7', 'involuntary_chapter_7_order_for_relief',
    'assignment_for_benefit_of_creditors', 'canadian_bankruptcy_assignment',
    'ccaa_restructuring', 'receivership_order', 'irish_winding_up_petition',
}


def evaluation_label(outcome, case):
    event = outcome.get('reviewed_event') or {}
    status = outcome.get('outcome_review_status')
    kind = outcome.get('event_type')
    scope = str(event.get('scope') or '')
    base = {'case_id': case['case_id']}
    if status != 'event_verified':
        return {**base, 'type': 'unverified', 'reason': status or 'no outcome review'}
    if kind not in INSOLVENCY_EVENTS:
        return {**base, 'type': 'unverified', 'reason': f'verified event is {kind}, not an insolvency'}
    if not scope.startswith('registrant'):
        return {**base, 'type': 'unverified', 'reason': f'event scope is {scope or "unknown"}, not the registrant'}
    if not event.get('event_date') or event['event_date'] <= case['window']['end']:
        return {**base, 'type': 'unverified', 'reason': 'event falls inside the observation window'}
    return {**base, 'type': 'positive', 'event': kind, 'event_date': event['event_date'], 'scope': scope}


def release_labels(dataset):
    from credit_risk.studio import releases
    path = releases.release(dataset)
    cases = {r['case_id']: r for r in releases.rows(path / 'cases.jsonl')}
    return [evaluation_label(r, cases[r['case_id']]) for r in releases.rows(path / 'outcomes.jsonl') if r['case_id'] in cases]


def save_as_resource(dataset, graph_id=''):
    from backend.features.data import data_resources
    rows = release_labels(dataset)
    return data_resources.create_from_records(rows, f'Labels · {dataset} · {len(rows)} cases', graph_id,
                                              origin={'labels_for': dataset})


if __name__ == '__main__':
    print(save_as_resource(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else '')['id'])
