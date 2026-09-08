"""
Budget tracker with hard per-phase gates.

Key lesson from early testing: a parallel run with no cost awareness overran
its budget 1.7x. This enforces a cumulative cap per phase and refuses to launch
a run whose estimate would blow it.
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
