"""The account check against fake local servers: it must never send the
login key anywhere but the one address it asks, and a broken answer must
never kill the background check or print a traceback over the dashboard.

Each case runs the script in a child process with a throwaway Claude folder
holding a fake login, and points the check at a local server.
"""
import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from _util import Sandbox, SCRIPT  # noqa: E402

KEY = 'fake-login-key-for-tests'

# Loads the script, points the account check at argv[2], runs `action`, and
# prints what the check recorded.
CHILD = r'''
import importlib.util, json, sys, time
spec = importlib.util.spec_from_file_location('cp', sys.argv[1])
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)
cp.ACCOUNT_URL = sys.argv[2]
cp.USE_ACCOUNT = True
action = sys.argv[3]
if action == 'fetch':
    cp.fetch_account(time.time())
elif action == 'fetch-crash':
    def boom(*a, **k):
        raise RuntimeError('unexpected')
    cp.read_login = boom
    cp.fetch_account(time.time())
elif action == 'tick-crash':
    def boom(*a, **k):
        raise RuntimeError('unexpected')
    cp.fetch_account = boom
    cp.account_tick()
print(json.dumps({'ok': cp.ACCOUNT['ok'], 'why': cp.ACCOUNT['why']}))
'''


class Server:
    """A one-shot TCP server that answers every connection with `reply` and
    records what it was sent."""

    def __init__(self, reply=b''):
        self.reply = reply
        self.seen = []
        self.sock = socket.socket()
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen(5)
        self.sock.settimeout(20)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.settimeout(5)
                data = b''
                try:
                    while b'\r\n\r\n' not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                except OSError:
                    pass
                self.seen.append(data)
                try:
                    conn.sendall(self.reply)
                except OSError:
                    pass

    def close(self):
        self.sock.close()

    @property
    def url(self):
        return f'http://127.0.0.1:{self.port}/api/oauth/usage'


@unittest.skipUnless(SCRIPT.exists(), f'{SCRIPT.name} not found')
class AccountTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox('account')
        login = {'claudeAiOauth': {'accessToken': KEY, 'expiresAt': 4102444800000,
                                   'subscriptionType': 'max'}}
        (self.box.claude / '.credentials.json').write_text(json.dumps(login), encoding='utf-8')

    def tearDown(self):
        self.box.__exit__()

    def check(self, url, action='fetch'):
        env = self.box.env()
        env.pop('CLAUDE_PACE_NO_ACCOUNT', None)
        for k in list(env):
            if k.lower() in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy'):
                del env[k]
        import subprocess
        r = subprocess.run([sys.executable, '-c', CHILD, str(SCRIPT), url, action], capture_output=True,
                           text=True, encoding='utf-8', errors='replace', env=env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(r.stderr.strip(), '', 'the check printed an error')
        self.assertNotIn(KEY, r.stdout, 'the login key was printed')
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_redirect_is_not_followed(self):
        # The key must never reach the address a redirect points to.
        target = Server(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}')
        first = Server(b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:%d/steal\r\n'
                       b'Content-Length: 0\r\nConnection: close\r\n\r\n' % target.port)
        try:
            got = self.check(first.url)
        finally:
            first.close()
            target.close()
        self.assertTrue(first.seen, 'the check never asked')
        self.assertIn(KEY.encode(), first.seen[0], 'the key should go to the address asked')
        self.assertEqual(target.seen, [], 'the redirect was followed')
        self.assertIs(got['ok'], False)

    def test_garbled_answer(self):
        s = Server(b'HTTP/1.1 abc\r\n\r\n')
        try:
            got = self.check(s.url)
        finally:
            s.close()
        self.assertEqual(got, {'ok': False, 'why': 'offline'})

    def test_cut_off_answer(self):
        s = Server(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 400\r\n'
                   b'Connection: close\r\n\r\n{"five_hour": {"utiliz')
        try:
            got = self.check(s.url)
        finally:
            s.close()
        self.assertEqual(got, {'ok': False, 'why': 'offline'})

    def test_good_answer_is_saved(self):
        body = json.dumps({'five_hour': {'utilization': 12.0, 'resets_at': '2030-01-01T10:00:00Z'},
                           'seven_day': {'utilization': 40.0, 'resets_at': '2030-01-03T04:00:00+00:00'}})
        s = Server(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n'
                   b'Connection: close\r\n\r\n%s' % (len(body), body.encode()))
        try:
            got = self.check(s.url)
        finally:
            s.close()
        self.assertEqual(got, {'ok': True, 'why': None})
        saved = (self.box.claude / 'usage-account.json').read_text(encoding='utf-8')
        self.assertNotIn(KEY, saved)
        self.assertIn('"used_percentage":40', saved)

    def test_unexpected_error_in_fetch(self):
        got = self.check('http://127.0.0.1:9/', 'fetch-crash')
        self.assertEqual(got, {'ok': False, 'why': 'error'})

    def test_unexpected_error_in_loop(self):
        got = self.check('http://127.0.0.1:9/', 'tick-crash')
        self.assertEqual(got, {'ok': False, 'why': 'error'})


if __name__ == '__main__':
    unittest.main()
