#!/usr/bin/env python3
"""Claude Usage - terminal dashboard for your Claude plan limits.

Standard library only. Launch with "Claude Usage.bat" or `python claude-usage.py`.

Reads two files that the Claude Code status bar (statusline.js) writes into
your Claude folder (~/.claude) every time a session refreshes:
  usage-latest.json    latest 5-hour and weekly percentages + reset times
  usage-history.jsonl  one line each time either number changes, kept forever

While it's open, the dashboard also asks your Claude account for the same two
numbers every 2 minutes and shows those: they're current even when you're in
the desktop app or on claude.ai, and the status bar's copy runs about a point
behind. They're saved to usage-account.json and the same history log. That
uses the login Claude Code saved in your Claude folder and sends it only to
Anthropic. If the account can't be reached, the status bar's numbers are
used. Turn it off with --no-account.

The plan name in the banner comes from asking Claude Code
(`claude auth status`) at startup and every 10 minutes.

Nothing is kept in memory between runs - every frame is worked out from those
files, so restarts lose nothing.

Keys: R = fetch fresh numbers from your account now, Q / Esc / Ctrl+C = quit.
Re-reads the files every 30 s.
Flags: --once        print one frame and exit (no account check)
       --no-account  never ask your account directly; status bar only
Env:   CLAUDE_CONFIG_DIR  your Claude folder, if it isn't ~/.claude
       CLAUDE_USAGE_DIR   read the usage files from another folder (for testing)
"""

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

try:
    import msvcrt
except ImportError:  # not Windows: Ctrl+C still quits
    msvcrt = None

CONFIG_DIR = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
DATA_DIR = Path(os.environ.get('CLAUDE_USAGE_DIR') or CONFIG_DIR)
LATEST = DATA_DIR / 'usage-latest.json'
HISTORY = DATA_DIR / 'usage-history.jsonl'
ACCOUNT_FILE = DATA_DIR / 'usage-account.json'

REFRESH_S = 30
PLAN_REFRESH_S = 600
ACCOUNT_EVERY_S = 120  # ask the account this often while the dashboard is open
ACCOUNT_FRESH_S = 2 * ACCOUNT_EVERY_S + 60  # account numbers younger than this win
SOURCE_SLACK = 1  # the status bar can read up to this many points below the account
STALE_S = 30 * 60
MIN_SESSION_PTS = 25   # 5-hour points needed before the sessions-per-week ratio is shown
MIN_WEEK_PTS = 3       # weekly points needed for the same
SAME_WINDOW_S = 3600   # reset times closer than this belong to the same window
FIVE_H_S = 5 * 3600
RESET_ERR = 2          # 5-hour points that can go unseen at each 5-hour reset
DAY_S = 86400
WEEK_S = 7 * DAY_S
MAX_TILES = 30


# ── colour ────────────────────────────────────────────────────────────────

COLOR = not os.environ.get('NO_COLOR')
RESET = '\x1b[0m' if COLOR else ''

ACCENT = (217, 119, 87)
GREEN = (94, 201, 125)
AMBER = (240, 184, 72)
RED = (236, 94, 94)
TEXT = (228, 228, 234)
MUTED = (150, 150, 162)
FAINT = (96, 96, 108)
TRACK = (50, 50, 60)
BORDER = (78, 78, 92)
WHITE = (255, 255, 255)


def fg(c):
    return f'\x1b[38;2;{c[0]};{c[1]};{c[2]}m' if COLOR else ''


def bg(c):
    return f'\x1b[48;2;{c[0]};{c[1]};{c[2]}m' if COLOR else ''


def paint(s, c, bold=False):
    if not COLOR:
        return s
    return ('\x1b[1m' if bold else '') + fg(c) + s + RESET


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def heat(t):
    """Green at the empty end, amber past the middle, red at the full end."""
    t = min(max(t, 0.0), 1.0)
    return mix(GREEN, AMBER, t / 0.6) if t < 0.6 else mix(AMBER, RED, (t - 0.6) / 0.4)


def level(pct):
    return GREEN if pct < 60 else AMBER if pct < 85 else RED


ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')


def vlen(s):
    return len(ANSI.sub('', s))


# ── formatting ────────────────────────────────────────────────────────────

def fmt_time(ts):
    return datetime.fromtimestamp(ts).strftime('%I:%M %p').lstrip('0')


def fmt_date(ts):
    d = datetime.fromtimestamp(ts)
    return f'{d:%b} {d.day}'


def fmt_when(ts, now):
    same_day = datetime.fromtimestamp(ts).date() == datetime.fromtimestamp(now).date()
    return fmt_time(ts) if same_day else f'{fmt_date(ts)}, {fmt_time(ts)}'


def fmt_span(sec):
    m = int(sec // 60)
    if m < 1:
        return 'under a minute'
    if m < 60:
        return f'{m}m'
    h, m = divmod(m, 60)
    if h < 24:
        return f'{h}h {m}m'
    d, h = divmod(h, 24)
    return f'{d}d {h}h'


def fmt_ago(sec):
    return 'just now' if sec < 60 else f'{fmt_span(sec)} ago'


def fmt_num(n):
    """Whole numbers from 10 up, one decimal below - for estimates."""
    return str(int(n + 0.5)) if n >= 10 else f'{n:.1f}'.removesuffix('.0')


def fmt_pct(n):
    """Always one decimal - for budget figures that are added and compared."""
    return f'{n:.1f}'.removesuffix('.0')


# ── bars ──────────────────────────────────────────────────────────────────

EIGHTHS = ' ▏▎▍▌▋▊▉'


def bar(frac, width, color=None, marker=None, red_from=None):
    """Solid bar with 1/8-cell precision on a dark track.

    color     fixed fill colour; default is a green→red heat gradient
    marker    0-1 position of a bright tick (e.g. today's stop point)
    red_from  0-1 position past which filled cells turn red (overspend)
    """
    frac = min(max(frac, 0.0), 1.0)
    cells = frac * width
    full = int(cells)
    part = int(round((cells - full) * 8))
    if part == 8:
        full, part = full + 1, 0
    mark = None if marker is None else min(int(min(max(marker, 0.0), 1.0) * width), width - 1)

    if not COLOR:
        s = ['█' if i < full else '░' for i in range(width)]
        if mark is not None:
            s[mark] = '|'
        return ''.join(s)

    out = []
    for i in range(width):
        t = (i + 0.5) / width
        c = color or heat(t)
        if red_from is not None and t > red_from:
            c = RED
        if i < full:
            ch, f, b = '█', c, TRACK
        elif i == full and part:
            ch, f, b = EIGHTHS[part], c, TRACK
        else:
            ch, f, b = ' ', TRACK, TRACK
        if i == mark:
            ch, f, b = '┃', WHITE, (c if i < full else TRACK)
        out.append(bg(b) + fg(f) + ch)
    return ''.join(out) + RESET


def tiles(used, total):
    """One square per session; the square in progress is half-filled."""
    total = max(1, min(int(total + 0.5), MAX_TILES))
    used = min(max(used, 0.0), total)
    s = []
    for i in range(total):
        if used >= i + 1:
            s.append(paint('■', ACCENT))
        elif used > i:
            s.append(paint('◧', ACCENT))
        else:
            s.append(paint('□', FAINT))
    return ' '.join(s)


# ── layout ────────────────────────────────────────────────────────────────

# How squeezed the frame is, 0 (roomy) to 4 (tightest). render() tries each
# level until the frame fits the window's height.
#   1  no blank spacer lines
#   2  one-line logo instead of the big one
#   3  merged rows and shorter wording
#   4  header folded into the logo line, least important details dropped
LVL = 0
NARROW = False  # window too narrow for dates and long wording; set by frame()

TOKEN = re.compile(r'(\x1b\[[0-9;?]*[A-Za-z])|(.)', re.S)


def clip(s, n):
    """Cut s to n visible characters, ending in … if anything was cut."""
    if vlen(s) <= n:
        return s
    out, count = [], 0
    for m in TOKEN.finditer(s):
        if m.group(1):
            out.append(m.group(1))
        elif count < n - 1:
            out.append(m.group(2))
            count += 1
    return ''.join(out) + RESET + '…'


def panel(title, rows, width):
    inner = width - 4
    head = clip(f'{paint("╭─", BORDER)} {paint(title, ACCENT, bold=True)} ', width - 2)
    lines = [head + paint('─' * max(width - vlen(head) - 1, 0) + '╮', BORDER)]
    for r in rows:
        if r == '' and LVL >= 1:
            continue
        r = clip(r, inner)
        lines.append(f'{paint("│", BORDER)} {r}{" " * max(inner - vlen(r), 0)} {paint("│", BORDER)}')
    lines.append(paint('╰' + '─' * (width - 2) + '╯', BORDER))
    return lines


def spread(left, right, width):
    return left + ' ' * max(width - vlen(left) - vlen(right), 1) + right


def wrap(text, width, color=MUTED):
    return [paint(line, color) for line in textwrap.wrap(text, width)] or ['']


# ── data ──────────────────────────────────────────────────────────────────

def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float('inf')


def is_limit(l):
    return isinstance(l, dict) and is_num(l.get('used_percentage'))


def read_history():
    try:
        raw = HISTORY.read_text(encoding='utf-8')
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            s = json.loads(line)
        except ValueError:
            continue
        if isinstance(s, dict) and all(is_num(s.get(k)) for k in ('t', 'h', 'hr', 'w', 'wr')):
            out.append(s)
    return sorted(out, key=lambda s: s['t'])


def session_math(samples):
    """How far each meter rose, over back-to-back readings taken inside the
    same 5-hour window AND the same week. weekly rise / 5-hour rise = share of
    the week one 5-hour point costs. claude.ai usage between readings moves
    both meters, so it still counts correctly.

    Readings come from the status bar and from the account, and the status
    bar can read a point lower, so a drop of up to SOURCE_SLACK isn't taken as
    a reset. Rises add up to (last - first) of each unbroken run, so only a
    run's two ends carry error: up to 1 point per meter when both ends come
    from one source (whole percentages), 1.5 when they mix. err carries that
    into the shown range."""
    d5 = d7 = err = 0.0
    in_run, whole, srcs = False, True, set()
    windows = set()

    def close_run():
        return (1 if len(srcs) <= 1 else 1.5) if in_run else 0

    for a, b in zip(samples, samples[1:]):
        same = (abs(b['hr'] - a['hr']) < SAME_WINDOW_S and b['h'] >= a['h'] - SOURCE_SLACK and
                abs(b['wr'] - a['wr']) < SAME_WINDOW_S and b['w'] >= a['w'] - SOURCE_SLACK)
        if not same:
            err += close_run()
            in_run = False
            continue
        if not in_run:
            in_run, srcs = True, {a.get('src')}
        srcs.add(b.get('src'))
        d5 += b['h'] - a['h']
        d7 += b['w'] - a['w']
        if b['h'] > a['h']:
            windows.add(round(b['hr'] / SAME_WINDOW_S))
        if any(float(x) != int(x) for x in (a['h'], b['h'], a['w'], b['w'])):
            whole = False
    err += close_run()
    return {
        'd5': d5, 'd7': d7, 'err': err if whole else 0, 'windows': len(windows),
        'weeks': len({round(s['wr'] / SAME_WINDOW_S) for s in samples}),
        'since': samples[0]['t'] if samples else None, 'count': len(samples),
    }


def five_hour_totals(samples):
    """Running total of 5-hour usage at each reading, carried across 5-hour
    resets: (segment, total, resets so far) per reading, or None for a stale
    reading. Within a window the total moves by the meter's change, so the
    one-point gap between the two sources cancels out; at a reset it adds the
    new window's reading. Readings that can't be trusted are skipped: one
    taken after its own window ended, a status-bar reading still showing an
    older window, or a drop bigger than the sources' one-point gap. A gap longer than
    a whole window might hide a window we never saw, so it starts a new
    segment."""
    out, seg, total, resets, cur = [], 0, 0.0, 0, None
    for s in samples:
        if s['t'] >= s['hr'] + 60:  # taken after its own window ended: shows a dead window
            out.append(None)
            continue
        if cur is not None:
            same = abs(s['hr'] - cur['hr']) < SAME_WINDOW_S
            if s['hr'] < cur['hr'] - SAME_WINDOW_S or (same and s['h'] < cur['h'] - SOURCE_SLACK):
                out.append(None)  # an older window, or a drop too big to be the sources' gap
                continue
            if same:
                total += s['h'] - cur['h']
            elif s['t'] - cur['t'] <= FIVE_H_S:
                total += s['h']
                resets += 1
            else:
                seg, total, resets = seg + 1, 0.0, 0
        cur = s
        out.append((seg, total, resets))
    return out


def tick_math(samples, totals, src):
    """Tick-to-tick measurement from one source (account or status bar).

    The weekly meter's true value is only known exactly at the moment it
    ticks up a point. From one tick to a later one, exactly (later - earlier)
    weekly points were used, with no rounding guesswork - only the 5-hour
    usage over the same stretch has to be measured. A tick happened somewhere
    between two readings, so the 5-hour total at the tick is taken as the
    middle of those two readings' totals, and half the gap goes into err,
    plus 1 for the 5-hour meter's own rounding and RESET_ERR per 5-hour reset
    in between (the old window's last bit of use can go unseen). Ticks are only compared within one source: the status bar runs
    a point behind, so its ticks land later than the account's."""
    stream = [(x, t) for x, t in zip(samples, totals) if x.get('src') == src and t is not None]
    groups = {}
    for (a, ta), (b, tb) in zip(stream, stream[1:]):
        if abs(b['wr'] - a['wr']) >= SAME_WINDOW_S or b['w'] <= a['w'] or ta[0] != tb[0]:
            continue
        groups.setdefault((round(b['wr'] / SAME_WINDOW_S), tb[0]), []).append(
            {'W': b['w'], 'c': (ta[1] + tb[1]) / 2, 'half': abs(tb[1] - ta[1]) / 2, 'r': tb[2]})
    d5 = d7 = err = 0.0
    most = 0
    for ticks in groups.values():
        most = max(most, len(ticks))
        if len(ticks) < 2:
            continue
        first, last = ticks[0], ticks[-1]
        d7 += last['W'] - first['W']
        d5 += last['c'] - first['c']
        err += first['half'] + last['half'] + 1 + RESET_ERR * (last['r'] - first['r'])
    return {'d5': max(d5, 0.0), 'd7': d7, 'err': err, 'ticks': most}


def estimate(samples):
    """Sessions-per-week inputs for the panels. Uses tick-to-tick when either
    source has seen the weekly meter tick twice in a row without a break;
    otherwise the rough whole-history method, once it has enough data."""
    # Rough method, one source at a time (mixing them biases it), keeping the
    # source that has seen more weekly movement. Readings taken after their
    # own 5-hour window ended show a dead window and are left out.
    live = [x for x in samples if x['t'] < x['hr'] + 60]
    rough = max((session_math([x for x in live if x.get('src') == src]) for src in ('account', None)),
                key=lambda r: (r['d7'], r['d5']))
    rough.update(since=samples[0]['t'] if samples else None, count=len(samples))
    totals = five_hour_totals(samples)
    best, most = None, 0
    for src in ('account', None):
        t = tick_math(samples, totals, src)
        most = max(most, t['ticks'])
        if t['d7'] >= 1 and t['d5'] > 0 and (best is None or t['err'] / t['d5'] < best['err'] / best['d5']):
            best = t
    m = dict(rough, ticks=most)
    if best:
        d5, d7, e = best['d5'], best['d7'], best['err']
        m.update(d5=d5, d7=d7, err=e, method='ticks', ready=True,
                 lo=max(d5 - e, 0) / d7, hi=(d5 + e) / d7)
    elif rough['d5'] >= MIN_SESSION_PTS and rough['d7'] >= MIN_WEEK_PTS:
        d5, d7, e = rough['d5'], rough['d7'], rough['err']
        m.update(method='rough', ready=True, lo=(d5 - e) / (d7 + e),
                 hi=(d5 + e) / (d7 - e) if d7 - e > 0 else None)
    else:
        m.update(method=None, ready=False)
    return m


def ratio_ready(m):
    return m['ready']


def today_budget(week, samples, now):
    """Days start at the weekly reset's clock time, so the week is exactly 7
    days. Allowance = weekly limit unused when today began, split evenly over
    the days left (today included). Fixed for the day: using less raises later
    days' allowances, going over lowers them.

    "When today began" is the last saved reading before the day boundary. If
    tracking started partway through today, the first reading today stands in
    and usage before it isn't counted as today's."""
    if not is_limit(week) or not is_num(week.get('resets_at')):
        return {'state': 'none'}
    reset = week['resets_at']
    if reset <= now:
        return {'state': 'reset'}
    week_start = reset - WEEK_S
    idx = min(max(int((now - week_start) // DAY_S), 0), 6)
    day_start = week_start + idx * DAY_S
    days_left = 7 - idx
    cur = week['used_percentage']

    this_week = [s for s in samples if abs(s['wr'] - reset) < SAME_WINDOW_S]
    before = [s for s in this_week if s['t'] <= day_start]
    after = [s for s in this_week if s['t'] > day_start]
    partial_from = None
    if before:
        base = before[-1]['w']
    elif idx == 0:
        base = 0
    elif after:
        base, partial_from = after[0]['w'], after[0]['t']
    else:
        base, partial_from = cur, now

    allowance = max(100 - base, 0) / days_left
    used = max(cur - base, 0)
    nxt = max(100 - cur, 0) / (days_left - 1) if days_left > 1 else None
    return {
        'state': 'ok', 'idx': idx, 'days_left': days_left, 'day_end': day_start + DAY_S,
        'base': base, 'cur': cur, 'allowance': allowance, 'used': used,
        'left': allowance - used, 'next': nxt, 'partial_from': partial_from,
    }


# ── panels ────────────────────────────────────────────────────────────────

def reset_text(reset, now):
    if reset is None:
        return paint('reset time unknown', FAINT)
    when = '' if NARROW else paint(' · ' + fmt_when(reset, now), MUTED)
    return f'{paint("resets in", MUTED)} {paint(fmt_span(reset - now), TEXT)}{when}'


def meter_rows(label, limit, inner, now, marker=None, marker_note=None):
    name = paint(label, TEXT, bold=True)
    if not is_limit(limit):
        return [spread(name, paint('no data yet', FAINT), inner)]
    reset = limit.get('resets_at') if is_num(limit.get('resets_at')) else None
    if reset is not None and reset <= now:
        return [spread(name, paint('0%', GREEN, bold=True), inner), bar(0, inner),
                paint('Reset - shows 0% until your next Claude Code message', MUTED)]
    pct = limit['used_percentage']
    num = paint(f'{fmt_num(pct)}%', level(pct), bold=True)
    b = bar(pct / 100, inner, marker=marker,
            red_from=marker if marker is not None and pct / 100 > marker else None)
    note = paint(marker_note, MUTED) if marker_note else ''
    if LVL >= 3:  # label, %, note and reset on one line above the bar
        left = f'{name} {num}' + (f'  {note}' if note else '')
        return [spread(left, reset_text(reset, now), inner), b]
    return [spread(name, num, inner), b, spread(note, reset_text(reset, now), inner)]


def limits_panel(data, today, width, now):
    inner = width - 4
    marker = note = None
    if today['state'] == 'ok':
        stop = min(today['base'] + today['allowance'], 100)
        marker = stop / 100
        note = f'┃ stop at {fmt_pct(stop)}%' if LVL >= 3 else f'┃ stop here today: {fmt_pct(stop)}%'
    rows = meter_rows('5-hour session' if LVL < 3 else '5-hour', data.get('five_hour'), inner, now)
    rows.append('')
    rows += meter_rows('Week', data.get('seven_day'), inner, now, marker, note)
    return panel('Limits', rows, width)


def today_panel(t, m, width, now):
    inner = width - 4
    tight = LVL >= 3 or inner < 72 or NARROW
    if t['state'] == 'none':
        return panel("Today's budget", [paint('No weekly numbers yet.', MUTED)], width)
    if t['state'] == 'reset':
        return panel("Today's budget", wrap("The week has reset. Today's budget appears after your next "
                                            'Claude Code message.', inner), width)

    def sessions(pct):
        if not ratio_ready(m):
            return ''
        n = fmt_num(pct * m['d5'] / m['d7'] / 100)
        return paint(f'≈ {n} sessions' if tight else f'≈ {n} full 5-hour sessions', MUTED)

    a, used, left = t['allowance'], t['used'], t['left']
    allow_txt = f'{paint("Allowance", MUTED)} {paint(fmt_pct(a) + "%", TEXT, bold=True)}'
    used_txt = f'{paint("Used", MUTED)} {paint(fmt_pct(used) + "%", TEXT, bold=True)}'
    if left >= 0:
        rest = f'{paint("Left", MUTED)} {paint(fmt_pct(left) + "%", GREEN, bold=True)}'
    else:
        rest = paint('OVER by ' + fmt_pct(-left) + '%', RED, bold=True)

    # Full bar = today's allowance (or today's use, if that's bigger). Green up
    # to the allowance, red past it, bright tick where the allowance ends.
    scale = max(a, used, 0.001)
    over = used > a
    b = bar(used / scale, inner, color=GREEN, red_from=(a / scale) if over else None,
            marker=(a / scale) if over else None)

    if LVL >= 4:
        rows = [spread(f'{allow_txt}   {used_txt}   {rest}', sessions(max(left, 0)), inner), b]
    else:
        rows = [spread(f'{allow_txt} {paint("of the week", MUTED)}', sessions(a), inner), b,
                spread(f'{used_txt}   {rest}', sessions(left) if left >= 0 else '', inner)]

    rows.append('')
    if t['next'] is not None:
        change = t['next'] - a
        if abs(change) < 0.05:
            trend = paint('same as today', MUTED)
        elif change > 0:
            trend = paint(f'▲ {fmt_pct(change)}%' + ('' if tight else ' more than today'), GREEN)
        else:
            trend = paint(f'▼ {fmt_pct(-change)}%' + ('' if tight else ' less than today'), RED)
        lead = 'Stop now → each later day gets' if tight else 'If you stop now, each remaining day gets'
        rows.append(f'{paint(lead, MUTED)} {paint(fmt_pct(t["next"]) + "%", TEXT, bold=True)}  {trend}')
    else:
        rows.append(paint('Last day before the reset - everything left is yours to use.', MUTED))

    dots = ' '.join(paint('●', FAINT) if i < t['idx'] else paint('◉', ACCENT) if i == t['idx']
                    else paint('○', MUTED) for i in range(7))
    left_days = t['days_left']
    day = f'{dots}  {paint(f"Day {t['idx'] + 1}" + ("/7" if NARROW else " of 7"), TEXT)}'
    what = 'New daily budget' if left_days > 1 else 'Week resets'
    countdown = paint('in ' + fmt_span(t['day_end'] - now), TEXT, bold=True)
    if not (tight and NARROW):
        countdown += paint(' · ' + fmt_when(t['day_end'], now), MUTED)
    if tight:
        rows.append(f'{day} {paint("· new budget" if left_days > 1 else "· week resets", MUTED)} {countdown}')
    else:
        rows.append(day + paint(f" · {left_days} day{'s' if left_days != 1 else ''} left", MUTED))
        rows.append(f'{paint(what, MUTED)} {countdown}')
    if t['partial_from'] is not None:
        rows.append('')
        since = fmt_time(t['partial_from'])
        if tight:
            rows.append(paint('! ', AMBER, bold=True) + paint(f'Partial day - only counting since {since}', AMBER))
        else:
            rows += [paint('! ', AMBER, bold=True) + line if i == 0 else '  ' + line for i, line in enumerate(
                wrap(f'Partial day - tracking started {since}; usage before that '
                     "isn't counted as today's.", inner - 2, AMBER))]
    clock = fmt_time(t['day_end'])
    return panel(f"Today's budget  {paint(f'{clock} → {clock}', MUTED)}", rows, width)


def sessions_panel(m, week, width):
    inner = width - 4
    title = 'Sessions per week'
    if not m['since']:
        return panel(title, [paint('Recording starts with your next Claude Code message.', MUTED)], width)

    if not ratio_ready(m):
        half = max((inner - 26) // 2, 8)
        rows = [] if LVL >= 4 else [
            paint('Still measuring', AMBER, bold=True)
            + ('' if NARROW else paint(' - exact once the weekly meter ticks up twice.', MUTED)), '']
        rows += [spread(paint('Weekly ticks seen', MUTED), paint(f'{min(m["ticks"], 2)} / 2', TEXT), 26)
                 + ' ' + bar(min(m['ticks'], 2) / 2, half, color=ACCENT),
                 spread(paint('5-hour usage seen', MUTED), paint(f'{fmt_num(m["d5"])}%', TEXT), 26)]
        return panel(title + (' - still measuring' if LVL >= 4 else ''), rows, width)

    sessions = m['d5'] / m['d7']
    rng = (f'likely {fmt_num(m["lo"])}–{fmt_num(m["hi"])}' if m['hi'] is not None
           else f'at least {fmt_num(m["lo"])}')
    rows = [spread(f'{paint("≈ " + fmt_num(sessions), ACCENT, bold=True)} '
                   f'{paint("full 5-hour sessions fill the week", TEXT)}', paint(rng, MUTED), inner)]
    if is_limit(week) and LVL < 4:
        used_sessions = week['used_percentage'] * m['d5'] / m['d7'] / 100
        rows.append('')
        rows.append(f'{tiles(used_sessions, sessions)}  '
                    f'{paint(f"{fmt_num(used_sessions)} of {fmt_num(sessions)} used this week", MUTED)}')
    rows.append('')
    rows.append(f'{paint("1% of the week", MUTED)} ≈ {paint(fmt_num(sessions) + "%", TEXT, bold=True)} '
                f'{paint("of a 5-hour session", MUTED)}   {paint("·", FAINT)}   '
                f'{paint("1 session", MUTED)} ≈ {paint(fmt_num(100 * m["d7"] / m["d5"]) + "%", TEXT, bold=True)} '
                f'{paint("of the week", MUTED)}')
    if LVL < 3:
        pts = f'{fmt_num(m["d7"])} exact weekly point{"s" if m["d7"] != 1 else ""}'
        if m['method'] == 'ticks':
            seen = (f'Measured tick to tick: {fmt_num(m["d5"])}% of 5-hour usage over {pts} · '
                    f'since {fmt_date(m["since"])}')
        else:
            seen = (f'Rough until the weekly meter ticks twice: {fmt_num(m["d5"])}% of 5-hour usage moved '
                    f'the week {fmt_num(m["d7"])}% · since {fmt_date(m["since"])}')
        rows += wrap(seen, inner, FAINT)
    return panel(title, rows, width)


# ── banner ────────────────────────────────────────────────────────────────

# 5x5 pixel letters, drawn two pixel-rows per text line with half blocks.
GLYPHS = {
    'A': ['.###.', '#...#', '#####', '#...#', '#...#'],
    'B': ['####.', '#...#', '####.', '#...#', '####.'],
    'C': ['.####', '#....', '#....', '#....', '.####'],
    'D': ['####.', '#...#', '#...#', '#...#', '####.'],
    'E': ['#####', '#....', '####.', '#....', '#####'],
    'F': ['#####', '#....', '####.', '#....', '#....'],
    'G': ['.####', '#....', '#..##', '#...#', '.###.'],
    'H': ['#...#', '#...#', '#####', '#...#', '#...#'],
    'I': ['#####', '..#..', '..#..', '..#..', '#####'],
    'J': ['..###', '....#', '....#', '#...#', '.###.'],
    'K': ['#...#', '#..#.', '###..', '#..#.', '#...#'],
    'L': ['#....', '#....', '#....', '#....', '#####'],
    'M': ['#...#', '##.##', '#.#.#', '#...#', '#...#'],
    'N': ['#...#', '##..#', '#.#.#', '#..##', '#...#'],
    'O': ['.###.', '#...#', '#...#', '#...#', '.###.'],
    'P': ['####.', '#...#', '####.', '#....', '#....'],
    'Q': ['.###.', '#...#', '#.#.#', '#..#.', '.##.#'],
    'R': ['####.', '#...#', '####.', '#..#.', '#...#'],
    'S': ['.####', '#....', '.###.', '....#', '####.'],
    'T': ['#####', '..#..', '..#..', '..#..', '..#..'],
    'U': ['#...#', '#...#', '#...#', '#...#', '.###.'],
    'V': ['#...#', '#...#', '#...#', '.#.#.', '..#..'],
    'W': ['#...#', '#...#', '#.#.#', '##.##', '#...#'],
    'X': ['#...#', '.#.#.', '..#..', '.#.#.', '#...#'],
    'Y': ['#...#', '.#.#.', '..#..', '..#..', '..#..'],
    'Z': ['#####', '...#.', '..#..', '.#...', '#####'],
    ' ': ['..', '..', '..', '..', '..'],
}
BANNER_FROM = (204, 102, 72)
BANNER_TO = (246, 196, 150)


def banner_line(text, width):
    rows = ['.'.join(GLYPHS[ch][r] for ch in text) for r in range(5)]
    cols = len(rows[0])
    rows.append('.' * cols)  # blank 6th pixel-row so the last text line pairs up
    lines = []
    for top, bot in ((0, 1), (2, 3), (4, 5)):
        out = []
        for x in range(cols):
            a, b = rows[top][x] == '#', rows[bot][x] == '#'
            ch = '█' if a and b else '▀' if a else '▄' if b else ' '
            out.append(paint(ch, mix(BANNER_FROM, BANNER_TO, x / max(cols - 1, 1))) if ch != ' ' else ' ')
        lines.append(' ' * max((width - cols) // 2, 0) + ''.join(out))
    return lines


def banner_width(text):
    text = [ch for ch in text.upper() if ch in GLYPHS]
    return sum(len(GLYPHS[ch][0]) for ch in text) + len(text) - 1


def banner(text, width):
    """Big letters, centred. Words move to a new line if they don't fit."""
    text = ''.join(ch for ch in text.upper() if ch in GLYPHS).strip()
    groups = []
    for word in text.split():
        if groups and banner_width(groups[-1] + ' ' + word) <= width:
            groups[-1] += ' ' + word
        else:
            groups.append(word)
    lines = []
    for g in groups:
        lines += banner_line(g, width)
    return lines


# ── plan ──────────────────────────────────────────────────────────────────

PLAN_NAMES = {'pro': 'Pro', 'max': 'Max', 'team': 'Team', 'enterprise': 'Enterprise', 'free': 'Free'}
PLAN = {}


def fetch_plan():
    """Which Claude plan you're signed in with, asked from Claude Code itself
    (`claude auth status --json`). The usage-limit size (e.g. "Max 5x") isn't
    in that answer, so it's read from Claude Code's saved login - only the
    plan fields, never the login keys. Falls back to the saved login for the
    plan name if the `claude` command isn't found."""
    plan = {'name': None, 'tier': None, 'method': None}
    exe = shutil.which('claude')
    if exe:
        try:
            r = subprocess.run([exe, 'auth', 'status', '--json'], capture_output=True, text=True,
                               timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            d = json.loads(r.stdout)
            plan['method'] = d.get('authMethod') if d.get('loggedIn') else 'none'
            if plan['method'] == 'claude.ai':
                plan['name'] = d.get('subscriptionType')
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    try:
        login = json.loads((CONFIG_DIR / '.credentials.json').read_text(encoding='utf-8')).get('claudeAiOauth') or {}
        if plan['method'] in (None, 'claude.ai'):
            plan['name'] = plan['name'] or login.get('subscriptionType')
            m = re.search(r'max_(\d+)x', login.get('rateLimitTier') or '')
            if m:
                plan['tier'] = f'Max {m.group(1)}x'
    except (OSError, ValueError, AttributeError):
        pass
    return plan


def refresh_plan():
    global PLAN
    PLAN = fetch_plan()


def plan_loop():
    while True:
        time.sleep(PLAN_REFRESH_S)
        refresh_plan()


def plan_info():
    """(banner text, subtitle, subtitle colour) for the signed-in plan."""
    name = PLAN.get('name')
    if not name:
        why = {'none': 'Not signed in to Claude Code',
               None: 'Plan unknown'}.get(PLAN.get('method'), 'Claude Code is not signed in with a Claude plan')
        return 'CLAUDE', why, FAINT
    label = PLAN_NAMES.get(name.lower(), name.title())
    tier = PLAN.get('tier')
    if not tier:
        sub = f'{label} plan'
    elif tier.startswith(label):
        sub = f'{tier} plan'
    else:
        sub = f'{label} plan · {tier} usage limits'
    return f'CLAUDE {label}', sub, MUTED


def small_logo(text):
    """The logo on one line: spaced-out letters in the same orange gradient."""
    chars = '  '.join(' '.join(word) for word in text.upper().split())
    n = max(len(chars) - 1, 1)
    return ''.join(paint(ch, mix(BANNER_FROM, BANNER_TO, i / n), bold=True) if ch != ' ' else ' '
                   for i, ch in enumerate(chars))


def plan_lines(width):
    text, sub, color = plan_info()
    big_fits = all(banner_width(w) <= width for w in text.split())
    if LVL <= 1 and big_fits:
        return banner(text, width) + [paint(sub.center(width).rstrip(), color)]
    line = f'{paint("✻", ACCENT, bold=True)} {small_logo(text)} {paint("✻", ACCENT, bold=True)}'
    if vlen(line) + 3 + len(sub) <= width:
        line += '   ' + paint(sub, color)
    return [' ' * max((width - vlen(line)) // 2, 0) + line]


# ── account backup ────────────────────────────────────────────────────────

ACCOUNT_URL = 'https://api.anthropic.com/api/oauth/usage'
USE_ACCOUNT = '--no-account' not in sys.argv and not os.environ.get('CLAUDE_USAGE_NO_ACCOUNT')
ACCOUNT = {'tried': None, 'ok': None, 'why': None}
REDRAW = threading.Event()

ACCOUNT_WHY = {
    'no_login': 'no Claude Code login found on this PC',
    'expired': 'the saved login has expired - send one message in Claude Code in a terminal to renew it',
    'offline': "couldn't reach Anthropic - offline?",
    'changed': "Anthropic's answer has changed - this backup needs an update",
}


def whole(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def save_reading(five, week, now):
    """Save account numbers to their own file (so they never fight the status
    bar's copy), and add a history line if either number changed since the
    last account reading."""
    try:
        prev = json.loads(ACCOUNT_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        prev = {}
    if not isinstance(prev, dict):
        prev = {}
    now = int(now)
    nxt = {'saved_at': now, 'five_hour': five, 'seven_day': week, 'source': 'account'}

    def key(l):
        return f"{l.get('used_percentage')}|{round((l.get('resets_at') or 0) / 60)}" if isinstance(l, dict) else '-'

    unchanged = key(prev.get('five_hour')) == key(five) and key(prev.get('seven_day')) == key(week)
    if not unchanged or not HISTORY.exists():
        f, w = five or {}, week or {}
        line = {'t': now, 'h': f.get('used_percentage'), 'hr': f.get('resets_at'),
                'w': w.get('used_percentage'), 'wr': w.get('resets_at'), 'src': 'account'}
        with HISTORY.open('a', encoding='utf-8', newline='\n') as fh:
            fh.write(json.dumps(line, separators=(',', ':')) + '\n')
    tmp = ACCOUNT_FILE.with_name(f'{ACCOUNT_FILE.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(nxt, separators=(',', ':')), encoding='utf-8')
    os.replace(tmp, ACCOUNT_FILE)


def fetch_account(now):
    """Ask your Claude account for the 5-hour and weekly numbers - the same
    ones Claude Code's usage screen shows. Uses the login key Claude Code
    saved and sends it only to Anthropic. This connection isn't documented,
    so any failure just leaves the status-bar numbers in place."""
    ACCOUNT['tried'] = now

    def fail(why):
        ACCOUNT.update(ok=False, why=why)

    try:
        login = json.loads((CONFIG_DIR / '.credentials.json').read_text(encoding='utf-8')).get('claudeAiOauth') or {}
    except (OSError, ValueError, AttributeError):
        return fail('no_login')
    key = login.get('accessToken')
    if not key:
        return fail('no_login')
    if is_num(login.get('expiresAt')) and login['expiresAt'] / 1000 <= now:
        return fail('expired')
    req = urllib.request.Request(ACCOUNT_URL, headers={
        'Authorization': f'Bearer {key}', 'anthropic-beta': 'oauth-2025-04-20',
        'Content-Type': 'application/json', 'User-Agent': 'claude-usage-tracker'})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return fail('expired' if e.code == 401 else 'changed')
    except (OSError, ValueError):
        return fail('offline')

    def meter(m):
        if not isinstance(m, dict) or not is_num(m.get('utilization')):
            return None
        try:
            reset = round(datetime.fromisoformat(m['resets_at']).timestamp()) if m.get('resets_at') else None
        except (TypeError, ValueError):
            reset = None
        return {'used_percentage': whole(m['utilization']), 'resets_at': reset}

    five, week = meter(d.get('five_hour') if isinstance(d, dict) else None), \
        meter(d.get('seven_day') if isinstance(d, dict) else None)
    if not five and not week:
        return fail('changed')
    try:
        save_reading(five, week, now)
    except OSError:
        return fail('changed')
    ACCOUNT.update(ok=True, why=None)


def refresh_now():
    """R key: fresh numbers from the account and a fresh plan name, without
    freezing the window while they load (about a second)."""
    if USE_ACCOUNT:
        fetch_account(time.time())
    refresh_plan()
    REDRAW.set()


def account_loop():
    """Ask the account every ACCOUNT_EVERY_S while the dashboard is open."""
    while True:
        now = time.time()
        if ACCOUNT['tried'] is None or now - ACCOUNT['tried'] >= ACCOUNT_EVERY_S:
            fetch_account(now)
            REDRAW.set()
        time.sleep(5)


# ── frame ─────────────────────────────────────────────────────────────────

def failure_text(check):
    at = fmt_time(check['at'])
    if check.get('reason') == 'no_plan':
        return (f"A Claude Code session at {at} isn't signed in with your plan (API key or another "
                'provider), so it has no plan limits to report.')
    return (f'Claude Code answered a message at {at} but sent no usage numbers. A Claude Code update '
            'may have changed how it reports them.')


def read_json(path):
    """(dict or None, what went wrong or None)."""
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return None, None
    except OSError as e:
        return None, f"couldn't be opened ({e.strerror or e})"
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return d, None
    except ValueError:
        pass
    return None, 'is damaged'


def has_numbers(d):
    return (isinstance(d, dict) and is_num(d.get('saved_at')) and
            (is_limit(d.get('five_hour')) or is_limit(d.get('seven_day'))))


def load(now):
    """Returns (status, status_colour, message, data). Fresh account numbers
    win; otherwise the newest of the account's and the status bar's."""
    acct = read_json(ACCOUNT_FILE)[0] if USE_ACCOUNT else None
    acct = acct if has_numbers(acct) else None
    if acct and now - acct['saved_at'] <= ACCOUNT_FRESH_S:
        return 'LIVE', GREEN, None, acct

    data, err = read_json(LATEST)
    if data is None and acct is None:
        if err:
            return "CAN'T READ", RED, f'The saved usage file {err}. It will fix itself on your next Claude Code message.', None
        return 'WAITING', AMBER, 'Send any message in Claude Code once to start the feed.', None

    bar = data if has_numbers(data) else None
    best = max((d for d in (bar, acct) if d), key=lambda d: d['saved_at'], default=None)
    check = data.get('last_check') if data and isinstance(data.get('last_check'), dict) else {}
    failing = (check.get('ok') is False and is_num(check.get('at')) and
               (best is None or check['at'] >= best['saved_at']))
    if failing:
        tail = ' Last good numbers shown below.' if best else ' No good numbers have been saved yet.'
        return 'NOT UPDATING', RED, failure_text(check) + tail, best
    if best is None:
        return "CAN'T READ", RED, 'The saved usage file has no usable numbers. It will fix itself on your next Claude Code message.', None
    age = now - best['saved_at']
    if USE_ACCOUNT and ACCOUNT['ok'] is False and age > 2 * ACCOUNT_EVERY_S:
        return ('IDLE', AMBER, f'No new numbers since {fmt_time(best["saved_at"])}. Checking your account '
                f'directly failed: {ACCOUNT_WHY.get(ACCOUNT["why"], ACCOUNT["why"])}.', best)
    if age > STALE_S:
        tail = ("If you've used the desktop app or claude.ai since then, these numbers are behind."
                if not USE_ACCOUNT else 'Checking your account directly...')
        return 'IDLE', AMBER, f'No Claude Code activity since {fmt_time(best["saved_at"])}. {tail}', best
    return 'LIVE', GREEN, None, best


def frame(now, cols):
    global NARROW
    width = min(max(cols - 2, 44), 92)
    NARROW = width < 68
    status, scol, message, data = load(now)

    pill = paint(('✖ ' if scol == RED else '● ') + status, scol, bold=True)
    right = f'{pill}   {paint(fmt_time(now), MUTED)} '
    if LVL >= 4:  # header and logo share one line
        text, _, _ = plan_info()
        out = [spread(f' {paint("✻", ACCENT, bold=True)} {small_logo(text)}', right, width)]
    else:
        head = spread(f' {paint("✻", ACCENT, bold=True)} {paint("CLAUDE USAGE", TEXT, bold=True)}', right, width)
        out = ['', head, ''] + plan_lines(width) + ['']
    if message:
        out += [' ' + line for line in wrap(message, width - 2, scol if scol == RED else MUTED)]
        out.append('')
    if data is None:
        return out, width

    samples = read_history()
    m = estimate(samples)
    t = today_budget(data.get('seven_day'), samples, now)
    out += limits_panel(data, t, width, now)
    out += today_panel(t, m, width, now)
    out += sessions_panel(m, data.get('seven_day'), width)

    saved = data['saved_at']
    source = 'your account' if data.get('source') == 'account' else 'Claude Code'
    info = f'Updated {fmt_time(saved)} ({fmt_ago(now - saved)}) from {source}'
    if m['since'] and LVL < 3 and not NARROW:
        info += f' · log: {m["count"]} reading{"s" if m["count"] != 1 else ""} since {fmt_date(m["since"])}'
    out.append(clip(' ' + paint(info, FAINT), width))
    return out, width


def keys_line(now):
    if NARROW or LVL >= 3:
        return f' {paint("R", TEXT, bold=True)} {paint("refresh", MUTED)}  {paint("Q", TEXT, bold=True)} {paint("quit", MUTED)}'
    return (f' {paint("R", TEXT, bold=True)} {paint("refresh", MUTED)}   {paint("Q", TEXT, bold=True)} '
            f'{paint("quit", MUTED)}   {paint(f"auto-refresh every {REFRESH_S}s · checked {fmt_time(now)}", FAINT)}')


def render(now=None, size=None):
    """The frame as a list of lines, squeezed until it fits the window."""
    global LVL
    now = time.time() if now is None else now
    cols, rows = size or shutil.get_terminal_size((100, 40))
    try:
        for LVL in range(5):
            lines, width = frame(now, cols)
            if LVL >= 3:  # "Updated ..." and the keys share one line
                lines[-1] = spread(lines[-1], keys_line(now).lstrip(), width)
            else:
                lines += ['', keys_line(now)]
            if LVL >= 1:
                lines = [line for line in lines if line != '']
            if len(lines) <= rows:
                break
    except Exception as e:  # never die on a bad frame - show it and keep running
        lines = ['', paint(f' ✖ WINDOW ERROR  {e}', RED, bold=True), paint(' Still running - press R to try again.', MUTED)]
    finally:
        LVL = 0
    # Never wider or taller than the window: overflow would scroll the top
    # (the logo) off screen.
    return [clip(line, cols) for line in lines[:rows]]


def enable_vt():
    """Turn on colour escape codes in the Windows console."""
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    if os.name != 'nt':
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            k.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


def main():
    enable_vt()
    refresh_plan()
    if '--once' in sys.argv:
        print('\n'.join(render()))
        return
    if os.name == 'nt':
        os.system('title Claude Usage')
    w = sys.stdout.write
    threading.Thread(target=plan_loop, daemon=True).start()
    if USE_ACCOUNT:
        threading.Thread(target=account_loop, daemon=True).start()
    w('\x1b[?1049h\x1b[?25l')  # alternate screen, hide cursor
    try:
        last, last_size = 0.0, None
        while True:
            size = shutil.get_terminal_size()
            if time.time() - last >= REFRESH_S or size != last_size or REDRAW.is_set():
                REDRAW.clear()
                w('\x1b[H' + '\n'.join(line + '\x1b[K' for line in render(size=size)) + '\x1b[J')
                sys.stdout.flush()
                last, last_size = time.time(), size
            if msvcrt and msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ('r', 'R'):
                    last = 0.0  # redraw from the files now...
                    threading.Thread(target=refresh_now, daemon=True).start()  # ...and again with fresh numbers
                elif key in ('q', 'Q', '\x1b', '\x03'):
                    break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        w('\x1b[?25h\x1b[?1049l')  # show cursor, leave alternate screen
        sys.stdout.flush()


if __name__ == '__main__':
    main()
