"""`claude_pace.py statusline` - what Claude Code runs after every message.
It must always print a status bar line and exit 0 (a failing status line
shows nothing in Claude Code), and save the numbers when there are any."""
import json
import sys
import unittest

sys.path.insert(0, __import__('os').path.dirname(__file__))
from _util import Sandbox, SCRIPT, numbers_in, payload  # noqa: E402


@unittest.skipUnless(SCRIPT.exists(), f'{SCRIPT.name} not found')
class StatuslineTest(unittest.TestCase):
    def feed(self, box, name_or_text, raw=False):
        text = name_or_text if raw else payload(name_or_text)
        r = box.run('statusline', stdin=text, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.strip(), 'status bar printed nothing')
        self.assertLessEqual(len(r.stdout.strip().splitlines()), 2, r.stdout)
        return r

    def test_full_payload_is_logged(self):
        with Sandbox('sl-full', data_dir=True) as box:
            self.feed(box, 'full')
            latest, history = box.data / 'usage-latest.json', box.data / 'usage-history.jsonl'
            self.assertTrue(latest.exists() and history.exists())
            self.assertIn(37, numbers_in(latest))
            self.assertIn(21, numbers_in(latest))
            self.assertEqual(len(history.read_text(encoding='utf-8').splitlines()), 1)
            # Nothing is written into the Claude folder when a data folder is set.
            self.assertFalse((box.claude / 'usage-latest.json').exists())

    def test_default_data_folder_is_claude_folder(self):
        with Sandbox('sl-default') as box:
            self.feed(box, 'full')
            self.assertTrue((box.claude / 'usage-latest.json').exists())

    def test_unchanged_numbers_add_no_history(self):
        with Sandbox('sl-repeat', data_dir=True) as box:
            for _ in range(3):
                self.feed(box, 'full')
            lines = (box.data / 'usage-history.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 1)

    def test_changed_numbers_add_history(self):
        with Sandbox('sl-change', data_dir=True) as box:
            self.feed(box, 'full')
            d = json.loads(payload('full'))
            d['rate_limits']['five_hour']['used_percentage'] = 38
            self.feed(box, json.dumps(d), raw=True)
            lines = (box.data / 'usage-history.jsonl').read_text(encoding='utf-8').splitlines()
            self.assertEqual(len(lines), 2)

    def test_no_rate_limits_logs_nothing(self):
        with Sandbox('sl-none', data_dir=True) as box:
            self.feed(box, 'no_rate_limits')
            history = box.data / 'usage-history.jsonl'
            self.assertTrue(not history.exists() or not history.read_text(encoding='utf-8').strip())

    def test_first_message_payload(self):
        with Sandbox('sl-first', data_dir=True) as box:
            self.feed(box, 'first_message')

    def test_seven_day_only(self):
        with Sandbox('sl-7d', data_dir=True) as box:
            self.feed(box, 'seven_day_only')
            self.assertIn(44, numbers_in(box.data / 'usage-latest.json'))
            self.assertIn(44, numbers_in(box.data / 'usage-history.jsonl'))

    def test_decimals_kept(self):
        with Sandbox('sl-dec', data_dir=True) as box:
            self.feed(box, 'decimals')
            got = numbers_in(box.data / 'usage-latest.json')
            self.assertIn(23.5, got)
            self.assertIn(12.25, got)

    def test_garbage_input_still_prints(self):
        with Sandbox('sl-bad', data_dir=True) as box:
            self.feed(box, 'this is not json', raw=True)
            self.feed(box, '', raw=True)

    def chain(self, box, command):
        (box.claude / 'claude-pace-config.json').write_text(
            json.dumps({'chain': {'type': 'command', 'command': command}}), encoding='utf-8')

    def test_chained_output_shown(self):
        with Sandbox('sl-chain', data_dir=True) as box:
            self.chain(box, 'echo CHAINED-OUT')
            r = self.feed(box, 'full')
            self.assertIn('CHAINED-OUT', r.stdout)
            self.assertIn(37, numbers_in(box.data / 'usage-latest.json'))

    def test_slow_chained_status_line_is_cut_off(self):
        # A chained status line that hangs (here: a program it starts that
        # never ends) must not hold the status bar past the time limit.
        import time
        with Sandbox('sl-slow', data_dir=True) as box:
            self.chain(box, 'sleep 40; echo LATE')
            start = time.monotonic()
            r = self.feed(box, 'full')
            took = time.monotonic() - start
            self.assertNotIn('LATE', r.stdout)
            self.assertIn('[', r.stdout, 'our own status bar text should show instead')
            self.assertLess(took, 25, f'held for {took:.1f}s')


if __name__ == '__main__':
    unittest.main()
