"""Accuracy check for the sessions-per-week estimate.

Simulates days of usage with a known answer (5-hour points per weekly
point), produces readings the way the two real sources do - the account
check rounds and runs every 2 minutes while the dashboard is open; the
status bar rounds down and is one message behind - and checks the
dashboard's estimate and its "likely" range against the truth.

    python tools/simulate_sessions.py [both|bar|account]

Exits with an error if the true value lands inside the "likely" range in
fewer than 95% of runs, or the median error is above 5%, so CI can run it.
"""
import importlib.util as u
import math
import random
import sys
MODE = sys.argv[1] if len(sys.argv) > 1 else 'both'

from pathlib import Path

s = u.spec_from_file_location('cu', Path(__file__).resolve().parent.parent / 'claude_pace.py')
m = u.module_from_spec(s)
s.loader.exec_module(m)


def rnd(x):
    return math.floor(x + 0.5)


def simulate(seed, days, ratio):
    R = random.Random(seed)
    week_reset = 7 * 86400
    W0 = R.uniform(5, 20)                        # weekly meter when tracking starts
    t, h, total = 0, 0.0, 0.0
    win_end = None
    samples, last_acct, last_bar = [], None, None
    lag_h = lag_hr = lag_w = None
    active_until = 0
    window_open = True
    while t < days * 86400:
        t += 60
        if R.random() < 0.004:                     # start a burst of use
            active_until = t + R.randint(10, 120) * 60
        if R.random() < 0.002:
            window_open = not window_open           # dashboard opened / closed
        active = t < active_until
        if active:
            if win_end is None or t >= win_end:     # new 5-hour window
                win_end, h = t + 5 * 3600, 0.0
            rate = R.uniform(0.1, 0.6)
            h += rate
            total += rate
        W = W0 + total / ratio
        hr = win_end if win_end else t + 5 * 3600
        if win_end and t >= win_end:
            h_now = 0.0                             # window over, meter shows 0 until next use
        else:
            h_now = h
        if MODE == 'bar':
            window_open = False
        if window_open and t % 120 == 0:            # account check every 2 min
            r = (rnd(h_now), hr, rnd(W))
            if r != last_acct:
                samples.append({'t': t, 'h': r[0], 'hr': r[1], 'w': r[2], 'wr': week_reset, 'src': 'account'})
                last_acct = r
        if active and t % 180 == 0 and MODE != 'account':                 # a Claude Code message every 3 min
            if lag_h is not None:                   # status bar: rounded down, one step behind
                r = (math.floor(lag_h), lag_hr, math.floor(lag_w))
                if r != last_bar:
                    samples.append({'t': t, 'h': r[0], 'hr': r[1], 'w': r[2], 'wr': week_reset})
                    last_bar = r
            lag_h, lag_hr, lag_w = h_now, hr, W
    return samples


trials, covered, errs, rough_errs, ready = 300, 0, [], [], 0
for seed in range(trials):
    ratio = random.Random(seed + 999).uniform(6, 14)
    days = random.Random(seed + 7).uniform(0.5, 4)
    e = m.estimate(simulate(seed, days, ratio))
    if e['method'] != 'ticks':
        continue
    ready += 1
    est = e['d5'] / e['d7']
    errs.append(abs(est - ratio) / ratio)
    if e['lo'] - 1e-9 <= ratio <= e['hi'] + 1e-9:
        covered += 1
    r = m.session_math([x for x in simulate(seed, days, ratio) if x['t'] < x['hr'] + 60 and x.get('src') == ('account' if MODE == 'account' else None)])
    if r['d7'] > 0:
        rough_errs.append(abs(r['d5'] / r['d7'] - ratio) / ratio)

if not ready:
    print('no simulated history reached the exact method')
    sys.exit(1)
errs.sort()
rough_errs.sort()
print(f'{ready} of {trials} simulated histories reached the exact method')
print(f'true value inside the "likely" range: {covered} of {ready} ({100 * covered / ready:.0f}%)')
print(f'tick-to-tick error: median {100 * errs[len(errs) // 2]:.1f}%, 90% of runs within {100 * errs[int(len(errs) * .9)]:.1f}%')
print(f'old rough method error on the same histories: median {100 * rough_errs[len(rough_errs) // 2]:.1f}%, '
      f'90% within {100 * rough_errs[int(len(rough_errs) * .9)]:.1f}%')
coverage, median = 100 * covered / ready, 100 * errs[len(errs) // 2]
if coverage < 95 or median > 5:
    print(f'FAIL ({MODE}): coverage {coverage:.0f}% (need >= 95%), median error {median:.1f}% (need <= 5%)')
    sys.exit(1)
