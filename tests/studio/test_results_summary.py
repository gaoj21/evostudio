"""The task page's run list carries what labels a run, never its outputs:
with full records it was hundreds of MB and the page sat on Loading."""
from backend.features.chat import result_chat


def test_a_summary_keeps_ids_status_and_label_fields_only():
    run = {'run_id': 'r1', 'batch_id': 'b1', 'status': 'success', 'created_at': 't',
           'inputs': {'key': 'A', 'text': 'x' * 5000, 'n': 3},
           'result': {'answer': 'y' * 5000}, 'node_outputs': {'work': {'answer': 'y' * 5000}},
           'nodes': [{'name': 'input', 'status': 'completed', 'output': {'key': 'A', 'body': 'z' * 5000}},
                     {'name': 'work', 'status': 'completed', 'output': {'label': 'short'}}]}

    row = result_chat.summary(run, sources={'input'})

    assert {k: row[k] for k in ('run_id', 'batch_id', 'status', 'created_at')} == \
        {'run_id': 'r1', 'batch_id': 'b1', 'status': 'success', 'created_at': 't'}
    assert row['inputs'] == {'key': 'A', 'n': 3}
    assert row['nodes'] == [{'name': 'input', 'status': 'completed', 'output': {'key': 'A'}},
                            {'name': 'work', 'status': 'completed'}]
    assert 'result' not in row and 'node_outputs' not in row
    assert len(str(row)) < 400
