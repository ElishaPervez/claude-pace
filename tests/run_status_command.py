"""Run the status line command that the installer wrote into settings.json,
exactly as written, through every shell Claude Code might use, and check it
prints a status bar and saves the numbers.

    python tests/run_status_command.py SETTINGS_JSON DATA_DIR
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _util import numbers_in, payload, shells  # noqa: E402


def main():
    settings, data = Path(sys.argv[1]), Path(sys.argv[2])
    cmd = json.loads(settings.read_text(encoding='utf-8'))['statusLine']['command']
    print(f'command: {cmd}')
    failed = 0
    for name, shell in shells():
        latest = data / 'usage-latest.json'
        latest.unlink(missing_ok=True)
        r = subprocess.run([*shell, cmd], input=payload('full'), capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=45)
        saved = latest.exists() and 37 in numbers_in(latest)
        ok = r.returncode == 0 and r.stdout.strip() and saved
        print(f'{"ok  " if ok else "FAIL"} {name:10} exit {r.returncode}  saved={saved}  -> {r.stdout.strip()[:120]}')
        if not ok:
            failed += 1
            if r.stderr.strip():
                print('     stderr:', r.stderr.strip()[:800])
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
