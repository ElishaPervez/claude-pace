"""The dashboard draws one frame (--once) without crashing, at several window
sizes, with colour, with NO_COLOR, and in plain-text mode, and never draws
wider or taller than the window."""
import sys
import unittest

sys.path.insert(0, __import__('os').path.dirname(__file__))
from _util import Sandbox, SCRIPT, visible  # noqa: E402
import make_fixture  # noqa: E402

SIZES = [(40, 16), (80, 30), (120, 50)]
BOX = {chr(c) for c in range(0x2500, 0x25A0)}   # box drawing + block elements


@unittest.skipUnless(SCRIPT.exists(), f'{SCRIPT.name} not found')
class RenderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.box = Sandbox('render', data_dir=True)
        make_fixture.build(cls.box.data)
        cls.empty = Sandbox('render-empty', data_dir=True)

    @classmethod
    def tearDownClass(cls):
        cls.box.__exit__()
        cls.empty.__exit__()

    def frame(self, box, cols, rows, **env):
        r = box.run('--once', COLUMNS=cols, LINES=rows, **env)
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = r.stdout.rstrip('\n').split('\n')
        text = '\n'.join(visible(x) for x in lines)
        self.assertNotIn('WINDOW ERROR', text)
        self.assertNotIn('Traceback', r.stderr)
        for line in lines:
            self.assertLessEqual(len(visible(line)), cols, f'line wider than {cols}: {visible(line)!r}')
        self.assertLessEqual(len(lines), rows, f'{len(lines)} lines in a {rows}-row window')
        return r.stdout, text

    def test_sizes_with_colour(self):
        for cols, rows in SIZES:
            with self.subTest(size=(cols, rows)):
                raw, text = self.frame(self.box, cols, rows, COLORTERM='truecolor')
                self.assertIn('%', text)

    def test_no_color(self):
        for cols, rows in SIZES:
            with self.subTest(size=(cols, rows)):
                raw, _ = self.frame(self.box, cols, rows, NO_COLOR='1')
                self.assertNotRegex(raw, r'\x1b\[[0-9;]*[34]8;', 'colour codes despite NO_COLOR')

    @unittest.skipIf(sys.platform == 'win32', 'Windows consoles get full colour')
    def test_256_colour_terminal(self):
        raw, _ = self.frame(self.box, 80, 30, TERM='xterm-256color')
        self.assertNotIn('38;2;', raw, 'full-colour codes without COLORTERM=truecolor')

    def test_ascii_mode(self):
        for cols, rows in SIZES:
            with self.subTest(size=(cols, rows)):
                _, text = self.frame(self.box, cols, rows, CLAUDE_PACE_ASCII='1')
                self.assertFalse(BOX & set(text), f'box/block characters in ASCII mode: {BOX & set(text)}')

    def test_waiting_screen(self):
        _, text = self.frame(self.empty, 80, 30)
        self.assertTrue(text.strip())

    def latest_only(self, name, history=None, five_only=False):
        """A data folder with fresh numbers but no usable log."""
        import json
        import time
        box = Sandbox(name, data_dir=True)
        self.addCleanup(box.__exit__)
        now = int(time.time())
        d = {'saved_at': now, 'five_hour': {'used_percentage': 12, 'resets_at': now + 3600},
             'seven_day': None if five_only else {'used_percentage': 40, 'resets_at': now + 3 * 86400},
             'last_check': {'at': now, 'ok': True}}
        (box.data / 'usage-latest.json').write_text(json.dumps(d), encoding='utf-8')
        if history is not None:
            (box.data / 'usage-history.jsonl').write_bytes(history)
        return box

    def test_numbers_but_no_log(self):
        # The log was deleted, but the latest numbers are there.
        _, text = self.frame(self.latest_only('render-nolog'), 80, 30)
        self.assertIn('40%', text)

    def test_five_hour_only(self):
        _, text = self.frame(self.latest_only('render-5h', five_only=True), 80, 30)
        self.assertIn('12%', text)

    def test_damaged_byte_in_log(self):
        import time
        now = int(time.time())
        good = ('{"t":%d,"h":10,"hr":%d,"w":39,"wr":%d}\n' % (now - 600, now + 3600, now + 3 * 86400)).encode()
        box = self.latest_only('render-badbyte', history=b'{"t":1,"w":\xff\xfe}\n' + good)
        _, text = self.frame(box, 80, 30)
        self.assertIn('40%', text)

    def test_damaged_latest_file(self):
        box = Sandbox('render-badlatest', data_dir=True)
        self.addCleanup(box.__exit__)
        (box.data / 'usage-latest.json').write_bytes(b'{"saved_at": \xff}')
        _, text = self.frame(box, 80, 30)
        self.assertIn('damaged', text)

    def test_version(self):
        r = self.box.run('--version', timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout.strip(), r'\d+\.\d+\.\d+')


if __name__ == '__main__':
    unittest.main()
