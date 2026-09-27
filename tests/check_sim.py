"""Run the sessions-per-week accuracy simulation and fail if it's worse than
the published numbers allow.

    python tests/check_sim.py [both|bar|account]

Fails when the true value lands inside the "likely" range in fewer than 95%
of runs, or the median error is above 5%.
"""
import re
import subprocess
import sys
from pathlib import Path

SIM = Path(__file__).resolve().parent.parent / 'tools' / 'simulate_sessions.py'
MIN_COVERAGE, MAX_MEDIAN = 95, 5.0  # the same limits tools/simulate_sessions.py enforces


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'both'
    r = subprocess.run([sys.executable, str(SIM), mode], capture_output=True, text=True, timeout=900)
    print(r.stdout, r.stderr, sep='')
    if r.returncode != 0:
        return r.returncode
    cov = re.search(r'range: .*\((\d+)%\)', r.stdout)
    med = re.search(r'tick-to-tick error: median ([\d.]+)%', r.stdout)
    if not cov or not med:
        print('could not find the coverage / median lines in the simulation output')
        return 1
    coverage, median = int(cov.group(1)), float(med.group(1))
    if coverage < MIN_COVERAGE or median > MAX_MEDIAN:
        print(f'FAIL ({mode}): coverage {coverage}% (need >= {MIN_COVERAGE}%), '
              f'median error {median}% (need <= {MAX_MEDIAN}%)')
        return 1
    print(f'ok ({mode}): coverage {coverage}%, median error {median}%')
    return 0


if __name__ == '__main__':
    sys.exit(main())
