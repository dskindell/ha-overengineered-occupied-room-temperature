"""The repository-convention checks in scripts/check_rules.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "check_rules.py"
_spec = importlib.util.spec_from_file_location("check_rules", _SCRIPT)
assert _spec is not None
assert _spec.loader is not None
rules = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rules)


@pytest.mark.parametrize(
    "message",
    ["feat: add x", "fix(engine): y", "refactor!: z", "ci(workflows): a", "revert: undo x"],
)
def test_conventional_messages_pass(message: str) -> None:
    assert rules.message_problems(message) == []


@pytest.mark.parametrize(
    "message", ["Fix stuff", "feat:no space", "feature: x", "feat(Engine): x", ""]
)
def test_other_subjects_fail(message: str) -> None:
    assert rules.message_problems(message)


def test_co_author_lines_fail() -> None:
    assert rules.message_problems("chore: x\n\nCo-authored-by: Someone <a@b.c>")


@pytest.mark.parametrize("text", ["(D12)", "(D12, D34)", "see D96", "R19", "(F1)", "review B2"])
def test_document_references_are_found(text: str) -> None:
    assert rules.REFERENCE.search(text)


@pytest.mark.parametrize(
    "text", ["sensor.epo_den_7f79d4", "D1ning", "3D5", "Dropout", "UUID 7a12D5"]
)
def test_ordinary_text_is_not_a_reference(text: str) -> None:
    assert not rules.REFERENCE.search(text)


def test_reference_scan_skips_this_file(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_rules.py").write_text("x = '(D12)'\n")
    assert rules.reference_problems(tmp_path) == []


def test_reference_scan_reports_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("a = 1\nb = 2  # see (D12)\n")
    (tmp_path / "README.md").write_text("fine\n")
    assert rules.reference_problems(tmp_path) == ["tests/test_x.py:2: '(D12)'"]


def test_message_file_ignores_git_comment_lines(tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("# Please enter the commit message\nfeat: add x\n\n# On branch main\n")
    assert rules.message_file_problems(message) == []


def test_message_file_rejects_a_bad_subject(tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("Added some stuff\n")
    assert rules.message_file_problems(message)


def test_merge_commits_are_not_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    git("init", "-b", "main")
    git("commit", "--allow-empty", "-m", "chore: start")
    git("checkout", "-b", "topic")
    git("commit", "--allow-empty", "-m", "feat: add a thing")
    git("checkout", "main")
    git("commit", "--allow-empty", "-m", "fix: something else")
    git("merge", "--no-ff", "topic", "-m", "Merge pull request 'feat: add a thing' (#1)")
    monkeypatch.chdir(tmp_path)
    assert rules.commit_problems("HEAD~1..HEAD") == []
    git("commit", "--allow-empty", "-m", "not conventional")
    assert len(rules.commit_problems("HEAD~1..HEAD")) == 1
