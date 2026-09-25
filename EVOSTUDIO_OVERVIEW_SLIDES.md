# EvoStudio — Platform Overview for Slides

Updated: September 14, 2026. This presentation describes the current repository implementation, not a claim of production readiness or measured performance gains.

## Slide 1 — What Is EvoStudio?

**A configurable workspace for building, running, inspecting, and improving agent workflows.**

EvoStudio brings visual workflow design, conversational assistance, tools, memory, execution, and evaluation into one application. It builds on the EvoAgentX framework and supports custom tasks beyond any single domain.

- Define a task and its data sources.
- Compose agents and tools on a visual canvas.
- Configure memory and inspect intermediate outputs.
- Run individual inputs or batches.
- Evaluate saved results and develop improved prompts.

**Design → Run → Inspect → Evaluate → Refine**

Speaker note: Credit Risk Monitoring is one application built with these capabilities. The platform itself is not restricted to credit risk.

## Slide 2 — User Journey

1. **Organize:** Create projects and tasks, with their own workflow designs and workspaces.
2. **Connect data:** Upload a dataset or configure a supported source; map fields to node inputs.
3. **Build:** Add agents and tools, define prompts and output fields, and connect dependencies.
4. **Configure context:** Attach skills and memory resources with explicit access settings.
5. **Execute:** Start a run or batch, track progress, inspect failures, and stop execution when needed.
6. **Learn from results:** Review inputs, intermediate steps, and final outputs; ask questions or compute summaries.
7. **Improve:** Evaluate outputs, examine proposed prompt changes, and apply selected changes.

Suggested visual: A horizontal workflow from task creation to evaluation, with a return arrow from refinement to design.

## Slide 3 — Visual Design and Conversational Control

The canvas makes the workflow structure editable and inspectable.

- Input nodes provide data; agent nodes perform reasoning; tool nodes perform registered operations.
- Edges map named output fields to input fields. Control-only edges express ordering without transferring data.
- Inspectors expose prompts, schemas, tools, skills, memory policies, and execution settings.
- Canvas editing includes undo and persisted layouts.
- Workflow Chat can understand and edit the current design, including unsaved changes.
- Chat can propose executable runs, inspect run history, and stop existing execution.

A run proposal presents editable inputs and requires the user to start it. Backend validation checks the design and inputs before execution.

## Slide 4 — Data Inputs and Batch Execution

**Bring task data and shared reference data into the same workflow.**

- Upload CSV, JSON, or JSONL records and configure field mappings.
- Combine one per-record source with multiple shared reference inputs, such as a company list or policy table.
- Run batches as source data arrive, collect all records before running, or preprocess the full dataset before batching.
- Use custom preprocessing tools for filtering, deduplication, normalization, and derived fields.
- Retain run inputs and outputs for later inspection and evaluation.

Two different batch concepts are supported:

| Layer | Purpose |
|---|---|
| Workflow batching | Process multiple input records through the workflow. |
| Provider-native LLM batching | Submit grouped model requests through a compatible adapter, including the SafeChain integration boundary. |

Provider-native batching depends on the configured adapter and its tool-call support. A shared reference is passed to each record; the platform does not automatically join two independent record streams. Uploads have no default fixed size cap, but parsing remains limited by deployment resources.

## Slide 5 — Agents, Tools, Skills, and Memory

| Component | Responsibility |
|---|---|
| Agent | Interpret instructions and produce declared outputs. |
| Tool | Perform a registered operation or retrieve external information. |
| Skill | Supply reusable task instructions and domain guidance. |
| Memory | Preserve and retrieve selected context across observations or conversations. |

- Standard workflow execution remains available; workflow agents can opt into a Deep Agents harness.
- Independent canvas Chat Agents use Deep Agents with LangGraph checkpoint persistence and run separately from workflow execution.
- Chat Agents support configurable tools, attached skills, conversation sessions, and connected memory.
- Memory options include workflow history, dated subject-level tables, and project-scoped shared Mem0 spaces.
- Memory connections use stable resource identities and explicit read/write access.

Speaker note: These are available platform options. A particular workflow does not automatically use every runtime or memory backend. Subagent delegation is currently disabled in the integrated Deep Agents harness.

## Slide 6 — Results, Evaluation, and Prompt Evolution

**Use execution evidence to guide the next iteration.**

- Results show the original inputs, intermediate node outputs, and final outputs.
- Result Chat supports questions, deterministic aggregations, and isolated Python analysis where supported by the deployment.
- Workflow Chat and Result Chat share a conversation engine while retaining separate context and session histories.
- Evaluation can score existing saved runs or batches without replaying the workflow.
- Users can choose evaluation alone or evaluation combined with prompt refinement.
- Saved-result refinement proposes prompt changes from existing evidence; MIPRO-based optimization provides a separate execution-based search path.

**A proposed prompt is not a validated improvement.** New prompts require a separate evaluation run before reporting a performance gain. Evaluation also depends on suitable labels, metric selection, and dataset coverage.

## Slide 7 — Modular Architecture

```text
frontend/                 React interface and feature modules
        │
backend/api/              FastAPI routes and application assembly
        │
backend/features/         Workflow, execution, chat, agents, memory,
                          data, evaluation, library, and workspace
        │
backend/evoagentx/         Agent and workflow framework
llm/                      Central model-provider integration boundary
                          (standalone package at the repository root)
backend/memory/           Memory storage adapters

projects/                 Domain code, skills, datasets, and documentation
  credit_risk/            Credit Risk Monitoring application
```

- Frontend and backend features are organized by function.
- Domain assets live under `projects/`, separate from general platform behavior.
- Model API differences are centralized in the LLM integration layer.
- The platform and framework expose several model contracts; replacing an API requires satisfying the contracts used by the selected execution path.
- Feature modules still share dependencies and compatibility imports; this is a modular monorepo, not a microservice architecture.

## Slide 8 — Current Positioning and Demonstration

**A working development and experimentation environment for configurable agent workflows.**

Suggested demonstration:

1. Open a task and explain its source, agent, tool, and memory connections.
2. Use Workflow Chat to modify a prompt or add a field mapping.
3. Run a small input set and inspect a decision at each step.
4. Ask Result Chat to summarize the saved outputs.
5. Evaluate the existing run and review a proposed prompt change.
6. Open an independent Chat Agent to inspect connected memory.

Boundaries to state accurately:

- External tools and model behavior depend on deployment configuration.
- Stop does not undo completed tool side effects.
- Deep Agents workflow export to standalone Python is not currently supported.
- No universal accuracy, latency, cost reduction, or production-readiness claim is established by this overview.

## Short Presentation Script

> EvoStudio is a configurable environment for building and improving agent workflows. Users can bring their own data, compose agents and tools on a visual canvas, attach memory and skills, and execute individual or batch runs. Conversational controls help edit workflows, inspect results, and analyze saved outputs. Evaluation and prompt refinement connect those results back to the design process. The system separates the interface, reusable backend features, model integration, and domain projects. Credit Risk Monitoring is one example application, rather than a restriction on what the platform can support.

## Repository References

- [Module boundaries and maintenance guide](MAINTENANCE.md)
- [Custom datasets and multiple inputs](docs/custom-datasets.md)
- [Workflow Chat controls](docs/workflow-chat-controls.md)
- [Deep Agents and LangGraph integration](docs/deepagents-harness.md)
- [Credit Risk application presentation](CREDIT_RISK_PROJECT_SLIDES.md)
