"""Check the repository's conventions.

python scripts/check_rules.py references           # no design-document references
python scripts/check_rules.py commits <range>      # Conventional Commit messages
python scripts/check_rules.py message-file <path>  # one message (commit-msg hook)
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys

REFERENCE_PATHS = ("custom_components", "tests", "README.md", "CONTRIBUTING.md")
# Holds sample references on purpose.
REFERENCE_EXEMPT = ("tests/test_rules.py",)
REFERENCE = re.compile(r"\([DRF]\d+[^)]*\)|\b[DR]\d{1,3}\b|\breview [A-Z]\d+\b")

TYPES = (
    "feat",
    "fix",
    "perf",
    "refactor",
    "style",
    "docs",
    "test",
    "ci",
    "build",
    "chore",
    "revert",
)
SUBJECT = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([a-z0-9._/-]+\))?!?: \S")
CO_AUTHOR = re.compile(r"^co-authored-by:", re.IGNORECASE | re.MULTILINE)


def reference_problems(root: Path) -> list[str]:
    problems = []
    for base in REFERENCE_PATHS:
        path = root / base
        files = (
            [path] if path.is_file() else sorted(path.rglob("*.py")) + sorted(path.rglob("*.json"))
        )
        for file in files:
            if file.relative_to(root).as_posix() in REFERENCE_EXEMPT:
                continue
            for number, line in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if match := REFERENCE.search(line):
                    problems.append(f"{file.relative_to(root)}:{number}: {match.group(0)!r}")
    return problems


def message_problems(message: str) -> list[str]:
    subject = message.splitlines()[0] if message else ""
    problems = []
    if not SUBJECT.match(subject):
        problems.append(f"subject isn't <type>[(scope)][!]: <description>: {subject!r}")
    if CO_AUTHOR.search(message):
        problems.append("has a Co-Authored-By line")
    return problems


def message_file_problems(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return message_problems("\n".join(line for line in lines if not line.startswith("#")).strip())


def commit_problems(revisions: str) -> list[str]:
    # Merge commits are written by the forge; the commits they bring in are checked.
    log = subprocess.run(
        ["git", "log", "--no-merges", "--format=%h%x00%B%x01", revisions],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    problems = []
    for entry in filter(None, (chunk.strip() for chunk in log.split("\x01"))):
        sha, message = entry.split("\x00", 1)
        problems += [f"{sha}: {problem}" for problem in message_problems(message.strip())]
    return problems


def main(argv: list[str]) -> int:
    if argv[:1] == ["references"]:
        problems = reference_problems(Path.cwd())
        hint = "Design references belong on the BookStack pages, not in the repository."
    elif argv[:1] == ["message-file"] and len(argv) == 2:
        problems = message_file_problems(Path(argv[1]))
        hint = f"Allowed types: {', '.join(TYPES)}. No Co-Authored-By lines."
    elif argv[:1] == ["commits"] and len(argv) == 2:
        problems = commit_problems(argv[1])
        hint = f"Allowed types: {', '.join(TYPES)}. No Co-Authored-By lines."
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for problem in problems:
        print(problem)
    if problems:
        print(hint)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
