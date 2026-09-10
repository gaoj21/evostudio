"""
Visual demo: workflow search with DeepSeek, with a rendered search report.

Runs WorkflowSearchOptimizer end-to-end on a small two-step arithmetic
benchmark, then draws a report figure (search trajectory + the workflow
structure of each round) to:

    examples/output/workflow_search/workflow_search_demo.png

Prerequisites:
    export DEEPSEEK_API_KEY=<your-key>   # or put it in a .env file

Run from the repository root:

    python examples/skills/workflow_search_visualization.py
"""

import os

import matplotlib

matplotlib.use("Agg")  # headless rendering

import matplotlib.pyplot as plt
import networkx as nx
from dotenv import load_dotenv

from evoagentx.models import LiteLLM, LiteLLMConfig
from evoagentx.skills import (
    ListBenchmark,
    WorkflowProgram,
    make_workflow_search_optimizer,
)
from evoagentx.workflow.workflow_graph import SequentialWorkFlowGraph, WorkFlowGraph

load_dotenv()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "..", "output", "workflow_search")

# Multi-step arithmetic with a STRICT exact-match evaluator: the workflow
# must output exactly the final number. The initial workflow below is
# deliberately weak — its prompt asks for detailed reasoning, so its verbose
# output never matches exactly. The search is expected to diagnose this from
# the failing examples it is shown and fix the output contract.
DATA = [
    {"problem": "What is 347 + 289 - 156 + 78?.", "label": "558"},
    {"problem": "Compute 17 * 19 - 23 * 8..", "label": "139"},
    {"problem": "A shop sells 128 items per day. After 9 days, 376 items were returned. How many were actually sold?.", "label": "776"},
    {"problem": "What is (864 / 12) * 7 - 159?.", "label": "345"},
    {"problem": "A train travels 245 km per day for 4 days, then 188 km per day for 3 days. Total distance?.", "label": "1544"},
    {"problem": "What is 15% of 240 plus 30% of 180?.", "label": "90"},
]


def strict_exact_match_eval(prediction, label) -> dict:
    score = 1.0 if str(prediction).strip() == str(label).strip() else 0.0
    return {"accuracy": score, "em": score}


def build_initial_graph() -> WorkFlowGraph:
    return SequentialWorkFlowGraph.from_dict({
        "goal": "Answer the math question correctly.",
        "tasks": [
            {
                "name": "answer",
                "description": "Answer the math question.",
                "inputs": [{"name": "problem", "type": "str", "required": True, "description": "The math question."}],
                "outputs": [{"name": "answer", "type": "str", "required": True, "description": "The final answer."}],
                # deliberately weak: verbose output breaks the strict output contract
                "prompt": "Think about the following math question step by step, explain your reasoning in detail, and discuss your thought process thoroughly:\n{problem}",
            }
        ],
    })


def draw_graph_panel(ax, graph: WorkFlowGraph, title: str):
    """Draw a workflow graph as layered node boxes with arrows."""
    ax.set_title(title, fontsize=10)
    ax.axis("off")

    digraph = nx.DiGraph()
    for node in graph.nodes:
        digraph.add_node(node.name)
    for edge in graph.edges:
        digraph.add_edge(edge.source, edge.target)

    # layered layout by topological generations
    try:
        generations = list(nx.topological_generations(digraph))
    except nx.NetworkXError:
        generations = [list(digraph.nodes)]
    pos = {}
    for layer_idx, layer in enumerate(generations):
        for i, name in enumerate(sorted(layer)):
            pos[name] = (i - (len(layer) - 1) / 2, -layer_idx)

    for name, (x, y) in pos.items():
        ax.add_patch(plt.Rectangle((x - 0.42, y - 0.16), 0.84, 0.32,
                                   facecolor="#dbeafe", edgecolor="#1e40af", zorder=2))
        ax.text(x, y, name, ha="center", va="center", fontsize=8, zorder=3)
    for source, target in digraph.edges:
        x1, y1 = pos[source]
        x2, y2 = pos[target]
        ax.annotate("", xy=(x2, y2 + 0.16), xytext=(x1, y1 - 0.16),
                    arrowprops=dict(arrowstyle="->", color="#374151", lw=1.2), zorder=1)

    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    ax.set_xlim(min(xs) - 0.8, max(xs) + 0.8)
    ax.set_ylim(min(ys) - 0.5, max(ys) + 0.5)


def render_report(history: list, save_path: str):
    """Render the search trajectory and per-round workflow structures."""
    scored = [h for h in history if h.get("config") is not None]
    n = len(scored)

    fig = plt.figure(figsize=(4.2 * n + 1, 8))
    grid = fig.add_gridspec(2, n, height_ratios=[1, 2.2], hspace=0.25, wspace=0.3)

    # top: score trajectory across ALL rounds (failed rounds shown as gaps)
    ax_curve = fig.add_subplot(grid[0, :])
    rounds = [h["round"] for h in history]
    scores = [h["score"] for h in history]
    valid_rounds = [r for r, s in zip(rounds, scores) if s is not None]
    valid_scores = [s for s in scores if s is not None]
    ax_curve.plot(valid_rounds, valid_scores, marker="o", color="#1e40af")
    failed = [r for r, s in zip(rounds, scores) if s is None]
    if failed:
        ax_curve.scatter(failed, [0] * len(failed), marker="x", color="#dc2626", label="failed proposal")
        ax_curve.legend(fontsize=8)
    best = max(valid_scores)
    ax_curve.axhline(best, color="#16a34a", linestyle="--", linewidth=1, label=f"best = {best:.2f}")
    ax_curve.set_xlabel("search round")
    ax_curve.set_ylabel("benchmark score")
    ax_curve.set_xticks(rounds)
    ax_curve.set_ylim(-0.05, 1.05)
    ax_curve.legend(fontsize=8)
    ax_curve.set_title("Workflow search trajectory", fontsize=12)

    # bottom: workflow structure per scored round
    for col, entry in enumerate(scored):
        ax = fig.add_subplot(grid[1, col])
        graph = WorkFlowGraph.from_dict(entry["config"])
        label = f"round {entry['round']} | score={entry['score']:.2f}"
        if entry["note"] == "initial workflow":
            label += " (initial)"
        elif entry["note"] == "new best":
            label += " (new best)"
        draw_graph_panel(ax, graph, label)

    fig.suptitle("Workflow Search Demo (DeepSeek)", fontsize=14, y=0.98)
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close(fig)


def main():
    if not DEEPSEEK_API_KEY:
        raise ValueError("DEEPSEEK_API_KEY not found. Set it via environment variable or .env file.")

    config = LiteLLMConfig(model="deepseek/deepseek-v4-flash", deepseek_key=DEEPSEEK_API_KEY)
    executor_llm = LiteLLM(config=config)
    optimizer_llm = LiteLLM(config=LiteLLMConfig(model="deepseek/deepseek-v4-flash", deepseek_key=DEEPSEEK_API_KEY))

    program = WorkflowProgram(graph=build_initial_graph())
    optimizer = make_workflow_search_optimizer(
        program,
        executor_llm=executor_llm,
        optimizer_llm=optimizer_llm,
        collate_func=lambda x: {"problem": x["problem"]},
        max_rounds=3,
        eval_mode="dev",
    )
    benchmark = ListBenchmark("math_strict", DATA, eval_func=strict_exact_match_eval)

    result = optimizer.optimize(benchmark)

    print("\n=== Search finished ===")
    for h in result["history"]:
        print(f"round {h['round']}: score={h['score']} ({h['note']})")

    program.save(os.path.join(OUTPUT_DIR, "best_workflow.json"), backup=True)
    report_path = os.path.join(OUTPUT_DIR, "workflow_search_demo.png")
    render_report(result["history"], report_path)
    print("report saved to:", os.path.abspath(report_path))


if __name__ == "__main__":
    main()
