"""A shared department mailbox on the roster must be treated as a missing
email so Pass 8b backfills the person's own address. Found 2026-09-25:
Peds-Endocrinology@ survived a forced scrape because the rule was an exact
list of local parts."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import faculty_scraper as fs  # noqa: E402

ROLE = [
    "Peds-Endocrinology@som.umaryland.edu",
    "peds-endocrinology@som.umaryland.edu",
    "neurosurgery.research@som.umaryland.edu",
    "cardiology-office@som.umaryland.edu",
    "info@som.umaryland.edu",
    "dept@som.umaryland.edu",
    "radonc.clinic@umm.edu",
]
PERSON = [
    "aratzki-leewing@som.umaryland.edu",   # hyphenated surname
    "mel-dallal@som.umaryland.edu",
    "kevin.jones@som.umaryland.edu",
    "ngoel1@som.umaryland.edu",
    "l-zhang@som.umaryland.edu",
    "jklee@som.umaryland.edu",
    "peixiang.zhang@som.umaryland.edu",
    "medina@som.umaryland.edu",           # surname that contains a department word
]


def test_role_mailboxes_detected():
    for em in ROLE:
        assert fs._is_role_mailbox(em), em


def test_personal_addresses_untouched():
    for em in PERSON:
        assert not fs._is_role_mailbox(em), em


if __name__ == "__main__":
    test_role_mailboxes_detected(); test_personal_addresses_untouched(); print("PASS")
