"""Feedback export import (2026-09-30): CSV exports are read, and a cumulative
export under a new filename does not re-add verdicts already in the store
(Form Ids are stable across downloads). Offline."""
import csv
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import feedback_store as fs  # noqa: E402

HDR = ["Id", "Start time", "Completion time", "Email", "Name", "Match record",
       "Matching Feedback", "Any additional comments to help future matching?"]


def _row(i, record, verdict, comment=""):
    return [str(i), "9/29/2026 8:00", "9/29/2026 8:01", "anonymous", "", record, verdict, comment]


def _write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(HDR); w.writerows(rows)


def test_csv_import_and_cross_file_dedupe():
    with tempfile.TemporaryDirectory() as tmp:
        first = Path(tmp) / "Form-23Sept2026.csv"
        _write_csv(first, [
            _row(1, "a@som.umaryland.edu|O-BJA-1|2026-09-22|59|keyword|self", "Not relevant"),
            _row(2, "b@som.umaryland.edu|RFA-1|2026-09-22|70|semantic|self", "Good match", "nice"),
        ])
        store = fs.load_store(Path(tmp) / "none.json")
        s1 = fs.import_export(first, store)
        assert (s1["rows_total"], s1["added"], s1["duplicate"]) == (2, 2, 0)

        # cumulative second export, different filename, one new row + an opt-out
        second = Path(tmp) / "Form-30Sept2026.csv"
        _write_csv(second, [
            _row(1, "a@som.umaryland.edu|O-BJA-1|2026-09-22|59|keyword|self", "Not relevant"),
            _row(2, "b@som.umaryland.edu|RFA-1|2026-09-22|70|semantic|self", "Good match", "nice"),
            _row(3, "c@som.umaryland.edu|OPTOUT|2026-09-29|-|-|self", "Not relevant"),
        ])
        s2 = fs.import_export(second, store)
        assert (s2["rows_total"], s2["added"], s2["duplicate"]) == (3, 1, 2), s2
        ids = sorted(v["form_id"] for v in store["verdicts"])
        assert ids == ["1", "2", "3"]
        assert [v["source_file"] for v in store["verdicts"]][:2] == ["Form-23Sept2026.csv"] * 2
        new = store["verdicts"][2]
        assert new["verdict"] == fs.VERDICT_OPTOUT and new["email"] == "c@som.umaryland.edu"
        assert new["source_file"] == "Form-30Sept2026.csv"
        # re-importing the same file again is a no-op
        s3 = fs.import_export(second, store)
        assert (s3["added"], s3["duplicate"]) == (0, 3)
        idx = fs.build_feedback_index(store)
        assert "c@som.umaryland.edu" in idx["optout"]
        assert ("a@som.umaryland.edu", "O-BJA-1") in idx["suppress"]


if __name__ == "__main__":
    test_csv_import_and_cross_file_dedupe(); print("PASS")
