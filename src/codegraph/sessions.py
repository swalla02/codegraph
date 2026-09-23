"""Session pointers: which conversation produced a commit, read from git.

Most code is now written in a session with an agent, and the session holds
the reasoning the commit message keeps one sentence of (#69). A commit can
say which session produced it with a trailer:

    Session: <uri>

This module reads those trailers and does nothing else. The contract is
deliberately small, because every part of it is a place an agent-specific
assumption could creep in:

- **The value is opaque.** A pointer is whatever string follows `Session:`
  -- a Claude Code session id, a Codex rollout path, a PR thread URL, a
  design doc. Nothing here parses it, validates it, or knows which agent
  wrote it. Resolving one into "open or fork this session" is a per-agent
  adapter's job and lives outside this package; an adapter that cannot
  find the session reports "session not available", never an error.
- **Git does the reading.** `git log`'s `%(trailers)` atom applies git's
  own definition of a trailer block (the last paragraph, `key: value`
  lines, a case-insensitive key), so a trailer written by hand, by
  `git interpret-trailers`, or by the opt-in hook in `session_hook.py` are
  all read the same way, and a `Session:` line in the middle of a message
  body is not one.
- **The link lives in git, not in the store.** Nothing here touches
  `.codegraph/`, so a pointer travels with push and clone, and no index
  has to be rebuilt to see one.
- **Absence is not an error.** A directory that is not a git repository,
  and a commit with no trailer, both answer with an empty list. A
  repository with no pointers at all answers every other command exactly as
  it did before this module existed -- the link is additive, like a trace.

A revision that does not resolve is still an error (`gitio.GitError`), the
same as for every other command: that is a wrong question, not an absent
answer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from codegraph import gitio

#: The trailer key. Matched case-insensitively, because that is how git
#: matches trailer keys.
TRAILER_KEY = "Session"

# One record per commit: the full sha, a NUL, then the trailer values one per
# line, then an ASCII record separator. `unfold` joins a value git wrapped
# across continuation lines back into one, so a line is always one pointer.
# Neither separator can appear in a sha, and a NUL cannot appear in a commit
# message at all.
_FORMAT = f"%H%x00%(trailers:key={TRAILER_KEY},valueonly,unfold)%x1e"


@dataclass(frozen=True)
class CommitSessions:
    """One commit and the session pointers its message carries, in the
    order they appear in the trailer block."""

    commit: str
    sessions: list[str]


def _parse(out: bytes) -> list[CommitSessions]:
    records: list[CommitSessions] = []
    for record in out.decode(errors="replace").split("\x1e"):
        record = record.lstrip("\n")
        if not record:
            continue
        commit, _, values = record.partition("\0")
        sessions = [line.strip() for line in values.splitlines() if line.strip()]
        records.append(CommitSessions(commit=commit, sessions=sessions))
    return records


def session_log(repo_root: Path, revspec: str = "HEAD") -> list[CommitSessions]:
    """Every commit `git log <revspec>` walks, newest first, with its
    session pointers -- an empty list for a commit that has none.

    `revspec` is anything `git log` takes as one argument: a revision
    (`HEAD`, a branch, a sha) or a range (`main..HEAD`). An empty list for
    a directory that is not a git repository; `gitio.GitError` for a
    revspec that does not resolve, including `HEAD` in a repository with
    no commits yet.
    """
    if not gitio.is_repo(repo_root):
        return []
    return _parse(gitio.log_format(repo_root, _FORMAT, revspec))


def sessions_by_commit(repo_root: Path, commits: Iterable[str]) -> dict[str, list[str]]:
    """Session pointers for many commits in one `git` process, keyed by
    the full sha of each.

    The batched form of `session_pointers`, for a caller that already
    holds a list of commits -- a history walk -- and would otherwise pay a
    process per commit. Only those commits are read, never their ancestors
    (see `gitio.log_format_commits`). An abbreviated sha or a ref comes
    back under the full sha it resolved to.
    """
    wanted = list(commits)
    if not wanted or not gitio.is_repo(repo_root):
        return {}
    out = gitio.log_format_commits(repo_root, _FORMAT, wanted)
    return {record.commit: record.sessions for record in _parse(out)}


def session_pointers(repo_root: Path, commit_sha: str) -> list[str]:
    """The session pointers one commit's message carries, as opaque
    strings in trailer order. Empty for a commit with none, and for a
    directory that is not a git repository."""
    if not gitio.is_repo(repo_root):
        return []
    sha = gitio.rev_parse(repo_root, f"{commit_sha}^{{commit}}")
    return sessions_by_commit(repo_root, [sha]).get(sha, [])
