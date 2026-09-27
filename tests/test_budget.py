"""Today's budget worked out from saved readings: where days start, and
which reading counts as "when today began" when both sources were saving."""
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))
from _util import SCRIPT  # noqa: E402

cp = None


def setUpModule():
    """Import the script with its folders pointed at an empty temp folder,
    so importing it can't read anything real."""
    global cp
    tmp = tempfile.mkdtemp(prefix='cu-budget-')
    saved = {k: os.environ.get(k) for k in ('CLAUDE_CONFIG_DIR', 'CLAUDE_PACE_DIR', 'CLAUDE_PACE_NO_ACCOUNT')}
    os.environ.update(CLAUDE_CONFIG_DIR=tmp, CLAUDE_PACE_DIR=tmp, CLAUDE_PACE_NO_ACCOUNT='1')
    try:
        spec = importlib.util.spec_from_file_location('cp_budget', SCRIPT)
        cp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cp)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


H = 3600
RESET = 1_800_000_000  # a fixed weekly reset time


def week(pct, reset=RESET):
    return {'used_percentage': pct, 'resets_at': reset}


def bar(t, w, reset=RESET):
    return {'t': t, 'w': w, 'wr': reset}


def acct(t, w, reset=RESET):
    return {'t': t, 'w': w, 'wr': reset, 'src': 'account'}


@unittest.skipUnless(SCRIPT.exists(), f'{SCRIPT.name} not found')
class StartOfDayTest(unittest.TestCase):
    def setUp(self):
        starts = cp.day_starts(RESET)
        self.day_start = starts[3]  # day 4 of 7
        self.now = self.day_start + 5 * H

    def budget(self, shown, samples, src):
        return cp.today_budget(week(shown), samples, self.now, src)

    def test_same_source_start(self):
        # The account says 20 at the boundary, the status bar (a point behind) 19.
        s = [bar(self.day_start - 2 * H, 19), acct(self.day_start - H, 20), acct(self.day_start + H, 22)]
        t = self.budget(22, s, 'account')
        self.assertEqual(t['base'], 20)
        self.assertEqual(t['used'], 2)

    def test_status_bar_shown_uses_status_bar_start(self):
        s = [bar(self.day_start - 2 * H, 19), acct(self.day_start - H, 20), bar(self.day_start + H, 21)]
        t = self.budget(21, s, None)
        self.assertEqual(t['base'], 19, 'an account reading 20 means the status bar was at least 19')
        self.assertEqual(t['used'], 2)

    def test_newer_reading_from_other_source_raises_the_start(self):
        # The dashboard was closed overnight: the last account reading is old,
        # the status bar saw use after it.
        s = [acct(self.day_start - 10 * H, 12), bar(self.day_start - H, 16)]
        t = self.budget(18, s, 'account')
        self.assertEqual(t['base'], 16)

    def test_only_other_source(self):
        s = [bar(self.day_start - H, 16)]
        self.assertEqual(self.budget(18, s, 'account')['base'], 16)
        s = [acct(self.day_start - H, 16)]
        self.assertEqual(self.budget(18, s, None)['base'], 15)

    def test_partial_day(self):
        s = [acct(self.day_start + H, 30)]
        t = self.budget(31, s, 'account')
        self.assertEqual((t['base'], t['used'], t['partial_from']), (30, 1, self.day_start + H))


@unittest.skipUnless(SCRIPT.exists(), f'{SCRIPT.name} not found')
class DayStartsTest(unittest.TestCase):
    def test_seven_days_ending_at_reset(self):
        starts = cp.day_starts(RESET)
        self.assertEqual(len(starts), 8)
        self.assertEqual(starts[-1], RESET)
        for a, b in zip(starts, starts[1:]):
            self.assertIn(b - a, (23 * H, 24 * H, 25 * H))

    @unittest.skipUnless(hasattr(time, 'tzset'), 'needs a settable time zone')
    def test_same_clock_time_across_daylight_saving(self):
        old = os.environ.get('TZ')
        os.environ['TZ'] = 'America/New_York'
        time.tzset()
        try:
            # Reset Thu 5 Nov 2026, 4:00 AM New York; clocks go back on Sun 1 Nov.
            reset = datetime(2026, 11, 5, 4, 0).timestamp()
            starts = cp.day_starts(reset)
            for s in starts:
                d = datetime.fromtimestamp(s)
                self.assertEqual((d.hour, d.minute), (4, 0), d)
            lengths = sorted(b - a for a, b in zip(starts, starts[1:]))
            self.assertEqual(lengths, [24 * H] * 6 + [25 * H])
            t = cp.today_budget(week(10, reset), [], starts[3] + 60, 'account')
            self.assertEqual(t['idx'], 3)
            self.assertEqual(t['day_end'], starts[4])
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            time.tzset()


@unittest.skipUnless(SCRIPT.exists(), f'{SCRIPT.name} not found')
class WordingTest(unittest.TestCase):
    def test_sessions_always_in_sessions(self):
        self.assertEqual(cp.sessions_text(110), '≈ 1.1 sessions')
        self.assertEqual(cp.sessions_text(88), '≈ 0.88 sessions')
        self.assertEqual(cp.sessions_text(100), '≈ 1 session')
        self.assertEqual(cp.sessions_text(0), '≈ 0 sessions')

    def test_env_flag(self):
        for v, on in (('1', True), ('yes', True), ('0', False), ('false', False), ('', False), ('off', False)):
            os.environ['CP_TEST_FLAG'] = v
            self.assertIs(cp.env_flag('CP_TEST_FLAG'), on, v)
        os.environ.pop('CP_TEST_FLAG', None)


if __name__ == '__main__':
    unittest.main()
