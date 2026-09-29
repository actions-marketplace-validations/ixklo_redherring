"""Team notes: tests and jobs a team has marked flaky or not flaky, in .github/redherring.toml.

History is the evidence redherring trusts; notes add what the team already knows.

- A failure marked `not_flaky` always looks real (unless the default branch is already failing
  it: then it still isn't this change's fault).
- A failure marked `flaky` counts as a known flake even with no history, except when it fails
  again on the same commit. That guard stays, so a coding agent never re-runs forever.

Notes are read from the default branch, so a pull request can't change its own verdicts.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache

from .logtext import strip_color

PATH = ".github/redherring.toml"
FLAKY = "flaky"
NOT_FLAKY = "not_flaky"
TEST = "test"
JOB = "job"
_MAX_REASON = 200


class NotesError(ValueError):
    """The notes file can't be read as notes; none of it is used."""


@lru_cache(maxsize=1024)
def _compile(pattern: str) -> re.Pattern[str]:
    # Only * is special. Test ids are full of [ ] and ?, which fnmatch would treat as patterns.
    return re.compile(".*".join(re.escape(part) for part in pattern.split("*")), re.S)


@dataclass(frozen=True)
class Mark:
    kind: str  # FLAKY or NOT_FLAKY
    target: str  # TEST or JOB
    pattern: str
    reason: str = ""

    def matches(self, value: str) -> bool:
        return _compile(self.pattern).fullmatch(value) is not None

    def describe(self, source: str) -> str:
        """Why a failure got this verdict, e.g. "the team marked this test flaky in …: reason"."""
        what = "this test" if self.target == TEST else "this job"
        label = "flaky" if self.kind == FLAKY else "not flaky"
        text = f"the team marked {what} {label} in {source}"
        if "*" in self.pattern:
            text += f" (pattern {self.pattern!r})"
        return f"{text}: {self.reason}" if self.reason else text


@dataclass
class Notes:
    source: str
    marks: list[Mark] = field(default_factory=list)

    def _first(self, kind: str, target: str, value: str) -> Mark | None:
        return next(
            (m for m in self.marks if m.kind == kind and m.target == target and m.matches(value)),
            None,
        )

    def for_test(self, test_id: str, job_names: list[str] | tuple[str, ...]) -> Mark | None:
        """The mark for a failing test. `not_flaky` wins over `flaky`; the test over its job."""
        for kind in (NOT_FLAKY, FLAKY):
            if mark := self._first(kind, TEST, test_id):
                return mark
            for name in job_names:
                if mark := self._first(kind, JOB, name):
                    return mark
        return None

    def for_job(self, job_name: str) -> Mark | None:
        return self._first(NOT_FLAKY, JOB, job_name) or self._first(FLAKY, JOB, job_name)

    def plain(self, kind: str, target: str) -> list[Mark]:
        """Marks naming one exact test or job (no *), in file order."""
        return [
            m for m in self.marks if m.kind == kind and m.target == target and "*" not in m.pattern
        ]


def parse(text: str, source: str = PATH) -> Notes:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise NotesError(f"{source} isn't valid TOML: {e}") from None
    if unknown := sorted(set(data) - {FLAKY, NOT_FLAKY}):
        raise NotesError(
            f"{source}: unknown section {unknown[0]!r}. Use [[flaky]] and [[not_flaky]] entries."
        )
    notes = Notes(source)
    for kind in (FLAKY, NOT_FLAKY):
        entries = data.get(kind, [])
        if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
            raise NotesError(
                f'{source}: write each {kind} entry as [[{kind}]] followed by test = "…" or job = "…".'
            )
        for i, entry in enumerate(entries, 1):
            where = f"{source}, [[{kind}]] entry {i}"
            if extra := sorted(set(entry) - {TEST, JOB, "reason"}):
                raise NotesError(f"{where}: unknown key {extra[0]!r} (use test, job and reason).")
            targets = [t for t in (TEST, JOB) if t in entry]
            if len(targets) != 1:
                raise NotesError(f'{where}: needs exactly one of test = "…" or job = "…".')
            pattern = entry[targets[0]]
            if not isinstance(pattern, str) or not pattern.strip():
                raise NotesError(f"{where}: {targets[0]} must be a non-empty string.")
            reason = entry.get("reason", "")
            if not isinstance(reason, str):
                raise NotesError(f"{where}: reason must be a string.")
            reason = " ".join(strip_color(reason).split())
            if len(reason) > _MAX_REASON:
                reason = reason[: _MAX_REASON - 1] + "…"
            notes.marks.append(Mark(kind, targets[0], pattern.strip(), reason))
    return notes
