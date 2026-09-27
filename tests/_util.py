"""Shared helpers for the tests. Every test runs against throwaway folders:
the Claude folder, the data folder and the home folder are all pointed at a
temp directory, so nothing on the machine running the tests is touched."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

WIN = os.name == 'nt'
REQUIRE_SHELLS = os.environ.get('CI_REQUIRE_SHELLS') == '1'
ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / 'claude_pace.py'
FIXTURES = Path(__file__).resolve().parent / 'fixtures'
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07')

# Variables from the machine running the tests that would point the script
# at real data or change its behaviour.
_SCRUB = ('CLAUDE_CONFIG_DIR', 'CLAUDE_PACE_DIR', 'CLAUDE_PACE_ASCII', 'NO_COLOR',
          'COLORTERM', 'CLAUDE_PACE_NO_ACCOUNT', 'COLUMNS', 'LINES',
          'TERM', 'TERM_PROGRAM', 'WT_SESSION', 'TMUX')


class Sandbox:
    """A fake home with its own Claude folder. Use as a context manager."""

    def __init__(self, name='sandbox', data_dir=False):
        self.tmp = Path(tempfile.mkdtemp(prefix=f'cu-{name}-'))
        self.home = self.tmp / 'home'
        self.claude = self.home / '.claude'
        self.claude.mkdir(parents=True)
        self.data = (self.tmp / 'data') if data_dir else self.claude
        self.data.mkdir(exist_ok=True)
        self.separate_data = data_dir

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def settings(self):
        return self.claude / 'settings.json'

    def env(self, **extra):
        e = {k: v for k, v in os.environ.items() if k not in _SCRUB}
        e.update(HOME=str(self.home), USERPROFILE=str(self.home),
                 CLAUDE_CONFIG_DIR=str(self.claude), CLAUDE_PACE_NO_ACCOUNT='1',
                 PYTHONIOENCODING='utf-8')
        if os.name == 'nt':  # keep %APPDATA% etc. inside the sandbox too
            e.update(APPDATA=str(self.home / 'AppData' / 'Roaming'),
                     LOCALAPPDATA=str(self.home / 'AppData' / 'Local'))
        if self.separate_data:
            e['CLAUDE_PACE_DIR'] = str(self.data)
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run(self, *args, stdin=None, script=None, timeout=60, **env):
        return subprocess.run([sys.executable, str(script or SCRIPT), *args], input=stdin,
                              capture_output=True, text=True, encoding='utf-8', errors='replace',
                              env=self.env(**env), timeout=timeout)

    def read_settings(self):
        return json.loads(self.settings.read_text(encoding='utf-8'))

    def write_settings(self, obj):
        self.settings.write_text(json.dumps(obj, indent=2) + '\n', encoding='utf-8')

    def files(self, pattern):
        return sorted(self.tmp.rglob(pattern))


def payload(name):
    return (FIXTURES / f'payload_{name}.json').read_text(encoding='utf-8')


def visible(line):
    return ANSI.sub('', line)


def numbers_in(path):
    """Every used_percentage value anywhere in a JSON (or JSON-lines) file."""
    found = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if k == 'used_percentage' or k in ('h', 'w'):
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        found.append(v)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    text = Path(path).read_text(encoding='utf-8')
    for line in text.splitlines() if path.suffix == '.jsonl' else [text]:
        if line.strip():
            walk(json.loads(line))
    return found


def git_bash():
    for p in (shutil.which('bash'), r'C:\Program Files\Git\bin\bash.exe'):
        if p and os.path.exists(p) and 'system32' not in p.lower() and 'windowsapps' not in p.lower():
            return p
    return None


def shells():
    """(name, argv-prefix) for each shell Claude Code could run the command in."""
    out, missing = [], []
    if WIN:
        wanted = [('git-bash', git_bash(), ['-c']),
                  ('powershell', shutil.which('powershell'), ['-NoProfile', '-NonInteractive', '-Command']),
                  ('pwsh', shutil.which('pwsh'), ['-NoProfile', '-NonInteractive', '-Command'])]
    else:
        wanted = [('sh', shutil.which('sh'), ['-c']), ('bash', shutil.which('bash'), ['-c']),
                  ('zsh', shutil.which('zsh'), ['-c'])]
    for name, exe, flags in wanted:
        (out if exe else missing).append((name, [exe, *flags] if exe else None))
    if REQUIRE_SHELLS:
        needed = {'git-bash', 'powershell', 'pwsh'} if WIN else {'sh', 'bash'}
        lacking = needed & {n for n, _ in missing}
        if lacking:
            raise RuntimeError(f'shells missing: {sorted(lacking)}')
    return out
