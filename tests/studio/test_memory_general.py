"""Memory is configured by the data's own fields, whatever the pipeline is about.

Nothing here knows about companies, windows or as_of. A support pipeline
keyed by customer, dated by created_at (or by nothing at all) has to get the
same guarantees the credit-risk pipeline got from its field names: the run
form asks for what memory needs, missing values degrade memory rather than
the run, and a batch runs dependent records in order.
"""
import asyncio

import pytest

from backend.features.memory import bindings, memory_policy as policy, sequencing, table_store as store


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "TABLES_DIR", tmp_path)
    store.forget()


def node(name="triage", memory=None, inputs=("customer", "text"), outputs=("reply",), **kw):
    return {"name": name, "use_long_term_memory": memory is not None,
            "inputs": [{"name": n} for n in inputs], "outputs": [{"name": n} for n in outputs],
            "memory": memory, **kw}


V2 = {"version": 2, "kind": "table", "write_mode": "append", "time_filter": False}


class TestFieldsMemoryNeedsBecomeWorkflowInputs:
    def test_a_time_field_no_node_produces_is_asked_for_optionally(self):
        t = node(memory={**V2, "match": "customer", "at": "created_at", "time_filter": True})
        extra = bindings.binding_inputs([t])
        assert [(e["name"], e["required"], e["memory_binding"]) for e in extra] == [("created_at", False, "time")]
        assert "reads only earlier records" in extra[0]["description"]

    def test_fields_a_node_already_has_are_not_added(self):
        t = node(memory={**V2, "match": "customer"})
        assert bindings.binding_inputs([t]) == []

    def test_qualified_references_and_disabled_nodes_add_nothing(self):
        assert bindings.binding_inputs([node(memory={**V2, "at": "nodes.loader.outputs.when"})]) == []
        assert bindings.binding_inputs([node(memory={**V2, "at": "when"}, enabled=False)]) == []
        assert bindings.binding_inputs([node(memory=None)]) == []

    def test_the_workflow_inputs_include_it(self):
        from backend.features.workflow.graphs import compute_workflow_inputs
        t = node(memory={**V2, "at": "created_at"})
        names = [w["name"] for w in compute_workflow_inputs([t], [])]
        assert names == ["customer", "text", "created_at"]


class TestValidationOnlyRejectsWhatCannotExist:
    def test_a_data_field_is_accepted(self):
        t = node(memory={**V2, "match": "account_id", "at": "created_at"})
        assert policy.validate(t, {"triage": t}) == []

    def test_a_named_node_field_that_does_not_exist_is_an_error(self):
        t = node(memory={**V2, "at": "nodes.loader.outputs.created_at"})
        errors = policy.validate(t, {"triage": t, "loader": {"outputs": [{"name": "id"}]}})
        assert errors and "does not exist" in errors[0]


class TestMissingValuesDegradeMemoryNotTheRun:
    def test_a_record_without_its_time_is_kept_and_says_so(self):
        t = node(memory={**V2, "match": "customer", "at": "created_at", "time_filter": True})
        payload = policy.select(t, {"customer": "acme", "text": "x"}, {"reply": "ok"}, run_data={})
        assert payload["unbound"] == [{"label": "event_time", "reference": "created_at"}]
        row = policy.table_write(t, payload)
        assert row["subject"] == "acme" and row["at"] == ""

    def test_a_record_without_its_entity_is_kept_when_appending(self):
        t = node(memory={**V2, "match": "customer"})
        payload = policy.select(t, {"text": "x"}, {"reply": "ok"}, run_data={})
        assert payload["unbound"][0]["label"] == "subject"
        assert policy.table_write(t, payload)["subject"] == ""

    def test_the_legacy_entity_and_date_update_skips_it(self):
        t = node(memory={"kind": "table", "match": "customer", "at": "created_at"})
        payload = policy.select(t, {"text": "x"}, {"reply": "ok"}, run_data={"created_at": "2026-09-01"})
        assert policy.table_write(t, payload) is None

    def test_an_update_without_its_unique_key_is_refused(self):
        t = node(memory={**V2, "write_mode": "upsert", "key": "ticket_id"})
        with pytest.raises(bindings.BindingError, match="unique key 'ticket_id'"):
            policy.select(t, {"customer": "a", "text": "x"}, {"reply": "ok"}, run_data={})

    def test_a_time_filtered_read_without_a_cutoff_explains_the_fix(self):
        t = node(memory={**V2, "at": "created_at", "time_filter": True})
        with pytest.raises(bindings.BindingError, match="Provide 'created_at'.*turn off"):
            asyncio.run(policy.recall_async({}, t, {}, table=store, graph_id="g"))

    def test_an_undated_record_never_reaches_a_time_filtered_read(self):
        t = node(memory={**V2, "match": "customer", "at": "created_at", "time_filter": True})
        undated = policy.table_write(t, policy.select(t, {"customer": "acme", "text": "x"}, {"reply": "later"}, run_data={}))
        store.write("g", "triage", undated["subject"], undated["at"], undated["payload"], "t", execution_id="1")
        text = asyncio.run(policy.recall_async({}, t, {"customer": "acme"}, run_data={"created_at": "2026-09-05"},
                                               table=store, graph_id="g"))
        assert "later" not in text


class TestWhichRecordsMustRunInOrder:
    def graph(self, *tasks):
        return {"tasks": list(tasks), "edges": []}

    def test_matched_memory_orders_records_of_the_same_entity(self):
        seq = sequencing.plan(self.graph(node(memory={**V2, "match": "customer"})))
        assert seq["mode"] == "entity" and seq["field"] == "customer"
        assert sequencing.key(seq, {"customer": "acme"}) == "memory:customer=acme"
        assert sequencing.key(seq, {"customer": ""}) is None

    def test_unsplit_memory_orders_the_whole_batch(self):
        seq = sequencing.plan(self.graph(node(memory=dict(V2))))
        assert seq["mode"] == "all"
        assert sequencing.key(seq, {"customer": "acme"}) == sequencing.key(seq, {"customer": "bolt"})

    def test_a_match_on_a_field_only_a_run_produces_orders_everything(self):
        seq = sequencing.plan(self.graph(node(memory={**V2, "match": "category"}, outputs=("reply", "category"))))
        assert seq["mode"] == "all" and "only exists once a record has run" in seq["reason"]

    def test_a_match_on_a_source_output_is_a_record_field(self):
        src = {"name": "loader", "kind": "source", "outputs": [{"name": "account"}]}
        seq = sequencing.plan(self.graph(src, node(memory={**V2, "match": "nodes.loader.outputs.account"})))
        assert seq == {**seq, "mode": "entity", "field": "account"}

    def test_no_reader_or_no_memory_means_no_ordering(self):
        assert sequencing.plan(self.graph(node()))["mode"] is None
        writer_only = node(memory={**V2, "read_enabled": False})
        assert sequencing.plan(self.graph(writer_only))["mode"] is None

    def test_different_match_fields_order_everything(self):
        a = node("a", memory={**V2, "match": "customer"})
        b = node("b", memory={**V2, "match": "text"})
        assert sequencing.plan(self.graph(a, b))["mode"] == "all"

    def test_stepped_samples_and_loader_groups_keep_their_own_order(self):
        from backend.features.execution import batch
        # A trajectory the Input declared (recorded by assign_sequences) wins
        # over memory's ordering; field names in the record alone mean nothing.
        assert batch._group_key({"account": "s", "day": "2026-01-01"},
                                {"trajectory": "s", "sequence": "memory:all"}) == "memory:all"
        assert batch._group_key({"sample_id": "s", "as_of": "2026-01-01"},
                                {"sequence": "memory:all"}) == "memory:all"
        assert batch._group_key({"_dataloader": {"group": 3}}, {"sequence": "memory:all"}) == "memory:all"
        assert batch._group_key({"customer": "a"}, {"sequence": "memory:customer=a"}) == "memory:customer=a"
        assert batch._group_key({"customer": "a"}) is None


class TestAGenericBatchEndToEnd:
    """Six support tickets, two customers, four workers: each ticket must see
    exactly the earlier tickets of its own customer."""

    def test_same_customer_tickets_read_each_other_in_order(self, tmp_path, monkeypatch):
        import time
        from backend.features.execution import batch
        from backend.features.memory import table_store
        monkeypatch.setattr(batch, "BATCHES_DIR", tmp_path / "batches")
        batch._batches.clear(); batch._threads.clear()

        t = node(memory={**V2, "match": "customer", "retrieve": 10})
        graph = {"id": "g-gen", "tasks": [t], "edges": []}
        seen, runs = {}, {}

        def start_run(graph, record, background, gray_zone, run_id, batch_id, session_started_at, **kw):
            rows = [r for r in table_store.rows("g-gen", "triage") if r["subject"] == record["customer"]]
            time.sleep(0.05)                    # overlapping, as model calls would
            seen[record["text"]] = len(rows)
            table_store.write("g-gen", "triage", record["customer"], "", {"outputs": {"reply": record["text"]}},
                              "t", execution_id=run_id)
            runs[run_id] = {"status": "success", "result": {"reply": "ok"}}

        monkeypatch.setattr(batch.runner, "start_run", start_run)
        monkeypatch.setattr(batch.runner, "get_run", lambda rid: runs.get(rid))
        records = [{"customer": c, "text": f"{c}-{i}"} for c in ("acme", "bolt") for i in (1, 2, 3)]
        bid = batch.start_batch(graph, records, {"type": "upload"}, workers=4)
        assert batch.wait_for(bid, timeout=20)
        assert seen == {"acme-1": 0, "acme-2": 1, "acme-3": 2, "bolt-1": 0, "bolt-2": 1, "bolt-3": 2}
        state = batch.get_batch(bid)
        assert state["sequencing"]["mode"] == "entity"
        assert {i["sequence"] for i in state["items"]} == {"memory:customer=acme", "memory:customer=bolt"}
