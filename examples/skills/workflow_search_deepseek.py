"""
Real-LLM demo: workflow search with DeepSeek via LiteLLM.

Runs WorkflowSearchOptimizer end-to-end on a tiny math benchmark:
the initial single-node workflow is evaluated, then the optimizer LLM
proposes structural variants, which are validated, evaluated and selected.

Prerequisites:
    export DEEPSEEK_API_KEY=<your-key>   # or put it in a .env file

Run from the repository root:

    python examples/skills/workflow_search_deepseek.py
"""

import os

from dotenv import load_dotenv

from evoagentx.models import LiteLLM, LiteLLMConfig
from evoagentx.skills import (
    ListBenchmark,
    WorkflowProgram,
    make_workflow_search_optimizer,
)
from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph

load_dotenv()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")


def contains_label_eval(prediction, label) -> dict:
    score = 1.0 if str(label).strip() in str(prediction) else 0.0
    return {"accuracy": score, "em": score}


def main():
    if not DEEPSEEK_API_KEY:
        raise ValueError("DEEPSEEK_API_KEY not found. Set it via environment variable or .env file.")

    executor_llm = LiteLLM(config=LiteLLMConfig(model="deepseek/deepseek-v4-flash", deepseek_key=DEEPSEEK_API_KEY))
    optimizer_llm = LiteLLM(config=LiteLLMConfig(model="deepseek/deepseek-v4-flash", deepseek_key=DEEPSEEK_API_KEY))

    # Initial workflow: a single node that answers directly.
    graph = SequentialWorkFlowGraph.from_dict({
        "goal": "Answer the math question correctly.",
        "tasks": [
            {
                "name": "answer",
                "description": "Answer the math question.",
                "inputs": [{"name": "problem", "type": "str", "required": True, "description": "The math question."}],
                "outputs": [{"name": "answer", "type": "str", "required": True, "description": "The final answer."}],
                "prompt": "Answer the following math question:\n{problem}",
            }
        ],
    })

    data = [
        {"problem": "What is 17 + 25? Just give the number.", "label": "42"},
        {"problem": "What is 6 * 7? Just give the number.", "label": "42"},
        {"problem": "What is 100 - 58? Just give the number.", "label": "42"},
    ]
    benchmark = ListBenchmark("math_toy", data, eval_func=contains_label_eval)

    program = WorkflowProgram(graph=graph)
    optimizer = make_workflow_search_optimizer(
        program,
        executor_llm=executor_llm,
        optimizer_llm=optimizer_llm,
        collate_func=lambda x: {"problem": x["problem"]},
        max_rounds=2,
        eval_mode="dev",
    )

    result = optimizer.optimize(benchmark)

    print("\n=== Search finished ===")
    for h in result["history"]:
        print(f"round {h['round']}: score={h['score']} ({h['note']})")
    print("best score:", result["best_score"])

    save_path = os.path.join(os.path.dirname(__file__), "..", "output", "workflow_search", "best_workflow.json")
    program.save(save_path, backup=True)
    print("best workflow saved to:", os.path.abspath(save_path))

    best_graph = program.build_graph()
    print("best workflow goal:", best_graph.goal)
    print("best workflow nodes:", [n.name for n in best_graph.nodes])


if __name__ == "__main__":
    main()
