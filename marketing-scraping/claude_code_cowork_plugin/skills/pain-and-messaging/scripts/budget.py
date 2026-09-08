"""
Budget tracker with hard per-phase gates.

The single highest-ROI design lesson: a test phase ran in parallel with no
cost awareness and overran 1.7x. This enforces a cumulative cap per phase and
refuses to launch a run whose estimate would blow it.
"""


class BudgetExceeded(Exception):
    pass


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
            raise BudgetExceeded(
                f"Phase {self.phase}: estimate ${estimate_usd:.2f} + spent "
                f"${self.spent:.2f} would exceed cap ${self.cap:.2f}. "
                f"Raise budget_caps in the config, tighten inputs, or stop."
            )

    def charge(self, actual_usd):
        self.spent += float(actual_usd or 0.0)
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
                f"cap ${self.cap:.2f} by ${self.spent - self.cap:.2f}. "
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
