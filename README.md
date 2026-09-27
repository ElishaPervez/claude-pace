# Claude usage tracker

A terminal dashboard for your Claude plan limits. It answers two questions the
built-in meters don't:

- **How much can I use today?** It splits what's left of your weekly limit
  evenly over the days left until the weekly reset, and shows how much of
  today's share you've used, what's left, or how far over you are. Use less
  than your share and every later day gets more; go over and they get less.
- **How many 5-hour sessions fill a week?** It watches how far the weekly
  meter moves for every point the 5-hour meter moves, and turns that into
  "about 9 full 5-hour sessions use up the whole week" - with a range that
  narrows as more usage is seen.

![Dashboard](docs/screenshot.png)

*(Screenshot uses made-up numbers.)*

Plain Python standard library, no packages. Windows is the main target (the
R / Q keys use the Windows console); the rest works anywhere Python and
Claude Code run.

## How it works

Claude Code sends your plan-limit percentages to its status line after every
message. `statusline.js` is that status line: it shows cache stats in the
status bar and saves the numbers into your Claude folder (`~/.claude`):

| File | What's in it |
| --- | --- |
| `usage-latest.json` | The latest 5-hour and weekly percentages and reset times |
| `usage-history.jsonl` | One line every time either number changes - the permanent log |
| `usage-account.json` | The latest numbers from your account (written by the dashboard) |

`claude-usage.py` reads those two files and draws the dashboard, re-reading
every 30 seconds. It keeps nothing in memory, so closing it or rebooting loses
nothing. The numbers move when any Claude Code session sends a message; see
below for the desktop app and claude.ai.

### Account check (desktop app, claude.ai, and the status bar's lag)

The status line is a terminal feature, so usage in the Claude desktop app or
on claude.ai doesn't send new numbers by itself - and even in the terminal,
the status line's copy of the numbers runs about a point behind your account.
So while the dashboard is open, it asks your Claude account for the two
numbers every 2 minutes and shows those (the footer says "from your
account"). They're saved to `usage-account.json` and added to the same
history log; the status line's numbers are used whenever the account can't
be reached or the dashboard wasn't open.

This uses the login key Claude Code saved in `~/.claude/.credentials.json`
and sends it only to Anthropic, the same way Claude Code's usage screen does.
The connection isn't documented by Anthropic and could change; if it stops
working, the dashboard says why and keeps using the status line. Run with
`--no-account` (or set `CLAUDE_USAGE_NO_ACCOUNT=1`) to turn it off.

The banner shows your plan (Pro, Max, Team, Enterprise...) by asking Claude
Code (`claude auth status`). The usage-limit size, such as "Max 5x", is read
from the plan fields of Claude Code's saved login.

### The daily budget

A "day" starts at the same clock time as your weekly reset, so the week is
always exactly 7 equal days. If your week resets Thursday 4:00 AM, every day
runs 4:00 AM to 4:00 AM.

Today's allowance = the weekly limit left when today began ÷ days left
(including today). It's fixed for the day. A fresh week starts at 100 ÷ 7 ≈
14.3% per day.

On the first day of tracking, usage from before the first saved reading can't
be attributed to today, so that day is marked **Partial day**.

### Sessions per week

The weekly meter only shows whole percentages, so its true value is only
known exactly at the moment it ticks up a point. From one tick to a later
one, exactly that many weekly points were used - so the dashboard measures
how much the 5-hour meter moved between two weekly ticks, adding up 5-hour
usage across 5-hour resets. Nothing before the first tick it sees is used,
so starting at 27.9% doesn't make the first 0.1% look like a whole point.

Ticks are only compared within one source (account or status bar), because
the status bar runs a point behind. The "likely" range allows for not
knowing the exact moment of a tick, the 5-hour meter's own rounding, and a
little unseen use at each 5-hour reset. Until either source has seen two
ticks, a rougher whole-history estimate is shown, marked as rough.

In simulations (`python tools/simulate_sessions.py`), the estimate is
typically within 1-3% of the true value and the true value falls inside the
range in 96-100% of runs, depending on which sources are available.

## Setup

1. Clone this repo.
2. Point Claude Code's status line at `statusline.js` in `~/.claude/settings.json`:

   ```json
   "statusLine": {
     "type": "command",
     "command": "node \"C:/path/to/claude-usage-tracker/statusline.js\""
   }
   ```

3. Send any message in Claude Code, then run `Claude Usage.bat` (or
   `python claude-usage.py`).

Keys: **R** fetch fresh numbers from your account now, **Q** / **Esc** quit. `--once` prints one frame and exits.

If your Claude folder isn't `~/.claude`, set `CLAUDE_CONFIG_DIR` - both scripts
respect it.
