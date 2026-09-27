#!/usr/bin/env python3
"""claude-pace - pace your Claude plan limits across the week.

Standard library only, Python 3.9 or newer, Windows / macOS / Linux.

    claude-pace                 open the dashboard
    claude-pace install         hook the status line into Claude Code
    claude-pace uninstall       undo that (--purge also deletes the log)
    claude-pace account on|off  remember whether to check your account
(Or `python3 claude_pace.py ...` when you run the file directly.)

Claude Code runs `claude-pace statusline` after every message. It prints
the status bar text and copies your plan's 5-hour and weekly percentages into
your Claude folder (~/.claude):
  usage-latest.json    latest numbers and reset times
  usage-history.jsonl  one line each time either number changes, kept forever
If you already had a status line, install keeps it: ours saves the numbers,
then runs yours and shows its output.

While it's open, the dashboard also asks your Claude account for the same two
numbers every 2 minutes and shows those: they're current even when you're in
the desktop app or on claude.ai, and the status bar's copy runs about a point
behind. They're saved to usage-account.json and the same history log. That
uses the login Claude Code saved and sends it only to Anthropic. On macOS the
login is in the Keychain, which can ask for your password, so there it's off
until you press A (this session) or run `account on` (always).

Nothing is kept in memory between runs - every frame is worked out from those
files, so restarts lose nothing.

Keys:  R = fetch fresh numbers now, A = turn the account check on (macOS),
       Q / Esc / Ctrl+C = quit.  Re-reads the files every 30 s.
Flags: --once        print one frame and exit (no account check)
       --account     check your account this run (even on macOS)
       --no-account  never ask your account directly; status bar only
       --version
Install flags: --python PATH (Python to run the status line with),
       --no-launcher (don't create the `claude-pace` command),
       --desktop (also put a launcher on the desktop / in the app menu)
Env:   CLAUDE_CONFIG_DIR       your Claude folder, if it isn't ~/.claude
       CLAUDE_PACE_DIR         keep the usage files in another folder
       CLAUDE_PACE_NO_ACCOUNT=1   same as --no-account
       CLAUDE_PACE_COLOR       truecolor | 256 | 16 | none (default: detected)
       CLAUDE_PACE_ASCII=1     plain ASCII drawing (0 = never)
       NO_COLOR                no colour
"""

import sys

if sys.version_info < (3, 9):
    sys.exit('claude-pace needs Python 3.9 or newer - this is Python %d.%d.' % sys.version_info[:2])

import json
import math
import os
import re
import shutil
import stat
import subprocess
import textwrap
import threading
import time
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

try:
    import msvcrt
except ImportError:  # not Windows
    msvcrt = None

VERSION = '1.0.0'
IS_WIN = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'
ARGS = sys.argv[1:]
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

CONFIG_DIR = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
DATA_DIR = Path(os.environ.get('CLAUDE_PACE_DIR') or CONFIG_DIR)
LATEST = DATA_DIR / 'usage-latest.json'
HISTORY = DATA_DIR / 'usage-history.jsonl'
ACCOUNT_FILE = DATA_DIR / 'usage-account.json'
# What install remembers (above all, the status line it chains to) lives next
# to settings.json, not in the data folder: CLAUDE_PACE_DIR may be set in one
# shell and not where Claude Code runs, and the chained status line must be
# found either way.
TRACKER_CONFIG = CONFIG_DIR / 'claude-pace-config.json'
OLD_CONFIG = CONFIG_DIR / 'usage-tracker-config.json'  # same file under the pre-release name
SETTINGS = CONFIG_DIR / 'settings.json'
KEEP_BACKUPS = 5  # settings.json backups kept; older ones are deleted
NAME = 'claude-pace'
MARK = 'claude-pace launcher'  # written into every launcher we create, so we never touch anyone else's
OLD_MARKS = ('claude-usage-tracker launcher',)

REFRESH_S = 30
PLAN_REFRESH_S = 600
ACCOUNT_EVERY_S = 120  # ask the account this often while the dashboard is open
ACCOUNT_FRESH_S = 2 * ACCOUNT_EVERY_S + 60  # account numbers younger than this win
BACKOFF_MAX_S = 30 * 60  # longest wait after Anthropic says "too many requests"
CHAIN_TIMEOUT_S = 10  # how long a chained status line may take
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


def read_config():
    """What install remembered. Falls back to the file's pre-release name,
    which the next write moves over."""
    for f in (TRACKER_CONFIG, OLD_CONFIG):
        try:
            d = json.loads(f.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        return d if isinstance(d, dict) else {}
    return {}


def our_file(text):
    return any(m in text for m in (MARK,) + OLD_MARKS)


def replace_file(tmp, path):
    """Rename tmp over path. Windows refuses while another program has the
    file open for a moment, so try a few times before giving up."""
    for i in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == 9:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            time.sleep(0.02)


def write_atomic(path, text, mode=None):
    """Write to a temp file, then swap it in, so no reader ever sees half a
    file. `mode` gives the new file the old one's permission bits."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    try:
        with open(tmp, 'w', encoding='utf-8', newline='\n') as fh:
            fh.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    replace_file(tmp, path)


def env_flag(name):
    """An on/off environment setting: empty, 0, false, no and off all mean off."""
    return os.environ.get(name, '').strip().lower() not in ('', '0', 'false', 'no', 'off')


# ── colour ────────────────────────────────────────────────────────────────

def mac_major():
    import platform
    try:
        return int(platform.mac_ver()[0].split('.')[0])
    except (ValueError, IndexError):
        return 0


def detect_depth():
    """Colours the terminal can show: 24 (truecolor), 256, 16 or 0 (none)."""
    forced = os.environ.get('CLAUDE_PACE_COLOR', '').strip().lower()
    if forced:
        return {'truecolor': 24, '24bit': 24, '24': 24, '256': 256, '16': 16,
                'none': 0, 'no': 0, '0': 0}.get(forced, 24)
    if os.environ.get('NO_COLOR'):
        return 0
    term = os.environ.get('TERM', '').lower()
    if term == 'dumb':
        return 0
    if os.environ.get('COLORTERM', '').lower() in ('truecolor', '24bit'):
        return 24
    if os.environ.get('TMUX') or term.startswith(('screen', 'tmux')):
        return 256  # tmux only passes full colour on when COLORTERM says so
    prog = os.environ.get('TERM_PROGRAM', '')
    if prog == 'Apple_Terminal':
        return 24 if mac_major() >= 26 else 256
    if prog in ('iTerm.app', 'vscode', 'WezTerm', 'ghostty', 'Hyper', 'Tabby', 'rio', 'WarpTerminal'):
        return 24
    if (os.environ.get('WT_SESSION') or os.environ.get('KITTY_WINDOW_ID') or os.environ.get('KONSOLE_VERSION')
            or os.environ.get('WEZTERM_EXECUTABLE') or 'kitty' in term or 'direct' in term or 'truecolor' in term):
        return 24
    if IS_WIN:
        try:
            return 24 if sys.getwindowsversion().build >= 14931 else 16
        except AttributeError:
            return 16
    return 256 if '256' in term else 16


def detect_ascii():
    v = os.environ.get('CLAUDE_PACE_ASCII', '').strip().lower()
    if v:
        return v not in ('0', 'no', 'false', 'off')
    if IS_WIN:  # the Windows console takes any character regardless of code page
        return False
    enc = (getattr(sys.stdout, 'encoding', None) or '').lower().replace('-', '')
    return bool(enc) and enc != 'utf8'


DEPTH = detect_depth()
ASCII = detect_ascii()
COLOR = DEPTH > 0
RESET = '\x1b[0m' if COLOR else ''


def set_depth(d):
    global DEPTH, COLOR, RESET
    DEPTH, COLOR = d, d > 0
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

CUBE = (0, 95, 135, 175, 215, 255)


@lru_cache(maxsize=4096)
def to256(c):
    """Nearest of the xterm 256 colours: the 6x6x6 cube or the grey ramp."""
    idx = [min(range(6), key=lambda i: abs(CUBE[i] - v)) for v in c]
    cube = tuple(CUBE[i] for i in idx)
    g = min(max(round((sum(c) / 3 - 8) / 10), 0), 23)
    grey = (8 + 10 * g,) * 3

    def dist(a):
        return sum((a[i] - c[i]) ** 2 for i in range(3))
    return 232 + g if dist(grey) < dist(cube) else 16 + 36 * idx[0] + 6 * idx[1] + idx[2]


@lru_cache(maxsize=4096)
def to16(c):
    """Nearest basic ANSI colour (0-15), by hue: the 16 basic colours differ
    from terminal to terminal, so matching by distance goes wrong (orange
    would come out grey)."""
    if c == TRACK or c == BORDER:
        return 8  # dark grey, so an empty bar still shows against a black background
    r, g, b = (v / 255 for v in c)
    hi, lo = max(r, g, b), min(r, g, b)
    if hi - lo < 0.2:  # grey
        return 0 if hi < 0.2 else 8 if hi < 0.5 else 7 if hi < 0.8 else 15
    if hi == r:
        hue = (60 * (g - b) / (hi - lo)) % 360
    elif hi == g:
        hue = 60 * (b - r) / (hi - lo) + 120
    else:
        hue = 60 * (r - g) / (hi - lo) + 240
    base = (1 if hue < 20 or hue >= 330 else 3 if hue < 70 else 2 if hue < 160 else
            6 if hue < 200 else 4 if hue < 260 else 5)
    return base + (8 if hi > 0.85 else 0)


def color_code(c, layer):
    """layer 38 = text colour, 48 = background."""
    if DEPTH == 24:
        return f'\x1b[{layer};2;{c[0]};{c[1]};{c[2]}m'
    if DEPTH == 256:
        return f'\x1b[{layer};5;{to256(c)}m'
    n = to16(c)
    base = 30 if layer == 38 else 40
    return f'\x1b[{base + n if n < 8 else base + 60 + n - 8}m'


def fg(c):
    return color_code(c, 38) if COLOR else ''


def bg(c):
    return color_code(c, 48) if COLOR else ''


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


# One ASCII stand-in per character, so the layout (worked out in characters)
# stays the same in ASCII mode.
ASCII_MAP = str.maketrans({
    '╭': '+', '╮': '+', '╰': '+', '╯': '+', '─': '-', '│': '|', '┃': '|', '█': '#', '░': '.',
    '▏': ' ', '▎': ' ', '▍': ' ', '▌': '#', '▋': '#', '▊': '#', '▉': '#', '▀': '"', '▄': '_',
    '■': '#', '◧': '+', '□': '.', '●': '*', '◉': '@', '○': 'o', '✻': '*', '▲': '^', '▼': 'v',
    '→': '>', '≈': '~', '–': '-', '…': '~', '·': '-', '×': 'x'})


def asciify(s):
    return s.translate(ASCII_MAP).encode('ascii', 'replace').decode('ascii')


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


def parse_iso(s):
    """ISO time to seconds since 1970. Python before 3.11 can't read a
    trailing 'Z' or fractions of a second that aren't 3 or 6 digits long."""
    s = s.strip()
    if s.endswith(('Z', 'z')):
        s = s[:-1] + '+00:00'
    m = re.match(r'^(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$', s)
    if m:
        s = f'{m.group(1)}.{(m.group(2) + "000000")[:6]}{m.group(3)}'
    return round(datetime.fromisoformat(s).timestamp())


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
        if ASCII:  # no block characters: colour the cell's background instead
            filled = i < full or (i == full and part >= 4)
            ch, f, b = ' ', c, (c if filled else TRACK)
        elif i < full:
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


def fit(width, *options):
    """The first (longest) wording that fits in width, else the last one.
    An option is a line, or a (left, right) pair to spread across the width."""
    def line(o):
        return spread(o[0], o[1], width) if isinstance(o, tuple) else o

    def size(o):
        return vlen(o[0]) + vlen(o[1]) + 2 if isinstance(o, tuple) else vlen(o)
    return line(next((o for o in options if size(o) <= width), options[-1]))


def wrap(text, width, color=MUTED):
    return [paint(line, color) for line in textwrap.wrap(text, width)] or ['']


# ── data ──────────────────────────────────────────────────────────────────

def is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v == v and abs(v) != float('inf')


def is_limit(l):
    return isinstance(l, dict) and is_num(l.get('used_percentage'))


def has5(s):
    return 'h' in s


def read_history():
    """Every saved reading, oldest first. A reading needs the weekly numbers;
    the 5-hour ones can be missing (Claude Code leaves the 5-hour window out
    once it has ended), and such readings still count for the daily budget.
    A damaged byte only costs the line it's on."""
    try:
        raw = HISTORY.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        try:
            s = json.loads(line)
        except ValueError:
            continue
        if not isinstance(s, dict) or not all(is_num(s.get(k)) for k in ('t', 'w', 'wr')):
            continue
        if not (is_num(s.get('h')) and is_num(s.get('hr'))):
            s = {k: v for k, v in s.items() if k not in ('h', 'hr')}
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
    otherwise the rough whole-history method, once it has enough data.
    'whole' is False when the meters report fractions: then a tick isn't an
    exact point and the wording drops "exact"."""
    since, count = (samples[0]['t'] if samples else None), len(samples)
    samples = [x for x in samples if has5(x)]
    # Rough method, one source at a time (mixing them biases it), keeping the
    # source that has seen more weekly movement. Readings taken after their
    # own 5-hour window ended show a dead window and are left out.
    live = [x for x in samples if x['t'] < x['hr'] + 60]
    rough = max((session_math([x for x in live if x.get('src') == src]) for src in ('account', None)),
                key=lambda r: (r['d7'], r['d5']))
    rough.update(since=since, count=count)
    totals = five_hour_totals(samples)
    best, most = None, 0
    for src in ('account', None):
        t = tick_math(samples, totals, src)
        most = max(most, t['ticks'])
        if t['d7'] >= 1 and t['d5'] > 0 and (best is None or t['err'] / t['d5'] < best['err'] / best['d5']):
            best = t
    m = dict(rough, ticks=most,
             whole=all(float(x[k]).is_integer() for x in samples for k in ('h', 'w')))
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


def five_hour_since(samples, start):
    """5-hour meter points used since `start`, added up across 5-hour resets.
    Measured straight from the 5-hour meter, so it's much finer than the
    weekly meter's whole points. None if there's nothing to measure from."""
    five = [s for s in samples if has5(s)]
    pts = [(s, t) for s, t in zip(five, five_hour_totals(five)) if t is not None]
    used, prev, seen = 0.0, None, False
    for s, t in pts:
        if s['t'] <= start:
            prev = (s, t)
            continue
        seen = True
        began_today = s['hr'] - FIVE_H_S >= start - 60
        if prev is not None and t[0] == prev[1][0]:
            used += t[1] - prev[1][1]
        elif began_today:  # first reading, or one after a long gap, in a window that began today
            used += s['h']
        prev = (s, t)
    return max(used, 0.0) if seen or prev is not None else None


def day_starts(reset):
    """When each of the week's 7 days starts, plus the reset itself: the
    reset's local clock time on each of the 7 days before it. Worked out in
    local time, so across a daylight-saving change a day still starts at the
    same clock time (that day is 23 or 25 hours long)."""
    end = datetime.fromtimestamp(reset)
    return [(end - timedelta(days=7 - k)).timestamp() for k in range(7)] + [reset]


def start_of_day(this_week, src, day_start):
    """The weekly meter when today began, on the scale of the source being
    shown (src). The status bar reads up to SOURCE_SLACK points below the
    account, so the other source's readings only give a floor: a status-bar
    reading of b means the account was at least b; an account reading of a
    means the status bar was at least a - SOURCE_SLACK. The shown source's
    own last reading is used, raised to any such floor from a later reading
    of the other source. None if nothing was saved before today."""
    before = [s for s in this_week if s['t'] <= day_start]
    if not before:
        return None
    own = [s for s in before if s.get('src') == src]
    base, since = (own[-1]['w'], own[-1]['t']) if own else (None, -math.inf)
    for s in before:
        if s.get('src') != src and s['t'] > since:
            floor = s['w'] if src == 'account' else s['w'] - SOURCE_SLACK
            base = floor if base is None else max(base, floor)
    return max(base, 0)


def today_budget(week, samples, now, src=None):
    """Days start at the weekly reset's clock time, so the week is exactly 7
    days. Allowance = weekly limit unused when today began, split evenly over
    the days left (today included). Fixed for the day: using less raises later
    days' allowances, going over lowers them.

    "When today began" is the last saved reading before the day boundary, from
    the same source as the numbers shown (src: 'account' or None for the
    status bar) where possible. If tracking started partway through today,
    the first reading today stands in and usage before it isn't counted as
    today's."""
    if not is_limit(week) or not is_num(week.get('resets_at')):
        return {'state': 'none'}
    reset = week['resets_at']
    if reset <= now:
        return {'state': 'reset'}
    starts = day_starts(reset)
    idx = max([k for k in range(7) if starts[k] <= now] or [0])
    day_start, day_end = starts[idx], starts[idx + 1]
    days_left = 7 - idx
    cur = week['used_percentage']

    this_week = [s for s in samples if abs(s['wr'] - reset) < SAME_WINDOW_S]
    after = [s for s in this_week if s['t'] > day_start]
    base = start_of_day(this_week, src, day_start)
    partial_from = None
    if base is None and idx == 0:
        base = 0
    elif base is None and after:
        base, partial_from = after[0]['w'], after[0]['t']
    elif base is None:
        base, partial_from = cur, now

    allowance = max(100 - base, 0) / days_left
    used = max(cur - base, 0)
    nxt = max(100 - cur, 0) / (days_left - 1) if days_left > 1 else None
    return {
        'state': 'ok', 'idx': idx, 'days_left': days_left, 'day_end': day_end,
        'base': base, 'cur': cur, 'allowance': allowance, 'used': used,
        'left': allowance - used, 'next': nxt, 'partial_from': partial_from,
        # On a partial day both figures count from the first reading, so they agree.
        'five': five_hour_since(this_week, day_start if partial_from is None else partial_from),
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
        short = paint('resets in ', MUTED) + paint(fmt_span(reset - now), TEXT) if reset is not None else ''
        return [fit(inner, (left, reset_text(reset, now)), (left, short), (f'{name} {num}', short)), b]
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


def five_text(pts, short, full=False):
    """5-hour meter points as a share of one session, or as sessions past one."""
    if pts >= 99.5:
        n = fmt_num(pts / 100)
        what = 'session' if n == '1' else 'sessions'
        return f'≈ {n} {what}' if short else f'≈ {n} {"full " if full else ""}5-hour {what}'
    return f'≈ {fmt_num(pts)}% of a {"session" if short else "5-hour session"}'


def sessions_text(pts):
    """5-hour meter points as a count of sessions, always in sessions - so the
    allowance and what's left of it read in the same unit."""
    n = pts / 100
    s = fmt_num(n) if n >= 1 else (f'{n:.2f}'.rstrip('0').rstrip('.') or '0')
    return f'≈ {s} {"session" if s == "1" else "sessions"}'


def today_panel(t, m, width, now):
    """Today's budget as its own 0-100% bar: 100% is today's allowance. The
    headline is the share of that allowance used; the weekly-meter points it
    comes from are shown second, and the 5-hour meter's view of today beside
    them (the weekly meter only moves in whole points)."""
    inner = width - 4
    tight = LVL >= 3 or inner < 72 or NARROW
    title = "Today's budget"
    if t['state'] == 'none':
        return panel(title, [paint('No weekly numbers yet.', MUTED)], width)
    if t['state'] == 'reset':
        return panel(title, wrap("The week has reset. Today's budget appears after your next "
                                 'Claude Code message.', inner), width)

    a, used, left = t['allowance'], t['used'], t['left']
    rows = []
    if a <= 0:
        rows.append(paint('The weekly limit was used up before today began - nothing left until the reset.', RED,
                          bold=True))
        b = bar(1, inner, color=RED)
    else:
        share = 100 * used / a
        over = used > a
        if over:
            head = paint(f'OVER by {fmt_num(share - 100)}%', RED, bold=True) + paint(" of today's budget", RED)
            right = paint(f'{fmt_pct(-left)}% of the week too much', RED)
        else:
            head = paint(f'{fmt_num(share)}%', level(share), bold=True) + paint(" of today's budget used", TEXT)
            right = paint(f'{fmt_num(100 - share)}% left', GREEN, bold=True)
        if tight and over:
            right = paint(f'+{fmt_pct(-left)}% of week', RED)
        # Full bar = today's allowance. Past it, the bar is rescaled so the
        # overflow shows in red after a bright tick where the allowance ends.
        scale = max(a, used)
        b = bar(used / scale, inner, color=GREEN, red_from=(a / scale) if over else None,
                marker=(a / scale) if over else None)
        short = (paint(f'OVER by {fmt_num(share - 100)}%', RED, bold=True) + paint(' today', RED) if over else
                 paint(f'{fmt_num(share)}%', level(share), bold=True) + paint(' of today used', TEXT))
        rows.append(fit(inner, (head, right), (short, right), head))

    # Second line: the weekly points behind the headline, and the 5-hour view.
    week_txt = (paint(fmt_pct(used) + '%', TEXT) + paint(" of " if tight else " of today's ", MUTED)
                + paint(fmt_pct(a) + '%', TEXT) + paint(' of week' if tight else ' of the week', MUTED))
    five = t.get('five')
    five_txt = paint(five_text(five, tight) + ('' if tight else ' used'), MUTED) if five is not None else ''
    sub = spread(week_txt, five_txt, inner) if vlen(week_txt) + vlen(five_txt) + 2 <= inner else week_txt
    if LVL >= 4:  # headline and bar only; the week figures join the headline if they fit
        if a > 0 and vlen(head) + vlen(week_txt) + vlen(right) + 5 <= inner:
            rows[0] = spread(f'{head}   {week_txt}', right, inner)
        rows.append(b)
    else:
        rows += [b, sub]
        if ratio_ready(m) and a > 0 and not tight:
            ratio = m['d5'] / m['d7']

            def pair(label, full):
                total = sessions_text(a * ratio)
                if full:  # "≈ 1.1 full 5-hour sessions"
                    total = total.replace(' session', ' full 5-hour session', 1)
                rest = (paint(f'left {sessions_text(left * ratio)}', MUTED) if left >= 0 else
                        paint(f'over by {sessions_text(-left * ratio)}', RED))
                return paint(f'{label} {total}', MUTED), rest
            rows.append(fit(inner, pair("Today's allowance", True), pair('Allowance', True),
                            pair('Allowance', False)))

    rows.append('')
    if t['next'] is not None:
        change = t['next'] - a

        def trend(long):
            if abs(change) < 0.05:
                return paint('same as today', MUTED)
            if change > 0:
                return paint(f'▲ {fmt_pct(change)}%' + (' more than today' if long else ''), GREEN)
            return paint(f'▼ {fmt_pct(-change)}%' + (' less than today' if long else ''), RED)

        def stop(lead, unit, long):
            return (f'{paint(lead, MUTED)} {paint(fmt_pct(t["next"]) + "%", TEXT, bold=True)}'
                    f'{paint(unit, MUTED)}' + (f'  {trend(long)}' if long is not None else ''))
        options = [stop('If you stop now, each remaining day gets', ' of the week', True),
                   stop('If you stop now, each remaining day gets', '', True)] if not tight else []
        rows.append(fit(inner, *options, stop('Stop now → each later day gets', '', False),
                        stop('Stop now → later days get', '', False), stop('Stop now → later days get', '', None)))
    else:
        rows.append(paint('Last day before the reset - everything left is yours to use.', MUTED))

    dots = ' '.join(paint('●', FAINT) if i < t['idx'] else paint('◉', ACCENT) if i == t['idx']
                    else paint('○', MUTED) for i in range(7))
    left_days = t['days_left']
    day_word = f'Day {t["idx"] + 1}' + ('/7' if NARROW else ' of 7')
    day = f'{dots}  {paint(day_word, TEXT)}'
    what = 'New daily budget' if left_days > 1 else 'Week resets'
    countdown = paint('in ' + fmt_span(t['day_end'] - now), TEXT, bold=True)
    if not (tight and NARROW):
        countdown += paint(' · ' + fmt_when(t['day_end'], now), MUTED)
    if tight:
        span = paint('in ' + fmt_span(t['day_end'] - now), TEXT, bold=True)
        rows.append(fit(inner, f'{day} {paint("· new budget" if left_days > 1 else "· week resets", MUTED)} {countdown}',
                        f'{day} {paint("· new budget" if left_days > 1 else "· week resets", MUTED)} {span}',
                        f'{day} {paint("· new" if left_days > 1 else "· reset", MUTED)} {span}'))
    else:
        rows.append(day + paint(f" · {left_days} day{'s' if left_days != 1 else ''} left", MUTED))
        rows.append(f'{paint(what, MUTED)} {countdown}')
    if t['partial_from'] is not None:
        rows.append('')
        since = fmt_time(t['partial_from'])
        if tight:
            rows.append(paint('! ', AMBER, bold=True) + fit(inner - 2, paint(f'Partial day - only counting since {since}', AMBER),
                                                           paint(f'Partial day - counting since {since}', AMBER),
                                                           paint(f'Counting since {since}', AMBER)))
        else:
            rows += [paint('! ', AMBER, bold=True) + line if i == 0 else '  ' + line for i, line in enumerate(
                wrap(f'Partial day - tracking started {since}; usage before that '
                     "isn't counted as today's.", inner - 2, AMBER))]
    clock = fmt_time(t['day_end'])
    return panel(f"{title}  {paint(f'{clock} → {clock}', MUTED)}", rows, width)


def sessions_panel(m, week, width):
    inner = width - 4
    title = 'Sessions per week'
    if not m['since']:
        return panel(title, [paint('Recording starts with your next Claude Code message.', MUTED)], width)
    exact = 'exact' if m.get('whole', True) else 'measured'

    if not ratio_ready(m):
        half = max((inner - 26) // 2, 8)
        rows = [] if LVL >= 4 else [
            paint('Still measuring', AMBER, bold=True)
            + ('' if NARROW else paint(f' - {exact} once the weekly meter ticks up twice.', MUTED)), '']
        rows += [spread(paint('Weekly ticks seen', MUTED), paint(f'{min(m["ticks"], 2)} / 2', TEXT), 26)
                 + ' ' + bar(min(m['ticks'], 2) / 2, half, color=ACCENT),
                 spread(paint('5-hour usage seen', MUTED), paint(f'{fmt_num(m["d5"])}%', TEXT), 26)]
        return panel(title + (' - still measuring' if LVL >= 4 else ''), rows, width)

    sessions = m['d5'] / m['d7']
    rng = (f'likely {fmt_num(m["lo"])}–{fmt_num(m["hi"])}' if m['hi'] is not None
           else f'at least {fmt_num(m["lo"])}')
    num = paint('≈ ' + fmt_num(sessions), ACCENT, bold=True)
    rows = [fit(inner, (f'{num} {paint("full 5-hour sessions fill the week", TEXT)}', paint(rng, MUTED)),
                (f'{num} {paint("sessions fill the week", TEXT)}', paint(rng, MUTED)),
                (f'{num} {paint("sessions / week", TEXT)}', paint(rng.replace('likely ', ''), MUTED)))]
    if is_limit(week) and LVL < 4:
        used_sessions = week['used_percentage'] * m['d5'] / m['d7'] / 100
        rows.append('')
        rows.append(f'{tiles(used_sessions, sessions)}  '
                    f'{paint(f"{fmt_num(used_sessions)} of {fmt_num(sessions)} used this week", MUTED)}')
    rows.append('')
    per_pt = f'{paint("1% of the week", MUTED)} ≈ {paint(fmt_num(sessions) + "%", TEXT, bold=True)} '
    per_s = (f'{paint("1 session", MUTED)} ≈ {paint(fmt_num(100 * m["d7"] / m["d5"]) + "%", TEXT, bold=True)} '
             f'{paint("of the week", MUTED)}')
    rows.append(fit(inner, f'{per_pt}{paint("of a 5-hour session", MUTED)}   {paint("·", FAINT)}   {per_s}',
                    f'{per_pt}{paint("of a session", MUTED)}  {paint("·", FAINT)}  {per_s}', per_s))
    if LVL < 3:
        pts = f'{fmt_num(m["d7"])} {"exact " if exact == "exact" else ""}weekly point{"s" if m["d7"] != 1 else ""}'
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


def find_claude():
    """The `claude` command. Desktop launchers often start programs without
    the folders the installers use on the search path, so look there too."""
    exe = shutil.which('claude')
    if exe:
        return exe
    home = Path.home()
    for p in (home / '.local' / 'bin' / ('claude.exe' if IS_WIN else 'claude'), home / '.claude' / 'local' / 'claude',
              Path('/opt/homebrew/bin/claude'), Path('/usr/local/bin/claude')):
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def fetch_plan():
    """Which Claude plan you're signed in with, asked from Claude Code itself
    (`claude auth status --json`). The usage-limit size (e.g. "Max 5x") isn't
    in that answer, so it's read from Claude Code's saved login - only the
    plan fields, never the login keys. Falls back to the saved login for the
    plan name if the `claude` command isn't found. On macOS the saved login
    is in the Keychain; it's only used if the account check already read it."""
    plan = {'name': None, 'tier': None, 'method': None}
    exe = find_claude()
    if exe:
        try:
            r = subprocess.run([exe, 'auth', 'status', '--json'], capture_output=True, text=True,
                               encoding='utf-8', errors='replace', timeout=15, creationflags=NO_WINDOW)
            d = json.loads(r.stdout)
            plan['method'] = d.get('authMethod') if d.get('loggedIn') else 'none'
            if plan['method'] == 'claude.ai':
                plan['name'] = d.get('subscriptionType')
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    login = login_file()[0] or LOGIN.get('data') or {}
    if plan['method'] in (None, 'claude.ai') and isinstance(login, dict):
        plan['name'] = plan['name'] or login.get('subscriptionType')
        m = re.search(r'max_(\d+)x', str(login.get('rateLimitTier') or ''))
        if m:
            plan['tier'] = f'Max {m.group(1)}x'
    return plan


def refresh_plan():
    """Never raises: this runs in background threads, and an error there
    would stop the refreshes and print over the dashboard. The last plan
    name found stays up."""
    global PLAN
    try:
        PLAN = fetch_plan()
    except Exception:
        pass


def plan_loop():
    while True:
        time.sleep(PLAN_REFRESH_S)
        refresh_plan()
        REDRAW.set()


def not_a_plan():
    """Signed in to Claude Code, but with an API key or a cloud provider
    rather than a Claude plan - there are no plan limits to track."""
    return PLAN.get('method') not in (None, 'none', 'claude.ai')


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
    if LVL <= 1 and big_fits and not ASCII:
        return banner(text, width) + [paint(sub.center(width).rstrip(), color)]
    line = f'{paint("✻", ACCENT, bold=True)} {small_logo(text)} {paint("✻", ACCENT, bold=True)}'
    if vlen(line) + 3 + len(sub) <= width:
        line += '   ' + paint(sub, color)
    return [' ' * max((width - vlen(line)) // 2, 0) + line]


# ── account check ─────────────────────────────────────────────────────────

ACCOUNT_URL = 'https://api.anthropic.com/api/oauth/usage'
KEYCHAIN_ITEM = 'Claude Code-credentials'


def account_setting():
    """(on?, why it's off). Flags win, then `account on|off`, then the
    default: on, except on macOS, where reading the login can pop up a
    Keychain password dialog."""
    if '--no-account' in ARGS or env_flag('CLAUDE_PACE_NO_ACCOUNT'):
        return False, 'flag'
    if '--account' in ARGS:
        return True, None
    saved = read_config().get('account')
    if saved is False:
        return False, 'config'
    if saved is True or not IS_MAC:
        return True, None
    return False, 'mac'


USE_ACCOUNT, ACCOUNT_OFF = account_setting()
ACCOUNT = {'tried': None, 'ok': None, 'why': None, 'next_at': 0.0, 'backoff': 0}
LOGIN = {'data': None, 'read_at': 0.0, 'denied': False, 'retried': False}  # the Keychain copy (macOS), in memory only
FETCH_LOCK = threading.Lock()
REDRAW = threading.Event()

ACCOUNT_WHY = {
    'no_login': 'no Claude Code login found on this computer',
    'no_login_mac': 'no Claude Code login found in the Keychain - sign in to Claude Code in a terminal',
    'keychain_denied': 'macOS Keychain access was refused - press R to ask again',
    'expired': 'the saved login has expired - send one message in Claude Code in a terminal to renew it',
    'offline': "couldn't reach Anthropic - offline?",
    'limited': 'Anthropic is limiting how often usage can be checked',
    'changed': "Anthropic's answer has changed - this check needs an update",
    'error': 'the check ran into an unexpected problem - press R to try again',
}


def whole(v):
    return int(v) if isinstance(v, float) and v.is_integer() else v


def login_file():
    """(login, None) from Claude Code's saved-login file, (None, 'missing')
    if there's no file, (None, 'no_login') if it can't be read."""
    try:
        d = json.loads((CONFIG_DIR / '.credentials.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return None, 'missing'
    except (OSError, ValueError):
        return None, 'no_login'
    login = d.get('claudeAiOauth') if isinstance(d, dict) else None
    return (login, None) if isinstance(login, dict) else (None, 'no_login')


def keychain_names():
    """Keychain entries Claude Code may have saved its login under. With
    CLAUDE_CONFIG_DIR set, the name gets a suffix made from that folder -
    the documented behaviour, but the exact suffix isn't documented; this
    is the one reported by users. Missing entries don't pop up a dialog, so
    trying the next name costs nothing."""
    names = [KEYCHAIN_ITEM]
    custom = os.environ.get('CLAUDE_CONFIG_DIR')
    if custom:
        import hashlib
        names.insert(0, f'{KEYCHAIN_ITEM}-{hashlib.sha256(custom.encode()).hexdigest()[:8]}')
    return names


def expired(login, now):
    return is_num(login.get('expiresAt')) and login['expiresAt'] / 1000 <= now


def read_login(now, force=False):
    """(login, None) or (None, why). The file first (Windows, Linux, and macOS
    over SSH). On macOS, the Keychain: read once and kept in memory, and read
    again only after that copy expires (Claude Code renews it) or on R - each
    read can ask for your password."""
    login, why = login_file()
    if login:
        return login, None
    if why != 'missing' or not IS_MAC:
        return None, 'no_login'
    cached = LOGIN['data']
    if cached and (not expired(cached, now) or (LOGIN['read_at'] >= cached['expiresAt'] / 1000 and not force)):
        return cached, None
    if LOGIN['denied'] and not force:
        return None, 'keychain_denied'
    for name in keychain_names():
        try:
            r = subprocess.run(['security', 'find-generic-password', '-s', name, '-w'],
                               capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            LOGIN['denied'] = True
            return None, 'keychain_denied'
        if r.returncode == 44:  # no such entry
            continue
        LOGIN['read_at'] = now
        if r.returncode != 0:
            LOGIN['denied'] = True
            return None, 'keychain_denied'
        try:
            d = json.loads(r.stdout.strip())
            login = d.get('claudeAiOauth') if isinstance(d, dict) else None
        except ValueError:
            login = None
        if not isinstance(login, dict):
            return None, 'no_login_mac'
        LOGIN.update(data=login, denied=False)
        return login, None
    return None, 'no_login_mac'


def retry_after(headers, now):
    """Seconds the server asked us to wait, if it said."""
    v = (headers or {}).get('Retry-After') if headers is not None else None
    if not v:
        return None
    try:
        return max(float(v), 0)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return max(parsedate_to_datetime(v).timestamp() - now, 0)
    except (TypeError, ValueError, IndexError):
        return None


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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not unchanged or not HISTORY.exists():
        f, w = five or {}, week or {}
        line = {'t': now, 'h': f.get('used_percentage'), 'hr': f.get('resets_at'),
                'w': w.get('used_percentage'), 'wr': w.get('resets_at'), 'src': 'account'}
        with HISTORY.open('a', encoding='utf-8', newline='\n') as fh:
            fh.write(json.dumps(line, separators=(',', ':')) + '\n')
    write_atomic(ACCOUNT_FILE, json.dumps(nxt, separators=(',', ':')))


def fetch_account(now, force=False):
    """Ask your Claude account for the 5-hour and weekly numbers - the same
    ones Claude Code's usage screen shows. Uses the login key Claude Code
    saved and sends it only to Anthropic. This connection isn't documented,
    so any failure just leaves the status-bar numbers in place. If Anthropic
    says "too many requests", wait as long as it asks and at least twice as
    long as last time."""
    if not FETCH_LOCK.acquire(blocking=False):
        return  # a fetch is already running
    try:
        _fetch_account(now, force)
    except Exception:  # never let the background check die or print over the dashboard
        ACCOUNT.update(ok=False, why='error')
    finally:
        FETCH_LOCK.release()


def no_redirects():
    """An opener that never follows a redirect. Python's own copies every
    header - the login key included - onto wherever the redirect points, even
    a plain-http address; the key must only ever go to ACCOUNT_URL. A
    redirect arrives as an error instead."""
    import urllib.request

    class Refuse(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    return urllib.request.build_opener(Refuse)


def _fetch_account(now, force):
    import http.client
    import urllib.error
    import urllib.request
    ACCOUNT['tried'] = now
    ACCOUNT['next_at'] = now + ACCOUNT_EVERY_S

    def fail(why):
        ACCOUNT.update(ok=False, why=why)

    login, why = read_login(now, force)
    if not login:
        return fail(why)
    key = login.get('accessToken')
    if not key:
        return fail('no_login_mac' if IS_MAC and LOGIN['data'] is login else 'no_login')
    if expired(login, now):
        return fail('expired')
    req = urllib.request.Request(ACCOUNT_URL, headers={
        'Authorization': f'Bearer {key}', 'anthropic-beta': 'oauth-2025-04-20',
        'Content-Type': 'application/json', 'User-Agent': f'{NAME}/{VERSION}'})
    try:
        with no_redirects().open(req, timeout=15) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            ACCOUNT['backoff'] = min(max(2 * ACCOUNT['backoff'], 2 * ACCOUNT_EVERY_S), BACKOFF_MAX_S)
            wait = max(min(retry_after(e.headers, now) or 0, 3600), ACCOUNT['backoff'])
            ACCOUNT['next_at'] = now + wait
            return fail('limited')
        if e.code == 401:
            if LOGIN['data'] is login and not LOGIN['retried']:
                # Claude Code may have renewed it early: allow one fresh Keychain read per launch
                LOGIN.update(data=dict(login, expiresAt=0), read_at=-1.0, retried=True)
            return fail('expired')
        return fail('changed')
    except (OSError, ValueError, http.client.HTTPException):  # incl. a garbled or cut-off answer
        return fail('offline')

    def meter(m):
        if not isinstance(m, dict) or not is_num(m.get('utilization')):
            return None
        try:
            reset = parse_iso(m['resets_at']) if m.get('resets_at') else None
        except (TypeError, ValueError, AttributeError):
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
    ACCOUNT.update(ok=True, why=None, backoff=0)


def refresh_now():
    """R key: fresh numbers from the account and a fresh plan name, without
    freezing the window while they load (about a second). While Anthropic is
    asking us to slow down, R doesn't ask again - that would only extend the
    wait."""
    now = time.time()
    if USE_ACCOUNT and not (ACCOUNT['why'] == 'limited' and now < ACCOUNT['next_at']):
        fetch_account(now, force=True)
    refresh_plan()
    REDRAW.set()


def account_tick():
    """One round of the background check. Never raises."""
    try:
        if USE_ACCOUNT and time.time() >= ACCOUNT['next_at']:
            fetch_account(time.time())
            REDRAW.set()
    except Exception:
        ACCOUNT.update(ok=False, why='error')


def account_loop():
    """Ask the account every ACCOUNT_EVERY_S while the dashboard is open."""
    while True:
        account_tick()
        time.sleep(5)


def account_why(now):
    why = ACCOUNT['why']
    text = ACCOUNT_WHY.get(why, why)
    if why == 'limited':
        text += f' - next try in {fmt_span(max(ACCOUNT["next_at"] - now, 0))}'
    return text


# ── frame ─────────────────────────────────────────────────────────────────

NO_PLAN_TEXT = ("This Claude Code login uses an API key or a cloud provider (Bedrock, Vertex), which have no "
                "5-hour or weekly limits to track. Those limits come with Claude Pro, Max and Team plans - "
                "sign in with one using /login in Claude Code.")


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
    except ValueError:  # not UTF-8 text
        return None, 'is damaged'
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
        if not_a_plan():
            return 'NO PLAN LIMITS', AMBER, NO_PLAN_TEXT, None
        if PLAN.get('method') == 'none':
            return 'WAITING', AMBER, ("Claude Code isn't signed in. Sign in with your Claude plan, then send "
                                      'any message to start the feed.'), None
        return 'WAITING', AMBER, 'Send any message in Claude Code once to start the feed.', None

    bar = data if has_numbers(data) else None
    best = max((d for d in (bar, acct) if d), key=lambda d: d['saved_at'], default=None)
    check = data.get('last_check') if data and isinstance(data.get('last_check'), dict) else {}
    failing = (check.get('ok') is False and is_num(check.get('at')) and
               (best is None or check['at'] >= best['saved_at']))
    if failing:
        if best is None and check.get('reason') == 'no_plan':
            return 'NO PLAN LIMITS', AMBER, NO_PLAN_TEXT, None
        tail = ' Last good numbers shown below.' if best else ' No good numbers have been saved yet.'
        return 'NOT UPDATING', RED, failure_text(check) + tail, best
    if best is None:
        return "CAN'T READ", RED, 'The saved usage file has no usable numbers. It will fix itself on your next Claude Code message.', None
    age = now - best['saved_at']
    if USE_ACCOUNT and ACCOUNT['ok'] is False and age > 2 * ACCOUNT_EVERY_S:
        return ('IDLE', AMBER, f'No new numbers since {fmt_time(best["saved_at"])}. Checking your account '
                f'directly failed: {account_why(now)}.', best)
    if age > STALE_S:
        tail = ("If you've used the desktop app or claude.ai since then, these numbers are behind."
                if not USE_ACCOUNT else 'Checking your account directly...')
        return 'IDLE', AMBER, f'No Claude Code activity since {fmt_time(best["saved_at"])}. {tail}', best
    return 'LIVE', GREEN, None, best


def account_hint():
    """On macOS, how to turn the account check on - it's off there until asked."""
    if USE_ACCOUNT or ACCOUNT_OFF != 'mac':
        return None
    if LVL >= 4:
        return 'A = check your account (Keychain)'
    if LVL >= 3 or NARROW:
        return 'A = also check your account directly (macOS may ask for Keychain access)'
    return ('Press A to also check your account directly - current even when you use the desktop app. It reads '
            'your Claude login from the macOS Keychain, which may ask for your password. `claude-pace '
            'account on` keeps it on.')


def frame(now, cols):
    global NARROW
    width = min(max(cols - 2, 44), 92)
    NARROW = width < 68
    status, scol, message, data = load(now)

    pill = paint(('× ' if scol == RED else '● ') + status, scol, bold=True)
    right = f'{pill}   {paint(fmt_time(now), MUTED)} '
    if LVL >= 4:  # header and logo share one line
        text, _, _ = plan_info()
        out = [spread(f' {paint("✻", ACCENT, bold=True)} {small_logo(text)}', right, width)]
    else:
        head = spread(f' {paint("✻", ACCENT, bold=True)} {paint("CLAUDE PACE", TEXT, bold=True)}', right, width)
        out = ['', head, ''] + plan_lines(width) + ['']
    if message:
        out += [' ' + line for line in wrap(message, width - 2, scol if scol == RED else MUTED)]
        out.append('')
    hint = account_hint()
    if hint:
        out += [' ' + line for line in wrap(hint, width - 2, FAINT)]
        out.append('')
    if data is None:
        return out, width

    samples = read_history()
    m = estimate(samples)
    t = today_budget(data.get('seven_day'), samples, now, 'account' if data.get('source') == 'account' else None)
    out += limits_panel(data, t, width, now)
    out += today_panel(t, m, width, now)
    out += sessions_panel(m, data.get('seven_day'), width)

    saved = data['saved_at']
    source = 'your account' if data.get('source') == 'account' else 'Claude Code'
    info = f'Updated {fmt_time(saved)} ({fmt_ago(now - saved)}) from {source}'
    if LVL >= 3 and NARROW:  # shares its line with the keys
        info = f'Updated {fmt_ago(now - saved)}'
    if m['since'] and LVL < 3 and not NARROW:  # no saved readings (e.g. the log was deleted): no log note
        log = f' · log: {m["count"]} reading{"s" if m["count"] != 1 else ""} since {fmt_date(m["since"])}'
        if len(info + log) < width:
            info += log
    out.append(clip(' ' + paint(info, FAINT), width))
    return out, width


def keys_line(now):
    acct = ACCOUNT_OFF == 'mac' and not USE_ACCOUNT
    a_short = f'  {paint("A", TEXT, bold=True)} {paint("account", MUTED)}' if acct else ''
    if NARROW or LVL >= 3:
        return (f' {paint("R", TEXT, bold=True)} {paint("refresh", MUTED)}{a_short}  '
                f'{paint("Q", TEXT, bold=True)} {paint("quit", MUTED)}')
    return (f' {paint("R", TEXT, bold=True)} {paint("refresh", MUTED)}{a_short}   {paint("Q", TEXT, bold=True)} '
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
        lines = ['', paint(f' × WINDOW ERROR  {e}', RED, bold=True), paint(' Still running - press R to try again.', MUTED)]
    finally:
        LVL = 0
    # Never wider or taller than the window: overflow would scroll the top
    # (the logo) off screen.
    lines = [clip(line, cols) for line in lines[:rows]]
    return [asciify(line) for line in lines] if ASCII else lines


# ── terminal ──────────────────────────────────────────────────────────────

def enable_vt():
    """UTF-8 output everywhere; on Windows, also turn on colour escape codes
    in the console. If the console can't do them, drop colour."""
    try:
        if not ASCII:
            sys.stdout.reconfigure(encoding='utf-8')
    except (AttributeError, ValueError):
        pass
    if not IS_WIN:
        return
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)) and not k.SetConsoleMode(h, mode.value | 0x0004):
            if not os.environ.get('CLAUDE_PACE_COLOR'):
                set_depth(0)
    except Exception:
        pass


class Keys:
    """Single key presses without Enter. Windows: the console's own key
    reader. Elsewhere: the terminal switched to deliver keys immediately
    (Ctrl+C and Ctrl+Z still work), put back exactly as it was on the way
    out. No keys at all when input isn't a terminal."""

    def __init__(self):
        self.fd = self.saved = None

    def start(self):
        if IS_WIN or self.saved is not None:
            return
        try:
            import termios
            import tty
            if not sys.stdin.isatty():
                return
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except Exception:  # no termios, or not a real terminal
            self.saved = None

    def stop(self):
        if self.saved is None:
            return
        try:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        except Exception:
            pass
        self.saved = None

    def get(self):
        if IS_WIN:
            if not (msvcrt and msvcrt.kbhit()):
                return None
            k = msvcrt.getwch()
            if k in ('\x00', '\xe0'):  # arrows / function keys: two codes, ignore both
                msvcrt.getwch()
                return None
            return k
        if self.saved is None:
            return None
        import select
        try:
            ready = select.select([self.fd], [], [], 0)[0]
            data = os.read(self.fd, 64) if ready else b''
        except (OSError, ValueError):
            return None
        s = data.decode('utf-8', 'ignore')
        if not s or (s.startswith('\x1b') and len(s) > 1):  # arrow keys etc. arrive as Esc + more
            return None
        return s[0]


SCREEN_ON = '\x1b[?1049h\x1b[?25l'  # alternate screen, hide cursor
SCREEN_OFF = '\x1b[?25h\x1b[?1049l'
TITLE = '\x1b]0;Claude Pace\x07'


def set_title():
    if not IS_WIN:
        sys.stdout.write('\x1b[22;0t')  # save the old title (restored on exit)
    sys.stdout.write(TITLE)
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW('Claude Pace')
        except Exception:
            pass


def enable_account():
    """A key (macOS): turn the account check on for this session."""
    global USE_ACCOUNT
    if USE_ACCOUNT:
        return
    USE_ACCOUNT = True
    ACCOUNT['next_at'] = 0.0
    threading.Thread(target=account_loop, daemon=True).start()
    REDRAW.set()


def dashboard(args):
    enable_vt()
    refresh_plan()
    if '--once' in args or not sys.stdout.isatty():
        print('\n'.join(render()))
        return
    w = sys.stdout.write
    keys = Keys()

    def screen_on():
        keys.start()
        w(SCREEN_ON)
        sys.stdout.flush()

    def screen_off():
        w(SCREEN_OFF)
        sys.stdout.flush()
        keys.stop()

    if not IS_WIN:
        import signal

        def on_exit(sig, _frame):
            raise SystemExit(0)

        def on_suspend(sig, _frame):  # Ctrl+Z: give the terminal back, then really stop
            screen_off()
            signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTSTP)

        def on_resume(sig, _frame):
            signal.signal(signal.SIGTSTP, on_suspend)
            screen_on()
            REDRAW.set()

        for name, fn in (('SIGTERM', on_exit), ('SIGHUP', on_exit), ('SIGTSTP', on_suspend), ('SIGCONT', on_resume)):
            if hasattr(signal, name):
                signal.signal(getattr(signal, name), fn)

    set_title()
    # The background checks catch their own errors; this is the last line of
    # defence against a traceback being printed over the dashboard.
    threading.excepthook = lambda _args: None
    if not IS_MAC:  # on macOS, `claude auth status` can touch the Keychain: startup and R only
        threading.Thread(target=plan_loop, daemon=True).start()
    if USE_ACCOUNT:
        threading.Thread(target=account_loop, daemon=True).start()
    screen_on()
    try:
        last, last_size = 0.0, None
        while True:
            size = shutil.get_terminal_size()
            if time.time() - last >= REFRESH_S or size != last_size or REDRAW.is_set():
                REDRAW.clear()
                w('\x1b[H' + '\n'.join(line + '\x1b[K' for line in render(size=size)) + '\x1b[J')
                sys.stdout.flush()
                last, last_size = time.time(), size
            key = keys.get()
            if key in ('r', 'R'):
                last = 0.0  # redraw from the files now...
                threading.Thread(target=refresh_now, daemon=True).start()  # ...and again with fresh numbers
            elif key in ('a', 'A') and ACCOUNT_OFF == 'mac':
                enable_account()
            elif key in ('q', 'Q', '\x1b', '\x03'):
                break
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    finally:
        screen_off()
        if not IS_WIN:
            w('\x1b[23;0t')  # restore the old title
            sys.stdout.flush()


# ── status line ───────────────────────────────────────────────────────────
# What Claude Code runs after every message. Copies the usage numbers into
# the data folder in the same format the old Node version (statusline.js)
# wrote, prints the status bar text, and never fails: a crashing status line
# shows nothing in Claude Code, on every message.

def js_truthy(v):
    if v is None or v is False:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v == v and v != 0
    if isinstance(v, str):
        return v != ''
    return True


def js_int_float(s):
    """Parse JSON numbers the way JavaScript prints them back: 16.0 stays 16."""
    f = float(s)
    return int(f) if f.is_integer() and abs(f) < 2 ** 53 else f


def dumps(v):
    return json.dumps(v, separators=(',', ':'), ensure_ascii=False)


def usage_key(l):
    if not js_truthy(l):
        return '-'
    l = l if isinstance(l, dict) else {}
    try:
        minutes = math.floor((l.get('resets_at') if js_truthy(l.get('resets_at')) else 0) / 60 + 0.5)
    except TypeError:
        minutes = None
    return f'{dumps(l.get("used_percentage"))}|{minutes}'


def append_history(prev, nxt):
    """One line per change of either percentage or reset time. Duplicate lines
    from two sessions racing are harmless; they add zero change."""
    unchanged = (usage_key(prev.get('five_hour')) == usage_key(nxt['five_hour']) and
                 usage_key(prev.get('seven_day')) == usage_key(nxt['seven_day']))
    if unchanged and HISTORY.exists():
        return
    f = nxt['five_hour'] if isinstance(nxt['five_hour'], dict) else {}
    w = nxt['seven_day'] if isinstance(nxt['seven_day'], dict) else {}
    line = {'t': nxt['saved_at']}
    for k, src, name in (('h', f, 'used_percentage'), ('hr', f, 'resets_at'),
                         ('w', w, 'used_percentage'), ('wr', w, 'resets_at')):
        if name in src:
            line[k] = src[name]
    with HISTORY.open('a', encoding='utf-8', newline='\n') as fh:
        fh.write(dumps(line) + '\n')


def save_usage(d):
    """last_check records whether the latest run got numbers. A failure is
    only recorded once the session has finished a turn (current_usage is set)
    - a fresh session with no messages yet legitimately has no rate_limits."""
    try:
        now = int(time.time())
        rl = d.get('rate_limits') if isinstance(d, dict) else None
        rl = rl if isinstance(rl, dict) else {}
        try:
            prev = json.loads(LATEST.read_text(encoding='utf-8'), parse_float=js_int_float)
        except (OSError, ValueError):
            prev = {}
        prev = prev if isinstance(prev, dict) else {}
        cw = d.get('context_window') if isinstance(d, dict) else None
        if js_truthy(rl.get('five_hour')) or js_truthy(rl.get('seven_day')):
            nxt = {'saved_at': now,
                   'five_hour': rl.get('five_hour') if js_truthy(rl.get('five_hour')) else None,
                   'seven_day': rl.get('seven_day') if js_truthy(rl.get('seven_day')) else None,
                   'last_check': {'at': now, 'ok': True}}
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            try:
                append_history(prev, nxt)
            except Exception:
                pass
        elif isinstance(cw, dict) and js_truthy(cw.get('current_usage')):
            reason = 'no_plan' if d.get('rate_limits_available') is False else 'missing'
            nxt = dict(prev, last_check={'at': now, 'ok': False, 'reason': reason})
        else:
            return
        write_atomic(LATEST, dumps(nxt))
    except Exception:
        pass


def node_basename(p):
    """Last part of a path, the way Node's path.basename does it."""
    if not isinstance(p, str):
        p = str(p)
    if IS_WIN:
        p = re.sub(r'^[A-Za-z]:', '', p)
        p = p.rstrip('/\\')
        return re.split(r'[/\\]', p)[-1] if p else ''
    p = p.rstrip('/')
    return p.split('/')[-1] if p else ''


def fmt_tokens(n):
    if not is_num(n):
        return str(n)
    if n >= 1000:
        from decimal import Decimal, ROUND_HALF_UP  # JavaScript's toFixed rounds halves up
        return f'{Decimal(n / 1000).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)}k'
    return str(whole(n))


def status_text(d):
    """Model, this turn's prompt-cache READ (reused) vs CREATE (freshly
    written) tokens, and the folder. Big READ = the cache was still warm;
    ~0 READ with a big CREATE = it expired and was rebuilt."""
    model = 'Claude'
    try:
        if d is None:
            return f'[{model}]'
        g = d.get if isinstance(d, dict) else (lambda k, v=None: v)
        mdl = g('model')
        name = mdl.get('display_name') if isinstance(mdl, dict) else None
        if js_truthy(name):
            model = name if isinstance(name, str) else dumps(name)
        ws = g('workspace')
        where = g('cwd') if js_truthy(g('cwd')) else (ws.get('current_dir') if isinstance(ws, dict) else None)
        folder = node_basename(where if js_truthy(where) else '.')
        cw = g('context_window')
        u = cw.get('current_usage') if isinstance(cw, dict) else None
        if not js_truthy(u):
            # null before the first API call, or this Claude Code build doesn't
            # expose current_usage in the statusline payload yet.
            return f'[{model}] | cache: (no turn data) | {folder}'
        u = u if isinstance(u, dict) else {}
        read = u.get('cache_read_input_tokens') if js_truthy(u.get('cache_read_input_tokens')) else 0
        create = u.get('cache_creation_input_tokens') if js_truthy(u.get('cache_creation_input_tokens')) else 0
        # Verdict heuristic: warm if most of the big context block came from the
        # cache (READ); "rebuilt" if the prefix had to be re-written (CREATE) with
        # almost nothing read back. The raw numbers are the real signal.
        verdict = ''
        if read + create > 0:
            if read >= create:
                verdict = ' WARM'
            elif read < 2000 and create > 5000:
                verdict = ' REBUILT'
        return f'[{model}] | cache read {fmt_tokens(read)} / create {fmt_tokens(create)}{verdict} | {folder}'
    except Exception:
        return f'[{model}]'


def git_bash():
    """Git Bash on Windows, which Claude Code prefers for commands. Never the
    `bash` from WSL, which lives in System32 and runs a different system."""
    p = os.environ.get('CLAUDE_CODE_GIT_BASH_PATH')
    if p and Path(p).is_file():
        return p
    git = shutil.which('git')
    candidates = []
    if git:
        root = Path(git).resolve().parent.parent
        candidates += [root / 'bin' / 'bash.exe', root.parent / 'bin' / 'bash.exe']
    for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)'), os.environ.get('LOCALAPPDATA')):
        if base:
            candidates += [Path(base) / 'Git' / 'bin' / 'bash.exe', Path(base) / 'Programs' / 'Git' / 'bin' / 'bash.exe']
    return next((str(c) for c in candidates if c.is_file()), None)


def shell_argv(cmd):
    """How Claude Code would run a status line command on this system."""
    if IS_WIN:
        bash = git_bash()
        if bash:
            return [bash, '-c', cmd]
        ps = shutil.which('pwsh') or shutil.which('powershell') or 'powershell'
        return [ps, '-NoProfile', '-NonInteractive', '-Command', cmd]
    return [os.environ.get('SHELL') or '/bin/sh', '-c', cmd]


def kill_tree(p):
    """Stop a chained status line and everything it started. Killing just the
    shell isn't enough: a program it ran keeps going, and on Windows keeps
    the output pipe open."""
    try:
        if IS_WIN:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(p.pid)], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, creationflags=NO_WINDOW)
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)  # its own process group, see run_chained
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        p.kill()
    except OSError:
        pass


def run_chained(entry, raw):
    """The status line you had before install: same input, environment and
    folder. Its output, or None if it failed or took longer than
    CHAIN_TIMEOUT_S. Input and output go through threads of their own and
    only the command itself is waited for, so neither a command that never
    reads its input nor a program it leaves running can hold us past the
    time limit."""
    cmd = entry.get('command') if isinstance(entry, dict) else None
    if not isinstance(cmd, str) or not cmd.strip():
        return None
    extra = {'creationflags': NO_WINDOW} if IS_WIN else {'start_new_session': True}
    try:
        p = subprocess.Popen(shell_argv(cmd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, **extra)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    chunks = []

    def feed():
        try:
            p.stdin.write(raw)
        except (OSError, ValueError):
            pass
        try:
            p.stdin.close()
        except (OSError, ValueError):
            pass

    def drain():
        try:
            for block in iter(lambda: p.stdout.read1(65536), b''):
                chunks.append(block)
        except (OSError, ValueError):
            pass

    reader = threading.Thread(target=drain, daemon=True)
    for t in (threading.Thread(target=feed, daemon=True), reader):
        t.start()
    deadline = time.monotonic() + CHAIN_TIMEOUT_S
    try:
        code = p.wait(timeout=CHAIN_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        kill_tree(p)
        return None
    reader.join(max(deadline - time.monotonic(), 0.2))  # output a leftover program holds open can't keep us
    out = b''.join(chunks)
    if code != 0 and not out.strip():
        return None
    return out


def statusline_main():
    out = sys.stdout.buffer
    try:
        raw = sys.stdin.buffer.read()
    except Exception:
        raw = b''
    try:
        try:
            d = json.loads(raw.decode('utf-8-sig', 'replace'), parse_float=js_int_float)
            parsed = True
        except ValueError:
            d, parsed = None, False
        if parsed:
            save_usage(d)
        chain = read_config().get('chain')
        if chain:
            text = run_chained(chain, raw)
            if text is not None:
                out.write(text)
                out.flush()
                return
        out.write(((status_text(d) if parsed else '[Claude]') + '\n').encode('utf-8', 'replace'))
    except BaseException:
        try:
            out.write(b'[Claude]\n')
        except Exception:
            pass
    try:
        out.flush()
    except Exception:
        pass


# ── install / uninstall ───────────────────────────────────────────────────

SAFE_WORD = re.compile(r'^[A-Za-z0-9_.~:/\\-]+$')


def win_short(p):
    """Windows' short 8.3 form of a folder (no spaces), if the drive keeps them."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 32768)
        if 0 < n < 32768:
            return buf.value
    except Exception:
        pass
    return str(p)


def command_word(p):
    """A path written so every shell Claude Code may use reads it the same:
    Git Bash, PowerShell, sh, bash, zsh. On Windows that means forward
    slashes and, where it has spaces, the short 8.3 folder name - a quoted
    first word is not run by PowerShell. Returns (word, fully safe?)."""
    p = Path(p)
    if not IS_WIN:
        import shlex
        return shlex.quote(str(p)), True
    s = str(p)
    if not SAFE_WORD.match(s):
        s = str(Path(win_short(p.parent)) / p.name)  # keep the file name itself readable
    s = s.replace('\\', '/')
    if SAFE_WORD.match(s):
        return s, True
    return f'"{s}"', False


def status_command(run):
    words = [command_word(p) for p in run]
    return ' '.join(w for w, _ in words) + ' statusline', words[0][1]


# `claude_pace.py statusline`, the installed `claude-pace(.exe) statusline`,
# `uvx claude-pace statusline`, and the pre-release file claude_usage.py.
OUR_COMMAND = re.compile(r'(claude[_-]pace(\.py|\.exe|\.cmd)?|claude[_-]usage\.py)["\']?\s+statusline\b', re.I)


def old_node_logger(cmd):
    """The Node status line this tool used before it moved to Python. It's
    recognised by what's inside the file - it wrote both of this tool's data
    files - never by its name or folder, so someone else's statusline.js is
    never mistaken for it (and thrown away)."""
    m = re.search(r'''["']?([^"']*?statusline\.js)["']?''', cmd)
    if not m:
        return False
    js = m.group(1).strip()
    js = js.split(' ', 1)[1] if js.lower().startswith('node ') else js
    try:
        path = Path(os.path.expanduser(js.replace('$HOME', str(Path.home())).strip()))
        with path.open(encoding='utf-8', errors='replace') as fh:
            text = fh.read(200_000)
    except (OSError, ValueError):
        return False
    return 'usage-history.jsonl' in text and 'usage-latest.json' in text and 'rate_limits' in text


def is_ours(entry):
    """Our status line: `claude-pace statusline` in any form (also under the
    pre-release name claude_usage.py), or the old Node version."""
    cmd = entry.get('command') if isinstance(entry, dict) else None
    if not isinstance(cmd, str):
        return False
    return bool(OUR_COMMAND.search(cmd.replace('\\', '/'))) or old_node_logger(cmd)


class Refused(Exception):
    """settings.json can't be changed safely; the message says why."""


def finite_float(s):
    f = float(s)
    if not math.isfinite(f):
        raise Refused(f'holds the number {s}, which is too large to write back unchanged')
    return f


def bad_constant(s):
    raise ValueError(f'{s} is not allowed in JSON')


def load_settings(path):
    """(settings dict, None) or (None, why). A missing file is an empty one.
    Anything that couldn't be written back exactly as Claude Code reads it -
    NaN / Infinity, or a number too big for a float - is refused."""
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except FileNotFoundError:
        return {}, None
    except OSError as e:
        return None, f"couldn't be opened ({e.strerror or e})"
    except ValueError:
        return None, "isn't UTF-8 text"
    if not raw.strip():
        return {}, None
    try:
        d = json.loads(raw, parse_float=finite_float, parse_constant=bad_constant)
    except Refused as e:
        return None, str(e)
    except ValueError as e:
        return None, f"isn't valid JSON ({e})"
    return (d, None) if isinstance(d, dict) else (None, "doesn't hold a JSON object")


def prune_backups(path, keep):
    """Delete all but the newest `keep` of our settings.json backups."""
    ours = sorted(path.parent.glob(f'{path.name}.bak-claude-pace-*'))
    for old in ours[:max(len(ours) - keep, 0)]:
        try:
            old.unlink()
        except OSError:
            pass


def save_settings(path, settings):
    """Back the old file up, then write the new one in one step, in the
    same layout Claude Code uses (2-space indent, keys in their order).
    If settings.json is a link (dotfile managers make these), the file it
    points to is written, so the link stays; the new file keeps the old
    one's permissions. Raises Refused if the file is read-only, OSError if
    writing fails - in both cases nothing was changed."""
    text = json.dumps(settings, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    real = Path(os.path.realpath(path))
    backup = mode = None
    if real.exists():
        if not os.access(real, os.W_OK):
            raise Refused('is read-only')
        mode = stat.S_IMODE(real.stat().st_mode)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        backup = path.with_name(f'{path.name}.bak-claude-pace-{stamp}')
        n = 2
        while backup.exists():  # two runs in the same second
            backup = path.with_name(f'{path.name}.bak-claude-pace-{stamp}-{n}')
            n += 1
        shutil.copy2(real, backup)
    write_atomic(real, text, mode)
    prune_backups(path, KEEP_BACKUPS)
    return backup


def write_config(cfg, drop_old=True):
    write_atomic(TRACKER_CONFIG, json.dumps(cfg, indent=2) + '\n')
    if drop_old:
        try:
            OLD_CONFIG.unlink()  # moved over to the new name
        except OSError:
            pass


def desktop_dir():
    """The desktop folder inside the home folder. On Windows the desktop can
    be moved (e.g. into OneDrive), so ask Windows - but only when the home
    folder is the real profile, so a changed HOME never writes elsewhere."""
    d = Path.home() / 'Desktop'
    if d.is_dir():
        return d
    if IS_WIN:
        try:
            import ctypes

            def folder(csidl):
                buf = ctypes.create_unicode_buffer(260)
                ok = ctypes.windll.shell32.SHGetFolderPathW(0, csidl, 0, 0, buf) == 0 and buf.value
                return Path(buf.value) if ok else None
            profile, desk = folder(0x28), folder(0x10)  # profile, desktop
            if profile and desk and os.path.normcase(str(profile)) == os.path.normcase(str(Path.home())):
                return desk
        except Exception:
            pass
    return None


def launcher_files(run, desktop, command=True):
    """(path, contents, executable?) for each launcher to create. `run` is
    the program and arguments that open the dashboard. No `claude-pace`
    command when a package manager (pip, pipx, uv) already made one."""
    home = Path.home()
    out = []
    if IS_WIN:
        line = ' '.join(f'"{str(p).replace("%", "%%")}"' for p in run)
        if command:
            out.append((home / '.local' / 'bin' / 'claude-pace.cmd',
                        f'@echo off\r\nrem {MARK}, made by claude-pace install\r\n{line} %*\r\n', False))
        d = desktop_dir() if desktop else None
        if d:
            out.append((d / 'Claude Pace.bat',
                        f'@echo off\r\nrem {MARK}, made by claude-pace install\r\ntitle Claude Pace\r\n'
                        f'{line}\r\nif errorlevel 1 pause\r\n', False))
        return out
    import shlex
    line = ' '.join(shlex.quote(str(p)) for p in run)
    if command:
        out.append((home / '.local' / 'bin' / 'claude-pace', f'#!/bin/sh\n# {MARK}, made by claude-pace install\n'
                                                             f'exec {line} "$@"\n', True))
    if desktop and IS_MAC and desktop_dir():
        out.append((desktop_dir() / 'Claude Pace.command', f'#!/bin/sh\n# {MARK}\nexec {line}\n', True))
    elif desktop:
        q = ' '.join(f'"{p}"' if ' ' in str(p) else str(p) for p in run)
        out.append((home / '.local' / 'share' / 'applications' / 'claude-pace.desktop',
                    f'[Desktop Entry]\n# {MARK}\nType=Application\nName=Claude Pace\n'
                    f'Comment=Pace your Claude plan limits across the week\nExec={q}\nTerminal=true\n'
                    f'Categories=Utility;\n', False))
    return out


def in_package():
    """Imported from an installed package (pip, pipx, uv tool, uvx) rather
    than run as a downloaded file."""
    return any(part.lower() in ('site-packages', 'dist-packages') for part in Path(__file__).resolve().parts)


def is_throwaway(path):
    """Inside a cache that can be wiped at any time: `uvx` and `pipx run`
    build their environments there."""
    # Not resolve(): a venv's python is a symlink out of the cache on macOS / Linux.
    parts = [x.lower() for x in Path(os.path.abspath(path)).parts]
    if any(x in ('archive-v0', 'builds-v0', 'environments-v2') for x in parts):
        return True
    for i, x in enumerate(parts[:-1]):
        if x in ('uv', 'pipx') and parts[i + 1] in ('cache', '.cache'):
            return True
        if x in ('cache', '.cache', 'caches') and parts[i + 1] in ('uv', 'pipx'):
            return True
    return False


def entry_point():
    """The `claude-pace` program the package manager made for this
    environment, if there is one."""
    folder = Path(sys.executable).parent
    for name in (('claude-pace.exe',) if IS_WIN else ('claude-pace',)):
        if (folder / name).is_file():
            return folder / name
    return None


def lasting_python():
    """A Python 3.9+ that isn't inside a throwaway cache, for running the
    copy install makes when started from `uvx`."""
    seen = set()
    # Inside uvx the PATH starts with the throwaway environment, so also try
    # the usual system places. Not /usr/bin/python3 on macOS: without Apple's
    # command line tools it's a stub that pops up an install dialog.
    fixed = [] if IS_WIN else ['/opt/homebrew/bin/python3', '/usr/local/bin/python3']
    if not IS_WIN and not IS_MAC:
        fixed.append('/usr/bin/python3')
    for c in (shutil.which('python3'), shutil.which('python'), *[f for f in fixed if os.path.isfile(f)],
              getattr(sys, '_base_executable', None), sys.executable):
        # WindowsApps holds app aliases, often the placeholder that only opens the Store.
        if not c or c in seen or is_throwaway(c) or 'windowsapps' in c.lower():
            continue
        seen.add(c)
        v = check_python(c)
        if v and v >= (3, 9):
            return Path(c)
    return None


def install_target(args):
    """How Claude Code should run us: (status line program + args, dashboard
    program + args, python checked, make the claude-pace command?, note) or
    an error string.

    - Run as a file (the one-line installer, or a manual download): the
      Python running it, plus the file.
    - From pip / pipx / `uv tool install`: the `claude-pace` program in that
      environment. Upgrades rebuild the environment in the same place, so
      the path keeps working, and there's no Python-version folder in it.
    - From `uvx` / `pipx run`: that environment lives in a cache that can be
      wiped, so the file is copied to ~/.claude-pace and run with a Python
      that isn't in the cache."""
    python = Path(sys.executable)
    if '--python' in args:
        i = args.index('--python')
        if i + 1 >= len(args):
            return '--python needs the path to a Python 3.9+ program.'
        python = Path(args[i + 1]).expanduser()
    script = Path(__file__).resolve()
    if not in_package() or '--python' in args:
        return [python, script], [python, script], python, True, None
    if not is_throwaway(sys.executable):
        exe = entry_point()
        if exe:
            return [exe], [exe], None, False, None
        return [python, script], [python, script], python, True, None
    python = lasting_python()
    if not python:
        return ('This is running from a temporary uvx / pipx run environment, and no other Python 3.9+ was '
                'found to run the status line with. Use `uv tool install claude-pace` (or pipx install) '
                'and run `claude-pace install` again.')
    home = Path.home() / '.claude-pace'
    home.mkdir(parents=True, exist_ok=True)
    copy = home / 'claude_pace.py'
    shutil.copyfile(script, copy)
    note = (f'uvx runs from a cache that can be cleared, so the script was copied to {copy}. '
            'For updates, `uv tool install claude-pace` is the lasting way to install it.')
    return [python, copy], [python, copy], python, True, note


def check_python(python):
    """Version of the Python at `python`, or None if it doesn't run (for
    example Windows' placeholder that only opens the Microsoft Store)."""
    try:
        r = subprocess.run([str(python), '-c', 'import sys; print("%d.%d" % sys.version_info[:2])'],
                           capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW)
        major, minor = (int(x) for x in r.stdout.strip().split('.'))
        return (major, minor) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def blockers(settings):
    """Things that would stop Claude Code from running our status line."""
    out = []
    if settings.get('disableAllHooks') is True:
        out.append(f'{SETTINGS} has "disableAllHooks": true, which also switches status lines off.')
    managed = ([Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'ClaudeCode' / 'managed-settings.json']
               if IS_WIN else [Path('/Library/Application Support/ClaudeCode/managed-settings.json')] if IS_MAC
               else [Path('/etc/claude-code/managed-settings.json')])
    for f in managed:
        d, _ = load_settings(f)
        if d and (d.get('statusLine') or d.get('disableAllHooks')):
            out.append(f'Your organisation\'s settings ({f}) set a status line or turn them off; theirs wins.')
    for f in (Path.cwd() / '.claude' / 'settings.json', Path.cwd() / '.claude' / 'settings.local.json'):
        d, _ = load_settings(f)
        if d and d.get('statusLine') and f.resolve() != SETTINGS.resolve():
            out.append(f'In {Path.cwd()} the project sets its own status line ({f}), which replaces this one there.')
    return out


def install(args):
    target = install_target(args)
    if isinstance(target, str):
        print(target)
        return 2 if target.startswith('--python') else 1
    run, dash, python, command, where_note = target
    if python is not None:
        version = check_python(python)
        if not version:
            print(f"{python} doesn't run as Python. Pass a working one with --python.")
            return 1
        if version < (3, 9):
            print(f'{python} is Python {version[0]}.{version[1]}; claude-pace needs 3.9 or newer.')
            return 1

    settings, err = load_settings(SETTINGS)
    if settings is None:
        print(f'Nothing changed: {SETTINGS} {err}. Fix or remove it, then run install again.')
        return 1
    cfg = read_config()
    cmd, cmd_ok = status_command(run)
    current = settings.get('statusLine')
    if current and not is_ours(current):
        cfg['chain'] = current
        note = 'your own status line is kept: ours saves the numbers, then runs yours and shows its output'
    elif current:
        note = 'replaced the previous claude-pace status line' if current.get('command') != cmd else 'already set'
    else:
        cfg.pop('chain', None)
        note = 'there was none before'
    if not SETTINGS.exists():
        cfg['settings_created'] = True  # uninstall removes the file again if nothing else is in it
    entry = dict(current) if isinstance(current, dict) else {}
    entry.update(type='command', command=cmd)
    settings['statusLine'] = entry

    # Remember the status line being replaced BEFORE settings.json changes:
    # if that can't be saved, the user's own status line must not be lost.
    try:
        previous = TRACKER_CONFIG.read_bytes() if TRACKER_CONFIG.exists() else None
        write_config(dict(cfg, installed_command=cmd), drop_old=False)
    except OSError as e:
        print(f"Nothing changed: couldn't save {TRACKER_CONFIG} ({e.strerror or e}).")
        return 1
    try:
        backup = save_settings(SETTINGS, settings)
    except (Refused, OSError) as e:
        why = str(e) if isinstance(e, Refused) else f"couldn't be written ({e.strerror or e})"
        print(f'Nothing changed: {SETTINGS} {why}. Make it writable, then run install again.')
        try:  # put the remembered settings back the way they were
            if previous is None:
                TRACKER_CONFIG.unlink()
            else:
                TRACKER_CONFIG.write_bytes(previous)
        except OSError:
            pass
        return 1

    made = []
    if '--desktop' in args and '--no-launcher' not in args and (IS_WIN or IS_MAC) and not desktop_dir():
        print(f'No desktop folder found in {Path.home()} - skipped the desktop shortcut.')
    if '--no-launcher' not in args:
        for path, text, exe in launcher_files(dash, '--desktop' in args, command):
            try:
                if path.exists() and not our_file(path.read_text(encoding='utf-8', errors='replace')):
                    print(f'Skipped {path}: a file that isn\'t ours is already there.')
                    continue
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('w', encoding='utf-8', newline='') as fh:  # write_text has no newline= before 3.10
                    fh.write(text)
                if exe:
                    path.chmod(0o755)
                made.append(str(path))
            except OSError as e:
                print(f"Couldn't create {path}: {e.strerror or e}")
    # Launchers an earlier install made that this one didn't (e.g. the
    # pre-release claude-usage command) would open an old copy: remove them.
    gone = []
    kept = []
    for p in cfg.get('launchers', []):
        if p in made:
            continue
        if '--no-launcher' not in args and remove_launcher(Path(p)):
            gone.append(p)
        elif Path(p).exists():
            kept.append(p)
    cfg.update(version=VERSION, installed_command=cmd, python=str(python or ''), script=str(run[-1]),
               launchers=sorted(set(kept + made)))
    if where_note:
        cfg['copied'] = str(run[-1])  # the copy made for uvx; uninstall deletes it
    else:
        cfg.pop('copied', None)
    try:
        write_config(cfg)
    except OSError as e:  # the status line to chain is already saved; only the launcher list is missing
        print(f"Warning: couldn't update {TRACKER_CONFIG} ({e.strerror or e}); uninstall may leave launchers behind.")

    print(f'claude-pace {VERSION} installed.')
    print(f'  Status line: {SETTINGS}')
    print(f'    runs  {cmd}')
    print(f'    ({note})')
    if backup:
        print(f'  Backup of your old settings: {backup}')
    for p in made:
        print(f'  Launcher: {p}')
    for p in gone:
        print(f'  Removed old launcher: {p}')
    print(f'  Remembered for uninstall: {TRACKER_CONFIG}')
    if where_note:
        print(f'  Note: {where_note}')
    direct = ' '.join(f'"{p}"' for p in dash)
    cmds = [p for p in made if Path(p).stem.lower() == 'claude-pace']
    if not command:
        print('  Open the dashboard with: ' + ('claude-pace' if shutil.which('claude-pace') else direct))
    elif cmds:
        folder = str(Path(cmds[0]).parent)
        on_path = any(os.path.normcase(os.path.abspath(p)) == os.path.normcase(folder)
                      for p in os.environ.get('PATH', '').split(os.pathsep) if p)
        print('  Open the dashboard with: claude-pace' + ('' if on_path else f'   ({folder} is not on your PATH yet - '
                                                          f'or run: {direct})'))
    else:
        print(f'  Open the dashboard with: {direct}')
    if not cmd_ok:
        print('  Note: the program path has spaces and Windows has no short name for it. Git Bash runs the command '
              'fine; PowerShell may not. Reinstall with --python pointing at a Python without spaces in its path.')
    for b in blockers(settings):
        print(f'  Warning: {b}')
    print('The numbers start with your next message in Claude Code.')
    return 0


def remove_launcher(p):
    """Delete a launcher we made. True if it was ours and is gone."""
    try:
        if p.exists() and our_file(p.read_text(encoding='utf-8', errors='replace')):
            p.unlink()
            return True
    except OSError as e:
        print(f"Couldn't remove {p}: {e.strerror or e}")
    return False


def uninstall(args):
    settings, err = load_settings(SETTINGS)
    if settings is None:
        print(f'Nothing changed: {SETTINGS} {err}.')
        return 1
    cfg = read_config()
    current = settings.get('statusLine')
    if current and is_ours(current):
        if cfg.get('chain'):
            settings['statusLine'] = cfg['chain']
            what = 'put your own status line back'
        else:
            del settings['statusLine']
            what = 'removed the status line'
        try:
            if not settings and cfg.get('settings_created') and Path(os.path.realpath(SETTINGS)) == SETTINGS:
                SETTINGS.unlink()  # install created it and nothing else was added since
                print(f'{SETTINGS}: {what} (install had created the file, so it is gone again).')
            else:
                backup = save_settings(SETTINGS, settings)
                print(f'{SETTINGS}: {what}. Backup: {backup}')
        except (Refused, OSError) as e:
            why = str(e) if isinstance(e, Refused) else f"couldn't be written ({e.strerror or e})"
            print(f'Nothing changed: {SETTINGS} {why}. Make it writable, then run uninstall again.')
            return 1
    elif current:
        print(f'{SETTINGS}: the status line there isn\'t ours any more - left alone.')
    else:
        print(f'{SETTINGS}: no status line set - nothing to undo.')
    for p in cfg.get('launchers', []):
        if remove_launcher(Path(p)):
            print(f'Removed {p}')
    for f in (TRACKER_CONFIG, OLD_CONFIG):
        try:
            f.unlink()
        except OSError:
            pass
    copied = cfg.get('copied')
    if copied and Path(copied).name == 'claude_pace.py' and Path(copied).parent == Path.home() / '.claude-pace':
        try:
            Path(copied).unlink()
            Path(copied).parent.rmdir()  # only if nothing else is in it
            print(f'Removed {Path(copied).parent}')
        except OSError:
            pass
    data = [p for p in (HISTORY, LATEST, ACCOUNT_FILE) if p.exists()]
    if '--purge' in args:
        for p in data + sorted(SETTINGS.parent.glob(f'{SETTINGS.name}.bak-claude-pace-*')):
            try:
                p.unlink()
                print(f'Deleted {p}')
            except OSError as e:
                print(f"Couldn't delete {p}: {e.strerror or e}")
    elif data:
        print(f'Your usage log is kept in {DATA_DIR} (run uninstall --purge to delete it).')
    return 0


def account_cmd(args):
    choice = args[0].lower() if args else ''
    if choice not in ('on', 'off', 'default'):
        on, why = account_setting()
        reasons = {'mac': 'the macOS default', 'config': 'turned off', 'flag': 'turned off by a flag or setting'}
        state = 'on' if on else f'off, {reasons[why]}'
        print(f'Account check: {state}. Change with: claude-pace account on|off|default')
        return 0 if not args else 2
    cfg = read_config()
    if choice == 'default':
        cfg.pop('account', None)
    else:
        cfg['account'] = choice == 'on'
    try:
        write_config(cfg)
    except OSError as e:
        print(f"Couldn't save {TRACKER_CONFIG} ({e.strerror or e}).")
        return 1
    print(f'Account check: {choice}. Saved in {TRACKER_CONFIG}')
    return 0


def main():
    """Also the `claude-pace` program's entry point (pip / pipx / uv)."""
    args = ARGS
    if args[:1] == ['statusline']:
        return statusline_main()
    if '--version' in args:
        print(f'{NAME} {VERSION}')
        return 0
    if '-h' in args or '--help' in args or args[:1] == ['help']:
        print(__doc__.strip())
        return 0
    if args[:1] == ['install']:
        return install(args[1:])
    if args[:1] == ['uninstall']:
        return uninstall(args[1:])
    if args[:1] == ['account']:
        return account_cmd(args[1:])
    unknown = [a for a in args if a not in ('--once', '--account', '--no-account')]
    if unknown:
        print(f'Unknown: {" ".join(unknown)}. Try --help.')
        return 2
    return dashboard(args)


if __name__ == '__main__':
    sys.exit(main() or 0)
