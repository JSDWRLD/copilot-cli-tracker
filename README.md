# Copilot CLI Tracker

A single-file, dependency-free Python status line for GitHub Copilot CLI. It
shows context usage, session and month-to-date AI-credit-equivalent spend,
cache efficiency, reasoning tokens, and total tokens. A pull-request indicator
is available as an opt-in segment.

Requires **Python 3.10+** and a Copilot CLI version that supports custom
`statusLine` commands. No Homebrew, Rust, or Python packages are needed.

## Install

Clone this repository, then copy the script somewhere permanent:

```sh
git clone https://github.com/JSDWRLD/copilot-cli-tracker.git
mkdir -p ~/.copilot
cp copilot-cli-tracker/copilot-cli-tracker.py ~/.copilot/copilot-cli-tracker.py
```

Add `statusLine` to your existing `~/.copilot/settings.json` without removing
your other settings. Replace `/absolute/path/to` with your actual home
directory path: the command must use an **absolute path**, not `~`.

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /absolute/path/to/.copilot/copilot-cli-tracker.py",
    "refreshInterval": 30
  }
}
```

Restart Copilot CLI after changing settings. The CLI sends a JSON payload on
stdin; the script prints one status line on stdout. To preview it without
Copilot:

```sh
printf '%s\n' '{"context_window":{"current_context_tokens":50000,"displayed_context_limit":100000,"current_context_used_percentage":50},"ai_used":{"total_nano_aiu":100000000000}}' |
  python3 ~/.copilot/copilot-cli-tracker.py --theme plain
```

## Configure

Run `python3 ~/.copilot/copilot-cli-tracker.py --init` to create
`~/.copilot/copilot-cli-tracker.json`; edit that file to reorder segments,
change colors, or opt into the optional features. It is not required for
defaults. A partial config works too:

```json
{
  "style": "minimal",
  "icon_set": "plain",
  "segments": ["tokens", "session_cost", "month_cost", "cache", "reasoning", "total_tokens"],
  "month_cost": {
    "budget_usd": 100
  }
}
```

The optional budget changes the month value to `spent/limit`. It turns amber
at 80% and red at 100%; change the threshold with
`month_cost.budget_warning_percent`. These dollar values are **AI-credit
equivalents**, not your GitHub invoice or remaining account balance. The
script converts the `total_nano_aiu` usage supplied by Copilot at 1 AI Credit
= $0.01; it does not maintain per-model prices.

The context segment has no emoji or block gauge by default. At high context
usage, text turns red. If Copilot supplies a count and percentage that
disagree, the status line shows only its reported percentage rather than
presenting contradictory numbers. Set `"tokens": {"show_bar": true}` if your
terminal supports the optional ten-cell Unicode gauge.

You can override style, theme, or icons with `--style`, `--theme`, or
`--icon-set`:

| Setting | Choices |
| --- | --- |
| Style | `minimal`, `powerline`, `capsule`, `plain` |
| Theme | `colorblind`, `github`, `nord`, `tokyo-night`, `plain` |
| Icons | `plain`, `nerd`, `emoji` (the last two are opt-in) |

The `powerline` and `capsule` styles and `nerd` icons require a compatible
terminal font.

### Data and privacy

Session usage comes from the Copilot CLI stdin payload. Month-to-date usage
comes from `~/.copilot/session-store.db`, opened read-only, with the current
session's unrecorded usage included. If the database is missing or unreadable,
the month segment says `unavailable` rather than showing an inaccurate zero.
The script also reads `~/.copilot/settings.json` for a theme preference.

There are **no network requests by default**. To opt into a PR indicator, add
`"pr"` to the `segments` array. That segment requires an installed,
authenticated `gh` CLI; it checks for an open pull request on the current
branch and caches the result locally for 60 seconds by default
(`pr.cache_ttl_seconds`). Only this opt-in feature invokes `gh`.

## Run tests

```sh
python3 -m unittest discover -s tests -v
```

The tests use Python's standard library and a temporary SQLite database; the
same command runs in GitHub Actions on pushes and pull requests.

## License

MIT. See [LICENSE](LICENSE) for the required copyright and permission notice.
