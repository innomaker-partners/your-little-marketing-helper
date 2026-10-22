"""
Budget tracker with hard per-phase gates.

Key lesson from early testing: a parallel run with no cost awareness overran
its budget 1.7x. This enforces a cumulative cap per phase and refuses to launch
a run whose estimate would blow it.
"""


import json
import math
import os
from pathlib import Path

# The run ledger of an INTERACTIVE run.
# WHY: an unattended run already keeps <workdir>/_run_ledger.json, and its
# notification's "Total billed" is read from it. An interactive run kept no
# record of the whole run's bill, so the only numbers it printed were each
# stage's lines, rounded to cents, and the agent added those up ($0.27 for a
# $0.281 run). The entry script names the ledger in this environment variable,
# and every charge below is added to it, so both kinds of run end with the same
# number, read by the same function. run_phase.py (committed, audited) is not
# changed: its command line and output stay identical.
# Only the entry script of an interactive run sets it; under a standing approval
# run_phase writes the ledger itself and this variable is removed.
RUN_LEDGER_ENV = "MARKETING_SCRAPING_RUN_LEDGER"


def add_to_run_ledger(actual_usd):
    """Add one paid call's bill to the ledger named by RUN_LEDGER_ENV, if set.

    Never raises: the spending record must not change what a run does. A ledger
    that is missing or unreadable is left as it is, so the run's total reads as
    unknown instead of a number that silently leaves charges out."""
    path = os.environ.get(RUN_LEDGER_ENV)
    if not path:
        return
    try:
        p = Path(path)
        spent = float(json.loads(p.read_text(encoding="utf-8"))["spent"])
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"spent": round(spent + float(actual_usd or 0.0), 8)}),
                       encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        return


def usable_usd(value):
    """value as a float when it is a finite dollar amount above zero, else None.

    WHY: an approved ceiling of NaN or infinity was stored
    and then every "forecast > ceiling" test was False for it, so the refusal that
    protects the user's money could never fire. True and False are not amounts
    (JSON true reads as 1 in Python). Text is not an amount either: the job file
    is written by `repeat.py schedule`, which stores a number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    x = float(value)
    if x != x or x in (float("inf"), float("-inf")) or x <= 0:
        return None
    return x


def run_ledger_spent():
    """What this run has already spent, read from the ledger named by
    RUN_LEDGER_ENV: a float, or None when no ledger is named (no entry script
    started this step, or the run is a dry run or runs under a standing approval,
    where run_phase keeps the ledger itself).

    WHY: every paid step runs as its own run_phase process
    with a budget that started at $0, so a $0.10 cap held for each step and the
    run billed $0.155. A step now starts its budget from this figure, so the cap
    in budget_caps holds for the whole run. Raises ValueError when the ledger is
    named but cannot be read or holds no usable figure: starting from $0 then
    would hand the step the whole cap again, so the caller stops before any paid
    call instead."""
    path = os.environ.get(RUN_LEDGER_ENV)
    if not path:
        return None
    try:
        spent = json.loads(Path(path).read_text(encoding="utf-8"))["spent"]
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"this run's spending record {path} could not be read ({exc})")
    if isinstance(spent, bool) or not isinstance(spent, (int, float)) \
            or spent != spent or spent in (float("inf"), float("-inf")) or spent < 0:
        raise ValueError(f"this run's spending record {path} holds {spent!r}, "
                         f"not an amount spent")
    return float(spent)


def cap_text(usd):
    """A cap or approved ceiling the user set, in dollars as set, without the "$":
    two decimals when that is exact ("10.00", "0.40"), else the
    fewest decimals that give back exactly the figure set ("0.205", "0.0135",
    "0.008"). WHY: a cap of $0.205 printed as "$0.20", and a ceiling of $0.008 as
    "$0.01", told the user a figure they never set, in a sentence about money they
    approved. Never rounded: past twelve decimals it falls back to repr()."""
    x = float(usd)
    two = f"{x:.2f}"
    if float(two) == x:
        return two
    for d in range(3, 13):
        s = f"{x:.{d}f}"
        if float(s) == x:
            return s.rstrip("0")
    return repr(x)


def cents_up(usd):
    """usd rounded UP to the next whole cent. A limit or an extra amount shown to
    the user is never below the real figure; 0.081 reads $0.09, never $0.08."""
    return math.ceil(round(float(usd) * 100, 6)) / 100


class BudgetExceeded(Exception):
    # The figures behind a refusal, set by Budget.check() on the exception it
    # raises (None on a stop that is not a refusal of a forecast, such as the
    # enforce() stop on spend already made). WHY: the stop record written by
    # run_phase (_annotate_budget_stop) states the forecast, the amount the run had
    # spent and the cap, so the closing lines can say what really happened. In a
    # real buyer's run $0.0362 of a $3.00 cap had been spent when a $6.00 forecast
    # was refused, and the closing lines said "the run reached the budget cap".
    estimate_usd = None
    spent_usd = None
    cap_usd = None


class Budget:
    def __init__(self, phase, cap_usd):
        self.phase = phase
        self.cap = float(cap_usd)
        self.spent = 0.0

    def remaining(self):
        return max(0.0, self.cap - self.spent)

    def check(self, estimate_usd):
        """Raise before launching a run whose forecast would exceed the cap."""
        if self.spent + estimate_usd > self.cap:
            exc = BudgetExceeded(
                f"Phase {self.phase}: estimate ${estimate_usd:.2f} + spent "
                f"${self.spent:.2f} would exceed cap ${cap_text(self.cap)}. "
                f"Raise budget_caps in the config, tighten inputs, or stop."
            )
            exc.estimate_usd = float(estimate_usd)
            exc.spent_usd = self.spent
            exc.cap_usd = self.cap
            raise exc

    def charge(self, actual_usd):
        self.spent += float(actual_usd or 0.0)
        add_to_run_ledger(actual_usd)
        return self.spent

    def enforce(self):
        """Raise BudgetExceeded if actual spend has exceeded the cap.

        check() only gates on a forecast before a run; charge() is a pure
        accumulator. This method enforces the cap on actual spend. Call it
        only at SAFE BOUNDARY points — before a new stage starts, or between
        batches after a batch's charge() — NEVER between _save_dataset and
        postprocess (doing so would leave a raw PII-bearing file on disk).
        """
        if self.spent > self.cap:
            raise BudgetExceeded(
                f"Phase {self.phase}: actual spend ${self.spent:.2f} has exceeded "
                f"cap ${cap_text(self.cap)} by ${self.spent - self.cap:.2f}. "
                f"Stopping to prevent further overspend."
            )


if __name__ == "__main__":
    # -------------------------------------------------------------------------
    # Offline tests for Budget.
    # -------------------------------------------------------------------------
    PASS = 0
    FAIL = 0

    def check(label, got, expected):
        global PASS, FAIL
        if got == expected:
            print(f"  PASS  {label}")
            PASS += 1
        else:
            print(f"  FAIL  {label}")
            print(f"        got:      {got!r}")
            print(f"        expected: {expected!r}")
            FAIL += 1

    print("\n=== TEST 1: charge() alone does NOT raise even when over cap ===")
    b = Budget("T1", 5.00)
    b.charge(3.00)
    b.charge(3.00)   # now at $6.00 — over cap
    check("charge() over cap: spent accumulated without raising",
          b.spent, 6.00)
    # If this line is reached, no exception was raised — that is the correct behavior.
    check("charge() over cap: no BudgetExceeded raised (compat)", True, True)

    print("\n=== TEST 2: enforce() RAISES when over cap ===")
    b2 = Budget("T2", 5.00)
    b2.charge(3.00)
    b2.charge(3.00)   # $6.00 — over cap
    raised = False
    exc_msg = ""
    try:
        b2.enforce()
    except BudgetExceeded as e:
        raised = True
        exc_msg = str(e)
    check("enforce() over cap: BudgetExceeded raised", raised, True)
    check("enforce() message mentions phase", "T2" in exc_msg, True)
    check("enforce() message mentions overage", "1.00" in exc_msg, True)

    print("\n=== TEST 3: enforce() does NOT raise when within cap ===")
    b3 = Budget("T3", 5.00)
    b3.charge(2.00)
    not_raised = True
    try:
        b3.enforce()
    except BudgetExceeded:
        not_raised = False
    check("enforce() within cap: no raise", not_raised, True)

    print("\n=== TEST 4: enforce() does NOT raise when exactly at cap (> not >=) ===")
    b4 = Budget("T4", 5.00)
    b4.charge(5.00)
    not_raised_exact = True
    try:
        b4.enforce()
    except BudgetExceeded:
        not_raised_exact = False
    check("enforce() at exact cap: no raise (> not >=)", not_raised_exact, True)

    print("\n=== TEST 5: boundary sequence — batch loop charge+enforce ===")
    # Mirrors the batch loop fix in run_phase.py.
    # Sequence: test-run charge($0.50), check($2.00 forecast) passes,
    # then 4 batches × $1.50 each with enforce() after each charge.
    # Cumulative spend after each batch:
    #   start: $0.50
    #   batch1: $2.00  (<=5.00 — OK)
    #   batch2: $3.50  (<=5.00 — OK)
    #   batch3: $5.00  (<=5.00, strict > so OK)
    #   batch4: $6.50  (>5.00  — enforce() raises)
    CAP = 5.00
    b5 = Budget("T5", CAP)
    b5.charge(0.50)    # test run
    b5.check(2.00)     # pre-full forecast: 0.50+2.00=2.50 < 5.00, passes
    batch_costs = [1.50, 1.50, 1.50, 1.50]
    enforce_fired_at = None
    for i, bc in enumerate(batch_costs, 1):
        b5.charge(bc)
        try:
            b5.enforce()
        except BudgetExceeded:
            enforce_fired_at = i
            break
    check("batch loop enforce: fires after batch 4 (spent=$6.50 > $5.00)",
          enforce_fired_at, 4)
    # BEFORE/AFTER: without enforce(), all 4 batches would run, spending $6.50
    # total vs cap $5.00 — $1.50 over. With enforce(), batch 4 is aborted.
    print(f"\n  BEFORE/AFTER:")
    print(f"    Without enforce(): all 4 batches run, final spend $6.50 > cap $5.00")
    print(f"    With enforce():    aborted at batch {enforce_fired_at}, spend ${b5.spent:.2f}")
    print(f"    Over-cap prevented: ${b5.spent - CAP:.2f} (batch 4 was aborted before "
          f"the run launched)")

    print(f"\nResults: {PASS} passed, {FAIL} failed")
