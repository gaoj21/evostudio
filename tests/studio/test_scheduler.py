"""Running a workflow on a timer.

Distinct from a watcher, which polls a source and runs only when new data turns
up: a schedule is about the clock. It also has to survive a restart — "every
day" that stops when the process does is not a schedule — and it spends money
with nobody watching, which is what the interval floor is for.
"""

from datetime import datetime, timedelta

import pytest


@pytest.fixture
def sched(tmp_path, monkeypatch):
    from studio.backend import scheduler
    monkeypatch.setattr(scheduler, "SCHEDULES_DIR", tmp_path / "schedules")
    scheduler._threads.clear()
    # Nothing in these tests should start a real thread.
    monkeypatch.setattr(scheduler, "_start_thread", lambda graph, schedule: None)
    return scheduler


def at(text):
    return datetime.fromisoformat(text)


class TestWhenItIsDue:
    def test_daily_later_today(self, sched):
        assert sched.next_fire({"mode": "daily", "time": "18:30"},
                               at("2026-09-06T15:00:00+00:00")) \
            == at("2026-09-06T18:30:00+00:00")

    def test_daily_already_past_means_tomorrow(self, sched):
        assert sched.next_fire({"mode": "daily", "time": "09:00"},
                               at("2026-09-06T15:00:00+00:00")) \
            == at("2026-09-07T09:00:00+00:00")

    def test_daily_exactly_now_means_tomorrow(self, sched):
        # Otherwise a fire at 09:00:00 would schedule itself for 09:00:00 and
        # spin.
        assert sched.next_fire({"mode": "daily", "time": "09:00"},
                               at("2026-09-06T09:00:00+00:00")) \
            == at("2026-09-07T09:00:00+00:00")

    def test_interval_counts_from_now(self, sched):
        assert sched.next_fire({"mode": "interval", "interval_minutes": 90},
                               at("2026-09-06T15:00:00+00:00")) \
            == at("2026-09-06T16:30:00+00:00")

    def test_an_interval_below_the_floor_is_lifted_to_it(self, sched):
        due = sched.next_fire({"mode": "interval", "interval_minutes": 1},
                              at("2026-09-06T15:00:00+00:00"))
        assert due == at("2026-09-06T15:00:00+00:00") + timedelta(
            minutes=sched.MIN_INTERVAL_MINUTES)


class TestWhatItAccepts:
    def test_a_daily_schedule(self, sched):
        assert sched.validate({"mode": "daily", "time": "9:5"})["time"] == "09:05"

    def test_an_unknown_frequency(self, sched):
        with pytest.raises(sched.ScheduleError) as raised:
            sched.validate({"mode": "weekly"})
        assert "daily" in str(raised.value)

    def test_a_time_that_is_not_a_time(self, sched):
        with pytest.raises(sched.ScheduleError):
            sched.validate({"mode": "daily", "time": "25:00"})
        with pytest.raises(sched.ScheduleError):
            sched.validate({"mode": "daily", "time": "morning"})

    def test_too_frequent_is_refused_with_the_reason(self, sched):
        # A typo of 1 instead of 60 should not be able to empty an account.
        with pytest.raises(sched.ScheduleError) as raised:
            sched.validate({"mode": "interval", "interval_minutes": 1})
        assert "unattended" in str(raised.value)

    def test_inputs_and_session_come_through(self, sched):
        parsed = sched.validate({"mode": "daily", "inputs": {"city": "Lima"},
                                 "session": " daily-report "})
        assert parsed["inputs"] == {"city": "Lima"}
        assert parsed["session"] == "daily-report"

    def test_a_blank_session_is_no_session(self, sched):
        assert sched.validate({"mode": "daily", "session": "   "})["session"] is None


class TestSettingOne:
    def test_it_is_stored_and_reported(self, sched):
        out = sched.set_schedule({"id": "g1"}, {"mode": "daily", "time": "09:00"})

        assert out["scheduled"] is True
        assert out["next_fire"]
        assert sched.load("g1")["time"] == "09:00"

    def test_it_is_not_written_into_the_graph(self, sched):
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        # Exporting a schedule onto someone else's machine would start firing
        # runs they never asked for.
        assert sched._path("g1").is_file()

    def test_replacing_one_keeps_its_history(self, sched):
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        stored = sched.load("g1")
        stored["fires"] = 7
        sched._save("g1", stored)

        out = sched.set_schedule({"id": "g1"}, {"mode": "interval",
                                                "interval_minutes": 30})
        assert out["fires"] == 7
        assert out["mode"] == "interval"

    def test_pausing_leaves_it_in_place_with_no_next_fire(self, sched):
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        out = sched.set_schedule({"id": "g1"}, {"mode": "daily", "enabled": False})

        assert out["scheduled"] is True and out["enabled"] is False
        assert out["next_fire"] is None

    def test_removing_it(self, sched):
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        assert sched.clear("g1") is True
        assert sched.load("g1") is None
        assert sched.status("g1") == {"graph_id": "g1", "scheduled": False}

    def test_a_workflow_with_no_schedule(self, sched):
        assert sched.status("never") == {"graph_id": "never", "scheduled": False}


class TestFiring:
    @pytest.fixture
    def fired(self, sched, monkeypatch):
        from studio.backend import graphs as graph_store
        started = []

        def fake_start_run(graph, inputs, background=True, session=None, **kw):
            started.append({"graph": graph.get("id"), "inputs": inputs,
                            "session": session})
            return f"run-{len(started)}"

        monkeypatch.setattr(sched.runner, "start_run", fake_start_run)
        monkeypatch.setattr(graph_store, "load_graph",
                            lambda gid: {"id": gid, "tasks": []})
        return sched, started

    def test_a_fire_starts_an_ordinary_run(self, fired):
        sched, started = fired
        sched.set_schedule({"id": "g1"}, {"mode": "daily", "inputs": {"city": "Lima"},
                                          "session": "daily"})
        sched._fire("g1", sched.load("g1"))

        assert started == [{"graph": "g1", "inputs": {"city": "Lima"},
                            "session": "daily"}]

    def test_it_records_what_happened(self, fired):
        sched, _ = fired
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        sched._fire("g1", sched.load("g1"))
        stored = sched.load("g1")

        assert stored["fires"] == 1
        assert stored["last_run_id"] == "run-1"
        assert stored["last_fire"] and stored["last_error"] is None

    def test_it_schedules_the_next_one(self, fired):
        sched, _ = fired
        sched.set_schedule({"id": "g1"}, {"mode": "interval", "interval_minutes": 30})
        sched._fire("g1", sched.load("g1"))

        due = datetime.fromisoformat(sched.load("g1")["next_fire"])
        assert due > sched._now()

    def test_a_fire_that_cannot_start_does_not_kill_the_schedule(self, sched,
                                                                 monkeypatch):
        from studio.backend import graphs as graph_store
        monkeypatch.setattr(graph_store, "load_graph", lambda gid: {"id": gid})
        monkeypatch.setattr(sched.runner, "start_run",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no LLM")))
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        sched._fire("g1", sched.load("g1"))
        stored = sched.load("g1")

        # The next one may well work, and the reason is kept where it shows.
        assert "no LLM" in stored["last_error"]
        assert stored["next_fire"]

    def test_a_deleted_workflow_is_reported_not_run(self, sched, monkeypatch):
        from studio.backend import graphs as graph_store
        monkeypatch.setattr(graph_store, "load_graph", lambda gid: None)
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        sched._fire("g1", sched.load("g1"))

        assert "no longer exists" in sched.load("g1")["last_error"]


class TestSurvivingARestart:
    def test_enabled_schedules_start_again(self, sched, monkeypatch):
        from studio.backend import graphs as graph_store
        started = []
        monkeypatch.setattr(sched, "_start_thread",
                            lambda graph, schedule: started.append(graph["id"]))
        monkeypatch.setattr(graph_store, "load_graph", lambda gid: {"id": gid})
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        sched.set_schedule({"id": "g2"}, {"mode": "daily"})
        started.clear()

        assert sorted(sched.restore()) == ["g1", "g2"]
        assert sorted(started) == ["g1", "g2"]

    def test_a_paused_schedule_stays_paused(self, sched, monkeypatch):
        from studio.backend import graphs as graph_store
        monkeypatch.setattr(graph_store, "load_graph", lambda gid: {"id": gid})
        sched.set_schedule({"id": "g1"}, {"mode": "daily", "enabled": False})
        assert sched.restore() == []

    def test_a_schedule_for_a_workflow_that_is_gone_is_skipped(self, sched,
                                                               monkeypatch):
        from studio.backend import graphs as graph_store
        monkeypatch.setattr(graph_store, "load_graph", lambda gid: None)
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        assert sched.restore() == []

    def test_a_fire_missed_while_the_process_was_down_is_already_due(self, sched):
        sched.set_schedule({"id": "g1"}, {"mode": "daily"})
        stored = sched.load("g1")
        stored["next_fire"] = (sched._now() - timedelta(hours=3)).isoformat()
        sched._save("g1", stored)

        # The loop waits until `next_fire`, which is in the past — so it fires
        # at once rather than skipping the day.
        assert datetime.fromisoformat(sched.load("g1")["next_fire"]) < sched._now()


class TestFollowingARename:
    def test_the_schedule_moves_with_the_workflow(self, sched):
        sched.set_schedule({"id": "old"}, {"mode": "daily", "time": "07:00"})
        sched.rename("old", "new")

        assert sched.load("old") is None
        assert sched.load("new")["time"] == "07:00"

    def test_renaming_a_workflow_with_no_schedule_does_nothing(self, sched):
        sched.rename("old", "new")
        assert sched.load("new") is None


class TestThroughTheApi:
    @pytest.fixture
    def client(self, sched, studio_data):
        from fastapi.testclient import TestClient

        from studio.backend import app as studio_app
        from studio.backend import graphs
        graphs.create_graph("Probe", "")
        return TestClient(studio_app.app)

    def test_setting_and_reading_one(self, client):
        res = client.put("/api/graphs/probe/schedule",
                         json={"mode": "daily", "time": "07:30"})
        assert res.status_code == 200
        assert res.json()["time"] == "07:30"
        assert client.get("/api/graphs/probe/schedule").json()["scheduled"] is True

    def test_a_workflow_that_does_not_exist(self, client):
        assert client.put("/api/graphs/ghost/schedule",
                          json={"mode": "daily"}).status_code == 404

    def test_a_refused_schedule_says_why(self, client):
        res = client.put("/api/graphs/probe/schedule",
                         json={"mode": "interval", "interval_minutes": 1})
        assert res.status_code == 422
        assert "at least" in res.json()["detail"]

    def test_removing_one(self, client):
        client.put("/api/graphs/probe/schedule", json={"mode": "daily"})
        assert client.delete("/api/graphs/probe/schedule").json()["removed"] is True
        assert client.get("/api/graphs/probe/schedule").json()["scheduled"] is False
