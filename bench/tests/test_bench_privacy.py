"""Nothing under bench/ may carry a real path, host, address, key or the name of anyone in the source data.

Names are checked by hash so this file does not itself contain them.
"""
import hashlib
import re
from pathlib import Path

from conftest import BENCH


PATTERNS = {
    "windows path": re.compile(r"\b[A-Za-z]:[\\/](?!/)"),
    "unc share": re.compile(r"\\\\[A-Za-z0-9_.-]+\\[A-Za-z0-9_.$-]+"),
    "private ipv4": re.compile(r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I),
    "api key": re.compile(r"\b(?:sk|pk|ghp|xox[bp])[-_][A-Za-z0-9_-]{16,}"),
    "bearer": re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"),
    "home dir": re.compile(r"/(?:home|Users|volume1)/[A-Za-z0-9_.-]+"),
}
TEXT_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".txt", ".csv", ".sha256", ".yaml", ".yml", ".ini", ".toml"}


def bench_files():
    for path in sorted(BENCH.rglob("*")):
        if path.is_file() and path.suffix in TEXT_SUFFIXES and "__pycache__" not in path.parts:
            yield path


def test_no_real_paths_hosts_addresses_or_keys():
    hits = []
    for path in bench_files():
        if path.name in ("test_bench_privacy.py", "test_bench_synthetic.py"):
            continue  # these hold the patterns and deliberate fake leaks
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, pattern in PATTERNS.items():
            match = pattern.search(text)
            if match:
                hits.append(f"{path.relative_to(BENCH)}: {name}: {match.group(0)[:40]!r}")
    assert not hits, "\n".join(hits)


def test_no_forbidden_names():
    """Compare every word to a set of hashes of names that must never appear (see forbidden_names.py)."""
    from forbidden_names import FORBIDDEN_NAME_HASHES  # a data file of hashes only

    words = re.compile(r"[A-Za-z0-9]+")
    hits = []
    for path in bench_files():
        if path.name in ("test_bench_privacy.py", "forbidden_names.py"):
            continue
        for word in set(words.findall(path.read_text(encoding="utf-8", errors="replace").lower())):
            if hashlib.sha256(word.encode()).hexdigest() in FORBIDDEN_NAME_HASHES:
                hits.append(f"{path.relative_to(BENCH)}: a forbidden name (hash {hashlib.sha256(word.encode()).hexdigest()[:8]})")
    assert not hits, "\n".join(hits)


def test_the_checker_catches_what_it_claims_to(tmp_path):
    samples = {"windows path": "D:\\data\\x", "unc share": "\\\\host\\share", "private ipv4": "192.168.1.5",
               "email": "someone@example.org", "api key": "sk-" + "a" * 20, "home dir": "/home/someone/x"}
    for name, text in samples.items():
        assert PATTERNS[name].search(text), name
    assert not PATTERNS["windows path"].search("https://example.org/path")
