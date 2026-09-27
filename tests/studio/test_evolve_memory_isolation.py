"""Every Evolve replay — the baseline and each candidate round — builds its
own memory from empty, reads only that, and never touches the workflow's."""
import copy
import json

from backend.api import runner
from backend.features.evaluation import canvas_evolution
from test_evaluator_evolve_audit import FakeLLM, _evaluator_worker_in_process, evaluator  # noqa: F401

ROWS = [{'company': c, 'as_of': f'2025-0{m}-01', 'text': f'{c}-{m}', 'expected': 'yes'}
        for m in (1, 2, 3) for c in ('acme', 'globex')]


def test_each_round_remembers_on_its_own(studio_data, monkeypatch):
    from backend.features.memory import table_store
    seen = []          # (generation, company, as_of, what the node recalled)

    async def answer(agent, task, inputs, state):
        # What this replay's memory holds for the company, before this step.
        recalled = table_store.recent(state['graph_id'], 'work', inputs.get('company', ''), limit=10)
        seen.append((state['graph_id'].rsplit('-', 1)[-1], inputs['company'], inputs['as_of'],
                     sorted(r['at'] for r in recalled)))
        return {'answer': 'yes' if task.get('prompt', '').startswith('new') else 'no'}

    monkeypatch.setattr(runner, 'execute_llm_node', answer)
    from backend.api import tools_registry
    monkeypatch.setattr(tools_registry, 'validate_tool_names', lambda names: None)
    monkeypatch.setattr(tools_registry, 'resolve_tools', lambda names, **kw: None)
    work = {'name': 'work', 'description': 'd', 'prompt': 'old {company} {as_of} {text}', 'inputs': [{'name': 'company'}, {'name': 'as_of'}, {'name': 'text'}],
            'outputs': [{'name': 'answer'}], 'use_long_term_memory': True,
            'memory': {'kind': 'table', 'match': 'company', 'at': 'as_of', 'retrieve': 3, 'read_from': ['work']}}
    g = {'id': 'real-workflow', 'name': 'R', 'goal': 'g', 'flow_version': 3, 'tasks': [work], 'edges': [],
         'evaluators': [evaluator()]}
    # The real workflow already remembers something; no replay may see it.
    table_store.write('real-workflow', 'work', 'acme', '2024-12-01', {'answer': 'real'}, '2024-12-01T00:00:00')
    from evoagentx.models import LiteLLMConfig

    class Model(FakeLLM):                  # builds the agents, and proposes the prompts
        config = LiteLLMConfig(model='deepseek/deepseek-chat', deepseek_key='test-only')

        def generate(self, prompt=None, **kw):
            self.prompts.append(prompt)
            return type('R', (), {'content': json.dumps({'prompts': {'work': 'new {company} {as_of} {text}'}})})()
    llm = Model()
    monkeypatch.setattr(runner, '_make_llm', lambda **kw: llm)
    state = {'task_id': 'iso', 'execution_graph': copy.deepcopy(g)}
    canvas_evolution.execute(state, g, copy.deepcopy(ROWS),
                             {'evaluator': 'quality', 'mode': 'evolve_evaluate', 'nodes': ['work'], 'rounds': 2,
                              'source': {'type': 'canvas', 'config': {'type': 'dataloader', 'read_batch_size': 2}}},
                             lambda s: None)

    by_round = {}
    for generation, company, as_of, recalled in seen:
        by_round.setdefault(generation, []).append((company, as_of, recalled))
    assert set(by_round) == {'baseline', '1', '2'}
    for generation, steps in by_round.items():
        for company, as_of, recalled in steps:
            # Only this replay's own earlier windows — never the real one's
            # 2024-12-01, never a later window, never another round's.
            assert '2024-12-01' not in recalled, (generation, company, recalled)
            assert all(at < as_of for at in recalled), (generation, company, as_of, recalled)
        acme = [recalled for company, _, recalled in steps if company == 'acme']
        assert acme == [[], ['2025-01-01'], ['2025-01-01', '2025-02-01']], (generation, acme)
    # The workflow's own memory is exactly as it was; the replays' is gone.
    assert [r['at'] for r in table_store.rows('real-workflow', 'work')] == ['2024-12-01']
    assert not [n for n in table_store.nodes('_evolve-iso-baseline')]
