"""Form opt-outs tombstone the subscription (2026-09-30): an enrolled address
that clicked the digest's opt-out link gets cadence "off" so it shows in the
dashboard's Opt-Outs panel. Offline: the subscription store is redirected to
a temp file."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("SENDGRID_API_KEY", "x")

import subscriptions  # noqa: E402
import main  # noqa: E402


def test_form_optout_tombstones_active_subscription():
    with tempfile.TemporaryDirectory() as tmp:
        store = Path(tmp) / "faculty_subscriptions.json"
        old = subscriptions.FACULTY_SUBS_FILE
        subscriptions.FACULTY_SUBS_FILE = store
        try:
            subscriptions.upsert_faculty_sub("weekly@som.umaryland.edu", name="Wk", cadence="weekly")
            subscriptions.upsert_faculty_sub("daily@som.umaryland.edu", name="Dy", cadence="daily")
            subscriptions.upsert_faculty_sub("stay@som.umaryland.edu", name="St", cadence="weekly")
            subscriptions.upsert_faculty_sub("gone@som.umaryland.edu", name="Go", cadence="off")

            done = main._tombstone_form_optouts({
                "weekly@som.umaryland.edu", "daily@som.umaryland.edu",
                "gone@som.umaryland.edu", "never-enrolled@som.umaryland.edu"})
            assert done == ["daily@som.umaryland.edu", "weekly@som.umaryland.edu"]

            subs = subscriptions.load_faculty_subs()
            assert subs["weekly@som.umaryland.edu"]["cadence"] == "off"
            assert subs["daily@som.umaryland.edu"]["cadence"] == "off"
            assert subs["stay@som.umaryland.edu"]["cadence"] == "weekly"
            assert subs["weekly@som.umaryland.edu"]["name"] == "Wk", "record kept, not deleted"
            assert "never-enrolled@som.umaryland.edu" not in subs, "no record is invented"
            assert subscriptions.faculty_subs_for_cadence("weekly").keys() == {"stay@som.umaryland.edu"}

            # idempotent: a second run finds nothing active to tombstone
            assert main._tombstone_form_optouts({"weekly@som.umaryland.edu"}) == []
            assert main._tombstone_form_optouts(set()) == []
        finally:
            subscriptions.FACULTY_SUBS_FILE = old


if __name__ == "__main__":
    test_form_optout_tombstones_active_subscription(); print("PASS")
