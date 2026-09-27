"""Write a believable usage log into a folder so the dashboard has something
to draw: two and a half days of status-bar and account readings ending a
minute ago, with the weekly meter ticking often enough for the tick-to-tick
estimate. Times are relative to now, so the dashboard never calls it stale.

    python tests/make_fixture.py DIR [--empty]

--empty writes nothing, for the "waiting for the first message" screen.
"""
import json
import math
import random
import sys
import time
from pathlib import Path

DAY = 86400


def build(folder, now=None):
    now = int(now or time.time())
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    R = random.Random(7)
    week_reset = now + 3 * DAY + 5 * 3600          # 3 days 5 hours left in the week
    start = now - int(2.5 * DAY)
    ratio = 9.0                                     # 5-hour points per weekly point
    week, h, win_end = 18.0, 0.0, None
    busy_until = 0
    lines, last = [], None
    t = start
    while t < now - 60:
        t += 120
        if R.random() < 0.012:
            busy_until = t + R.randint(20, 90) * 60
        if t < busy_until:
            if win_end is None or t >= win_end:
                win_end, h = t + 5 * 3600, 0.0
            step = R.uniform(0.2, 0.7)
            h += step
            week += step / ratio
        hr = win_end if win_end and t < win_end else t + 5 * 3600
        shown = h if win_end and t < win_end else 0.0
        reading = (math.floor(shown + 0.5), hr, math.floor(week + 0.5))
        if reading != last:
            lines.append({'t': t, 'h': reading[0], 'hr': reading[1], 'w': reading[2],
                          'wr': week_reset, 'src': 'account'})
            lines.append({'t': t + 30, 'h': math.floor(shown), 'hr': hr,
                          'w': math.floor(week), 'wr': week_reset})
            last = reading
    five = {'used_percentage': lines[-1]['h'], 'resets_at': lines[-1]['hr']}
    seven = {'used_percentage': lines[-1]['w'], 'resets_at': week_reset}
    saved = now - 60
    latest = {'saved_at': saved, 'five_hour': five, 'seven_day': seven,
              'last_check': {'at': saved, 'ok': True}}
    with open(folder / 'usage-history.jsonl', 'w', encoding='utf-8', newline='\n') as f:
        for line in lines:
            f.write(json.dumps(line) + '\n')
    (folder / 'usage-latest.json').write_text(json.dumps(latest), encoding='utf-8')
    return len(lines)


if __name__ == '__main__':
    target = Path(sys.argv[1])
    if '--empty' in sys.argv:
        target.mkdir(parents=True, exist_ok=True)
        print(f'empty folder {target}')
    else:
        print(f'{build(target)} readings written to {target}')
