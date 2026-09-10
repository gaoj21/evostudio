"""What each node keeps in its long-term memory.

Memory used to be one switch that stored everything a node touched. On a node
whose input is a page of text that filled the entry with the page and left no
room for the judgement the node reached — the only part worth recalling. These
cover the selection that replaced it, and the budget that now protects the
short fields from the long ones.
"""

import json

import pytest

from backend.api import memory_policy
def task(**extra):
    base = {
        "name": "judge",
        "description": "Decide whether the news is a concern",
        "use_long_term_memory": True,
        "inputs": [{"name": "company"}, {"name": "news"}],
        "outputs": [{"name": "verdict"}, {"name": "why"}],
    }
    base.update(extra)
    return base


class TestDefaults:
    def test_a_node_configured_before_any_of_this_keeps_everything(self):
        # No settings at all: exactly the behaviour that already shipped.
        settings = memory_policy.policy(task())
        assert settings["outputs"] is None and settings["inputs"] is None
        assert settings["when"] == "success"
        assert settings["retrieve"] == memory_policy.DEFAULT_RETRIEVE

    def test_it_keeps_everything_when_nothing_is_selected_yet(self):
        payload = memory_policy.select(
            task(), {"company": "Gaucho", "news": "x"}, {"verdict": "fine", "why": "w"}
        )
        assert set(payload["inputs"]) == {"company", "news"}
        assert set(payload["outputs"]) == {"verdict", "why"}

    def test_nonsense_settings_fall_back_rather_than_raising(self):
        settings = memory_policy.policy(task(memory={"retrieve": "lots", "when": "maybe"}))
        assert settings["retrieve"] == memory_policy.DEFAULT_RETRIEVE
        assert settings["when"] == "success"

    def test_recall_is_capped(self):
        assert memory_policy.policy(task(memory={"retrieve": 999}))["retrieve"] \
            == memory_policy.MAX_RETRIEVE
        assert memory_policy.policy(task(memory={"retrieve": -5}))["retrieve"] == 0


class TestSelection:
    def test_a_node_keeps_only_the_outputs_it_chose(self):
        payload = memory_policy.select(
            task(memory={"outputs": ["verdict"], "inputs": []}),
            {"company": "Gaucho", "news": "x"}, {"verdict": "fine", "why": "long reason"},
        )
        assert payload["outputs"] == {"verdict": "fine"}
        assert payload["inputs"] == {}

    def test_an_empty_input_list_means_none_not_all(self):
        # The difference that makes the setting worth having: conclusions
        # without the material they were drawn from.
        payload = memory_policy.select(
            task(memory={"inputs": []}), {"company": "Gaucho"}, {"verdict": "fine"}
        )
        assert payload["inputs"] == {}
        assert payload["outputs"] == {"verdict": "fine"}

    def test_a_field_the_run_never_produced_is_skipped_quietly(self):
        payload = memory_policy.select(
            task(memory={"outputs": ["verdict", "why"]}), {}, {"verdict": "fine"}
        )
        assert payload["outputs"] == {"verdict": "fine"}

    def test_a_run_with_none_of_the_chosen_fields_stores_nothing(self):
        # An empty entry would still take up one of the slots retrieved later.
        assert memory_policy.select(
            task(memory={"outputs": ["verdict"], "inputs": []}), {"company": "G"}, {}
        ) is None

    def test_text_stays_text_rather_than_a_quoted_json_string(self):
        payload = memory_policy.select(task(), {"company": "Gaucho"}, {"verdict": "fine"})
        assert payload["inputs"]["company"] == "Gaucho"

    def test_a_structured_value_is_stored_as_json(self):
        payload = memory_policy.select(
            task(), {}, {"verdict": {"action": "suppress", "score": 90}}
        )
        assert json.loads(payload["outputs"]["verdict"])["action"] == "suppress"


class TestBudget:
    def test_a_huge_input_no_longer_crowds_out_a_short_output(self):
        # The whole reason the budget is spent per field: cutting the
        # serialised whole truncated whichever field came last, and that was
        # usually the output.
        payload = memory_policy.select(
            task(), {"news": "x" * 50_000}, {"verdict": "concern", "why": "short reason"}
        )
        assert payload["outputs"] == {"verdict": "concern", "why": "short reason"}
        assert len(payload["inputs"]["news"]) < 4100

    def test_the_entry_stays_within_its_budget(self):
        payload = memory_policy.select(
            task(memory={"limit": 500}),
            {"company": "Gaucho", "news": "x" * 9000}, {"verdict": "fine", "why": "y" * 900},
        )
        total = sum(len(v) for side in ("inputs", "outputs") for v in payload[side].values())
        assert total <= 500

    def test_a_trimmed_field_says_so(self):
        payload = memory_policy.select(task(memory={"limit": 300}), {"news": "x" * 5000}, {})
        assert payload["inputs"]["news"].endswith("…")

    def test_the_budget_is_configurable_within_reason(self):
        assert memory_policy.policy(task(memory={"limit": 10}))["limit"] == 200
        assert memory_policy.policy(task(memory={"limit": 10 ** 9}))["limit"] == 20000

    def test_too_many_fields_for_the_budget_drops_some_whole(self):
        # A handful of meaningless characters from each field helps nobody.
        wide = {f"f{i}": "y" * 200 for i in range(20)}
        t = task(inputs=[{"name": k} for k in wide], memory={"limit": 400})
        payload = memory_policy.select(t, wide, {})
        assert 0 < len(payload["inputs"]) < 20
        assert all(len(v) > 30 for v in payload["inputs"].values())


class TestWhen:
    def test_a_failed_run_is_not_remembered_by_default(self):
        assert memory_policy.select(
            task(), {"company": "G"}, {"verdict": "fine"}, succeeded=False
        ) is None

    def test_a_node_can_ask_to_remember_failures_too(self):
        payload = memory_policy.select(
            task(memory={"when": "always"}), {"company": "G"}, {}, succeeded=False
        )
        assert payload["inputs"] == {"company": "G"}


class TestValidation:
    def test_a_field_the_node_does_not_have_is_rejected(self):
        # Silently storing nothing would look exactly like remembering.
        errors = memory_policy.validate(task(memory={"outputs": ["verdcit"]}))
        assert errors and "verdcit" in errors[0]

    def test_keeping_nothing_while_memory_is_on_is_rejected(self):
        errors = memory_policy.validate(task(memory={"outputs": [], "inputs": []}))
        assert errors and "nothing is selected" in errors[0].lower()

    def test_keeping_nothing_is_fine_when_memory_is_off(self):
        assert memory_policy.validate(
            task(use_long_term_memory=False, memory={"outputs": [], "inputs": []})
        ) == []

    def test_an_unknown_when_is_rejected(self):
        errors = memory_policy.validate(task(memory={"when": "sometimes"}))
        assert errors and "'when'" in errors[0]

    def test_settings_of_the_wrong_shape_are_rejected(self):
        assert memory_policy.validate(task(memory="everything"))

    def test_a_node_with_no_settings_has_nothing_to_complain_about(self):
        assert memory_policy.validate(task()) == []


class TestRender:
    def test_a_stored_entry_reads_as_prose(self):
        content = json.dumps({"task": "judge", "inputs": {"company": "Gaucho"},
                              "outputs": {"verdict": "fine"}})
        assert memory_policy.render(content) == "company: Gaucho → verdict: fine"

    def test_it_unwraps_content_the_store_encoded_a_second_time(self):
        # What comes back is not always what went in; handed over raw it is a
        # wall of escaped quotes that spends the prompt budget on syntax.
        once = json.dumps({"task": "j", "inputs": {"a": "1"}, "outputs": {"b": "2"}})
        assert memory_policy.render(json.dumps(once)) == "a: 1 → b: 2"

    def test_outputs_alone_render_without_a_dangling_arrow(self):
        content = json.dumps({"task": "j", "inputs": {}, "outputs": {"verdict": "fine"}})
        assert memory_policy.render(content) == "verdict: fine"

    def test_something_that_is_not_a_stored_entry_is_passed_through(self):
        assert memory_policy.render("just some text") == "just some text"

    def test_it_respects_the_length_it_is_given(self):
        content = json.dumps({"inputs": {"a": "x" * 900}, "outputs": {}})
        assert len(memory_policy.render(content, limit=100)) == 100


class TestDescribe:
    @pytest.mark.parametrize("memory,expected", [
        (None, "keeps all outputs and all inputs"),
        ({"outputs": ["verdict"], "inputs": []}, "keeps outputs verdict"),
        ({"inputs": ["company"], "outputs": []}, "keeps inputs company"),
        ({"when": "always"}, "keeps all outputs and all inputs, including failed runs"),
    ])
    def test_it_says_what_the_node_keeps(self, memory, expected):
        assert memory_policy.describe(task(memory=memory)) == expected

    def test_memory_that_is_off_says_so(self):
        assert memory_policy.describe(task(use_long_term_memory=False)) == "off"


class TestWhatItReads:
    """The third decision a node makes on its own.

    Whether to remember, what to keep, and — separately — what to read back.
    Reading used to be fixed: its own store, whole entries. A node that decides
    could not draw on what the node that investigated had learned, and a node
    that kept the material it reasoned over had it read back at it every time.
    """

    def test_by_default_a_node_reads_its_own_store(self):
        assert memory_policy.policy(task())["read_from"] is None
        assert memory_policy.stores_read_by(task()) is None

    def test_it_can_name_other_nodes(self):
        chosen = memory_policy.stores_read_by(
            task(memory={"read_from": ["investigate", "judge"]}))
        assert chosen == ["investigate", "judge"]

    def test_it_can_read_nothing(self):
        # Write-only: remember what happened without being told about it again.
        assert memory_policy.stores_read_by(task(memory={"read_from": []})) == []

    def test_by_default_a_recalled_entry_comes_back_whole(self):
        assert memory_policy.policy(task())["read"] is None

    def test_it_can_narrow_what_comes_back(self):
        content = json.dumps({"inputs": {"company": "Gaucho", "news": "long"},
                              "outputs": {"verdict": "fine", "why": "short"}})
        assert memory_policy.render(content, keep=["verdict"]) == "verdict: fine"

    def test_narrowing_spans_both_sides_of_an_entry(self):
        content = json.dumps({"inputs": {"company": "Gaucho"},
                              "outputs": {"verdict": "fine"}})
        assert memory_policy.render(content, keep=["company", "verdict"]) \
            == "company: Gaucho → verdict: fine"

    def test_a_field_the_entry_does_not_have_is_simply_absent(self):
        content = json.dumps({"inputs": {}, "outputs": {"verdict": "fine"}})
        assert memory_policy.render(content, keep=["verdict", "missing"]) == "verdict: fine"


class TestReadValidation:
    def _siblings(self):
        return {
            "judge": task(),
            "investigate": {"name": "investigate",
                            "inputs": [{"name": "company"}],
                            "outputs": [{"name": "context"}]},
        }

    def test_reading_from_a_node_that_is_not_there(self):
        errors = memory_policy.validate(
            task(memory={"read_from": ["ghost"]}), self._siblings())
        assert errors and "not a node in this workflow" in errors[0]

    def test_reading_from_a_real_node_is_fine(self):
        assert memory_policy.validate(
            task(memory={"read_from": ["investigate"]}), self._siblings()) == []

    def test_reading_a_field_that_store_does_not_have(self):
        # Silently reading nothing would look exactly like an empty memory.
        errors = memory_policy.validate(
            task(memory={"read_from": ["investigate"], "read": ["verdict"]}),
            self._siblings())
        assert errors and "does not have" in errors[0]

    def test_reading_a_field_that_store_does_have(self):
        assert memory_policy.validate(
            task(memory={"read_from": ["investigate"], "read": ["context"]}),
            self._siblings()) == []

    def test_its_own_fields_are_checked_when_it_reads_its_own_store(self):
        errors = memory_policy.validate(
            task(memory={"read": ["nonsense"]}), self._siblings())
        assert errors and "nonsense" in errors[0]

    def test_nothing_is_checked_without_the_other_nodes(self):
        # graphs.validate_task_memory always passes them; a caller that cannot
        # must not get a spurious complaint.
        assert memory_policy.validate(task(memory={"read_from": ["ghost"]})) == []


class TestOneSubjectsHistory:
    """What has happened to *this* obligor, as opposed to what looks like it.

    Similarity search answers "when has something like this happened"; a
    monitoring pipeline also has to answer "have we flagged this company
    before", and asking a vector index about Sleep Number returns whatever is
    semantically nearest — which in a real run was three other companies.
    """

    @staticmethod
    def entry(when, company, **outputs):
        return {"timestamp": when,
                "content": json.dumps({"inputs": {"company": company},
                                       "outputs": outputs})}

    def entries(self):
        return [
            self.entry("2026-06-01", "Sleep Number", decision="alert"),
            self.entry("2026-03-01", "Sleep Number", decision="suppress"),
            self.entry("2026-05-01", "Lucid", decision="suppress"),
        ]

    def tracked(self, **extra):
        return task(memory={"match": "company", "retrieve": 5, **extra})

    def test_only_this_subject(self):
        block = memory_policy.history_for(self.entries(), self.tracked(),
                                          {"company": "Sleep Number"})
        assert "Sleep Number" in block
        assert "Lucid" not in block

    def test_oldest_first_because_it_is_a_timeline(self):
        block = memory_policy.history_for(self.entries(), self.tracked(),
                                          {"company": "Sleep Number"})
        assert block.index("suppress") < block.index("alert")

    def test_each_entry_is_dated(self):
        block = memory_policy.history_for(self.entries(), self.tracked(),
                                          {"company": "Sleep Number"})
        assert "2026-03-01" in block and "2026-06-01" in block

    def test_it_says_what_it_is_tracking(self):
        block = memory_policy.history_for(self.entries(), self.tracked(),
                                          {"company": "Sleep Number"})
        assert "company = 'Sleep Number'" in block

    def test_a_subject_with_no_history_gets_nothing(self):
        assert memory_policy.history_for(self.entries(), self.tracked(),
                                         {"company": "Wayfair"}) == ""

    def test_the_cap_keeps_the_most_recent(self):
        block = memory_policy.history_for(self.entries(),
                                          self.tracked(retrieve=1),
                                          {"company": "Sleep Number"})
        assert "alert" in block and "suppress" not in block

    def test_it_matches_an_output_field_too(self):
        # The subject is not always an input: a node that *derives* the obligor
        # can still be tracked by it.
        entries = [{"timestamp": "2026-01-01",
                    "content": json.dumps({"inputs": {}, "outputs": {"obligor": "X"}})}]
        block = memory_policy.history_for(
            entries, task(memory={"match": "obligor", "retrieve": 3}), {"obligor": "X"})
        assert "obligor: X" in block

    def test_nothing_happens_without_a_tracking_field(self):
        assert memory_policy.history_for(self.entries(), task(),
                                         {"company": "Sleep Number"}) == ""

    def test_nothing_happens_when_the_run_has_no_value_for_it(self):
        assert memory_policy.history_for(self.entries(), self.tracked(),
                                         {"company": ""}) == ""

    def test_a_node_that_reads_nothing_gets_no_history_either(self):
        assert memory_policy.history_for(self.entries(),
                                         self.tracked(retrieve=0),
                                         {"company": "Sleep Number"}) == ""

    def test_the_read_selection_applies(self):
        block = memory_policy.history_for(self.entries(),
                                          self.tracked(read=["decision"]),
                                          {"company": "Sleep Number"})
        assert "decision: alert" in block and "company:" not in block

    def test_an_unreadable_entry_is_skipped_not_fatal(self):
        entries = [{"timestamp": "2026-01-01", "content": "not json"},
                   self.entry("2026-02-01", "Sleep Number", decision="alert")]
        block = memory_policy.history_for(entries, self.tracked(),
                                          {"company": "Sleep Number"})
        assert "alert" in block


class TestWhatTheEntryIsAbout:
    """An entry has to say what period it covers, not when it was written.

    Every record of one batch is written within the same minute, so the write
    time orders a timeline by nothing. The period is in the run's data — but on
    the source node, not on the node doing the deciding, and declaring it as an
    input would force it into that node's prompt.
    """

    @staticmethod
    def deciding(**memory):
        return {"name": "decide",
                "inputs": [{"name": "company"}], "outputs": [{"name": "decision"}],
                "use_long_term_memory": True, "memory": memory}

    RUN = {"company": "Sleep Number", "decision": "alert",
           "window_start": "2025-12-14", "window_end": "2026-06-11",
           "news_batch": "x" * 5000}

    def test_it_records_a_field_the_node_never_took(self):
        payload = memory_policy.select(
            self.deciding(context=["window_start", "window_end"]),
            {"company": "Sleep Number"}, {"decision": "alert"}, run_data=self.RUN)
        assert payload["inputs"]["window_end"] == "2026-06-11"

    def test_only_the_fields_it_asked_for(self):
        payload = memory_policy.select(
            self.deciding(context=["window_end"]),
            {"company": "Sleep Number"}, {"decision": "alert"}, run_data=self.RUN)
        # Not the whole run: news_batch is 5000 characters of source material.
        assert "news_batch" not in payload["inputs"]

    def test_it_survives_a_narrowed_input_selection(self):
        payload = memory_policy.select(
            self.deciding(inputs=["company"], context=["window_end"]),
            {"company": "Sleep Number"}, {"decision": "alert"}, run_data=self.RUN)
        assert set(payload["inputs"]) == {"company", "window_end"}

    def test_a_field_missing_from_the_run_is_simply_absent(self):
        payload = memory_policy.select(
            self.deciding(context=["event_date"]),
            {"company": "Sleep Number"}, {"decision": "alert"}, run_data=self.RUN)
        assert "event_date" not in payload["inputs"]

    def test_without_it_nothing_changes(self):
        payload = memory_policy.select(
            self.deciding(), {"company": "Sleep Number"}, {"decision": "alert"},
            run_data=self.RUN)
        assert set(payload["inputs"]) == {"company"}


class TestDatingTheTimeline:
    @staticmethod
    def entry(window_end, decision, written):
        return {"timestamp": written, "content": json.dumps(
            {"inputs": {"company": "S", "window_end": window_end},
             "outputs": {"decision": decision}})}

    def entries(self):
        # Written seconds apart, covering periods months apart — a batch.
        return [self.entry("2026-09-11", "suppress", "2026-09-06 23:41:20"),
                self.entry("2026-06-11", "alert", "2026-09-06 23:41:26"),
                self.entry("2025-12-01", "suppress", "2026-09-06 23:41:24")]

    def tracked(self, **extra):
        return task(memory={"match": "company", "retrieve": 5, **extra})

    def test_ordered_by_the_period_it_covers(self):
        block = memory_policy.history_for(
            self.entries(), self.tracked(at="window_end", read=["window_end"]),
            {"company": "S"})
        assert block.index("2025-12-01") < block.index("2026-06-11") \
            < block.index("2026-09-11")

    def test_the_write_time_would_order_it_differently(self):
        # The whole reason `at` exists: this is the order without it.
        block = memory_policy.history_for(self.entries(), self.tracked(),
                                          {"company": "S"})
        assert block.index("23:41:20") < block.index("23:41:24")

    def test_it_says_what_it_is_dated_by(self):
        block = memory_policy.history_for(
            self.entries(), self.tracked(at="window_end"), {"company": "S"})
        assert "oldest first by window_end" in block

    def test_an_entry_with_no_such_field_falls_back_to_the_write_time(self):
        entries = [{"timestamp": "2026-01-01", "content": json.dumps(
            {"inputs": {"company": "S"}, "outputs": {"decision": "alert"}})}]
        block = memory_policy.history_for(
            entries, self.tracked(at="window_end"), {"company": "S"})
        assert "2026-01-01" in block

    def test_the_cap_keeps_the_latest_period_not_the_latest_write(self):
        block = memory_policy.history_for(
            self.entries(), self.tracked(at="window_end", retrieve=1,
                                         read=["window_end"]),
            {"company": "S"})
        assert "2026-09-11" in block and "2025-12-01" not in block


class TestValidatingWhatIsRecorded:
    def _siblings(self):
        return {
            "feed": {"name": "feed", "outputs": [{"name": "company"},
                                                 {"name": "window_end"}]},
            "decide": {"name": "decide", "inputs": [{"name": "company"}],
                       "outputs": [{"name": "decision"}]},
        }

    def test_recording_something_no_node_produces(self):
        errors = memory_policy.validate(
            {"name": "decide", "inputs": [{"name": "company"}],
             "outputs": [{"name": "decision"}], "use_long_term_memory": True,
             "memory": {"context": ["window_ends"]}}, self._siblings())
        assert errors and "no node in this workflow produces" in errors[0]

    def test_dating_by_something_the_entry_will_not_carry(self):
        # It would silently fall back to the write time, which is the thing
        # this setting exists to avoid.
        errors = memory_policy.validate(
            {"name": "decide", "inputs": [{"name": "company"}],
             "outputs": [{"name": "decision"}], "use_long_term_memory": True,
             "memory": {"at": "window_end"}}, self._siblings())
        assert errors and "neither takes nor records" in errors[0]

    def test_recording_it_makes_dating_by_it_valid(self):
        assert memory_policy.validate(
            {"name": "decide", "inputs": [{"name": "company"}],
             "outputs": [{"name": "decision"}], "use_long_term_memory": True,
             "memory": {"context": ["window_end"], "at": "window_end"}},
            self._siblings()) == []


class TestRecallCannotSeeTheFuture:
    """Walking a window month by month is only worth doing if December
    cannot see May.

    Recall matched on the obligor and sorted by date, but never compared
    those dates against the run being processed. A stepped backtest therefore
    handed each early step the verdict reached at the end of the window — the
    answer it was supposed to predict — under a header saying "before".
    """

    task = {
        "name": "decide",
        "memory": {"match": "company", "at": "as_of", "retrieve": 10,
                   "outputs": ["decision"], "inputs": ["company", "as_of"]},
    }

    @staticmethod
    def entry(as_of, decision, company="Bitcoin Depot"):
        return {
            "content": json.dumps({
                "task": "decide",
                "inputs": {"company": company, "as_of": as_of},
                "outputs": {"decision": decision},
            }),
            "timestamp": "2026-09-07 01:00:00",
        }

    def history(self, entries, as_of, company="Bitcoin Depot"):
        return memory_policy.history_for(
            entries, self.task, {"company": company, "as_of": as_of})

    def test_a_later_verdict_never_reaches_an_earlier_run(self):
        entries = [self.entry("2025-12-18", "suppress"),
                   self.entry("2026-05-17", "alert GOING CONCERN")]
        block = self.history(entries, "2025-12-18")

        assert "GOING CONCERN" not in block
        assert "2026-05-17" not in block

    def test_what_did_happen_first_still_arrives(self):
        entries = [self.entry("2025-08-01", "suppress low"),
                   self.entry("2026-05-17", "alert")]
        block = self.history(entries, "2025-12-18")

        assert "suppress low" in block
        assert "alert" not in block

    def test_the_run_does_not_read_its_own_previous_answer(self):
        # Re-running one date must behave like running it the first time,
        # or a second pass just agrees with itself.
        entries = [self.entry("2025-12-18", "suppress")]
        assert self.history(entries, "2025-12-18") == ""

    def test_an_undated_entry_cannot_be_placed_and_is_left_out(self):
        # Nothing says whether it precedes the run; admitting it is exactly
        # how a later conclusion gets in.
        entries = [self.entry(None, "alert from an older schema")]
        assert self.history(entries, "2025-12-18") == ""

    def test_the_header_says_which_date_it_stops_at(self):
        block = self.history([self.entry("2025-08-01", "suppress")], "2025-12-18")
        assert "before 2025-12-18" in block

    def test_an_undated_run_still_sees_everything(self):
        # A node with no `at` field is not a time series, and filtering it
        # would silently empty the memory of every workflow set up before
        # dating existed.
        plain = {"name": "decide", "memory": {"match": "company", "retrieve": 10}}
        entries = [self.entry("2025-12-18", "suppress"), self.entry("2026-05-17", "alert")]
        block = memory_policy.history_for(entries, plain, {"company": "Bitcoin Depot"})

        assert "suppress" in block and "alert" in block

    def test_another_obligors_past_is_still_not_mixed_in(self):
        entries = [self.entry("2025-08-01", "alert", company="Wayfair")]
        assert self.history(entries, "2025-12-18") == ""


class TestTheCutoffSurvivesTheRealWiring:
    """The filter is only as good as the date reaching it.

    `as_of` is a *context* field: the node is dated by it but never takes it
    as an input, so the inputs handed to recall at run time do not contain it.
    The cutoff was therefore computed from a blank and filtered nothing —
    correct in isolation, inert in the pipeline.
    """

    task = {
        "name": "decide",
        "inputs": [{"name": "company"}, {"name": "detection"}],
        "memory": {"match": "company", "at": "as_of", "retrieve": 10,
                   "context": ["as_of"], "inputs": ["company"]},
    }

    @staticmethod
    def entry(as_of, decision):
        return {"content": json.dumps({
            "inputs": {"company": "Lucid", "as_of": as_of},
            "outputs": {"decision": decision}}), "timestamp": "t"}

    def test_context_fields_reach_the_cutoff(self):
        # What the node is actually handed: no as_of anywhere in it.
        node_inputs = {"company": "Lucid", "detection": "..."}
        run_data = {"company": "Lucid", "as_of": "2025-11-17",
                    "window_end": "2026-04-16"}

        resolved = memory_policy.with_context(self.task, node_inputs, run_data)
        assert resolved["as_of"] == "2025-11-17"

        block = memory_policy.history_for(
            [self.entry("2026-04-16", "suppress LATER")], self.task, resolved)
        assert block == ""

    def test_without_the_run_data_there_is_no_date_to_filter_on(self):
        # Pinning the failure mode itself: this is what shipped.
        node_inputs = {"company": "Lucid", "detection": "..."}
        bare = memory_policy.with_context(self.task, node_inputs, None)

        assert "as_of" not in bare
        leaked = memory_policy.history_for(
            [self.entry("2026-04-16", "suppress LATER")], self.task, bare)
        assert "LATER" in leaked      # no cutoff is available, so nothing is held back

    def test_the_write_side_resolves_context_the_same_way(self):
        # If the two disagreed, entries would be dated by one rule and
        # filtered by another.
        payload = memory_policy.select(
            self.task, {"company": "Lucid"}, {"decision": "alert"},
            run_data={"as_of": "2025-11-17"})
        assert payload["inputs"]["as_of"] == "2025-11-17"


class TestOneEntryPerSubject:
    """A company's memory is its history, not a pile of days.

    Memory stored one entry per run, so an obligor watched over six months was
    six unrelated files and "what has this company done over time" had to be
    reassembled by reading all of them. The stored unit is now the subject,
    and a run is a dated state folded into it.
    """

    task = {
        "name": "decide",
        "memory": {"match": "company", "at": "as_of", "retrieve": 10,
                   "context": ["as_of"], "inputs": ["company"]},
    }

    def point(self, as_of, verdict, company="Sleep Number"):
        return memory_policy.as_timeline(self.task, {
            "task": "decide",
            "inputs": {"company": company, "as_of": as_of},
            "outputs": {"decision": verdict}})

    def test_a_run_becomes_a_dated_state_under_its_subject(self):
        entry = self.point("2026-01-12", "suppress")

        assert entry["subject"] == {"company": "Sleep Number"}
        assert entry["timeline"] == [{
            "at": "2026-01-12",
            "inputs": {"company": "Sleep Number", "as_of": "2026-01-12"},
            "outputs": {"decision": "suppress"}}]

    def test_a_second_run_extends_the_same_entry(self):
        merged = memory_policy.merge_timeline(
            [self.point("2026-01-12", "suppress")],
            self.point("2026-06-11", "alert"), "as_of")

        assert [p["at"] for p in merged["timeline"]] == ["2026-01-12", "2026-06-11"]

    def test_the_timeline_is_in_time_order_not_write_order(self):
        # Batches run their records in parallel, so the later date is often
        # written first.
        merged = memory_policy.merge_timeline(
            [self.point("2026-06-11", "alert")],
            self.point("2026-01-12", "suppress"), "as_of")

        assert [p["at"] for p in merged["timeline"]] == ["2026-01-12", "2026-06-11"]

    def test_re_running_a_date_replaces_it(self):
        merged = memory_policy.merge_timeline(
            [self.point("2026-01-12", "suppress")],
            self.point("2026-01-12", "alert"), "as_of")

        assert len(merged["timeline"]) == 1
        assert merged["timeline"][0]["outputs"]["decision"] == "alert"

    def test_entries_written_before_this_are_absorbed(self):
        # The old shape: one run, flat, no timeline. It has to become part of
        # the subject's history rather than being stranded beside it.
        old = {"task": "decide",
               "inputs": {"company": "Sleep Number", "as_of": "2025-11-01"},
               "outputs": {"decision": "an older look"}}
        merged = memory_policy.merge_timeline(
            [old], self.point("2026-01-12", "suppress"), "as_of")

        assert [p["at"] for p in merged["timeline"]] == ["2025-11-01", "2026-01-12"]
        assert "an older look" in str(merged["timeline"][0])

    def test_a_long_history_keeps_its_most_recent_states(self):
        many = [self.point(f"2026-{m:02d}-01", f"v{m}") for m in range(1, 13)]
        merged = memory_policy.merge_timeline(many[:-1], many[-1], "as_of", limit=4)

        assert [p["at"] for p in merged["timeline"]] == [
            "2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01"]

    def test_a_node_with_no_subject_stores_runs_as_before(self):
        plain = {"name": "summarise", "memory": {"at": "as_of"}}
        assert memory_policy.subject_of(plain, {"inputs": {"company": "X"}}) is None

    def test_recall_reads_a_timeline_entry(self):
        entry = {"content": json.dumps(memory_policy.merge_timeline(
            [self.point("2026-01-12", "suppress")],
            self.point("2026-06-11", "alert"), "as_of")), "timestamp": "t"}
        block = memory_policy.history_for(
            [entry], self.task,
            {"company": "Sleep Number", "as_of": "2026-12-01"})

        assert "suppress" in block and "alert" in block
        assert block.index("suppress") < block.index("alert")

    def test_the_cutoff_still_applies_inside_one_entry(self):
        # The whole history living in one entry must not smuggle the future in
        # with it.
        entry = {"content": json.dumps(memory_policy.merge_timeline(
            [self.point("2026-01-12", "suppress")],
            self.point("2026-06-11", "alert LATER"), "as_of")), "timestamp": "t"}
        block = memory_policy.history_for(
            [entry], self.task, {"company": "Sleep Number", "as_of": "2026-03-01"})

        assert "suppress" in block
        assert "LATER" not in block

    def test_a_duplicate_entry_is_not_read_out_twice(self):
        # A delete that failed leaves the old entry beside the merged one.
        merged = memory_policy.merge_timeline(
            [self.point("2026-01-12", "suppress")],
            self.point("2026-06-11", "alert"), "as_of")
        entries = [{"content": json.dumps(merged), "timestamp": "t"},
                   {"content": json.dumps(self.point("2026-01-12", "suppress")),
                    "timestamp": "t"}]
        block = memory_policy.history_for(
            entries, self.task, {"company": "Sleep Number", "as_of": "2026-12-01"})

        assert block.count("suppress") == 1

    def test_another_companys_timeline_is_not_mixed_in(self):
        entry = {"content": json.dumps(self.point("2026-01-12", "alert", "Wayfair")),
                 "timestamp": "t"}
        assert memory_policy.history_for(
            [entry], self.task,
            {"company": "Sleep Number", "as_of": "2026-12-01"}) == ""



class TestAContextFieldCanReachIntoAnOutput:
    """`detection.source`: which source a detection came from is a key inside
    the node's JSON answer, not a field of its own. A dotted context name
    reaches in, so the memory row can record it — and later be read by it."""

    task = {"name": "decide",
            "memory": {"match": "company", "at": "as_of",
                       "context": ["as_of", "detection.source"], "inputs": ["company"]}}

    def test_a_key_inside_a_json_output_is_recorded(self):
        run_data = {"company": "Acme", "as_of": "2026-01-01",
                    "detection": json.dumps({"topic": "debt_default", "source": "8-K"})}
        got = memory_policy.with_context(self.task, {"company": "Acme"}, run_data)
        assert got["detection.source"] == "8-K"
        assert got["as_of"] == "2026-01-01"

    def test_it_is_written_into_the_row_and_read_back(self):
        run_data = {"company": "Acme", "as_of": "2026-01-01",
                    "detection": json.dumps({"source": "news"})}
        payload = memory_policy.select(self.task, {"company": "Acme"},
                                       {"decision": "alert"}, run_data=run_data)
        assert payload["inputs"]["detection.source"] == "news"
        row = memory_policy.table_write(self.task, payload)
        assert row["payload"]["inputs"]["detection.source"] == "news"

    def test_a_missing_key_is_absent_not_invented(self):
        run_data = {"detection": json.dumps({"topic": "none"})}
        assert "detection.source" not in memory_policy.with_context(self.task, {}, run_data)

    def test_an_output_that_is_not_json_is_left_alone(self):
        assert "detection.source" not in memory_policy.with_context(
            self.task, {}, {"detection": "not json at all"})

    def test_validation_asks_only_that_the_field_exist(self):
        siblings = {"feed": {"name": "feed", "outputs": [{"name": "as_of"}]},
                    "detect": {"name": "detect", "outputs": [{"name": "detection"}]},
                    "decide": {**self.task, "inputs": [{"name": "company"}], "outputs": []}}
        assert memory_policy.validate(siblings["decide"], siblings) == []
        bad = {**self.task, "memory": {**self.task["memory"], "context": ["nothing.source"]}}
        assert any("nothing" in e for e in memory_policy.validate(bad, siblings))


def test_read_only_can_keep_no_fields_and_never_selects_a_write():
    node = task(memory={"write_enabled": False, "outputs": [], "inputs": []})
    assert memory_policy.validate(node) == []
    assert memory_policy.select(node, {"company": "A"}, {"verdict": "yes"}) is None


@pytest.mark.parametrize('settings', [{"read_from": []}, {"read_enabled": False}])
def test_table_recall_respects_empty_sources_and_disabled_reads(settings):
    import asyncio
    from unittest.mock import Mock
    store = Mock()
    node = task(memory={"match": "company", **settings})
    assert asyncio.run(memory_policy.recall_async({}, node, {"company": "A"}, table=store)) == ""
    store.before.assert_not_called()
