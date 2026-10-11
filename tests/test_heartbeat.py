"""Scheduler heartbeat (2026-10-11): a long pipeline run must not look like a
dead scheduler to /health, and a dead scheduler must still be caught."""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import heartbeat as hb  # noqa: E402

NOW = datetime(2026, 10, 10, 11, 30, tzinfo=timezone.utc)
iso = lambda dt: dt.isoformat()


def test_assess_idle_and_dead():
    assert hb.assess({}, NOW) == []                                  # first boot
    assert hb.assess({"beat": "garbage"}, NOW) == []                 # unparseable
    assert hb.assess({"beat": iso(NOW - timedelta(minutes=10))}, NOW) == []
    assert hb.assess({"beat": iso(NOW - timedelta(minutes=50))}, NOW) == ["scheduler_heartbeat_stale"]
    # naive timestamps are treated as UTC (the old writer used utcnow().isoformat())
    assert hb.assess({"beat": (NOW - timedelta(minutes=50)).replace(tzinfo=None).isoformat()}, NOW) == ["scheduler_heartbeat_stale"]


def test_assess_long_run_is_not_dead():
    # the 2026-10-10 case: beat at 10:00, run started 10:00, now 11:30
    doc = {"beat": iso(NOW - timedelta(minutes=90)), "run_in_progress": True,
           "run_started": iso(NOW - timedelta(minutes=90))}
    assert hb.assess(doc, NOW) == []
    # a run that has been "in progress" for longer than the grace window IS a problem
    doc_old = {"beat": iso(NOW - timedelta(hours=5)), "run_in_progress": True,
               "run_started": iso(NOW - timedelta(hours=5))}
    assert hb.assess(doc_old, NOW) == ["scheduler_heartbeat_stale"]
    # run flag without a start time gets no leniency
    assert hb.assess({"beat": iso(NOW - timedelta(minutes=90)), "run_in_progress": True}, NOW) == ["scheduler_heartbeat_stale"]
    # idle flag false with a stale beat: dead
    assert hb.assess({"beat": iso(NOW - timedelta(minutes=90)), "run_in_progress": False,
                      "run_started": None}, NOW) == ["scheduler_heartbeat_stale"]


def test_beat_writes_run_state_and_throttles():
    with tempfile.TemporaryDirectory() as tmp:
        old_file = hb.HEARTBEAT_FILE
        hb.HEARTBEAT_FILE = str(Path(tmp) / "data" / "scheduler_heartbeat.json")
        hb._state.update({"run_started": None, "stage": "idle", "last_write": 0.0, "next_fire": None})
        try:
            assert hb.beat("idle", next_fire=NOW, force=True)
            d = json.load(open(hb.HEARTBEAT_FILE, encoding="utf-8"))
            assert d["run_in_progress"] is False and d["run_started"] is None
            assert d["next_fire"].startswith("2026-10-10T11:30")
            # throttled: a second un-forced beat right away does not write
            assert hb.beat("still idle") is False
            hb.start_run("pipeline")
            d = json.load(open(hb.HEARTBEAT_FILE, encoding="utf-8"))
            assert d["run_in_progress"] is True and d["run_started"] and d["stage"] == "pipeline"
            assert d["next_fire"] is None
            hb.beat("step 2/3 fetch", force=True)
            assert json.load(open(hb.HEARTBEAT_FILE, encoding="utf-8"))["stage"] == "step 2/3 fetch"
            hb.end_run()
            d = json.load(open(hb.HEARTBEAT_FILE, encoding="utf-8"))
            assert d["run_in_progress"] is False and d["stage"] == "idle"
            # a run document written now, assessed 90 min later with no beats since: lenient
            hb.start_run("pipeline")
            d = json.load(open(hb.HEARTBEAT_FILE, encoding="utf-8"))
            later = datetime.fromisoformat(d["beat"]) + timedelta(minutes=90)
            assert hb.assess(d, later) == []
            hb.end_run()
        finally:
            hb.HEARTBEAT_FILE = old_file
            hb._state.update({"run_started": None, "stage": "idle", "last_write": 0.0, "next_fire": None})


if __name__ == "__main__":
    test_assess_idle_and_dead(); test_assess_long_run_is_not_dead(); test_beat_writes_run_state_and_throttles(); print("PASS")
