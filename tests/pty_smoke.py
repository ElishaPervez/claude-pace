"""Open the live dashboard in a pseudo-terminal (macOS/Linux), wait for it to
draw, press q, and check that it quits by itself and puts the terminal back
(leaves the alternate screen and shows the cursor again).

    python tests/pty_smoke.py
"""
import os
import pty
import select
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from _util import Sandbox, SCRIPT  # noqa: E402
import make_fixture  # noqa: E402


def read_for(fd, seconds):
    out, end = b'', time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    return out


def main():
    with Sandbox('pty', data_dir=True) as box:
        make_fixture.build(box.data)
        env = box.env(COLUMNS=100, LINES=40, TERM='xterm-256color')
        pid, fd = pty.fork()
        if pid == 0:
            os.execve(sys.executable, [sys.executable, str(SCRIPT)], env)
        out = read_for(fd, 4)
        if b'%' not in out:
            print('dashboard drew nothing recognisable:', out[-500:])
            os.kill(pid, 9)
            return 1
        os.write(fd, b'q')
        deadline, status = time.time() + 8, None
        while time.time() < deadline:
            out += read_for(fd, 0.2)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                break
        else:
            os.kill(pid, 9)
            print('dashboard did not quit on q within 8 s')
            return 1
        code = os.waitstatus_to_exitcode(status)
        if code != 0:
            print(f'dashboard exited with {code}:', out[-800:])
            return 1
        tail = out[-400:]
        if b'\x1b[?1049l' not in tail or b'\x1b[?25h' not in tail:
            print('terminal not restored on exit (alternate screen / cursor):', tail)
            return 1
        print('ok: drew, quit on q, restored the terminal')
        return 0


if __name__ == '__main__':
    sys.exit(main())
