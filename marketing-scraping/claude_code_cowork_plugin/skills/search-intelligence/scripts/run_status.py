#!/usr/bin/env python3
"""
run_status.py -- how a run tells the Claude that started it that it has ended.

WHY this exists: a paid run can take longer than one Bash call may last, so
Claude may start it in the background. Nothing then told Claude when it ended,
and a Claude that ended its turn stopped the run midway, after money was spent.
This file is both halves of the answer:

  - the entry script (pain_intel.py, ad_intel.py, search_intel.py) calls
    start() once the run folder exists and finish() on every way out, so the
    run folder holds a small hidden status file, .run_status.json:
      while running:  state "running", the process id, the time that process
                      started, and the start time; the file's modification time
                      is refreshed every few seconds (the run's sign of life);
      at the end:     state "finished", how it ended, the exit code and the
                      closing lines exactly as the run printed them.
  - Claude runs `python3 run_status.py wait --workdir <run folder>`. Each call
    waits a short while (well under the Bash tool's own limit) and answers
    STILL RUNNING, FINISHED (with the closing lines), SILENT or DIED. Claude
    calls it again until the run has ended. Waiting in short calls is not a
    time limit: the run itself is never stopped or cut short, however long.

A run that dies without writing its end (killed, crashed, or its process gone
after the computer slept) is detected from its process (the process id plus the
time that process started) and reported in plain words, so waiting never goes
on forever on a dead run.

WHY a live process is never called dead: DIED invites a restart,
and a restart beside a run that is still alive can pay for the same step twice.
So while the run's own process still exists, `wait` never says DIED, however
long its sign of life has been silent (paused, the computer asleep, one long
step): it says SILENT instead, and that nothing may be started in the folder
while that process exists. For the same reason start() refuses a second run in
a folder whose run is still alive, before anything is spent.

WHY the run folder is locked: the entry script does its paid
steps in child processes (run_phase.py). Stopping the entry alone once left its
child working while the status said the run had ended, and a new run was let
into the folder beside it. And two runs started at the same moment both passed
a check-then-write guard. So start() takes an exclusive lock (flock) on the run
folder itself, atomically, and every step started through run_child() holds the
same lock: the folder counts as busy while ANY process of the run lives, and the
system drops the lock by itself when the last one ends (a crash leaves nothing
behind to clean up). No file is added to the folder. A stop by SIGTERM, SIGHUP or
SIGINT is passed on to the running step, and the run records its end only once that
step has ended; a second stop while it waits ends the step at once (SIGKILL). Where the folder cannot be locked (Windows, or a file system
without locks), the process-id checks above are used instead, as before.

WHY `wait` does not answer at once from an ended run: a run
started again in the same folder takes a moment to claim it, and a `wait` called
in that moment read the previous run's end and printed its closing lines as if
they were the new run's. So when a call finds the run already ended, it watches
the folder for SETTLE seconds first; a run that claims the folder in that time
is followed instead.

Each skill ships its own identical copy (a skill may be installed alone);
A development test pins the three copies together. Stdlib only.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Python floor: a Mac's own python3 is 3.9. On anything older, say so in one
# plain line instead of failing later with a traceback.
if sys.version_info < (3, 9):
    sys.exit("This tool needs Python 3.9 or newer, and this is Python %d.%d. "
             "A Mac already has a new enough one: /usr/bin/python3" % sys.version_info[:2])

try:
    import fcntl
except ImportError:  # Windows: no folder lock; the process-id checks are used
    fcntl = None

STATUS_NAME = ".run_status.json"

# The run refreshes its sign of life this often (seconds).
HEARTBEAT_EVERY = 10
# A sign of life older than this means the run has gone silent.
STALE_AFTER = 45
# When the sign of life is old, how long one wait call watches for it to come
# back before it says the run is silent. WHY a grace: after the computer sleeps,
# the run's sign of life is old until the run gets to refresh it (a few seconds).
GRACE = 60
# How long one wait call may block. The Bash tool stops a command after two
# minutes unless told otherwise, so this stays well under that.
WAIT_PER_CALL = 90
POLL_EVERY = 2
# When a wait call finds the run already ended (or dead), it first watches the
# folder this long for a run that is just starting there, polling this often.
# WHY 5 s: a run claims its folder 0.05 to 0.08 s after its command starts, and
# 0.17 to 0.21 s with no compiled Python cache (measured on the development
# machine); 5 s leaves over twenty times that for a slow machine, and
# costs a finished run's answer only those few seconds.
SETTLE = 5
SETTLE_POLL = 0.05
# An answer about a run that had already ended this long before the call began
# says so, and that nothing started since is running (see _earlier_note).
EARLIER_AFTER = 60
# A run's claim retries this many times, this far apart, before it counts the
# folder as busy. WHY: a `wait` checking the lock holds it (shared) for a few
# microseconds, and that alone must never refuse a run. Kept short (20 ms in all)
# so a run that is refused because another holds the folder is refused at once.
CLAIM_TRIES = 10
CLAIM_RETRY = 0.002

# When this process loaded this module: near its start, as the entry scripts import
# it before anything slow. WHY: two runs started together in a folder
# where every step is already done each hold the folder for a split second, so the
# one that loses the claim can find the folder free again once the other has ended.
# A run that claimed the folder AFTER this process began was started at the same
# moment as this one, and this one then refuses instead of running a second time.
_BEGUN = time.time()

# Exit code of a run refused because another run is still alive in its folder.
# 3 is what the entry scripts already use for "refused before spending anything".
REFUSED_EXIT = 3

# The signals that end a run from outside and are recorded before it ends.
# SIGTERM: a plain `kill`, or a tool stopping a background command. SIGHUP: the
# terminal or session that started the run closed. SIGINT: Ctrl+C, or
# `kill -INT`. WHY SIGINT too: Python turns it into KeyboardInterrupt, which could land
# while the run was waiting for a stopped step and leave that step alive with the run
# recorded as ended. As a stop like the others, it is passed on and waited for.
_RECORDED_SIGNALS = ("SIGTERM", "SIGHUP", "SIGINT")

_state = {"path": None, "doc": None, "stop": None, "thread": None, "signals": {},
          "lock": None, "children": set(), "stopping": None}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write(path, doc):
    """Replace the status file atomically, so a reader never sees half of it."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _heartbeat(path, stop):
    while not stop.wait(HEARTBEAT_EVERY):
        try:
            os.utime(path, None)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Is the run's process still there?
# ---------------------------------------------------------------------------

def _proc_probe(pid):
    """What the system says about a process: {"start": text, "zombie": bool}, or
    None when it cannot tell (the process is not listed, or neither /proc nor ps
    is available).

    "start" is when the process started, as the system records it. WHY: a process
    id is reused. A run that died long ago can leave its number to an unrelated
    program, and the number alone would then say the run is alive for ever. The
    number plus its start time names one process only. The text is compared only
    with text made the same way (the "proc:" or "ps:" prefix), never parsed.
    Linux: /proc/<pid>/stat (field 3 is the state, field 22 the start time).
    Elsewhere (macOS): `ps -o stat= -o lstart=`, with a fixed locale and time
    zone so two readings of one process always give the same text."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
        rest = raw.rsplit(")", 1)[1].split()
        return {"start": "proc:" + rest[19], "zombie": rest[0] == "Z"}
    except (OSError, IndexError):
        pass
    try:
        r = subprocess.run(["ps", "-o", "stat=", "-o", "lstart=", "-p", str(pid)],
                           capture_output=True, text=True,
                           env=dict(os.environ, LC_ALL="C", TZ="UTC"))
    except (OSError, ValueError):
        return None
    out = r.stdout.strip()
    if r.returncode != 0 or not out:
        return None
    stat, _, lstart = out.partition(" ")
    if not lstart.strip():
        return None
    return {"start": "ps:" + " ".join(lstart.split()), "zombie": stat.startswith("Z")}


def _gone_reason(doc):
    """Why the run's process certainly no longer exists, or None when it may.

    None means "it may still exist", and that is the safe answer: while it is
    None nothing calls the run dead and nothing may start beside it."""
    pid = doc.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        # Every status file start() writes names this process. Never probe 0 or a
        # negative number: they address whole groups of processes.
        return "its status file names no process"
    if os.name == "nt":
        return None  # no safe probe: on Windows os.kill(pid, 0) would send Ctrl+C
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "its process is gone"
    except OSError:
        pass  # it exists (for example, owned by another user)
    probe = _proc_probe(pid)
    if probe is None:
        return None
    if probe["zombie"]:
        return "its process is gone"  # ended; only its exit status is left
    rec = doc.get("proc_start")
    if (isinstance(rec, str) and rec.split(":", 1)[0] == probe["start"].split(":", 1)[0]
            and rec != probe["start"]):
        return ("its process is gone (its process number now belongs to a different "
                "program)")
    return None


def _identity_confirmed(doc):
    """True when the process now holding the run's process id is known to be the
    run itself (the same start time was read at the start and now).

    WHY it can be unknown: some sandboxes (Claude Code's own macOS sandbox among
    them) do not let a program read another program's start time, so the start
    time is not recorded. The process id alone then cannot tell the run from a
    program that was later given the same number, and the answer says so rather
    than asking the user to stop a process that may not be the run."""
    rec = doc.get("proc_start")
    if not isinstance(rec, str) or os.name == "nt":
        return False
    probe = _proc_probe(doc.get("pid"))
    return probe is not None and probe["start"] == rec


def _identity_caution(doc):
    pid = doc.get("pid")
    return (f"This computer did not let the check confirm that process id {pid} is still "
            f"the run (another program may have been given that number since): look at "
            f"what process {pid} is before stopping it.")


# ---------------------------------------------------------------------------
# The run folder's lock: held while any process of the run lives
# ---------------------------------------------------------------------------

def _claim(folder):
    """Take the run folder's lock for this run: ("held", fd), ("busy", None) when
    a process of another run holds it, or ("unsupported", None) when this system
    or file system cannot lock the folder.

    The lock is flock() on the folder itself: atomic (two runs cannot both get
    it), inherited by the steps run_child() starts, and dropped by the system
    when the last process holding it ends, however it ends."""
    if fcntl is None:
        return "unsupported", None
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return "unsupported", None
    for _ in range(CLAIM_TRIES):
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return "held", fd
        except BlockingIOError:
            time.sleep(CLAIM_RETRY)
        except OSError:
            os.close(fd)
            return "unsupported", None
    os.close(fd)
    return "busy", None


def _lock_state(folder):
    """"held" when a process of a run holds the folder's lock, "free" when none
    does, "unknown" when the lock cannot be checked here. The check takes the lock
    shared for a moment and lets it go; it never blocks and never claims."""
    if fcntl is None:
        return "unknown"
    try:
        fd = os.open(str(folder), os.O_RDONLY)
    except OSError:
        return "unknown"
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return "free"
    except BlockingIOError:
        return "held"
    except OSError:
        return "unknown"
    finally:
        os.close(fd)  # closing it lets go of the shared lock


def run_child(cmd):
    """Run one step of the run as a child process and wait for it, as
    subprocess.run(cmd, check=False) did, and return its CompletedProcess.

    WHY not subprocess.run: (1) the child holds the run folder's lock too, so
    the folder stays claimed while the step works, even if this process is gone;
    (2) this process knows the child, so a SIGTERM or SIGHUP to it is passed on to
    the step and the run records its end only after the step has ended."""
    fd = _state.get("lock")
    kw = {"pass_fds": (fd,)} if fd is not None else {}
    with subprocess.Popen(cmd, **kw) as p:
        _state["children"].add(p.pid)
        try:
            p.wait()
        except BaseException:
            p.kill()  # as subprocess.run does (for example on Ctrl+C)
            raise
        finally:
            _state["children"].discard(p.pid)
    return subprocess.CompletedProcess(cmd, p.returncode)


# ---------------------------------------------------------------------------
# The entry script's side: start() and finish()
# ---------------------------------------------------------------------------

def _live_run_in(path):
    """The status of a run still alive in this folder (not this process), or None."""
    doc = _read(path)
    if doc is None or doc.get("state") != "running" or doc.get("pid") == os.getpid():
        return None
    return doc if _gone_reason(doc) is None else None


def _say_refused(folder, doc):
    if not (doc and doc.get("state") == "running" and _gone_reason(doc) is None):
        # The folder is locked, but not by a run whose main process is alive: a run
        # that is just starting, or a step of an earlier run that is still working.
        return "\n".join([
            f"REFUSED: another run is still working in {folder} (one that is just "
            f"starting, or a step of an earlier run that has not ended yet). This run "
            f"was not started and spent nothing. Why: two runs in one folder could pay "
            f"for the same step twice.",
            f"To follow it, wait for it: python3 {Path(__file__).resolve()} wait "
            f"--workdir {folder}",
            "Start this run again only after that one has ended."])
    pid = doc.get("pid")
    lines = [f"REFUSED: a run is already going in {folder} (started "
             f"{doc.get('started', '?')}, process id {pid}) and has not finished. This run "
             f"was not started and spent nothing. Why: two runs in one folder could pay for "
             f"the same step twice."]
    age = _age(folder / STATUS_NAME)
    if age is not None and age > STALE_AFTER:
        lines.append(f"That run has shown no sign of life for {_mins(age)}: it may be paused "
                     f"or stuck. If the user wants it gone, they can stop that process "
                     f"themselves (process id {pid}); then this can be run again.")
        if not _identity_confirmed(doc):
            lines.append(_identity_caution(doc))
    lines.append(f"To follow the run that is going, wait for it: python3 "
                 f"{Path(__file__).resolve()} wait --workdir {folder}")
    lines.append("Start this run again only after that one has ended.")
    return "\n".join(lines)


def _say_raced(folder, doc):
    return "\n".join([
        f"REFUSED: another run was started in {folder} at the same moment as this one "
        f"(it started {doc.get('started', '?')}), and only one of them runs. This run was "
        f"not started and spent nothing. Why: two runs in one folder could pay for the "
        f"same step twice.",
        f"To see how that run ended: python3 {Path(__file__).resolve()} wait --workdir "
        f"{folder}"])


def start(workdir, tool, dry_run=False, scheduled=False):
    """Record that a run has started in workdir.

    Refuses (prints why, exits REFUSED_EXIT) when another run is still alive in
    the same folder: this is called before any paid step, so the refusal spends
    nothing, and it writes nothing (the live run's status file is left alone).
    "Alive" means any process of that run, its steps included: the folder's lock
    says so (see _claim). A folder whose run finished, or whose processes are all
    gone (a crash), is run again as before: that is how an interrupted run is
    resumed.

    Otherwise never fails the run: the status file is a convenience."""
    folder = Path(workdir)
    path = folder / STATUS_NAME
    claim, fd = _claim(folder)
    if claim == "busy":
        print(_say_refused(folder, _read(path)), flush=True)
        raise SystemExit(REFUSED_EXIT)
    prev = _read(path)
    if (claim == "held" and prev is not None and prev.get("lock")
            and prev.get("pid") != os.getpid()
            and isinstance(prev.get("started_ts"), (int, float))
            and prev["started_ts"] > _BEGUN):
        os.close(fd)
        print(_say_raced(folder, prev), flush=True)
        raise SystemExit(REFUSED_EXIT)
    if claim != "held" or (prev is not None and not prev.get("lock")):
        # No lock here, or the folder's last status was written without one: the
        # process-id checks decide whether that run is still alive.
        holder = _live_run_in(path)
        if holder is not None:
            if fd is not None:
                os.close(fd)
            print(_say_refused(folder, holder), flush=True)
            raise SystemExit(REFUSED_EXIT)
    _state["lock"] = fd  # held until finish(), or until this process ends
    try:
        doc = {"state": "running", "tool": tool, "pid": os.getpid(),
               "proc_start": (_proc_probe(os.getpid()) or {}).get("start"),
               "lock": claim == "held",
               "started": _now_iso(), "started_ts": time.time(),
               "dry_run": bool(dry_run), "scheduled": bool(scheduled)}
        _write(path, doc)
        stop = threading.Event()
        t = threading.Thread(target=_heartbeat, args=(path, stop), daemon=True)
        t.start()
        _state.update(path=path, doc=doc, stop=stop, thread=t)
        _record_signals()
    except Exception:  # noqa: BLE001 - the status file is a convenience, never the run
        _state.update(path=None, doc=None, stop=None, thread=None)


def _record_signals():
    """Make SIGTERM and SIGHUP write the run's end before the run ends.

    WHY: without this a run stopped by a signal left its status file saying
    "running". Only signals left at their default are taken (one set to be
    ignored, as under nohup, stays ignored), and the handler then ends the
    process by the same signal, so its exit status is the signal's.

    WHY the step is stopped too, and waited for: the paid steps run
    in child processes. Before, the signal ended the entry script alone: its step
    kept working, possibly writing results into the folder, while the status
    already said the run had ended. Now the signal is passed on to the step that
    is running, and the end is recorded only once that step has ended. A step
    stopped midway leaves its Apify run's id behind (.pending_run.json), so running
    the same command again picks that Apify run up instead of paying for a new one.
    While the step is ending, the status still says running (with "stopping")."""
    prev = {}
    for name in _RECORDED_SIGNALS:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            cur = signal.getsignal(signum)
            # Python's own SIGINT default (KeyboardInterrupt) counts as the default;
            # a SIGINT set to be ignored (a run started with `&` by a script) stays so.
            if cur is signal.SIG_DFL or (name == "SIGINT"
                                         and cur is signal.default_int_handler):
                signal.signal(signum, _on_signal)
                prev[signum] = cur
        except (ValueError, OSError):  # not the main thread
            pass
    _state["signals"] = prev


def _restore_signals():
    prev, _state["signals"] = _state.get("signals") or {}, {}
    for signum, handler in prev.items():
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):
            pass


def _children_now():
    """This process's child processes: the steps run_child() started, plus any
    other child the system lists (for example a report step's `claude`)."""
    kids = set(_state["children"])
    try:
        r = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True)
        kids |= {int(x) for x in r.stdout.split() if x.isdigit()}
    except (OSError, ValueError):
        pass  # no pgrep here: the steps run_child() started are still known
    return kids


def _reap(pid):
    """Wait for a child process to end (no time limit: the step decides)."""
    while True:
        try:
            os.waitpid(pid, 0)
            return
        except ChildProcessError:
            return  # already reaped, or not our child
        except InterruptedError:
            continue


def _on_signal(signum, frame):  # noqa: ARG001
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = f"signal {signum}"
    if _state.get("stopping"):
        # A second stop while the run waits for its steps: end them at
        # once. WHY: a step that ignores the first stop, or hangs, kept the run waiting
        # forever, and a user who asks twice to stop expects it to stop. The first
        # handler, still waiting below, then reaps them and records the run as stopped
        # from outside, by the first signal, only once no step is left alive.
        for pid in _children_now():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return
    _state["stopping"] = name
    kids = _children_now()
    for pid in kids:
        try:
            os.kill(pid, signum)
        except OSError:
            pass
    path, doc = _state["path"], _state["doc"]
    if path is not None and doc is not None and kids:
        try:  # the status says the run is ending, and stays "running" until it has
            _write(path, dict(doc, stopping=name))
        except Exception:  # noqa: BLE001
            pass
    for pid in kids:
        _reap(pid)
    finish(128 + signum, signalled=name)
    try:
        signal.signal(signum, signal.SIG_DFL)
    except (ValueError, OSError):
        pass
    os.kill(os.getpid(), signum)


def exit_of(code):
    """(exit code, error text) for a SystemExit(code): the process exit code as a
    number (1 for a message, which Python prints and exits 1 with), and the
    message itself, if any."""
    if code is None:
        return 0, None
    if isinstance(code, int):
        return int(code), None
    return 1, str(code)


def _outcome(exit_code, reached_end, error):
    if error:
        return "failed"
    if exit_code == 0:
        return "complete" if reached_end else "ended_early"
    if exit_code == 3:
        return "refused"
    if exit_code == 4:
        return "stopped"
    return "failed"


def _scheduled_closing(workdir, started_ts):
    """A scheduled run's closing words are its notification (message and report),
    when this run wrote one; the entry script prints no bill line of its own then."""
    p = Path(workdir) / "notification.json"
    try:
        if p.stat().st_mtime < started_ts - 1:
            return ""
        n = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(n, dict):
        return ""
    return "\n".join(x for x in (str(n.get("message") or ""), str(n.get("report") or "")) if x)


def finish(exit_code, reached_end=False, closing="", error=None, signalled=None):
    """Record how the run ended, with its closing lines. Never fails the run."""
    path, doc = _state["path"], _state["doc"]
    if path is None or doc is None:
        _release()
        return
    _state.update(path=None, doc=None)
    _restore_signals()
    try:
        if _state["stop"] is not None:
            _state["stop"].set()
            _state["thread"].join(timeout=5)
        text = closing or ""
        if not text and doc.get("scheduled"):
            text = _scheduled_closing(path.parent, doc.get("started_ts") or 0)
        doc = dict(doc)
        doc.update({"state": "finished", "finished": _now_iso(),
                    "exit_code": int(exit_code),
                    "outcome": ("signalled" if signalled
                                else _outcome(exit_code, reached_end, error)),
                    "closing": text.strip("\n"),
                    "error": error or ""})
        if signalled:
            doc["signal"] = signalled
        _write(path, doc)
    except Exception:  # noqa: BLE001
        pass
    _release()


def _release():
    """Let go of the folder's lock (after the end is written; the system also
    drops it when this process ends)."""
    fd, _state["lock"] = _state.get("lock"), None
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The wait command (what Claude runs)
# ---------------------------------------------------------------------------

_ENDED = {
    "complete": "The run finished all its steps.",
    "ended_early": ("The run ended before its last step, without an error. Its own "
                    "output says why (read its last lines)."),
    "refused": "The run was refused before it spent anything.",
    "stopped": "The run stopped before finishing, at its spending limit or because a step failed.",
    "failed": "The run failed with an error and did not finish.",
    "signalled": ("The run was stopped from outside before it finished: its process was "
                  "told to end (for example, someone stopped it, or the window or session "
                  "running it was closed)."),
}

# The opening words of the closing lines when a run reached its end with a step
# left undone (each tool's _incomplete_report_lines writes them). In a real run
# a step the budget cap refused was never started, and the run still ended with
# exit code 0 and outcome "complete" (scheduled repeats and the notification read that
# value, so it stays). The status answer said "The run finished all its steps."
# and then quoted a closing that began "This run did not finish everything it was
# asked to do", two sentences that contradict each other. The answer now looks at the
# closing it is about to quote, and does not claim every step finished when that
# closing names one that did not.
_LEFT_UNDONE = "This run did not finish everything it was asked to do"
_ENDED_WITH_STEPS_LEFT = ("The run ended; some steps did not finish or were not started, "
                          "as its closing lines below say.")

# What to tell the user about a run that ended without finishing its steps and
# without printing its bill (it died, or it was stopped from outside).
_AFTER_UNFINISHED = [
    "Tell the user this in plain words. The folder holds only what was collected "
    "before it stopped, so do not present it as a finished run. A paid step that was "
    "running when it stopped may still have been charged: the exact amount is in the "
    "user's Apify console.",
    "Do not start the run again without the user's go-ahead. If they want to, the "
    "same command on the same folder skips the steps that had already finished and "
    "does not bill them again.",
]


def _read(path):
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _age(path):
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _mins(seconds):
    s = max(0, int(seconds))
    return f"{s // 60} min {s % 60} s" if s >= 60 else f"{s} s"


def _ended_sentence(doc):
    """The one sentence that says how the run ended. "complete" means the run reached
    its last line with exit code 0; it does not mean every step ran (see _LEFT_UNDONE),
    so that sentence is only used when the closing lines name no step left undone."""
    outcome = doc.get("outcome")
    if outcome == "complete" and _LEFT_UNDONE in str(doc.get("closing") or ""):
        return _ENDED_WITH_STEPS_LEFT
    return _ENDED.get(outcome, "The run has ended.")


def _say_finished(folder, doc, earlier_age=None):
    kind = "dry run (it spent nothing)" if doc.get("dry_run") else "run"
    lines = [f"FINISHED: the {kind} in {folder} has ended "
             f"(started {doc.get('started', '?')}, ended {doc.get('finished', '?')}, "
             f"exit code {doc.get('exit_code')}).",
             _ended_sentence(doc)]
    lines += _earlier_note(earlier_age)
    if doc.get("error"):
        lines.append(f"The error was: {doc['error']}")
    if doc.get("closing"):
        lines.append("Its closing lines, exactly as the run wrote them. Quote them as "
                     "they are; do not reword them, round them or add anything up:")
        lines.append("-----")
        lines.append(doc["closing"])
        lines.append("-----")
    elif doc.get("outcome") != "signalled":
        lines.append("It wrote no closing lines (a run that spends nothing prints no bill).")
    if doc.get("outcome") == "signalled":
        lines += _AFTER_UNFINISHED
    return "\n".join(lines)


def _earlier_note(age):
    """For an answer about a run that had already ended (or died) when this call
    began, long enough ago that it may not be the run the caller means."""
    if age is None or age < EARLIER_AFTER:
        return []
    return [f"That run had already ended {_mins(age)} before this check began. If a run "
            f"was started in this folder after that, it has not started: nothing is "
            f"running in this folder now. Read that command's own output to see why."]


def _say_died(folder, doc, why, earlier_age=None):
    return "\n".join([
        f"DIED: the run in {folder} stopped without finishing ({why}). It did not write "
        f"its end, so it was killed, crashed, or its process ended (for example "
        f"when the computer shut down).",
    ] + _earlier_note(earlier_age) + _AFTER_UNFINISHED)


def _say_silent(folder, doc, age):
    pid = doc.get("pid")
    caution = [] if _identity_confirmed(doc) else [_identity_caution(doc)]
    return "\n".join([
        f"SILENT: the run in {folder} has not finished, and its process (process id {pid}) "
        f"still exists, but it has shown no sign of life for {_mins(age)}. It may be "
        f"paused or stuck, or the computer may have been asleep.",
        "Do not start anything again in this folder while that process exists: a second "
        "run there could pay for the same step twice (and it would be refused).",
        "Tell the user this in plain words, and ask what they want. They can wait: then "
        "call this same command again, and it says STILL RUNNING if the run comes back, "
        "FINISHED when it ends, or DIED once its process is gone. Or they can stop that "
        f"process themselves (process id {pid}) if they want it gone.",
    ] + caution)


def _say_still_running(folder, doc, note):
    started = (doc or {}).get("started", "?")
    so_far = _mins(time.time() - ((doc or {}).get("started_ts") or time.time()))
    if note == "starting":
        # Plain words; "claimed" was the code's word for holding the folder.
        head = (f"STILL RUNNING: a run in {folder} is starting (it is using this folder "
                f"right now and has not written its first status yet).")
    elif note == "step":
        head = (f"STILL RUNNING: the run in {folder} started {started}. Its main process has "
                f"ended without finishing, but a step it started is still working in this "
                f"folder. Do not start anything in this folder until that step ends (a new "
                f"run there would be refused). When it ends, this says how the run ended.")
    else:
        head = (f"STILL RUNNING: the run in {folder} started {started} and is still working "
                f"({so_far} so far).")
        if note == "stopping":
            head += (f" It was told to stop ({doc.get('stopping')}) and is waiting for the "
                     f"step that was running to end; then it records how it ended. Telling "
                     f"it to stop a second time ends that step at once.")
    return (head + " Call this same command again now to keep waiting. Do not end your turn "
            "while the run is going, and do not stop it because it is slow: slow is normal.")


def cmd_wait(a):
    folder = Path(a.workdir).expanduser()
    path = folder / STATUS_NAME
    if not folder.is_dir():
        print(f"NO RUN FOUND: the folder {folder} does not exist. Check the --workdir "
              f"the run was started with.")
        return 2
    t0 = time.time()
    stale_since = None
    seen_live = False  # this call has seen a process of the run alive
    note = None
    while True:
        doc = _read(path)
        state = doc.get("state") if doc else None
        lock = _lock_state(folder)
        # An ended run is reported only once this call has seen the run alive, or has
        # watched the folder for SETTLE seconds (see the module docstring).
        settled = seen_live or time.time() - t0 >= SETTLE
        note = None
        live_doc = None
        if lock == "held":
            # A process of a run holds the folder: never FINISHED or DIED now.
            seen_live = True
            if state == "running" and _gone_reason(doc) is None:
                live_doc = doc
                note = "stopping" if doc.get("stopping") else None
            else:
                note = "step" if state == "running" else "starting"
                stale_since = None
        elif state == "finished":
            if settled:
                print(_say_finished(folder, doc, None if seen_live else _age(path)))
                return 0
        elif state == "running":
            gone = _gone_reason(doc)
            if (gone is None and doc.get("lock") and lock == "free"
                    and not _identity_confirmed(doc)):
                # The run held the folder's lock, and nothing holds it now: none of its
                # processes is left, whatever now has its process number.
                gone = "its process is gone, and nothing of it holds the run folder any more"
            if gone:
                if settled:
                    print(_say_died(folder, doc, gone, None if seen_live else _age(path)))
                    return 1
            else:
                seen_live = True
                live_doc = doc
        if live_doc is not None:
            # The run's own process still exists: never DIED from here on, only STILL
            # RUNNING or, after a long silence, SILENT (see the module docstring).
            age = _age(path)
            if age is not None and age > STALE_AFTER:
                stale_since = stale_since or time.time()
                if time.time() - stale_since >= GRACE:
                    print(_say_silent(folder, live_doc, age))
                    return 3
            else:
                stale_since = None
        # GRACE is shorter than one call, so a run that stopped showing signs of
        # life before this call began is judged within this call.
        if time.time() - t0 >= WAIT_PER_CALL:
            break
        time.sleep(POLL_EVERY if settled else SETTLE_POLL)
    if doc is None and not seen_live:
        print(f"NO RUN FOUND YET: nothing in {folder} says a run has started. If you "
              f"started one in the background a moment ago, call this again. If it still "
              f"says this, the run never started: read the background command's output.")
        return 2
    print(_say_still_running(folder, doc, note))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Wait for a run started in the background.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("wait", help="wait a short while for the run in a folder to end")
    w.add_argument("--workdir", required=True, help="the run folder (the run's --workdir)")
    a = ap.parse_args(argv)
    return cmd_wait(a)


if __name__ == "__main__":
    sys.exit(main())
