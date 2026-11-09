"""faculty_to_text must not embed the faculty member's name (2026-11-09): an EM
physician named Gabriella Miller matched the "Gabriella Miller Kids First" U24 at
70% because her name sat in the embedding sentence."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("SENDGRID_API_KEY", "x")
import embedder  # noqa: E402


def test_faculty_text_has_department_and_keywords_but_not_the_name():
    txt = embedder.faculty_to_text({"name": "Gabriella M. Miller", "department": "Emergency Medicine",
                                    "keywords": ["safety net care", "hypertension"]})
    assert "Miller" not in txt and "Gabriella" not in txt
    assert "Emergency Medicine" in txt
    assert "safety net care" in txt and "hypertension" in txt


def test_faculty_text_without_department_is_still_a_sentence():
    txt = embedder.faculty_to_text({"name": "Jane Doe", "keywords": ["malaria"]})
    assert "Doe" not in txt and "malaria" in txt and txt.endswith(".")


def test_embedding_version_bumped_so_vectors_regenerate():
    assert embedder.EMBEDDING_VERSION >= 7
