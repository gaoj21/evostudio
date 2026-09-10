"""Every rebuild of the agents during an optimization carries the tools."""


def test_rebuilds_get_the_tools_the_first_build_had():
    calls = []

    class Manager:
        def update_agents_from_workflow(self, workflow_graph, llm_config=None, tools=None, **kw):
            calls.append(tools)

    m = Manager()
    base_tools = ["ObligorMatchToolkit-instance"]
    _update = m.update_agents_from_workflow

    def _update_with_tools(workflow_graph, llm_config=None, tools=None, **kw):
        return _update(workflow_graph=workflow_graph, llm_config=llm_config,
                       tools=tools if tools is not None else base_tools, **kw)
    m.update_agents_from_workflow = _update_with_tools

    m.update_agents_from_workflow(workflow_graph="g", llm_config="c")          # as the evaluator calls it
    m.update_agents_from_workflow(workflow_graph="g", llm_config="c", tools=["other"])
    assert calls == [base_tools, ["other"]]
