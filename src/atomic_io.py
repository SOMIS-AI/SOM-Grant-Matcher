"""
Atomic JSON persistence helper.

Every state file under data/ lives on an Azure Files (SMB) mount, where a
container restart, OOM kill, or network blip mid-write leaves a truncated file.
The readers all catch JSONDecodeError and fall back to empty defaults, so a torn
write doesn't crash — it silently WIPES state (a torn seen_grants.json re-emails
the entire grant catalog as "new"; a torn faculty_profiles.json forces a full
3-6h re-scrape and loses departed-faculty history).

Pattern: write to a temp file in the SAME directory, then os.replace() over the
target. rename() within one directory is atomic on POSIX and on SMB, so readers
only ever see the old file or the complete new one. (subscriptions.py and
eval_app_keywords.py already used this pattern; this module makes it shared.)
"""

import json
import os
import tempfile
from pathlib import Path


def atomic_write_json(path, obj, indent=None):
    """Serialize `obj` as JSON to `path` atomically (tmp file + os.replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, indent=indent)
        os.replace(tmp, str(path))
    except BaseException:
        # Never leave a stray tmp file behind on failure.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
