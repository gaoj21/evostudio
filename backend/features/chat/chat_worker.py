"""Isolated blocking calls so Stop can terminate sockets and model retries."""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root.parent))
sys.path.insert(0, str(root))


def main():
    kind, directory = sys.argv[1:]
    directory = Path(directory)
    payload = json.loads((directory / 'input.json').read_text())
    try:
        if kind == 'model':
            from llm import chat
            value = chat(None, payload['messages'])
        elif kind == 'collect':
            from backend.api import sources
            from backend.api.source_apis import collect_gdelt_records, fetch_http_api
            config = payload['source']
            def emit(record):
                with (directory / 'records.jsonl').open('a') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            if config['type'] == 'gdelt_news':
                value = collect_gdelt_records(config, lambda done, total: (directory / 'stage.txt').write_text(json.dumps({'completed': done, 'total': total})), on_record=emit)
            elif config['type'] == 'http_api' and config.get('batch_items') == 'items':
                value = fetch_http_api(config, split_records=True)
            else:
                value = sources.records_from_source_node(payload)
            if config['type'] != 'gdelt_news':
                for record in value:
                    emit(record)
        elif kind == 'preprocess':
            from backend.api.custom_tools import run_custom_tool
            value = run_custom_tool(payload['name'], payload['arguments'])
        elif kind == 'evaluate_python':
            from backend.features.evaluation.python_evaluator import execute
            value = execute(payload)
        elif kind == 'dataset':
            from backend.features.data.torch_loader import execute_python
            value = execute_python(payload)
        elif kind == 'verify_tool':
            from backend.features.library.tool_verification import verify
            value = verify(payload['spec'])
        elif kind == 'generate':
            from backend.api.chat_api import _workflow_from_goal
            value = _workflow_from_goal(payload['goal'], on_stage=lambda text: (directory / 'stage.txt').write_text(text))
        else:
            raise ValueError('Unknown assistant worker')
        result = {'value': value}
    except Exception as exc:
        result = {'error': f'{type(exc).__name__}: {exc}'}
    (directory / 'result.json').write_text(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
