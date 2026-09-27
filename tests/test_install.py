"""`claude_pace.py install` / `uninstall` against a throwaway Claude folder.

Checks that the installer only ever changes the status line entry, keeps a
user's own status line working by running it after ours, refuses to touch a
settings file it can't read, and that uninstall puts things back exactly.
The command it writes is run through every shell Claude Code might use
(Git Bash / PowerShell on Windows, sh / bash / zsh elsewhere).

Set CI_REQUIRE_SHELLS=1 to fail, instead of skip, when one of those shells
is missing.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _util import Sandbox, SCRIPT, numbers_in, payload, shells  # noqa: E402

WIN = os.name == 'nt'
FOREIGN = {'type': 'command', 'command': 'echo FOREIGN-OUT', 'padding': 2}


def status_command(box):
    sl = box.read_settings().get('statusLine')
    assert isinstance(sl, dict) and sl.get('type') == 'command', sl
    return sl['command']


@unittest.skipUnless(SCRIPT.exists(), f'{SCRIPT.name} not found')
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox('install')
        # Install from a copy, the way the installers do, not from the repo.
        self.app = self.box.home / '.claude-pace'
        self.app.mkdir()
        self.script = self.app / 'claude_pace.py'
        shutil.copy2(SCRIPT, self.script)

    def tearDown(self):
        self.box.__exit__()

    def cli(self, *args, script=None):
        return self.box.run(*args, script=script or self.script)

    def install(self, *extra, script=None, ok=True):
        r = self.cli('install', '--python', sys.executable, '--no-launcher', *extra, script=script)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def uninstall(self, *extra):
        r = self.cli('uninstall', *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def run_command(self, cmd, shell, text=None):
        r = subprocess.run([*shell, cmd], input=payload('full') if text is None else text,
                           capture_output=True, text=True, encoding='utf-8', errors='replace',
                           env=self.box.env(), timeout=45, cwd=str(self.box.tmp))
        return r

    def assert_command_works(self, expect_foreign=False):
        cmd = status_command(self.box)
        for name, shell in shells():
            with self.subTest(shell=name):
                for f in ('usage-latest.json', 'usage-history.jsonl'):
                    (self.box.data / f).unlink(missing_ok=True)
                r = self.run_command(cmd, shell)
                self.assertEqual(r.returncode, 0, f'{name}: {cmd}\n{r.stdout}{r.stderr}')
                self.assertTrue(r.stdout.strip(), f'{name} printed nothing: {cmd}\n{r.stderr}')
                self.assertIn(37, numbers_in(self.box.data / 'usage-latest.json'), f'{name} saved nothing')
                if expect_foreign:
                    self.assertIn('FOREIGN-OUT', r.stdout, f'{name}: chained status line not shown')
                else:
                    self.assertNotIn('FOREIGN-OUT', r.stdout)

    # -- cases --

    def test_no_settings_file(self):
        self.install()
        self.assertIn('statusLine', self.box.read_settings())
        self.assert_command_works()
        self.uninstall()
        after = self.box.read_settings() if self.box.settings.exists() else {}
        self.assertNotIn('statusLine', after)

    def test_no_status_line(self):
        original = {'model': 'opus', 'env': {'SOME_FLAG': '1'}, 'permissions': {'allow': ['Bash(ls:*)']}}
        self.box.write_settings(original)
        before = self.box.settings.read_bytes()
        self.install()
        now = self.box.read_settings()
        self.assertEqual({k: v for k, v in now.items() if k != 'statusLine'}, original)
        backups = list(self.box.claude.glob('settings.json.bak-claude-pace-*'))
        self.assertEqual(len(backups), 1, backups)
        self.assertEqual(backups[0].read_bytes(), before)
        self.assert_command_works()
        self.uninstall()
        self.assertEqual(self.box.read_settings(), original)

    def test_foreign_status_line_is_chained(self):
        original = {'theme': 'dark', 'statusLine': dict(FOREIGN)}
        self.box.write_settings(original)
        self.install()
        sl = self.box.read_settings()['statusLine']
        self.assertNotEqual(sl['command'], FOREIGN['command'])
        self.assertEqual(sl.get('padding'), 2, 'the original padding setting was dropped')
        self.assert_command_works(expect_foreign=True)
        self.uninstall()
        self.assertEqual(self.box.read_settings(), original)

    def test_reinstall_does_not_chain_itself(self):
        self.box.write_settings({'statusLine': dict(FOREIGN)})
        self.install()
        first = self.box.read_settings()
        self.install()
        self.assertEqual(self.box.read_settings(), first)
        self.assert_command_works(expect_foreign=True)
        self.uninstall()
        self.assertEqual(self.box.read_settings()['statusLine'], FOREIGN)

    def test_invalid_json_untouched(self):
        broken = b'{\n  "model": "opus",\n  // a comment Claude Code tolerates but JSON does not\n'
        self.box.settings.write_bytes(broken)
        r = self.install(ok=False)
        self.assertNotEqual(r.returncode, 0, 'install claimed success on an unreadable settings.json')
        self.assertEqual(self.box.settings.read_bytes(), broken)

    def old_node_logger(self):
        """A copy of this tool's old Node status line, recognised by what's in it."""
        js = self.box.home / 'claude-usage-tracker' / 'statusline.js'
        js.parent.mkdir(parents=True, exist_ok=True)
        js.write_text("// Claude Code status line + usage feed\n"
                      "const rl = d.rate_limits;\n"
                      "const out = path.join(DATA_DIR, 'usage-latest.json');\n"
                      "const file = path.join(DATA_DIR, 'usage-history.jsonl');\n", encoding='utf-8')
        return {'type': 'command', 'command': f'node "{js.as_posix()}"'}

    def test_old_node_status_line_is_replaced(self):
        self.box.write_settings({'statusLine': self.old_node_logger()})
        self.install()
        cmd = status_command(self.box)
        self.assertNotIn('statusline.js', cmd)
        self.assertNotIn('node', cmd.split()[0].lower())
        self.assert_command_works()
        self.uninstall()
        # The old status line was this tool's own, so there's nothing to go back to.
        self.assertNotIn('statusLine', self.box.read_settings())

    def test_script_in_folder_with_spaces(self):
        spaced = self.box.home / 'My Apps (x)' / 'claude pace'
        spaced.mkdir(parents=True)
        shutil.copy2(SCRIPT, spaced / 'claude_pace.py')
        self.install(script=spaced / 'claude_pace.py')
        self.assert_command_works()

    def is_launcher(self, p):
        # The config file and the installed script share the name; they're not launchers.
        if self.box.claude in p.parents or self.app in p.parents:
            return False
        return p.name.lower().startswith(('claude-pace', 'claude pace'))

    def test_prerelease_install_is_upgraded(self):
        # This PC ran the pre-release name: claude_usage.py, its config file and
        # a claude-usage launcher. Installing claude-pace must take all of it over.
        old_cmd = f'{sys.executable} {self.app / "claude_usage.py"} statusline'.replace('\\', '/')
        self.box.write_settings({'statusLine': {'type': 'command', 'command': old_cmd, 'padding': 2}})
        bindir = self.box.home / '.local' / 'bin'
        bindir.mkdir(parents=True)
        old_launcher = bindir / ('claude-usage.cmd' if WIN else 'claude-usage')
        old_launcher.write_text('rem claude-usage-tracker launcher, made by claude_usage.py install\n', encoding='utf-8')
        foreign = {'type': 'command', 'command': 'echo FOREIGN-OUT'}
        (self.box.claude / 'usage-tracker-config.json').write_text(
            json.dumps({'chain': foreign, 'launchers': [str(old_launcher)], 'account': False}), encoding='utf-8')
        r = self.cli('install', '--python', sys.executable)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        sl = self.box.read_settings()['statusLine']
        self.assertIn('claude_pace.py', sl['command'])
        self.assertEqual(sl.get('padding'), 2)
        self.assertFalse(old_launcher.exists(), 'old claude-usage launcher left behind')
        self.assertFalse((self.box.claude / 'usage-tracker-config.json').exists(), 'old config not moved')
        cfg = json.loads((self.box.claude / 'claude-pace-config.json').read_text(encoding='utf-8'))
        self.assertEqual(cfg.get('chain'), foreign, 'the chained status line was lost in the move')
        self.assertIs(cfg.get('account'), False)
        self.uninstall()
        self.assertEqual(self.box.read_settings()['statusLine'], foreign)
        self.assertFalse((self.box.claude / 'claude-pace-config.json').exists())
        for p in cfg.get('launchers', []):
            self.assertFalse(Path(p).exists(), f'{p} left behind')

    def test_uninstall_without_install(self):
        original = {'model': 'opus'}
        self.box.write_settings(original)
        self.uninstall()
        self.assertEqual(self.box.read_settings(), original)

    def test_no_launcher(self):
        before = {p for p in self.box.home.rglob('*')}
        self.install()
        new = {p for p in self.box.home.rglob('*')} - before
        launchers = [p for p in new if self.is_launcher(p)]
        self.assertEqual(launchers, [], f'--no-launcher still created {launchers}')

    # On Windows the desktop shortcut may go to the real desktop (found through
    # the system, not the home folder), so only try launchers on CI machines.
    @unittest.skipIf(WIN and not os.environ.get('CI'), 'launcher test only runs on CI on Windows')
    def test_launcher_created_and_removed(self):
        before = {p for p in self.box.home.rglob('*')}
        r = self.cli('install', '--python', sys.executable)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        new = {p for p in self.box.home.rglob('*')} - before
        launchers = [p for p in new if p.is_file() and self.is_launcher(p)]
        self.assertTrue(launchers, 'no launcher created')
        self.uninstall()
        self.assertEqual([p for p in launchers if p.exists()], [], 'uninstall left launchers behind')

    # -- things that must never lose or damage the user's settings --

    def assert_refused(self, r, before):
        self.assertNotEqual(r.returncode, 0, 'install claimed success')
        self.assertNotIn('Traceback', r.stdout + r.stderr)
        self.assertIn('Nothing changed', r.stdout)
        self.assertEqual(self.box.settings.read_bytes(), before)

    def test_someone_elses_statusline_js_next_to_the_script(self):
        # The script sits in the Claude folder right next to the user's own
        # statusline.js: that one is theirs and must be kept, not replaced.
        js = self.box.claude / 'statusline.js'
        js.write_text("console.log('FOREIGN-OUT')\n", encoding='utf-8')
        mine = {'type': 'command', 'command': f'node "{js.as_posix()}"'}
        self.box.write_settings({'statusLine': mine})
        shutil.copy2(SCRIPT, self.box.claude / 'claude_pace.py')
        r = self.install(script=self.box.claude / 'claude_pace.py')
        self.assertIn('your own status line is kept', r.stdout)
        cfg = json.loads((self.box.claude / 'claude-pace-config.json').read_text(encoding='utf-8'))
        self.assertEqual(cfg.get('chain'), mine)
        r = self.cli('uninstall', script=self.box.claude / 'claude_pace.py')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.box.read_settings(), {'statusLine': mine})

    def test_too_large_number_refused(self):
        for text in ('{"x": 1e400}\n', '{"x": NaN}\n', '{"x": -Infinity}\n'):
            with self.subTest(text=text):
                self.box.settings.write_text(text, encoding='utf-8')
                before = self.box.settings.read_bytes()
                self.assert_refused(self.install(ok=False), before)

    def test_not_utf8_refused(self):
        self.box.settings.write_bytes(b'{"model": "caf\xe9"}\n')
        self.assert_refused(self.install(ok=False), b'{"model": "caf\xe9"}\n')

    def test_read_only_settings_refused(self):
        import stat
        self.box.write_settings({'model': 'opus'})
        before = self.box.settings.read_bytes()
        self.box.settings.chmod(stat.S_IREAD)
        try:
            if os.access(self.box.settings, os.W_OK):
                self.skipTest('running with rights that ignore read-only files')
            self.assert_refused(self.install(ok=False), before)
            self.assertFalse((self.box.claude / 'claude-pace-config.json').exists(),
                             'the refused install left its remembered settings behind')
        finally:
            self.box.settings.chmod(stat.S_IREAD | stat.S_IWRITE)

    @unittest.skipIf(WIN, 'POSIX permissions')
    def test_permissions_kept(self):
        self.box.write_settings({'env': {'SECRET': 'x'}})
        self.box.settings.chmod(0o600)
        self.install()
        self.assertEqual(self.box.settings.stat().st_mode & 0o777, 0o600)
        self.uninstall()
        self.assertEqual(self.box.settings.stat().st_mode & 0o777, 0o600)

    @unittest.skipIf(WIN, 'symlinks need extra rights on Windows')
    def test_symlinked_settings_stay_a_link(self):
        real = self.box.home / 'dotfiles' / 'claude-settings.json'
        real.parent.mkdir()
        real.write_text('{"model": "opus"}\n', encoding='utf-8')
        self.box.settings.symlink_to(real)
        self.install()
        self.assertTrue(self.box.settings.is_symlink(), 'the link was replaced by a plain file')
        self.assertIn('statusLine', json.loads(real.read_text(encoding='utf-8')))
        self.uninstall()
        self.assertTrue(self.box.settings.is_symlink())
        self.assertEqual(json.loads(real.read_text(encoding='utf-8')), {'model': 'opus'})

    def test_chain_saved_before_settings_change(self):
        # If what install must remember can't be saved, settings.json isn't touched.
        self.box.write_settings({'statusLine': dict(FOREIGN)})
        before = self.box.settings.read_bytes()
        (self.box.claude / 'claude-pace-config.json').mkdir()  # can't be written as a file
        self.assert_refused(self.install(ok=False), before)

    def test_chain_found_without_data_folder_setting(self):
        # Installed with CLAUDE_PACE_DIR set; Claude Code runs without it.
        self.box.write_settings({'statusLine': dict(FOREIGN)})
        data = self.box.tmp / 'elsewhere'
        r = self.box.run('install', '--python', sys.executable, '--no-launcher', script=self.script,
                         CLAUDE_PACE_DIR=data)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assert_command_works(expect_foreign=True)
        self.uninstall()
        self.assertEqual(self.box.read_settings()['statusLine'], FOREIGN)

    def test_created_settings_file_removed_again(self):
        self.install()
        self.assertTrue(self.box.settings.exists())
        self.uninstall()
        self.assertFalse(self.box.settings.exists(), 'uninstall left an empty settings.json behind')

    def test_backups_do_not_pile_up(self):
        self.box.write_settings({'model': 'opus'})
        for _ in range(8):
            self.install()
        backups = list(self.box.claude.glob('settings.json.bak-claude-pace-*'))
        self.assertLessEqual(len(backups), 5, backups)
        self.uninstall('--purge')
        self.assertEqual(list(self.box.claude.glob('settings.json.bak-claude-pace-*')), [])
        self.assertEqual(self.box.read_settings(), {'model': 'opus'})


if __name__ == '__main__':
    unittest.main()
