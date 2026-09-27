#!/usr/bin/env python3
"""Claude Usage - terminal dashboard for your Claude plan limits.

Standard library only. Launch with "Claude Usage.bat" or `python claude-usage.py`.

Reads two files that the Claude Code status bar (statusline.js) writes into
your Claude folder (~/.claude) every time a session refreshes:
  usage-latest.json    latest 5-hour and weekly percentages + reset times
  usage-history.jsonl  one line each time either number changes, kept forever
Never contacts Anthropic, so numbers only move when a Claude Code session
sends a message. The plan name in the banner comes from asking Claude Code
(`claude auth status`) at startup and every 10 minutes. Nothing is kept in memory between runs - every frame is
worked out from those files, so restarts lose nothing.

Keys: R = re-read now, Q / Esc / Ctrl+C = quit. Re-reads every 30 s.
Flags: --once  print one frame and exit.
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

REFRESH_S = 30
PLAN_REFRESH_S = 600
STALE_S = 30 * 60
MIN_SESSION_PTS = 25   # 5-hour points needed before the sessions-per-week ratio is shown
MIN_WEEK_PTS = 3       # weekly points needed for the same
SAME_WINDOW_S = 3600   # reset times closer than this belong to the same window
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

def panel(title, rows, width):
    inner = width - 4
    head = f'{paint("╭─", BORDER)} {paint(title, ACCENT, bold=True)} '
    lines = [head + paint('─' * max(width - vlen(head) - 1, 0) + '╮', BORDER)]
    for r in rows:
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

    Meters are whole percentages, so each unbroken run of readings can be off
    by up to 1 point per meter; err carries that into the shown range."""
    d5 = d7 = 0.0
    runs, in_run, whole = 0, False, True
    windows = set()
    for a, b in zip(samples, samples[1:]):
        same = (abs(b['hr'] - a['hr']) < SAME_WINDOW_S and b['h'] >= a['h'] and
                abs(b['wr'] - a['wr']) < SAME_WINDOW_S and b['w'] >= a['w'])
        if not same:
            in_run = False
            continue
        if not in_run:
            runs, in_run = runs + 1, True
        d5 += b['h'] - a['h']
        d7 += b['w'] - a['w']
        if b['h'] > a['h']:
            windows.add(round(b['hr'] / SAME_WINDOW_S))
        if any(float(x) != int(x) for x in (a['h'], b['h'], a['w'], b['w'])):
            whole = False
    return {
        'd5': d5, 'd7': d7, 'err': runs if whole else 0, 'windows': len(windows),
        'weeks': len({round(s['wr'] / SAME_WINDOW_S) for s in samples}),
        'since': samples[0]['t'] if samples else None, 'count': len(samples),
    }


def ratio_ready(m):
    return m['d5'] >= MIN_SESSION_PTS and m['d7'] >= MIN_WEEK_PTS


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

def meter_rows(label, limit, inner, now, marker=None, marker_note=None):
    if not is_limit(limit):
        return [paint(label, TEXT, bold=True), paint('no data yet', FAINT)]
    reset = limit.get('resets_at') if is_num(limit.get('resets_at')) else None
    if reset is not None and reset <= now:
        return [spread(paint(label, TEXT, bold=True), paint('0%', GREEN, bold=True), inner),
                bar(0, inner), paint('Reset - shows 0% until your next Claude Code message', MUTED)]
    pct = limit['used_percentage']
    rows = [spread(paint(label, TEXT, bold=True), paint(f'{fmt_num(pct)}%', level(pct), bold=True), inner),
            bar(pct / 100, inner, marker=marker,
                red_from=marker if marker is not None and pct / 100 > marker else None)]
    reset_text = (f'resets in {paint(fmt_span(reset - now), TEXT)}{paint(" · " + fmt_when(reset, now), MUTED)}'
                  if reset is not None else paint('reset time unknown', FAINT))
    rows.append(spread(paint(marker_note, MUTED) if marker_note else '', paint(reset_text, MUTED), inner))
    return rows


def limits_panel(data, today, width, now):
    inner = width - 4
    marker = note = None
    if today['state'] == 'ok':
        stop = min(today['base'] + today['allowance'], 100)
        marker = stop / 100
        note = f'┃ stop here today: {fmt_pct(stop)}%'
    rows = meter_rows('5-hour session', data.get('five_hour'), inner, now)
    rows.append('')
    rows += meter_rows('Week', data.get('seven_day'), inner, now, marker, note)
    return panel('Limits', rows, width)


def today_panel(t, m, width, now):
    inner = width - 4
    if t['state'] == 'none':
        return panel("Today's budget", [paint('No weekly numbers yet.', MUTED)], width)
    if t['state'] == 'reset':
        return panel("Today's budget", wrap("The week has reset. Today's budget appears after your next "
                                            'Claude Code message.', inner), width)

    def sessions(pct):
        return paint(f'≈ {fmt_num(pct * m["d5"] / m["d7"] / 100)} full 5-hour sessions', MUTED) if ratio_ready(m) else ''

    a, used, left = t['allowance'], t['used'], t['left']
    rows = [spread(f'{paint("Allowance", MUTED)}  {paint(fmt_pct(a) + "%", TEXT, bold=True)} '
                   f'{paint("of the week", MUTED)}', sessions(a), inner)]

    # Full bar = today's allowance (or today's use, if that's bigger). Green up
    # to the allowance, red past it, bright tick where the allowance ends.
    scale = max(a, used, 0.001)
    over = used > a
    rows.append(bar(used / scale, inner, color=GREEN, red_from=(a / scale) if over else None,
                    marker=(a / scale) if over else None))

    used_txt = f'{paint("Used", MUTED)} {paint(fmt_pct(used) + "%", TEXT, bold=True)}'
    if left >= 0:
        rest = f'{paint("Left", MUTED)} {paint(fmt_pct(left) + "%", GREEN, bold=True)}'
        rows.append(spread(f'{used_txt}   {rest}', sessions(left), inner))
    else:
        rows.append(f'{used_txt}   {paint("OVER by " + fmt_pct(-left) + "%", RED, bold=True)}')

    rows.append('')
    if t['next'] is not None:
        change = t['next'] - a
        if abs(change) < 0.05:
            trend = paint('same as today', MUTED)
        elif change > 0:
            trend = paint(f'▲ {fmt_pct(change)}% more than today', GREEN)
        else:
            trend = paint(f'▼ {fmt_pct(-change)}% less than today', RED)
        rows.append(f'{paint("If you stop now, each remaining day gets", MUTED)} '
                    f'{paint(fmt_pct(t["next"]) + "%", TEXT, bold=True)}  {trend}')
    else:
        rows.append(paint('Last day before the reset - everything left is yours to use.', MUTED))

    dots = ' '.join(paint('●', FAINT) if i < t['idx'] else paint('◉', ACCENT) if i == t['idx']
                    else paint('○', MUTED) for i in range(7))
    left_days = t['days_left']
    rows.append(f'{dots}   {paint(f"Day {t['idx'] + 1} of 7", TEXT)}'
                f'{paint(f" · {left_days} day{'s' if left_days != 1 else ''} left", MUTED)}')
    what = 'New daily budget' if left_days > 1 else 'Week resets'
    rows.append(f'{paint(what, MUTED)} {paint("in " + fmt_span(t["day_end"] - now), TEXT, bold=True)}'
                f'{paint(" · " + fmt_when(t["day_end"], now), MUTED)}')
    if t['partial_from'] is not None:
        rows.append('')
        rows += [paint('⚠ ', AMBER) + line if i == 0 else '  ' + line for i, line in enumerate(
            wrap(f'Partial day - tracking started {fmt_time(t["partial_from"])}; usage before that '
                 "isn't counted as today's.", inner - 2, AMBER))]
    clock = fmt_time(t['day_end'])
    return panel(f"Today's budget  {paint(f'{clock} → {clock}', MUTED)}", rows, width)


def sessions_panel(m, week, width):
    inner = width - 4
    title = 'Sessions per week'
    if not m['since']:
        return panel(title, [paint('Recording starts with your next Claude Code message.', MUTED)], width)

    seen = (f'Seen so far: {fmt_num(m["d5"])}% of 5-hour usage moved the week {fmt_num(m["d7"])}% · '
            f'{m["windows"]} window{"s" if m["windows"] != 1 else ""} · since {fmt_date(m["since"])}')
    if not ratio_ready(m):
        half = (inner - 26) // 2
        rows = [paint('Still measuring', AMBER, bold=True) + paint(' - the estimate appears once both bars fill.', MUTED), '',
                spread(paint('5-hour usage seen', MUTED), paint(f'{fmt_num(m["d5"])} / {MIN_SESSION_PTS}%', TEXT), 26)
                + ' ' + bar(m['d5'] / MIN_SESSION_PTS, half, color=ACCENT),
                spread(paint('Weekly usage seen', MUTED), paint(f'{fmt_num(m["d7"])} / {MIN_WEEK_PTS}%', TEXT), 26)
                + ' ' + bar(m['d7'] / MIN_WEEK_PTS, half, color=ACCENT)]
        return panel(title, rows, width)

    sessions = m['d5'] / m['d7']
    lo = (m['d5'] - m['err']) / (m['d7'] + m['err'])
    hi = (m['d5'] + m['err']) / (m['d7'] - m['err']) if m['d7'] - m['err'] > 0 else None
    rng = f'likely {fmt_num(lo)}–{fmt_num(hi)}' if hi is not None else f'at least {fmt_num(lo)}'
    rows = [spread(f'{paint("≈ " + fmt_num(sessions), ACCENT, bold=True)} '
                   f'{paint("full 5-hour sessions fill the week", TEXT)}', paint(rng, MUTED), inner)]
    if is_limit(week):
        used_sessions = week['used_percentage'] * m['d5'] / m['d7'] / 100
        rows.append('')
        rows.append(f'{tiles(used_sessions, sessions)}  '
                    f'{paint(f"{fmt_num(used_sessions)} of {fmt_num(sessions)} used this week", MUTED)}')
    rows.append('')
    rows.append(f'{paint("1% of the week", MUTED)} ≈ {paint(fmt_num(sessions) + "%", TEXT, bold=True)} '
                f'{paint("of a 5-hour session", MUTED)}   {paint("·", FAINT)}   '
                f'{paint("1 session", MUTED)} ≈ {paint(fmt_num(100 * m["d7"] / m["d5"]) + "%", TEXT, bold=True)} '
                f'{paint("of the week", MUTED)}')
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


def plan_lines(width):
    name = PLAN.get('name')
    if not name:
        why = {'none': 'Not signed in to Claude Code',
               None: 'Plan unknown'}.get(PLAN.get('method'), 'Claude Code is not signed in with a Claude plan')
        return banner('CLAUDE', width) + [paint(why.center(width).rstrip(), FAINT)]
    label = PLAN_NAMES.get(name.lower(), name.title())
    tier = PLAN.get('tier')
    if not tier:
        sub = f'{label} plan'
    elif tier.startswith(label):
        sub = f'{tier} plan'
    else:
        sub = f'{label} plan · {tier} usage limits'
    return banner(f'CLAUDE {label}', width) + [paint(sub.center(width).rstrip(), MUTED)]


def load(now):
    """Returns (status, status_colour, message, data)."""
    try:
        raw = LATEST.read_text(encoding='utf-8')
    except FileNotFoundError:
        return 'WAITING', AMBER, 'Send any message in Claude Code once to start the feed.', None
    except OSError as e:
        return "CAN'T READ", RED, f"The saved usage file couldn't be opened ({e.strerror or e}).", None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
    except ValueError:
        return "CAN'T READ", RED, 'The saved usage file is damaged. It will fix itself on your next Claude Code message.', None

    has = is_num(data.get('saved_at')) and (is_limit(data.get('five_hour')) or is_limit(data.get('seven_day')))
    check = data.get('last_check') if isinstance(data.get('last_check'), dict) else {}
    failing = check.get('ok') is False and is_num(check.get('at')) and (not has or check['at'] >= data['saved_at'])
    if failing:
        tail = ' Last good numbers shown below.' if has else ' No good numbers have been saved yet.'
        return 'NOT UPDATING', RED, failure_text(check) + tail, data if has else None
    if not has:
        return "CAN'T READ", RED, 'The saved usage file has no usable numbers. It will fix itself on your next Claude Code message.', None
    if now - data['saved_at'] > STALE_S:
        return ('IDLE', AMBER, f'No Claude Code activity since {fmt_time(data["saved_at"])}. '
                "If you've used claude.ai since then, these numbers are behind.", data)
    return 'LIVE', GREEN, None, data


def frame(now):
    cols = shutil.get_terminal_size((100, 40)).columns
    width = min(max(cols - 2, 66), 92)
    inner = width - 4
    status, scol, message, data = load(now)

    pill = paint(('✖ ' if scol == RED else '● ') + status, scol, bold=True)
    head = spread(f' {paint("✻", ACCENT, bold=True)} {paint("CLAUDE USAGE", TEXT, bold=True)}',
                  f'{pill}   {paint(fmt_time(now), MUTED)} ', width)
    out = ['', head, ''] + plan_lines(width) + ['']
    if message:
        out += [' ' + line for line in wrap(message, width - 2, scol if scol == RED else MUTED)]
        out.append('')
    if data is None:
        return out

    samples = read_history()
    m = session_math(samples)
    t = today_budget(data.get('seven_day'), samples, now)
    out += limits_panel(data, t, width, now)
    out += today_panel(t, m, width, now)
    out += sessions_panel(m, data.get('seven_day'), width)

    saved = data['saved_at']
    info = f'Updated {fmt_time(saved)} ({fmt_ago(now - saved)})'
    if m['since']:
        info += (f' · saved log: {m["count"]} reading{"s" if m["count"] != 1 else ""} since '
                 f'{fmt_date(m["since"])}, {fmt_time(m["since"])}')
    out += [' ' + line for line in wrap(info, width - 2, FAINT)]
    return out


def render(now=None):
    now = time.time() if now is None else now
    try:
        lines = frame(now)
    except Exception as e:  # never die on a bad frame - show it and keep running
        lines = ['', paint(f' ✖ WINDOW ERROR  {e}', RED, bold=True), paint(' Still running - press R to try again.', MUTED)]
    keys = (f' {paint("R", TEXT, bold=True)} {paint("refresh", MUTED)}   {paint("Q", TEXT, bold=True)} '
            f'{paint("quit", MUTED)}   {paint(f"auto-refresh every {REFRESH_S}s · checked {fmt_time(now)}", FAINT)}')
    return lines + ['', keys]


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
    w('\x1b[?1049h\x1b[?25l')  # alternate screen, hide cursor
    try:
        last, last_size = 0.0, None
        while True:
            size = shutil.get_terminal_size()
            if time.time() - last >= REFRESH_S or size != last_size:
                w('\x1b[H' + '\n'.join(line + '\x1b[K' for line in render()) + '\x1b[J')
                sys.stdout.flush()
                last, last_size = time.time(), size
            if msvcrt and msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ('r', 'R'):
                    refresh_plan()
                    last = 0.0
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
