"""The image must never contain collected data. .dockerignore patterns do not
recurse unless they start with **/ — a plain `*.db` once let the whole ./data
volume (live DB + backups, 16 GB) into every build and filled the VPS disk."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _patterns():
    with open(os.path.join(ROOT, ".dockerignore")) as fh:
        return {l.strip() for l in fh if l.strip() and not l.startswith("#")}


def test_data_volume_is_never_built_in():
    p = _patterns()
    assert "data/" in p
    for pat in ("**/*.db", "**/*.db-wal", "**/*.db-shm", "**/backups/", "**/*.log",
                "**/config.json", "**/.env"):
        assert pat in p, pat


def test_data_patterns_recurse():
    """Every data/secret pattern must recurse, or it only guards the top level."""
    for pat in _patterns():
        if any(k in pat for k in (".db", "backups", ".log", "config.json", ".env")):
            assert pat.startswith("**/"), pat
