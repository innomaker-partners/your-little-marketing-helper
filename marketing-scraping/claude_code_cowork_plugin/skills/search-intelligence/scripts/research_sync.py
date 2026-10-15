#!/usr/bin/env python3
"""
research_sync.py -- keep a research folder in step with the user's own private
git repository, so a monthly repeat can run in Claude's cloud.

This file is byte-identical in ad-intelligence, pain-and-messaging,
search-intelligence and marketing-viewer (each skill ships standalone; a
development test pins the four copies together).  Standard library only; git is
always called with an argument list, never through a shell.

WHY every git step lives in this file: a cloud month exists only if the run
itself saves it to the repository, because the cloud machine is thrown away
afterwards.  A model that chains git steps by itself can improvise (save
everything, retry, force).  Here each step is fixed code, and the model only
quotes what it prints.

Commands (each prints ONE JSON object with a plain "message"; a refusal also
has "error" and exits 2):

  status         --root R              linked? which branch? is the copy of the
                                       tools in the folder up to date?  Writes nothing.
  link           --root R --remote URL make R a git folder on the branch
                                       claude/marketing-research with that remote.
                                       Never creates a repository, never saves to it.
  cloud-setup    --root R              copy this plugin's three research tools into
                                       R/.marketing-scraping/plugin/, write VERSION,
                                       add the ignore lines that keep favorites,
                                       notes and lock files on this computer.
  publish        --root R              save the research (run folders, approvals,
                                       the tool copy, .gitignore) to the repository.
  pull           --root R              bring new months from the repository
                                       (fast-forward only); a plain no-op when R is
                                       not linked.
  routine-prompt --job J               print the fixed prompt for the cloud routine
                                       and the one-time checklist, filled in.

Library functions used by repeat.py run-scheduled --publish git:

  claim(job_path, check)   before anything is spent: bring the folder up to date,
                           check the approval and settings equal the saved ones,
                           write .repeat_jobs/<job>.claim, save it and push it.  A
                           push that is refused means another run claimed first,
                           or this machine cannot save: the run refuses at $0.
  save(ctx, run_dir)       after the run: save the new month's folder, remove the
                           claim, push.  One `pull --rebase` and push again when
                           the first push is refused; never a force push.

What is ever saved (FROZEN): run folders, .repeat_jobs/*.json approvals and
*.claim files, .marketing-scraping/, .gitignore.  Never `git add -A`.  Only
plain files are saved: a link (git would store the path it points to, which
can name the user's home folder) or a separate git folder inside is left out
and named in the output.  Nothing is ever
written, removed, saved or updated through a link: every part of the way from
the research folder to a file must be a plain folder, and the file must stay
inside the research folder once links are followed.  Before
every save the staged content is compared with the values of APIFY_TOKEN and
APIFY_API_TOKEN, when they are set; a match refuses the save.  The comparison
is a Python `in` test on bytes; neither value is ever printed, logged, hashed
or put in a message.

Plain words only in "message" (and in the words repeat.py builds from it):
git's own text names addresses and folders of this machine and uses words a
user cannot act on, so it goes in a separate "troubleshooting" list, which
nothing asks Claude to quote.
"""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The branch every research repository uses.  WHY this name: Claude's cloud
# routines clone the repository's default branch, and branches whose names start
# with claude/ are always accepted for pushes from a cloud session (Claude Code
# documentation: routines and cloud environments).
BRANCH = "claude/marketing-research"
REMOTE = "origin"

TOOL_SKILLS = ("pain-and-messaging", "ad-intelligence", "search-intelligence")
TOOL_TO_SKILL = {"pain": "pain-and-messaging", "ad": "ad-intelligence",
                 "search": "search-intelligence"}

VENDOR_DIR = ".marketing-scraping"
PLUGIN_SUBDIR = "plugin"

# Lines the research folder's .gitignore must hold.  WHY each:
#   .viewer/        favorites, notes, trashed records and saved views stay on
#                   this computer (they are never uploaded);
#   *.lock, *.stale.*, *.status.json, *.tmp
#                   a run's own working files on one machine (lock, status for
#                   `repeat.py wait`, half-written files);
#   .env            a place people put credentials;
#   __pycache__/, .DS_Store
#                   noise from Python and macOS.
GITIGNORE_LINES = (".viewer/", "*.lock", "*.stale.*", "*.status.json", "*.tmp",
                   ".env", "__pycache__/", ".DS_Store")

# What copying the plugin leaves out.  WHY .env: a user may keep credentials in
# one; it must never reach a repository.
_COPY_SKIP_NAMES = frozenset(("__pycache__", ".env", ".DS_Store"))
_COPY_SKIP_SUFFIXES = (".pyc",)

# A run folder is a folder holding one of these (same markers as repeat.py and
# the app).
_RUN_MARKERS = ("frontend_index.json", "AD_INTEL_CONFIG.json",
                "PAIN_CONFIG.json", "SEARCH_CONFIG.json")

# A claim younger than this belongs to a run that may still be going; an older
# one was left behind by a run that ended without removing it.  WHY 24 hours:
# the same rule as the one-run lock in repeat.py (a real run takes minutes to a
# few hours; a month-old claim must never block a month).
CLAIM_STALE_SECONDS = 24 * 3600

_TOKEN_VARS = ("APIFY_TOKEN", "APIFY_API_TOKEN")

# Who a save is made as when git has no name or e-mail set (a fresh cloud
# machine may have none).  WHY a fixed, obviously generic identity instead of
# refusing: without it every cloud month would refuse; with it the history still
# says plainly what made the save.  Used only when git has none of its own.
_FALLBACK_NAME = "Marketing research"
_FALLBACK_EMAIL = "marketing-research@users.noreply.localhost"


class SyncError(Exception):
    """A refusal with a plain message for the user.  `troubleshooting` holds
    git's own words, when there are any, for diagnosis only (never quoted).
    `reason`, when set, is the cause alone in plain words (no "nothing was
    saved" around it), for save() to put after "Why it was not saved:"."""

    def __init__(self, message, troubleshooting=None, reason=None):
        super().__init__(message)
        self.troubleshooting = list(troubleshooting or [])
        self.reason = reason


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _refuse(message, **extra):
    d = {"error": message, "message": message}
    d.update(extra)
    _out(d)
    sys.exit(2)


# ---------------------------------------------------------------------------
# Secrets: never printed
# ---------------------------------------------------------------------------

def _token_values():
    """The token values set in this environment, as bytes, for comparison only."""
    vals = []
    for k in _TOKEN_VARS:
        v = os.environ.get(k)
        if v and v.strip():
            for form in {v, v.strip()}:
                vals.append(form.encode("utf-8"))
    return vals


def _contains_token(data, tokens):
    """True when any token value occurs in data (bytes).  Never prints either."""
    return any(t in data for t in tokens)


_USERINFO_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]*@")


def _safe_url(url):
    """A remote address with any user name or password in it hidden.  WHY: an
    address like https://name:secret@host/... carries a credential."""
    if not url:
        return url
    return _USERINFO_RE.sub(lambda m: m.group("scheme") + "***@", str(url))


def _clean(text):
    """git's own words, safe to show: credentials in addresses and any token
    value are hidden, and the text is kept short."""
    text = _safe_url(text or "")
    for t in _token_values():
        text = text.replace(t.decode("utf-8", "replace"), "***")
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return " ".join(lines)[:400]


def _said(step, err):
    """One troubleshooting line: which step, and git's own words (cleaned)."""
    return f"{step}: git said: {_clean(err) or 'nothing'}"


# Phrases in git's text meaning the repository could not be reached at all
# (wrong or missing address, no network, no access), as opposed to reached and
# refusing.  WHY: the user's next step differs (check the address and access,
# versus look at what the repository refuses).
_UNREACHABLE = ("does not appear to be a git repository", "could not read from remote",
                "unable to access", "could not resolve host", "repository not found",
                "connection refused", "connection timed out", "operation timed out",
                "network is unreachable", "unable to connect", "permission denied",
                "authentication failed")

REACH_ADVICE = ("Check that the research repository still exists and that its access "
                "settings still allow saving to it.")


def _unreachable(err):
    low = (err or "").lower()
    return any(s in low for s in _UNREACHABLE)


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

# The only ways git may reach the research repository:
# https and ssh (GitHub's two address forms, git@host:path included), file (a
# plain or file:// path, a Windows path), and git and http. Every other transport
# is refused by git itself, whatever the user's git configuration allows: ext::
# and fd:: and any other "<helper>::" address name a program git would run.
_GIT_PROTOCOLS = "https:ssh:file:git:http"


def _git_env():
    # WHY GIT_TERMINAL_PROMPT=0: an unattended run must never stop and wait for a
    # password nobody will type; git fails instead, and the run says so.
    # WHY LC_ALL=C: git's messages are read below (for example the names of
    # files that would be overwritten), so they must be in one language.
    # WHY GIT_ALLOW_PROTOCOL: link refuses a transport address, but an
    # address saved before that check, or by hand in the folder's git settings,
    # was still used by publish, pull and the cloud month's fetch and push, and a
    # git set to allow ext:: ran the command in it. This covers every git call
    # here, and an address another git setting (insteadOf) rewrites as well.
    return dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C",
                GIT_ALLOW_PROTOCOL=_GIT_PROTOCOLS)


def _git(root, *args, binary=False):
    """Run git in root.  Returns (exit code, stdout, stderr)."""
    try:
        r = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                           env=_git_env())
    except FileNotFoundError:
        raise SyncError("git is not installed on this machine, so the research "
                        "cannot be saved to or read from the repository.")
    except OSError as exc:
        raise SyncError(f"git could not be started ({exc.strerror or exc}).")
    if binary:
        return r.returncode, r.stdout, r.stderr.decode("utf-8", "replace")
    return (r.returncode, r.stdout.decode("utf-8", "replace"),
            r.stderr.decode("utf-8", "replace"))


def _git_ok(root, *args):
    rc, out, _ = _git(root, *args)
    return out if rc == 0 else None


def _toplevel(root):
    """The git folder root belongs to, resolved, or None."""
    out = _git_ok(root, "rev-parse", "--show-toplevel")
    if out is None:
        return None
    try:
        return Path(out.strip()).resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _is_own_repo(root):
    top = _toplevel(root)
    return top is not None and top == Path(root).resolve()


def _remote_url(root):
    out = _git_ok(root, "config", "--get", f"remote.{REMOTE}.url")
    return out.strip() if out else None


TRANSPORT_STORED_MESSAGE = (
    "Nothing was sent or read, because the repository address saved in this research "
    "folder names a program for git to run instead of a repository: link the folder "
    "again with the address exactly as GitHub shows it.")


def _check_stored_remote(root):
    """Refuse, in one plain sentence, when the saved address is a transport address.
    Called before the first git call that reaches the
    repository in publish, pull and a cloud month's claim (save runs after the
    claim). git refuses it anyway (_git_env); this says what to do."""
    url = _remote_url(root)
    if url is not None and _TRANSPORT_RE.match(url):
        raise SyncError(TRANSPORT_STORED_MESSAGE)


def _current_branch(root):
    out = _git_ok(root, "symbolic-ref", "-q", "--short", "HEAD")
    return out.strip() if out else None


def _has_commits(root):
    return _git_ok(root, "rev-parse", "-q", "--verify", "HEAD^{commit}") is not None


def _remote_ref():
    return f"refs/remotes/{REMOTE}/{BRANCH}"


def _fetch(root):
    """Fetch the branch.  Returns True when the repository has it, False when it
    does not have it yet.  Raises when the repository cannot be reached."""
    rc, _, err = _git(root, "fetch", "-q", REMOTE,
                      f"+refs/heads/{BRANCH}:{_remote_ref()}")
    if rc == 0:
        return True
    low = err.lower()
    if "couldn't find remote ref" in low or "could not find remote ref" in low:
        return False
    raise SyncError(f"The research repository could not be reached. {REACH_ADVICE}",
                    [_said("Reading the repository", err)])


def _identity_args(root):
    """-c options for a save when git has no name or e-mail of its own."""
    name = _git_ok(root, "config", "user.name")
    email = _git_ok(root, "config", "user.email")
    args = []
    if not (name and name.strip()):
        args += ["-c", f"user.name={_FALLBACK_NAME}"]
    if not (email and email.strip()):
        args += ["-c", f"user.email={_FALLBACK_EMAIL}"]
    return args


_IGNORE_RULE_RE = re.compile(r"^(?P<source>.*?):(?P<line>\d+):(?P<pattern>.*)$")


def _ignored_cause(root, err):
    """The cause in plain words when git would not mark paths for saving
    because an ignore rule leaves them out, or None when git's text does not
    say that.

    WHY: a run folder left out by a .gitignore line was reported as "the save
    could not be prepared", which names no cause the user can act on.  git
    lists the paths it refused; `check-ignore -v` names the rule and where it
    is written.  A rule in a .gitignore file of the research folder is named
    with its line; a rule kept anywhere else (a git setting on this computer)
    is named as such, never by its path (a path of this machine)."""
    names, grab = [], False
    for ln in err.splitlines():
        if "the following paths are ignored by" in ln.lower():
            grab = True
            continue
        if grab:
            s = ln.strip()
            if not s or s.lower().startswith("hint:"):
                break
            names.append(s)
    if not names:
        return None
    _, out, _ = _git(root, "check-ignore", "-v", "--", *names)
    rules, found = {}, set()
    for ln in out.splitlines():
        src, _, path = ln.partition("\t")
        m = _IGNORE_RULE_RE.match(src)
        if not m or not path:
            continue
        source = m.group("source")
        in_folder = (not os.path.isabs(source) and not source.startswith(".git/")
                     and source.split("/")[-1] == ".gitignore")
        key = (source if in_folder else None, m.group("pattern") if in_folder else None)
        rules.setdefault(key, []).append(path)
        found.add(path)
    rest = [n for n in names if n not in found]
    if rest:
        rules.setdefault((None, None), []).extend(rest)
    parts = []
    for (source, pattern), paths in rules.items():
        what = ", ".join(paths)
        if source is None:
            parts.append(f"a git setting on this computer, outside the research folder's "
                         f".gitignore file, leaves out {what}")
        else:
            where = ("the research folder's .gitignore file" if source == ".gitignore" else
                     f"the .gitignore file in {source.rsplit('/', 1)[0]}")
            parts.append(f"the line {pattern!r} in {where} leaves out {what}")
    many = len(names) > 1
    return ("; ".join(parts) + " (such a rule lists what git must never save), so "
            + ("they" if many else "it") + " could not be saved")


def _prepare_failed(root, step, err):
    """A refusal for a git step that failed while a save was being prepared,
    naming the cause in plain words.

    WHY one helper for every such step: each of them used to say only "the
    save could not be prepared", which names nothing the user can act on.
    The causes git reports in a recognisable way are named; any other one is
    called what it is, an error of git at that step, with git's own words
    kept apart in troubleshooting.  Nothing else changes: the caller still
    unstages everything and nothing is saved."""
    low = (err or "").lower()
    cause = _ignored_cause(root, err or "")
    advice = " Remove or change that rule, then save again." if cause else ""
    if cause is None and "index.lock" in low:
        cause = ("another git program was working in this research folder at the same "
                 "time, or one stopped halfway and left its lock file behind")
        advice = " Wait a minute, then save again."
    if cause is None and ("permission denied" in low or "insufficient permission" in low):
        cause = ("a file or folder in the research folder could not be read or changed, "
                 "because its permissions do not allow it")
    if cause is None:
        cause = f"git stopped with an error while {step}"
    return SyncError(f"Nothing was saved: {cause}.{advice}",
                     [_said(step[0].upper() + step[1:], err)], reason=cause)


def _staged_paths(root):
    rc, out, err = _git(root, "diff", "--cached", "--name-only", "-z", "--no-renames")
    if rc != 0:
        raise _prepare_failed(root, "listing what is marked for saving", err)
    return [p for p in out.split("\0") if p]


def _unstage_all(root):
    # WHY reset also before the first save: `git rm --cached` refuses to unstage
    # a separate git folder, which left everything marked for saving; reset
    # works on a branch with no saves yet (every git since 2013).
    _git(root, "reset", "-q")


# The kinds of entry git can stage that are not plain files, with the words
# used to name them.  WHY never saved: git stores a link as the path it points
# to (for example a folder in the user's home), and a separate git folder as a
# pointer to another repository; neither is research data.
_NOT_A_FILE = {"120000": "a link to another place on this computer",
               "160000": "a separate git folder"}


def _staged_modes(root):
    """{path: new mode} of everything staged ("000000" for a removal)."""
    rc, out, err = _git(root, "diff", "--cached", "--raw", "-z", "--no-renames",
                        "--no-abbrev")
    if rc != 0:
        raise _prepare_failed(root, "listing what is marked for saving", err)
    parts = out.split("\0")
    modes, i = {}, 0
    while i + 1 < len(parts):
        meta = parts[i]
        if meta.startswith(":"):
            fields = meta[1:].split(" ")
            modes[parts[i + 1]] = fields[1] if len(fields) > 1 else ""
            i += 2
        else:
            i += 1
    return modes


def _leave_out_non_files(root):
    """Unstage every staged link or separate git folder.  Returns
    [(path, words)] of what was left out.  WHY after staging and by git's own
    record: git decides what a folder brings with it, at any depth; its record
    of each entry's kind is exactly what would be saved."""
    left = []
    for rel, mode in sorted(_staged_modes(root).items()):
        if mode in _NOT_A_FILE:
            rc, _, err = _git(root, "--literal-pathspecs", "reset", "-q", "--", rel)
            if rc != 0:
                raise _prepare_failed(root, "leaving out a link", err)
            left.append((rel, _NOT_A_FILE[mode]))
    return left


def _link_on_the_way(root, rel):
    """The first part of rel (a path inside root, with /) that is a link or
    leads outside root once links are followed; None when the whole way from
    root to rel is plain folders inside root.

    WHY every part and not only the last: writing root/rel follows a link
    anywhere on the way (a folder .repeat_jobs that is a link sends the claim
    somewhere else although the claim file itself is plain).  WHY also the
    resolved path: it is the place a write actually lands.  Links ABOVE root
    (on macOS /tmp is one) are not checked: root is the user's own choice."""
    root = Path(root)
    parts = [p for p in str(rel).split("/") if p]
    try:
        for i in range(1, len(parts) + 1):
            if root.joinpath(*parts[:i]).is_symlink():
                return "/".join(parts[:i])
        root.joinpath(*parts).resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return "/".join(parts)
    return None


def _update_through_link(root, ref):
    """The first link an update of root to ref would write through, or None.
    WHY checked before git writes: git itself refuses most such writes, but
    with its own words, after it started; this names the link plainly and
    stops before anything is changed."""
    if _has_commits(root):
        rc, out, _ = _git(root, "diff", "--name-only", "-z", "--no-renames", "HEAD", ref)
    else:
        rc, out, _ = _git(root, "ls-tree", "-r", "-z", "--name-only", ref)
    if rc != 0:
        return None
    for rel in sorted(p for p in out.split("\0") if p):
        bad = _link_on_the_way(root, rel)
        if bad is not None:
            return bad
    return None


# What the two folders the tools keep in the research folder are, in words a
# user who never opened them understands.  WHY: a message naming only
# ".marketing-scraping" or ".repeat_jobs" names something the user may never
# have seen (both start with a dot, so most file browsers hide them).
_TOOL_FOLDER_WORDS = {
    ".repeat_jobs": "the folder where the approvals of your monthly repeats are kept",
    VENDOR_DIR: "the folder where the copy of the research tools that the cloud runs "
                "is kept",
}


def _link_words(bad):
    top = str(bad).split("/")[0]
    what = _TOOL_FOLDER_WORDS.get(top)
    said = f" ({top} is {what})" if what else ""
    return (f"{bad} in the research folder is a link to another place on this "
            f"computer{said}, and nothing is ever written through a link")


def _in_head(root, rel):
    """True when the last save holds at least one file under the folder rel."""
    out = _git_ok(root, "ls-tree", "-r", "--name-only", "HEAD", "--", rel + "/")
    return bool(out and out.strip())


def left_out_words(left):
    """One plain sentence naming what was left out, or ""."""
    if not left:
        return ""
    names = "; ".join(f"{rel} ({why})" for rel, why in left)
    return f"Not saved, because it is not a plain file or folder: {names}."


def _forbidden_reason(rel):
    """Why a path may never be saved, or None."""
    parts = rel.split("/")
    name = parts[-1]
    if ".viewer" in parts:
        return "it holds favorites, notes or views, which stay on this computer"
    if ".git" in parts:
        return "it is part of git's own data"
    if name == ".env":
        return "it is a file where credentials are kept"
    if (name.endswith(".lock") or name.endswith(".status.json")
            or name.endswith(".tmp") or ".stale." in name):
        return "it is a run's working file on one machine"
    return None


def _allowed(rel, prefixes):
    for p in prefixes:
        if p.endswith("/"):
            if rel.startswith(p):
                return True
        elif rel == p:
            return True
    return False


def _commit(root, add, remove, message, prefixes, left_out=None):
    """Save exactly these paths in one commit.

    add:      paths (relative to root) to stage with `git add --`, each one named;
    remove:   paths to delete from the folder and the repository;
    prefixes: every staged path must be one of these (a name ending in / covers
              a folder).  WHY checked after staging: git decides what a folder
              brings with it (its ignore rules apply); the check reads what git
              actually staged, which is exactly what would be saved.

    Refuses (and unstages everything) when something else was already staged,
    when a staged path is outside prefixes or may never be saved, or when the
    staged content contains the token's value.  A link or a separate git folder
    is left out (never saved) and added to left_out, when given, as
    (path, words).  Returns True when a commit was made, False when there was
    nothing new."""
    if _staged_paths(root):
        raise SyncError("Some changes in this research folder are already marked for "
                        "saving by something else, so nothing was saved. Save or undo "
                        "those first.")
    try:
        if add:
            rc, _, err = _git(root, "add", "--", *add)
            if rc != 0:
                raise _prepare_failed(root, "marking the research for saving", err)
        for rel in remove:
            rc, _, err = _git(root, "rm", "-q", "--cached", "--ignore-unmatch", "--", rel)
            if rc != 0:
                raise _prepare_failed(root, "removing this month's reservation", err)
            try:
                (Path(root) / rel).unlink()
            except FileNotFoundError:
                pass
        left = _leave_out_non_files(root)
        if left_out is not None:
            left_out.extend(left)
        staged = _staged_paths(root)
        tokens = _token_values()
        for rel in staged:
            why = _forbidden_reason(rel)
            if why is None and not _allowed(rel, prefixes):
                why = "it is not research data"
            if why is not None:
                raise SyncError(f"Nothing was saved: {rel} was about to be included, "
                                f"and {why}.")
            if tokens and _contains_token(rel.encode("utf-8"), tokens):
                raise SyncError("Nothing was saved: a file name in this folder contains "
                                "your Apify token. Rename that file first.")
            rc, blob, _ = _git(root, "cat-file", "blob", f":{rel}", binary=True)
            if rc == 0 and tokens and _contains_token(blob, tokens):
                raise SyncError(f"Nothing was saved: {rel} contains your Apify token, "
                                f"and a token must never be saved to a repository. "
                                f"Remove it from that file first.")
        if not staged:
            return False
        rc, _, err = _git(root, *_identity_args(root), "commit", "-q", "-m", message)
        if rc != 0:
            raise SyncError("The save could not be made, so nothing was saved.",
                            [_said("Saving", err)])
        return True
    except SyncError:
        _unstage_all(root)
        raise


def _push(root):
    """Push the branch to origin.  Never forced.  Returns (ok, git's words)."""
    rc, _, err = _git(root, "push", "-q", REMOTE, f"HEAD:refs/heads/{BRANCH}")
    return rc == 0, _clean(err)


def _run_folders(root):
    """Names of the run folders directly inside root."""
    names = []
    try:
        entries = sorted(Path(root).iterdir())
    except OSError:
        return names
    for d in entries:
        if d.name.startswith(".") or not d.is_dir() or d.is_symlink():
            continue
        if any((d / m).is_file() for m in _RUN_MARKERS):
            names.append(d.name)
    return names


def _linked_run_folders(root):
    """Names of the entries directly inside root that are links to a run
    folder somewhere.  WHY: such a folder is never saved (what it points to is
    not in the research folder), and it used to be skipped without a word, so
    the user believed it was in the repository."""
    names = []
    try:
        entries = sorted(Path(root).iterdir())
    except OSError:
        return names
    for d in entries:
        if d.name.startswith(".") or not d.is_symlink():
            continue
        try:
            is_run = d.is_dir() and any((d / m).is_file() for m in _RUN_MARKERS)
        except OSError:
            is_run = False
        if is_run:
            names.append(d.name)
    return names


def _saved_through_link(root):
    """The first link on the way to a file of the last save, or None.  WHY:
    a saved folder (for example .marketing-scraping) replaced here by a link
    makes git refuse to bring in the repository's changes, and git's refusal
    used to be reported as a clash with the repository, which it is not."""
    out = _git_ok(root, "ls-tree", "-r", "-z", "--name-only", "HEAD")
    for rel in sorted(p for p in (out or "").split("\0") if p):
        bad = _link_on_the_way(root, rel)
        if bad is not None:
            return bad
    return None


def _changed_since_save(root):
    """Top-level names of saved files that differ here from the last save and
    are not part of it (for example a saved run folder deleted on this
    computer).  WHY: git will not bring in the repository's changes over
    them, and its refusal used to be reported as a clash with the
    repository."""
    rc, out, _ = _git(root, "diff", "--name-only", "-z", "--no-renames")
    if rc != 0:
        return []
    names = []
    for rel in (p for p in out.split("\0") if p):
        top = rel.split("/")[0]
        if top not in names:
            names.append(top)
    return names


def _changed_words(names):
    return (f"{', '.join(names)} changed on this computer after it was last saved, in a way "
            f"a save does not include (for example a saved run folder was deleted or "
            f"replaced)")


def _gitignore_ok(root):
    p = Path(root) / ".gitignore"
    try:
        lines = {ln.strip() for ln in p.read_text(encoding="utf-8").splitlines()}
    except (OSError, ValueError):
        return False
    return all(line in lines for line in GITIGNORE_LINES)


def _need_linked(root):
    """Refuse unless root is its own git folder with a remote, on the branch."""
    if not _is_own_repo(root):
        raise SyncError("This research folder is not linked to a repository yet, so "
                        "nothing was changed. Link it first.")
    if _remote_url(root) is None:
        raise SyncError("This research folder has no repository address, so nothing "
                        "was changed. Link it first.")
    _check_stored_remote(root)
    br = _current_branch(root)
    if br != BRANCH:
        raise SyncError(f"This research folder is on the branch {br or '(none)'!s}, not "
                        f"{BRANCH}, so nothing was changed.")


# ---------------------------------------------------------------------------
# The plugin copy
# ---------------------------------------------------------------------------

def _source_skills():
    """The installed plugin's skills folder, from this file's own location."""
    return Path(__file__).resolve().parent.parent.parent


def _installed_version():
    manifest = _source_skills().parent / ".claude-plugin" / "plugin.json"
    try:
        v = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return v if isinstance(v, str) and v else None


def _vendored_version(root):
    p = Path(root) / VENDOR_DIR / PLUGIN_SUBDIR / "VERSION"
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _root_arg(raw):
    try:
        root = Path(raw).expanduser().resolve()
        ok = root.is_dir()
    except (OSError, ValueError, RuntimeError):
        ok = False
    if not ok:
        raise SyncError(f"The research folder {raw!r} was not found.")
    return root


def _jobs(root):
    rows = []
    d = Path(root) / ".repeat_jobs"
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        files = []
    for f in files:
        if f.name.endswith(".status.json"):
            continue
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(j, dict):
            rows.append({"job": f.name, "tool": j.get("tool"),
                         "mode": j.get("mode", "local"),
                         "claimed": (d / (f.name[:-5] + ".claim")).exists()})
    return rows


def cmd_status(a):
    root = _root_arg(a.root)
    own = _is_own_repo(root)
    remote = _remote_url(root) if own else None
    branch = _current_branch(root) if own else None
    inst, vend = _installed_version(), _vendored_version(root)
    jobs = _jobs(root)
    linked = bool(own and remote)
    if not linked:
        msg = "This research folder is not linked to a repository."
    elif branch != BRANCH:
        msg = f"This research folder is linked, but it is on {branch or 'no branch'}, not {BRANCH}."
    elif vend is None:
        msg = "This research folder is linked; the cloud setup has not been done yet."
    elif inst is not None and vend != inst:
        msg = (f"This research folder is linked. Its copy of the tools is version {vend}, "
               f"and the installed plugin is version {inst}: run the cloud setup again "
               f"and save, so the cloud uses the same version.")
    else:
        msg = "This research folder is linked and set up for the cloud."
    _out({"root": str(root), "linked": linked, "remote": _safe_url(remote),
          "branch": branch, "expected_branch": BRANCH,
          "vendored_version": vend, "installed_version": inst,
          "vendored_current": bool(vend and inst and vend == inst),
          "cloud_jobs": [j for j in jobs if j["mode"] == "cloud"],
          "jobs": jobs, "message": msg})
    return 0


# The plain words for a `link` that a command sandbox stopped.  WHY its own
# message: Claude Code's sandbox never lets a command write to .git/hooks, and
# by default not to .git/config either (Claude Code documentation, sandboxing,
# "Protected paths"), so `git init` and `git remote add` fail there with
# "Operation not permitted".  That is a security boundary the user controls; the
# product says plainly what stopped and what the user may decide, and never
# works around it.
SANDBOX_LINK_MESSAGE = ("Claude's command sandbox does not allow creating git settings in "
                        "this folder, so it was not linked. This one step, the same link "
                        "command, can be run outside the sandbox with your permission.")


def _sandbox_blocked(err):
    """True when git's text says a write to .git/hooks or .git/config was not
    permitted (the operating system's own words; git runs with LC_ALL=C)."""
    err = err or ""
    return "Operation not permitted" in err and (".git/hooks" in err or ".git/config" in err)


# The hedged words for a `git init` refused on the research folder's own .git.
# WHY hedged: a sandbox rule over the whole folder fails this
# way, but so does a folder the operating system locks for another reason, so
# the message names the sandbox only as a possibility, and the skill's
# permission rule still decides whether anything runs outside it.
FOLDER_BLOCKED_LINK_MESSAGE = ("Your computer would not let this folder be set up, so it was "
                               "not linked. If Claude's command sandbox is on, this one step, "
                               "the same link command, can be run outside it with your "
                               "permission.")


def _folder_blocked(err, root):
    """True when git's text says creating the research folder's own .git (the
    folder itself, not a file or folder inside it) was not permitted."""
    return (os.path.join(str(root), ".git") + ": Operation not permitted") in (err or "")


# git reads "<transport>::<address>" as "run this transport helper": ext:: runs
# any command it names, fd:: talks to an open file. Link stored ext::sh -c ... as the origin, and
# only git's default refusal of
# ext:: kept `publish` from running it; a git that allows it would have run the
# command. A scheme followed by "::" at the very start is refused. An ssh address
# with an IPv6 host (ssh://git@[::1]/repo.git) has "://" after its scheme and
# "::" only inside the brackets, so it does not match; nor do https, ssh,
# git@host:path, file:// and plain paths.
# Git also takes a helper name that starts with a digit
# (9ext::), so the first character may be a digit too, as in git's own rule.
_TRANSPORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.-]*::")


def cmd_link(a):
    root = _root_arg(a.root)
    remote = a.remote or ""
    if (not remote.strip() or remote.startswith("-") or any(c.isspace() for c in remote)
            or "\0" in remote):
        raise SyncError("That repository address cannot be used. Give the address "
                        "exactly as GitHub shows it.")
    if _TRANSPORT_RE.match(remote):
        raise SyncError("That address asks git to run a helper program instead of "
                        "naming a repository, so it was not linked. Give the address "
                        "exactly as GitHub shows it.")
    top = _toplevel(root)
    if top is not None and top != root:
        raise SyncError(f"This research folder is inside another git folder ({top}), so "
                        f"it was not linked. Use a research folder of its own.")
    created = False
    if top is None:
        rc, _, err = _git(root, "init", "-q")
        if rc != 0:
            if _sandbox_blocked(err):
                raise SyncError(SANDBOX_LINK_MESSAGE, [_said("Setting up the folder", err)])
            if _folder_blocked(err, root):
                raise SyncError(FOLDER_BLOCKED_LINK_MESSAGE,
                                [_said("Setting up the folder", err)])
            raise SyncError("This folder could not be set up for saving, so it was not "
                            "linked.", [_said("Setting up the folder", err)])
        # WHY symbolic-ref and not `init -b`: it works on every git version.
        _git(root, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
        created = True
    else:
        br = _current_branch(root)
        if br != BRANCH:
            if _has_commits(root):
                raise SyncError(f"This research folder is already a git folder on the "
                                f"branch {br or '(none)'}, so it was not linked. It must "
                                f"use {BRANCH}.")
            _git(root, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
    existing = _remote_url(root)
    if existing is None or _TRANSPORT_RE.match(existing):
        # A saved transport address is replaced, which is what the
        # refusal (TRANSPORT_STORED_MESSAGE) tells the user to do.
        how = ("add", REMOTE, remote) if existing is None else ("set-url", REMOTE, remote)
        rc, _, err = _git(root, "remote", *how)
        if rc != 0:
            if _sandbox_blocked(err):
                raise SyncError(SANDBOX_LINK_MESSAGE, [_said("Recording the address", err)])
            raise SyncError("The repository address could not be recorded, so the "
                            "folder was not linked.", [_said("Recording the address", err)])
    elif existing != remote:
        raise SyncError(f"This research folder is already linked to another repository "
                        f"({_safe_url(existing)}), so nothing was changed.")
    _out({"root": str(root), "linked": True, "branch": BRANCH,
          "remote": _safe_url(remote), "created": created,
          "message": (f"This research folder is now linked to {_safe_url(remote)} on "
                      f"the branch {BRANCH}. Nothing was saved to the repository yet.")})
    return 0


def _make_owner_writable(folder):
    """Give the owner write permission on every folder and file under folder.
    WHY: copying keeps each file's permissions, so a plugin installed read-only
    gave a read-only copy; git could then not update it on a pull, and the next
    setup could not remove the old copy (which would then be saved with the
    research)."""
    for dirpath, _, filenames in os.walk(folder):
        for p in [dirpath] + [os.path.join(dirpath, n) for n in filenames]:
            try:
                st = os.lstat(p)
                if not stat.S_ISLNK(st.st_mode) and not st.st_mode & stat.S_IWUSR:
                    os.chmod(p, st.st_mode | stat.S_IWUSR)
            except OSError:
                pass


def _remove_copy(folder):
    _make_owner_writable(folder)
    shutil.rmtree(folder, ignore_errors=True)


def _copy_ignore(_dir, names):
    return [n for n in names if n in _COPY_SKIP_NAMES or n.endswith(_COPY_SKIP_SUFFIXES)]


def _links_in(folder):
    found = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if d not in _COPY_SKIP_NAMES]
        for n in dirnames + filenames:
            if os.path.islink(os.path.join(dirpath, n)):
                found.append(os.path.join(dirpath, n))
    return found


def cmd_cloud_setup(a):
    root = _root_arg(a.root)
    skills = _source_skills()
    version = _installed_version()
    if version is None:
        raise SyncError("This command must be run from the installed plugin (its "
                        "version file was not found), so nothing was copied.")
    try:
        inside = skills == root or root in skills.parents
    except (OSError, ValueError):
        inside = True
    if inside:
        raise SyncError("This command was run from the copy inside the research folder. "
                        "Run it from the installed plugin, so nothing was copied.")
    for s in TOOL_SKILLS:
        if not (skills / s).is_dir():
            raise SyncError(f"The installed plugin has no {s} folder, so nothing was copied.")
        links = _links_in(skills / s)
        if links:
            raise SyncError(f"The installed plugin's {s} folder holds a link to another "
                            f"place, so nothing was copied (a link could bring files "
                            f"from elsewhere on this computer).")
    base = root / VENDOR_DIR
    dest = base / PLUGIN_SUBDIR
    tmp = base / f"{PLUGIN_SUBDIR}.new-{os.getpid()}"
    old = base / f"{PLUGIN_SUBDIR}.old-{os.getpid()}"
    # WHY before anything is written, and with the same check as publish, pull
    # and the claim (_link_on_the_way): a .marketing-scraping that is a link
    # sent the whole copy of the tools wherever the link points, anywhere on
    # this computer, and the setup still said it succeeded.  Every place this
    # command writes, renames or removes is checked; .gitignore too, because it
    # is written below.
    for rel in (f"{VENDOR_DIR}/{dest.name}", f"{VENDOR_DIR}/{tmp.name}",
                f"{VENDOR_DIR}/{old.name}", ".gitignore"):
        bad = _link_on_the_way(root, rel)
        if bad is not None:
            what = "file" if bad == ".gitignore" else "folder"
            raise SyncError(f"Nothing was copied: {_link_words(bad)}. Replace the link "
                            f"with a plain {what}, then run the cloud setup again.")
    try:
        base.mkdir(exist_ok=True)
        for s in TOOL_SKILLS:
            shutil.copytree(skills / s, tmp / s, ignore=_copy_ignore)
        _make_owner_writable(tmp)
        (tmp / "VERSION").write_text(version + "\n", encoding="utf-8")
        # WHY: the copy must hold no path of this machine (a stranger's clone
        # would carry it).  The plugin's own location is the path most likely
        # to leak into a file.
        needle = str(skills.parent).encode("utf-8")
        n_files = 0
        for dirpath, _, filenames in os.walk(tmp):
            for n in filenames:
                n_files += 1
                if needle in Path(dirpath, n).read_bytes():
                    raise SyncError(f"A file of the plugin names its folder on this "
                                    f"computer ({n}), so nothing was copied.")
        if dest.exists():
            os.rename(dest, old)
        os.rename(tmp, dest)
    except SyncError:
        if tmp.parent == base and tmp.exists():
            _remove_copy(tmp)
        raise
    except OSError as exc:
        if tmp.parent == base and tmp.exists():
            _remove_copy(tmp)
        if old.exists() and not dest.exists():
            os.rename(old, dest)
        raise SyncError(f"The tools could not be copied into the research folder "
                        f"({exc.strerror or exc}).")
    leftover = None
    if old.parent == base and old.exists():
        _remove_copy(old)
        if old.exists():
            leftover = f"{VENDOR_DIR}/{old.name}"
    added = _ensure_gitignore(root)
    msg = (f"The three research tools (version {version}) were copied into the research "
           f"folder, and favorites, notes and working files are set to stay on this "
           f"computer. Nothing was saved to the repository yet.")
    if leftover:
        msg += (f" The previous copy, {leftover}, could not be removed: delete that folder "
                f"before saving, or it would be saved with the research.")
    _out({"root": str(root), "version": version, "skills": list(TOOL_SKILLS),
          "files": n_files, "gitignore_added": added, "leftover": leftover,
          "message": msg})
    return 0


def _ensure_gitignore(root):
    p = Path(root) / ".gitignore"
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    have = {ln.strip() for ln in text.splitlines()}
    missing = [ln for ln in GITIGNORE_LINES if ln not in have]
    if missing:
        if text and not text.endswith("\n"):
            text += "\n"
        text += "".join(ln + "\n" for ln in missing)
        p.write_text(text, encoding="utf-8")
    return missing


def _publish_paths(root):
    """What publish stages: every run folder, the approvals and claims, the tool
    copy and .gitignore.  WHY named one by one and never `git add -A`: the
    research folder is also the user's own working folder."""
    add = list(_run_folders(root))
    jobs = Path(root) / ".repeat_jobs"
    if jobs.is_dir() and not jobs.is_symlink():
        for f in sorted(jobs.iterdir()):
            if not f.is_file():
                continue
            if (f.name.endswith(".json") and not f.name.endswith(".status.json")) \
                    or f.name.endswith(".claim"):
                add.append(f".repeat_jobs/{f.name}")
    if (Path(root) / VENDOR_DIR).is_dir():
        add.append(VENDOR_DIR)
    if (Path(root) / ".gitignore").is_file():
        add.append(".gitignore")
    prefixes = [n + "/" for n in _run_folders(root)] + [
        ".repeat_jobs/", VENDOR_DIR + "/", ".gitignore"]
    return add, prefixes


def cmd_publish(a):
    root = _root_arg(a.root)
    _need_linked(root)
    if not _gitignore_ok(root):
        raise SyncError("The cloud setup has not been done in this research folder, so "
                        "nothing was saved (without it, favorites and notes could be "
                        "uploaded). Run the cloud setup first.")
    add, prefixes = _publish_paths(root)
    folders = _run_folders(root)
    left = []
    if (root / ".repeat_jobs").is_symlink():
        # WHY named and not saved: what it points to is not in this folder.
        left.append((".repeat_jobs", _NOT_A_FILE["120000"]))
    for name in _linked_run_folders(root):
        # WHY named and not saved: what it points to is not in this folder.
        left.append((name, _NOT_A_FILE["120000"]))
    committed = _commit(root, add, [], f"Save marketing research ({len(folders)} run "
                        f"folders)", prefixes, left)
    not_saved = [rel for rel, _ in left]
    has_remote_branch = _fetch(root)
    if has_remote_branch and _has_commits(root):
        bad = _update_through_link(root, _remote_ref()) or _saved_through_link(root)
        if bad is not None:
            raise SyncError(f"Nothing was sent: {_link_words(bad)}. The research is still "
                            f"on this computer. Replace the link with a plain folder, then "
                            f"publish again.")
        rc, _, err = _git(root, "pull", "-q", "--rebase", REMOTE, BRANCH)
        if rc != 0:
            _git(root, "rebase", "--abort")
            changed = _changed_since_save(root)
            if changed:
                raise SyncError(f"Nothing was sent: {_changed_words(changed)}, so the newer "
                                f"research in the repository could not be brought in first. "
                                f"The research is still on this computer. Put it back as it "
                                f"was when it was saved, then publish again.",
                                [_said("Updating from the repository", err)])
            raise SyncError("The research repository has changes that clash with this "
                            "folder, so nothing was sent. The research is still on this "
                            "computer.", [_said("Updating from the repository", err)])
    if not _has_commits(root):
        _out({"root": str(root), "committed": False, "pushed": False,
              "run_folders": folders, "not_saved": not_saved,
              "message": " ".join(x for x in ("There was nothing to save yet.",
                                              left_out_words(left)) if x)})
        return 0
    ok, err = _push(root)
    if not ok:
        what = ("could not be reached" if _unreachable(err) else "refused the save")
        raise SyncError(f"The research repository {what}, so nothing was sent. The "
                        f"research is still on this computer. {REACH_ADVICE} Then "
                        f"publish again.", [f"Sending the save: git said: {err or 'nothing'}"])
    # WHY counted from the repository and not from this folder: a run folder
    # whose files are all links has nothing saved, and must not be counted as
    # saved (the user would believe it is in the repository).
    empty = [f for f in folders if not _in_head(root, f)]
    n_saved = len(folders) - len(empty)
    first = (f"The research was saved to the repository ({n_saved} run folders)."
             if n_saved else "No run folder was saved to the repository.")
    none_saved = (f"Nothing of {', '.join(empty)} was saved: it holds no plain file."
                  if empty else "")
    _out({"root": str(root), "committed": committed, "pushed": True,
          "run_folders": folders, "not_saved": not_saved, "empty_run_folders": empty,
          "message": " ".join(x for x in (
              first, "Favorites and notes stayed on this computer.", none_saved,
              left_out_words(left)) if x)})
    return 0


def cmd_pull(a):
    root = _root_arg(a.root)
    if not _is_own_repo(root) or _remote_url(root) is None:
        _out({"root": str(root), "linked": False, "new_run_folders": [],
              "message": ("This research folder is not linked to a repository, so there "
                          "was nothing to update.")})
        return 0
    _check_stored_remote(root)
    br = _current_branch(root)
    if br != BRANCH:
        raise SyncError(f"This research folder is on {br or 'no branch'}, not {BRANCH}, "
                        f"so nothing was updated.")
    before = set(_run_folders(root))
    if not _fetch(root):
        _out({"root": str(root), "linked": True, "new_run_folders": [],
              "message": "The repository has nothing saved yet, so there was nothing new."})
        return 0
    bad = _update_through_link(root, _remote_ref())
    if bad is not None:
        raise SyncError(f"The update stopped, and nothing was changed: {_link_words(bad)}. "
                        f"Replace the link with a plain folder, then update again.")
    if _has_commits(root):
        rc, _, err = _git(root, "merge", "-q", "--ff-only", _remote_ref())
    else:
        rc, _, err = _git(root, "checkout", "-q", "-B", BRANCH, _remote_ref())
    if rc != 0:
        clash = _overwritten_folders(err)
        if clash:
            raise SyncError(
                "The update stopped, and nothing was changed: this computer has its own "
                f"copy of {', '.join(clash)} that was never saved to the repository, and "
                f"the repository has one with the same name.")
        if "diverg" in err.lower() or "not possible to fast-forward" in err.lower() \
                or "fast-forward" in err.lower():
            raise SyncError("The update stopped, and nothing was changed: this computer "
                            "has saved changes that are not in the repository yet. "
                            "Publish them first.")
        raise SyncError("The update stopped, and nothing was changed.",
                        [_said("Updating from the repository", err)])
    new = sorted(set(_run_folders(root)) - before)
    _out({"root": str(root), "linked": True, "new_run_folders": new,
          "message": (f"{len(new)} new run folder(s) arrived from the repository."
                      if new else "Already up to date: nothing new in the repository.")})
    return 0


def _overwritten_folders(err):
    names, grab = [], False
    for ln in err.splitlines():
        if "would be overwritten" in ln:
            grab = True
            continue
        if grab:
            if ln.startswith(("\t", " ")) and ln.strip():
                top = ln.strip().split("/")[0]
                if top not in names:
                    names.append(top)
            else:
                grab = False
    return names


_PROMPT = (
    "Run my scheduled marketing research repeat, unattended, in the cloud. From the "
    "repository folder, run exactly this one command:\n"
    "python3 .marketing-scraping/plugin/{skill}/scripts/repeat.py run-scheduled --job "
    ".repeat_jobs/{job} --publish git\n"
    "If the command moves to the background because it takes long, wait for it with:\n"
    "python3 .marketing-scraping/plugin/{skill}/scripts/repeat.py wait --job "
    ".repeat_jobs/{job}\n"
    "and call that again until it says the run finished. Never end your turn while the "
    "run is going.\n"
    "Then print its message and report word for word, each under its own label. Do not "
    "ask questions. Do not retry, resume or re-run anything. Never edit any file. Never "
    "change or drop any part of the command. Never print, echo or read environment "
    "variables. Never open an app or a browser. If a notification tool is available, "
    "send the message with it; if not, write exactly: \"No notification tool is "
    "available in this session, so this was not sent to your phone.\" Then stop."
)

_AD_HOSTS = ("*.fbcdn.net", "*.licdn.com", "*.cdninstagram.com",
             "tpc.googlesyndication.com", "displayads-formats.googleusercontent.com")


def _pain_config(job_path, job):
    raw = job.get("last_run_dir")
    if not isinstance(raw, str) or not raw or "/" in raw or "\\" in raw:
        return {}
    folder = job_path.parent.parent / raw
    try:
        cfg = json.loads((folder / "PAIN_CONFIG.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _trustpilot_domains(job_path, job):
    doms = _pain_config(job_path, job).get("trustpilot_domains") or []
    if not isinstance(doms, list):
        return []
    return [d for d in doms if isinstance(d, str) and d and "/" not in d]


def _store_hosts(job_path, job):
    """The app store hosts a pain month's free lookups reach.

    WHY: before each paid App Store or Google Play step the
    tool looks the apps up for free on the store itself. The checklist never
    listed those hosts, so in a cloud environment that allows only the listed
    ones, a month with App Store or Google Play apps could never look them up.
    Listed only when the source is on and an app is set, as the run decides."""
    cfg = _pain_config(job_path, job)
    sources = cfg.get("sources") or ["maps"]
    if not isinstance(sources, list):
        return []
    hosts = []
    if "appstore" in sources and (cfg.get("appstore_names") or cfg.get("appstore_app_ids")):
        hosts.append("itunes.apple.com")
    if "googleplay" in sources and (cfg.get("googleplay_names")
                                    or cfg.get("googleplay_app_ids")):
        hosts.append("play.google.com")
    return hosts


def cmd_routine_prompt(a):
    try:
        job_path = Path(a.job).expanduser().resolve()
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError):
        raise SyncError(f"The monthly repeat's approval {a.job!r} could not be read.")
    if not isinstance(job, dict):
        raise SyncError(f"The monthly repeat's approval {a.job!r} could not be read.")
    skill = TOOL_TO_SKILL.get(job.get("tool"))
    if skill is None:
        raise SyncError("The approval does not name one of the three research tools.")
    if job.get("mode") != "cloud":
        raise SyncError("This repeat is set to run on this computer, not in the cloud, so "
                        "there is no cloud routine to set up.")
    prompt = _PROMPT.format(skill=skill, job=job_path.name)
    domains = ["api.apify.com"]
    if job.get("tool") == "ad":
        domains += list(_AD_HOSTS)
    extra = ""
    if job.get("tool") == "pain":
        tp = _trustpilot_domains(job_path, job)
        stores = _store_hosts(job_path, job)
        why = []
        if tp:
            domains += tp
            why.append("the company websites are there because this research checks "
                       "their Trustpilot pages")
        if stores:
            domains += stores
            why.append(f"{' and '.join(stores)} {'is' if len(stores) == 1 else 'are'} "
                       f"there because this research looks up your apps there, for "
                       f"free, before paying for their reviews")
        if why:
            extra = " (" + "; ".join(why) + ")"
    checklist = [
        ("Create an empty private repository on GitHub (no README, so its first branch "
         "becomes the default) and give me its address. Make sure Claude has access to "
         "it (the Claude GitHub App, or /web-setup)."),
        ("At claude.ai/code, create a NEW cloud environment used only for this. Network "
         "access: Custom. Allowed domains: " + ", ".join(domains) + extra +
         ". Keep \"Also include default list\" off unless it is needed."),
        ("In that environment's variables, type APIFY_TOKEN= and paste your Apify token "
         "yourself. Never paste it into the chat for this routine: type it only into that "
         "environment's variables. Anything that runs in that environment "
         "can read it, so use the environment only for this."),
        ("Create the routine: this repository, that environment, the prompt above, and "
         "remove every connector (all of your connected connectors are included by "
         "default)."),
        ("Set the schedule: pick the closest preset, then run /schedule update in the "
         "command line to set a monthly time, such as the 1st at 9:07 (a few minutes "
         "past the hour)."),
    ]
    _out({"job": job_path.name, "skill": skill, "prompt": prompt, "checklist": checklist,
          "message": ("Quote the prompt and the checklist to the user exactly as printed. "
                      "The user does the checklist steps themselves.")})
    return 0


# ---------------------------------------------------------------------------
# Library: claim before spending, save after
# ---------------------------------------------------------------------------

def _claim_rel(job_path):
    stem = job_path.name[:-5] if job_path.name.endswith(".json") else job_path.name
    return f".repeat_jobs/{stem}.claim"


def _started_text(epoch):
    try:
        return datetime.fromtimestamp(float(epoch), timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OverflowError, OSError):
        return "at an unknown time"


def claim(job_path, check=None, now=None):
    """Claim this month's run before anything is spent.

    job_path: the job file as given (<root>/.repeat_jobs/<job>.json), NOT
              resolved.  WHY: resolving follows a link on the way (a folder
              .repeat_jobs that is a link), which would make the place it
              points to the research folder; the links are checked here.
    check:    called after the folder is brought up to date; returns a list of
              paths that must equal the saved version in the repository (the
              approval and the settings the run will use) and an optional
              refusal message of its own.  WHY after the update: the saved
              version is what the user approved; a copy edited in this session
              (for example a raised ceiling) must never be what spends.

    Returns a context dict for save().  Raises SyncError with a plain message
    when the run must not go ahead; nothing has been spent then."""
    now = time.time() if now is None else now
    given = Path(job_path)
    job_path = Path(os.path.abspath(str(given)))
    root = job_path.parent.parent
    crel = _claim_rel(job_path)

    def _no_links():
        # WHY before anything and again after the update: a link found here
        # stops the run at $0; the update itself may bring one.
        try:
            same = job_path.resolve() == given.resolve()
        except (OSError, RuntimeError, ValueError):
            same = False
        if not same:
            raise SyncError(f"The path given for {job_path.name} goes through a link to "
                            f"another place on this computer, so this cloud month did not "
                            f"run and spent nothing.")
        for rel in (f".repeat_jobs/{job_path.name}", crel):
            bad = _link_on_the_way(root, rel)
            if bad == crel:
                raise SyncError("The file that reserves this month for one run of this "
                                "repeat is a link (a shortcut to another place on this "
                                "computer), so this cloud month did not run and spent "
                                "nothing.")
            if bad is not None:
                raise SyncError(f"This cloud month did not run and spent nothing: "
                                f"{_link_words(bad)}. Replace it with a plain folder.")

    _no_links()
    if not _is_own_repo(root) or _remote_url(root) is None:
        raise SyncError("This research folder is not linked to a repository, so this "
                        "cloud month could not be saved and did not run.")
    try:
        _check_stored_remote(root)
        has_branch = _fetch(root)
    except SyncError as exc:
        raise SyncError(f"{exc} This cloud month did not run and spent nothing.",
                        exc.troubleshooting)
    if not has_branch:
        raise SyncError("The repository has nothing saved on the branch "
                        f"{BRANCH}, so this cloud month did not run.")
    bad = _update_through_link(root, _remote_ref())
    if bad is not None:
        raise SyncError(f"This cloud month did not run and spent nothing: "
                        f"{_link_words(bad)}. Replace it with a plain folder.")
    br = _current_branch(root)
    if br != BRANCH:
        rc, _, err = _git(root, "checkout", "-q", BRANCH)
        if rc != 0:
            raise SyncError(f"This machine could not switch to the branch {BRANCH}, so "
                            f"this cloud month did not run and spent nothing.",
                            [_said("Switching branch", err)])
    rc, _, err = _git(root, "merge", "-q", "--ff-only", _remote_ref())
    if rc != 0:
        raise SyncError("The research folder on the machine this run used could not be "
                        "brought up to date with your research repository, so this cloud "
                        "month did not run and spent nothing.",
                        [_said("Updating from the repository", err)])
    paths, own_refusal = (check() if check else ([], None))
    if own_refusal:
        raise SyncError(own_refusal)
    for p in paths:
        try:
            rel = Path(p).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError, RuntimeError):
            raise SyncError("A file this run needs is outside the research folder, so "
                            "this cloud month did not run.")
        in_repo = _git_ok(root, "cat-file", "-e", f"{_remote_ref()}:{rel}") is not None
        rc, _, _ = _git(root, "diff", "--quiet", _remote_ref(), "--", rel)
        if not in_repo or rc != 0:
            raise SyncError(
                "This repeat's approval or its settings on this machine differ from the "
                "version saved in the repository, so this run was skipped and spent "
                "nothing. Only the saved version may run.")
    cpath = root / crel
    # WHY again: the update may have brought a link, and writing the claim
    # would write through it to another place.
    _no_links()
    took_over = None
    if cpath.exists():
        try:
            info = json.loads(cpath.read_text(encoding="utf-8"))
            started = float(info["started_epoch"])
        except (OSError, ValueError, TypeError, KeyError):
            info, started = {}, None
        if started is None:
            out = _git_ok(root, "log", "-1", "--format=%ct", _remote_ref(), "--", crel)
            try:
                started = float(out.strip())
            except (AttributeError, ValueError):
                started = now
        if now - started < CLAIM_STALE_SECONDS:
            raise SyncError(
                f"Another run of this repeat reserved this month (started "
                f"{_started_text(started)}) and may still be going, so this run was "
                f"skipped and spent nothing.")
        took_over = {"started": _started_text(started)}
    claim_id = uuid.uuid4().hex
    doc = {"job": job_path.name, "claim_id": claim_id, "started_epoch": now,
           "started": _started_text(now)}
    cpath.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    left = []
    try:
        _commit(root, [crel], [], f"Reserve this month's run of {job_path.name}",
                [crel], left)
    except SyncError as exc:
        raise SyncError(f"{exc} This cloud month did not run and spent nothing.",
                        exc.troubleshooting)
    if left:
        raise SyncError("The file that reserves this month for one run of this repeat "
                        "is not a plain file, so this cloud month did not run and spent "
                        "nothing.")
    ok, err = _push(root)
    if not ok:
        raise SyncError(
            "The research repository did not accept this run's reservation of this "
            "month (another run of this repeat may have reserved it first, or this machine "
            "cannot save to the repository), so this run was skipped and spent nothing.",
            [f"Sending the reservation: git said: {err or 'nothing'}"])
    return {"root": root, "job_path": job_path, "claim_rel": crel,
            "claim_id": claim_id, "started_epoch": now, "took_over": took_over}


def _free_from_text(epoch):
    """The first whole minute at which a claim made at epoch no longer blocks
    a run, as _started_text writes it.

    WHY rounded up, not down: claim() skips a run while the claim is less than
    CLAIM_STALE_SECONDS old, so a claim exactly 24 hours old no longer blocks.
    _started_text drops the seconds; rounded down, the minute stated could be
    up to 59 seconds before that moment, and a run started at the stated time
    would be skipped.  Rounded up, a run at the stated time always goes ahead."""
    try:
        secs = float(epoch) + CLAIM_STALE_SECONDS
        minute = int(secs // 60)
    except (TypeError, ValueError, OverflowError):
        return _started_text(None)
    if secs > minute * 60:
        minute += 1
    return _started_text(minute * 60)


def claim_kept_until(ctx):
    """When this run's claim stops blocking a new run, as words that follow
    "this month can be run again", or None when save() removed it from the
    repository and it no longer blocks one.

    WHY a claim is kept after a month that was collected and not saved, and
    not removed: in the cloud the collected month is lost with the machine,
    so a new run collects it again and is charged again.  Removing the claim
    would let that second payment start at once, and as many times as the run
    is started; kept, a new run is skipped at $0 until the claim is 24 hours
    old, the same rule as for a run that may still be going.  The report must
    then say that time, not "can be run again".

    WHY this run's own time even when save() found a claim in this run's copy
    that is not its own (ctx["claim_mine"] False): save() never sends the
    claim file then, so the repository still holds the claim this run sent at
    its start, unless something else replaced it there meanwhile.  The caller
    says that it cannot tell which (repeat._save_month)."""
    if ctx.get("claim_released"):
        return None
    if not isinstance(ctx.get("started_epoch"), (int, float)):
        return "24 hours after this run reserved this month"
    return "from " + _free_from_text(ctx["started_epoch"])


def _clashing_paths(root):
    """The paths a failed update left unmerged (the files that clash), sorted;
    None when git could not say."""
    rc, out, _ = _git(root, "diff", "--name-only", "-z", "--diff-filter=U")
    if rc != 0:
        return None
    return sorted(p for p in out.split("\0") if p)


def _clash_words(ctx, clashed, crel, month):
    """Why the update before save()'s second try failed, in plain words that
    state only the cause git's record of the clashing files proves.

    WHY by the files: the words used to blame "a folder of the
    same name" for every clash, also when the clash was on this month's
    reservation (another run took the month over), where there is no month
    folder at all.  A cause the code cannot prove is never stated as fact:
    when the record is missing or names something else, every possibility
    is said.  Sets ctx["claim_clash"] when the reservation clashed, so the
    report does not then say this run's reservation stays in the repository."""
    def _in_month(p):
        return month is not None and (p == month or p.startswith(month + "/"))

    known = clashed is not None and all(p == crel or _in_month(p) for p in clashed)
    on_res = bool(clashed) and crel in clashed
    on_month = bool(clashed) and any(_in_month(p) for p in clashed)
    ctx["claim_clash"] = on_res
    res = ("this month's reservation in the research repository was changed there while "
           "this run was going (for example another run of this repeat took the month "
           "over)")
    folder = "a folder of the same name as this month's was saved there from somewhere else"
    if not known:
        return ("the newer research in the research repository could not be brought in "
                f"first, and this run cannot tell why: either {res}, or {folder}, or "
                "something else stopped the update")
    if on_res and on_month:
        return (f"the research repository has changes that clash with this month's save: "
                f"{res}, and {folder}")
    if on_res:
        return f"{res}, and this run's save clashes with that change"
    if on_month:
        return ("the research repository has a change that clashes with this month's save "
                "(for example a folder of the same name saved from somewhere else)")
    return ("the newer research in the research repository could not be brought in first, "
            "for a reason this run cannot name")


def save(ctx, run_dir=None):
    """Save the new month's folder (if any) and remove the claim; push.

    Returns (saved, plain reason when not saved).  Never raises for a git
    failure: a month that was collected and not saved must be REPORTED as not
    saved, and the caller decides the words and the exit code.

    Also fills ctx["left_out"] (what was not saved because it is not a plain
    file, as (path, words)) and ctx["troubleshooting"] (git's own words, and
    whether the one retry was made and how it ended), for the caller, and
    ctx["claim_note"] (plain words) when the claim could not be removed.

    Saved means a file of the month reached the repository: a month folder
    whose files are all links saves nothing, and is reported as not saved.
    A month folder reached through a link is saved at its real place when
    that is directly in the research folder (nothing outside is written), and
    reported as not saved otherwise; it is never saved from outside."""
    root, crel = ctx["root"], ctx["claim_rel"]
    trouble = ctx.setdefault("troubleshooting", [])
    left = ctx.setdefault("left_out", [])
    # WHY recorded: whether the claim is still in the repository decides what
    # the report may say about running this month again (claim_kept_until).
    ctx["claim_released"] = False
    add, prefixes = [], [crel]
    if run_dir is not None:
        try:
            rel = Path(run_dir).resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError, RuntimeError):
            return False, "the new month's folder is outside the research folder"
        if "/" in rel or rel.startswith("."):
            return False, "the new month's folder is not directly in the research folder"
        bad = _link_on_the_way(root, rel)
        if bad is not None:
            return False, f"the new month's folder could not be saved: {_link_words(bad)}"
        if (root / rel).is_dir():
            add.append(rel)
            prefixes.append(rel + "/")
    remove = []
    # WHY recorded apart from claim_mine: the report says whether the claim in
    # this run's copy was another run's or could not be read (words only).
    # WHY claim_link apart from claim_unreadable: a link and content
    # that does not parse need different action from the user, so the report
    # names which.  claim_unreadable stays True for both (reports and tests
    # read it); claim_link is the part of the path that is a link, or None.
    ctx["claim_unreadable"] = False
    ctx["claim_link"] = None
    ctx["claim_clash"] = False
    bad = _link_on_the_way(root, crel)
    if bad is not None:
        # WHY left in place: removing it would remove a file in another place.
        mine = False
        ctx["claim_unreadable"] = True
        ctx["claim_link"] = bad
        ctx["claim_note"] = (f"This month's reservation was not removed from your research "
                             f"repository ({_link_words(bad)}), so this run left it alone, "
                             f"and another cloud run of this repeat in the next 24 hours "
                             f"would be skipped.")
    else:
        try:
            mine = json.loads((root / crel).read_text(encoding="utf-8")).get("claim_id") \
                == ctx["claim_id"]
        except (OSError, ValueError, AttributeError):
            mine = False
            ctx["claim_unreadable"] = True
    ctx["claim_mine"] = mine
    if mine:
        remove.append(crel)
    name = Path(add[0]).name if add else "no new folder"
    try:
        _commit(root, add, remove, f"Save this month's run ({name})", prefixes, left)
    except SyncError as exc:
        trouble.extend(exc.troubleshooting)
        # WHY the reason when there is one: the caller puts it after "Why it
        # was not saved:", where "Nothing was saved: ..." would say it twice.
        return False, exc.reason or str(exc)
    month = add[0] if add else (rel if run_dir is not None else None)

    def _reached():
        # WHY from the repository: saved must mean the month's research is
        # there, not only that a save was sent.
        if month is None or _in_head(root, month):
            return True, None
        if any(p.startswith(month + "/") for p, _ in left):
            return False, ("none of this month's files could be saved: its folder holds "
                           "only links to other places on this computer, and a link is "
                           "never saved")
        return False, ("none of this month's files could be saved: its folder holds no "
                       "file that can be saved")

    ok, err = _push(root)
    if ok:
        ctx["claim_released"] = bool(remove)
        return _reached()
    trouble.append(f"Sending the save, first try: failed. git said: {err or 'nothing'}")
    # One update and one more try: the repository may have moved on (for example
    # the user saved from their computer meanwhile).  git only; nothing is spent.
    _fetch_ok = _git(root, "fetch", "-q", REMOTE,
                     f"+refs/heads/{BRANCH}:{_remote_ref()}")[0] == 0
    bad = _update_through_link(root, _remote_ref()) if _fetch_ok else None
    if bad is not None:
        trouble.append("One retry: not made, because the update would have written "
                       "through a link in the research folder.")
        return False, f"the research repository could not be updated: {_link_words(bad)}"
    rc, _, rerr = _git(root, "pull", "-q", "--rebase", REMOTE, BRANCH)
    if rc != 0:
        # WHY read before the abort: the abort clears git's record of which
        # files clashed, and that record is the only proof of the cause.
        clashed = _clashing_paths(root)
        _git(root, "rebase", "--abort")
        trouble.append("One retry: updating from the repository failed, so there was no "
                       f"second try. {_said('Updating', rerr)}")
        if _unreachable(rerr):
            return False, f"the research repository could not be reached. {REACH_ADVICE}"
        changed = _changed_since_save(root)
        if changed:
            return False, (f"{_changed_words(changed)}, so the newer research in the "
                           f"repository could not be brought in first")
        return False, _clash_words(ctx, clashed, crel, month)
    ok, err2 = _push(root)
    if ok:
        trouble.append("One retry: updated from the repository, and the second try saved it.")
        ctx["claim_released"] = bool(remove)
        return _reached()
    trouble.append(f"One retry: updated from the repository, and the second try was refused "
                   f"too. git said: {err2 or 'nothing'}")
    if _unreachable(err2):
        return False, f"the research repository could not be reached. {REACH_ADVICE}"
    return False, f"the research repository refused the save. {REACH_ADVICE}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message):
        _refuse(f"{self.prog}: {message}")


def main(argv=None):
    ap = _Parser(prog="research_sync.py",
                 description="Keep a research folder in step with its git repository.")
    sub = ap.add_subparsers(dest="cmd")
    for name in ("status", "cloud-setup", "publish", "pull"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
    p = sub.add_parser("link")
    p.add_argument("--root", required=True)
    p.add_argument("--remote", required=True)
    p = sub.add_parser("routine-prompt")
    p.add_argument("--job", required=True)
    a = ap.parse_args(argv)
    handlers = {"status": cmd_status, "link": cmd_link, "cloud-setup": cmd_cloud_setup,
                "publish": cmd_publish, "pull": cmd_pull,
                "routine-prompt": cmd_routine_prompt}
    if a.cmd not in handlers:
        _refuse("Give a command: status, link, cloud-setup, publish, pull or routine-prompt.")
    try:
        return handlers[a.cmd](a)
    except SyncError as exc:
        if exc.troubleshooting:
            _refuse(str(exc), troubleshooting=exc.troubleshooting)
        _refuse(str(exc))


if __name__ == "__main__":
    sys.exit(main())
