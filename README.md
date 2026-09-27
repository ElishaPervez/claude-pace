# claude-pace

**Pace your Claude limits across the whole week.** A terminal dashboard for
your Claude Pro / Max / Team plan limits that answers the two questions the
built-in meters don't:

- **How much can I use today?** What's left of your weekly limit, split over
  the days left until it resets. Use less than today's share and every later
  day gets more; go over and they get less.
- **How many 5-hour sessions fill a week?** Measured from your own usage, e.g.
  "about 9 full 5-hour sessions use up the whole week", with a range that
  narrows as it sees more.

![Dashboard](https://raw.githubusercontent.com/ElishaPervez/claude-pace/main/docs/screenshot.png)

*(Screenshot uses made-up numbers.)*

One Python file, no dependencies, works on macOS, Linux and Windows.

## Install

**macOS / Linux**

```sh
curl -fsSL https://github.com/ElishaPervez/claude-pace/releases/latest/download/install.sh | sh
```

**Windows** (PowerShell)

```powershell
irm https://github.com/ElishaPervez/claude-pace/releases/latest/download/install.ps1 | iex
```

Then send any message in Claude Code and run:

```sh
claude-pace
```

Want a desktop shortcut too? Run `claude-pace install --desktop`.

**With uv or pipx** (if you already use them):

```sh
uv tool install claude-pace     # or: pipx install claude-pace
claude-pace install             # hook it into Claude Code
```

Updates are `uv tool upgrade claude-pace` / `pipx upgrade claude-pace`; the
status line keeps working across them. To just try it once without
installing anything, `uvx claude-pace` opens the dashboard. If you run
`uvx claude-pace install`, the script is copied to `~/.claude-pace` first,
because uvx runs from a cache that can be cleared - `uv tool install` is the
better way to keep it.

The installer downloads `claude_pace.py` into `~/.claude-pace`, checks it
against the release's published checksums, and sets it as Claude Code's
status line. If you already have your own status line, it's kept: yours
still shows, and this tool just saves the numbers before handing over to it.
Your `settings.json` is backed up first (the newest 5 backups are kept),
and left alone entirely if it isn't valid JSON or is read-only. If it's a
link into a dotfiles folder, the file it points to is updated and the link
stays.

**Requirements:** Python 3.9 or newer (the Python that comes with macOS
works) and Claude Code signed in with a Claude **Pro, Max or Team** plan -
Claude Code only reports plan limits for those. API-key, Bedrock and Vertex
setups have no plan limits to show.

## What you see

**Limits** - the 5-hour and weekly meters, with when each resets. The weekly
bar has a "stop here today" marker; past it turns red.

**Today's budget** - a "day" starts at the same clock time as your weekly
reset, so the week is always exactly 7 days (if your week resets Thursday
4:00 AM, every day runs 4:00 AM to 4:00 AM - on a daylight-saving change
that day is an hour shorter or longer, and still starts at 4:00 AM). Today's allowance is
the weekly limit left when today began, divided by the days left including
today, so it stays fixed for the day. You see how much of it you've used,
what's left or how far over you are, what each remaining day would get if
you stopped now, and a countdown to the next day's budget.

**Sessions per week** - the weekly meter only shows whole percentages, so its
true value is only known exactly at the moment it ticks up a point. The
dashboard measures how much 5-hour usage happens between two weekly ticks -
exactly that many weekly points - adding 5-hour usage up across 5-hour
resets. Starting at 27.9% doesn't make the first 0.1% look like a whole
point. Until it has seen two ticks it shows a rougher estimate, labelled as
such. In simulations with a known answer (`python tools/simulate_sessions.py
both|bar|account`), the typical (median) error is 0.7-2.5% depending on which
readings it has - 1.3% with both sources, 2.5% with the status line alone,
0.7% with the account check alone - and 9 in 10 runs are within 3-6.4%. The
true value falls inside the shown "likely" range in 96-100% of runs.

Nothing is kept only in memory: every reading goes into a log file, so
closing the dashboard or rebooting loses nothing and the estimates keep
improving over weeks.

Keys: **R** fetch fresh numbers now, **A** turn on the account check (macOS),
**Q** / **Esc** quit.

## Where the numbers come from

1. **Claude Code's status line.** After each reply, Claude Code hands its
   status line the current 5-hour and weekly percentages. This tool is that
   status line: it saves them into your Claude folder (`~/.claude`) and shows
   a one-line summary in Claude Code's status bar. This is the documented
   route and works everywhere.
2. **Your Claude account (optional).** While the dashboard is open, it can
   also ask your account for the same two numbers every 2 minutes. That
   covers usage in the Claude desktop app or on claude.ai (which never reach
   the status line), and it's slightly ahead of the status line, which
   typically reads about a point behind. The footer says which source you're
   looking at.

| File in `~/.claude` | What's in it |
| --- | --- |
| `usage-history.jsonl` | One line every time a number changes - the permanent log |
| `usage-latest.json` | The latest numbers from the status line |
| `usage-account.json` | The latest numbers from your account |
| `claude-pace-config.json` | Installer settings (e.g. the status line it's chained to). Always in `~/.claude`, next to `settings.json` |

## Privacy

- Everything stays on your machine, in your Claude folder. Nothing is sent
  anywhere except the account check below, which talks only to Anthropic.
- The account check uses the login Claude Code already saved: on Windows and
  Linux, `~/.claude/.credentials.json`; on macOS, the Keychain. It reads the
  login key and your plan name, sends the key only to Anthropic's
  `api.anthropic.com` (it never follows a redirect elsewhere), and never
  prints or stores it anywhere else.
- It uses an **undocumented** Anthropic address - the one Claude Code's own
  usage screen uses. It could change or stop working at any time; if it does,
  the dashboard says so and keeps going on the status line alone. It backs
  off if Anthropic asks it to slow down.
- Anthropic's terms say subscription logins are meant for Claude Code and
  claude.ai. This check only reads your usage numbers, but if you'd rather
  not use your login outside Claude Code at all, turn it off - the status
  line alone covers everything you do in Claude Code.
- **Turn it off:** run `claude-pace --no-account`, or set
  `CLAUDE_PACE_NO_ACCOUNT=1`.
- **On macOS it's off until you turn it on** (`claude-pace --account`),
  because reading the Keychain makes macOS ask for permission ("security
  wants to access key Claude Code-credentials"). The dashboard explains this
  before asking. It reads the Keychain once and reuses the key until it
  expires, so the prompt doesn't repeat every 2 minutes.

## Uninstall

**macOS / Linux**

```sh
curl -fsSL https://github.com/ElishaPervez/claude-pace/releases/latest/download/install.sh | sh -s -- --uninstall
```

**Windows**

```powershell
$env:CLAUDE_PACE_UNINSTALL=1; irm https://github.com/ElishaPervez/claude-pace/releases/latest/download/install.ps1 | iex
```

Installed with uv or pipx? Run `claude-pace uninstall`, then
`uv tool uninstall claude-pace` / `pipx uninstall claude-pace`.

This puts back the status line you had before (or removes it if you had
none), removes the `claude-pace` command and shortcut, and deletes
`~/.claude-pace`. Your usage log is kept; add `--purge` (macOS / Linux:
`sh -s -- --uninstall --purge`; Windows: set `CLAUDE_PACE_ARGS=--purge`
too) to delete it, and the installer's `settings.json` backups, as well.

## Troubleshooting

**The dashboard says it's waiting / not updating, or the status bar is
blank.** The numbers only arrive after Claude Code gets a reply, so send a
message first. If it still doesn't move:

- Claude Code only runs the status line in folders you've trusted - accept
  the trust prompt for the folder you're working in.
- `"disableAllHooks": true` in a settings file also turns off the status
  line.
- A project's own `.claude/settings.json` with a `statusLine` overrides
  yours in that project.
- Plan limits are only reported for Pro, Max and Team plans.

**WSL:** if Claude Code runs inside WSL, it saves its numbers on the Linux
side, so install and run the tracker inside WSL too.

**Colors look off:** it uses full color where the terminal supports it and
falls back to 256 colors (e.g. macOS Terminal before macOS 26, or tmux).
`NO_COLOR=1` turns color off. If boxes or bars show as odd symbols, set
`CLAUDE_PACE_ASCII=1` for plain characters.

**Different Claude folder:** set `CLAUDE_CONFIG_DIR` (Claude Code's own
setting) and the tracker follows it. `CLAUDE_PACE_DIR` moves just the
tracker's usage files (the installer's own settings stay next to
`settings.json`).

## Manual install

1. Download `claude_pace.py` from the
   [latest release](https://github.com/ElishaPervez/claude-pace/releases/latest)
   (optionally check it against `SHA256SUMS`) and put it somewhere permanent.
2. Run `python3 claude_pace.py install` (Windows: `py claude_pace.py install`).
   Add `--no-launcher` to skip the `claude-pace` command and shortcut.
3. Send a message in Claude Code, then run `python3 claude_pace.py`.

Other commands: `--once` prints one frame and exits, `--version`,
`account on|off|default` (remember whether to check your account), and
`uninstall [--purge]`.

To set the status line by hand instead, point `statusLine` in
`~/.claude/settings.json` at `python3 /path/to/claude_pace.py statusline`.

## License

MIT
