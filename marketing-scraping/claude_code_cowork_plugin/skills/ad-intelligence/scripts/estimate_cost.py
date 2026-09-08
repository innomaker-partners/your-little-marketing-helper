#!/usr/bin/env python3
"""
Pre-run cost estimate from EMPIRICAL rates (not the actor page's advertised rate).

    python3 estimate_cost.py                 # print the whole rate table
    python3 estimate_cost.py B1 500          # estimate 500 SERP queries on B1
    python3 estimate_cost.py B4 1000 --rate 0.0005    # override with a test-calibrated rate

Pilot lesson: forecast the FULL run from the ACTUAL cost/record of a small TEST
run, not from the actor page. run_phase.py does this automatically; this CLI is
for planning by hand.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import EMPIRICAL_RATES, SAFETY_MARGIN, estimate


def main():
    args = sys.argv[1:]
    if not args:
        print("\nEmpirical Apify rates (incl. platform overhead):\n")
        for k, (actor, unit, rate, unv) in EMPIRICAL_RATES.items():
            print(f"  {k:4s} {actor:52s} ${rate}/{unit}" + ("  (UNVERIFIED)" if unv else ""))
        print(f"\nForecasts include a {SAFETY_MARGIN}x safety margin.\n")
        return

    stage = args[0]
    if len(args) < 2:
        sys.exit("Provide an expected count, e.g. `python3 estimate_cost.py B1 500`")
    count = float(args[1])
    rate_override = None
    if "--rate" in args:
        rate_override = float(args[args.index("--rate") + 1])

    usd, unit, rate, unv = estimate(stage, count, rate_override)
    actor = EMPIRICAL_RATES[stage][0]
    print(f"\n  Actor:    {actor}")
    print(f"  Expect:   {count:,.0f} {unit}s")
    print(f"  Rate:     ${rate}/{unit}"
          + (" (override)" if rate_override is not None else " (UNVERIFIED — calibrate)" if unv else ""))
    print(f"  Estimate: ~${usd:.2f}  (incl. {SAFETY_MARGIN}x margin)\n")


if __name__ == "__main__":
    main()
