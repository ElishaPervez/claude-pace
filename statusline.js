#!/usr/bin/env node
// Claude Code status line + usage feed for claude-usage.py.
//
// Status bar text: model, this turn's prompt-cache READ (reused) vs CREATE
// (freshly written) tokens, and the folder. Big READ = the cache was still
// warm; ~0 READ with a big CREATE = it expired and was rebuilt.
//
// Usage feed: Claude Code passes rate_limits.{five_hour,seven_day}
// .{used_percentage,resets_at} on stdin. They're copied into your Claude
// folder (CLAUDE_CONFIG_DIR, default ~/.claude):
//   usage-latest.json    latest numbers. Written to a temp file then renamed
//                        so the dashboard never reads a half-written file.
//   usage-history.jsonl  one line each time either percentage or reset time
//                        changes - the permanent log the dashboard works from.
//                        Duplicate lines from two sessions racing are harmless;
//                        they add zero change.
// Never allowed to break the status line.
//
// last_check records whether the latest run got numbers. A failure is only
// recorded once the session has finished a turn (current_usage is set) - a
// fresh session with no messages yet legitimately has no rate_limits.
const DATA_DIR = process.env.CLAUDE_CONFIG_DIR ||
  require('path').join(require('os').homedir(), '.claude');

function appendHistory(fs, path, prev, next) {
  const file = path.join(DATA_DIR, 'usage-history.jsonl');
  const key = l => (l ? `${l.used_percentage}|${Math.round((l.resets_at || 0) / 60)}` : '-');
  const unchanged = key(prev.five_hour) === key(next.five_hour) && key(prev.seven_day) === key(next.seven_day);
  if (unchanged && fs.existsSync(file)) return;
  const f = next.five_hour || {}, w = next.seven_day || {};
  const line = { t: next.saved_at, h: f.used_percentage, hr: f.resets_at, w: w.used_percentage, wr: w.resets_at };
  fs.appendFileSync(file, JSON.stringify(line) + '\n');
}

function saveUsage(d) {
  try {
    const fs = require('fs');
    const path = require('path');
    const out = path.join(DATA_DIR, 'usage-latest.json');
    const now = Math.floor(Date.now() / 1000);
    const rl = d.rate_limits;
    let prev = {};
    try { prev = JSON.parse(fs.readFileSync(out, 'utf8')); } catch {}

    let next;
    if (rl && (rl.five_hour || rl.seven_day)) {
      next = {
        saved_at: now,
        five_hour: rl.five_hour || null,
        seven_day: rl.seven_day || null,
        last_check: { at: now, ok: true },
      };
      try { appendHistory(fs, path, prev, next); } catch {}
    } else if (d.context_window?.current_usage) {
      const reason = d.rate_limits_available === false ? 'no_plan' : 'missing';
      next = { ...prev, last_check: { at: now, ok: false, reason } };
    } else {
      return;
    }

    const tmp = `${out}.${process.pid}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify(next));
    fs.renameSync(tmp, out);
  } catch {}
}

let input = '';
process.stdin.on('data', c => (input += c));
process.stdin.on('end', () => {
  let model = 'Claude';
  try {
    const d = JSON.parse(input);
    model = d.model?.display_name || model;
    const dir = require('path').basename(d.cwd || d.workspace?.current_dir || '.');
    const u = d.context_window?.current_usage;

    saveUsage(d);

    const fmt = n => (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n));

    if (!u) {
      // null before the first API call, or this Claude Code build doesn't
      // expose current_usage in the statusline payload yet.
      console.log(`[${model}] | cache: (no turn data) | ${dir}`);
      return;
    }

    const read = u.cache_read_input_tokens || 0;
    const create = u.cache_creation_input_tokens || 0;

    // Verdict heuristic: warm if most of the big context block came from the
    // cache (READ); "rebuilt" if the prefix had to be re-written (CREATE) with
    // almost nothing read back. The raw numbers are the real signal.
    let verdict = '';
    if (read + create > 0) {
      if (read >= create) verdict = ' WARM';
      else if (read < 2000 && create > 5000) verdict = ' REBUILT';
    }

    console.log(`[${model}] | cache read ${fmt(read)} / create ${fmt(create)}${verdict} | ${dir}`);
  } catch {
    console.log(`[${model}]`);
  }
});
