"""Expose typed data/evaluation components through the ordinary tool registry."""
from evoagentx.tools.tool import Tool, Toolkit


class ReadDataset(Tool):
    name: str = 'read_dataset'
    description: str = 'Read and preprocess an uploaded data resource with a DataLoader. Returns records and a configuration snapshot.'
    inputs: dict = {'config': {'type':'object', 'description':'DataLoader configuration: resource_id, loader, optional transform and grouping settings.'}}
    required: list = ['config']

    def __call__(self, config: dict):
        from backend.features.data.dataloaders import prepare
        records, snapshot = prepare(config)
        return {'records':records, 'snapshot':snapshot}


class EvaluateRecords(Tool):
    name: str = 'evaluate_records'
    description: str = 'Evaluate supplied predictions with a configured evaluator. The tool chooses aggregation and returns metrics with optional coverage and details.'
    inputs: dict = {'records': {'type':'array', 'description':'Complete saved executions: id, inputs, result, nodes, node_outputs, status, error, execution_snapshot and focus.'},
                    'config': {'type':'object', 'description':'Evaluator settings including type and field paths.'}}
    required: list = ['records', 'config']

    def __call__(self, records: list, config: dict):
        from backend.features.evaluation.evaluator_tools import report
        return report(config, records)


class DataEvaluationToolkit(Toolkit):
    def __init__(self, **kwargs):
        super().__init__(name='DataEvaluationToolkit', tools=[ReadDataset(),EvaluateRecords()])
