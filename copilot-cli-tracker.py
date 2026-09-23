#!/usr/bin/env python3
"""A dependency-free statusLine command for GitHub Copilot CLI.

Reads the Copilot CLI status-line JSON payload from stdin, reads
~/.copilot/session-store.db (read-only) for month-to-date spend, and prints
a single-line status string.

Config: ~/.copilot/copilot-cli-tracker.json (JSON, no external deps).
Run with --init to write the default config.
Set month_cost.budget_usd to a positive number to show an optional monthly
AI-credit-equivalent budget (not a billing or account balance).
The context gauge is hidden by default; set tokens.show_bar to true to opt in.
When Copilot's token count and percentage disagree, show only the percentage.

Segments: tokens, session_cost, month_cost, cache, reasoning, total_tokens, pr
Styles:   minimal, powerline, capsule, plain
Icons:    plain, nerd, emoji
Themes:   colorblind (default), github, nord, tokyo-night, plain
"""
import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
COPILOT_DIR = HOME / ".copilot"

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "style": "minimal",
    "icon_set": "plain",
    "theme": "colorblind",
    "segments": ["tokens", "session_cost", "month_cost", "cache", "reasoning", "total_tokens"],
    "tokens": {
        "enabled": True, "prefix": None, "show_percentage": True,
        "alert_threshold": 100_000, "show_bar": False,
    },
    "session_cost": {
        "enabled": True, "prefix": None, "currency_symbol": "$",
        "show_aic": False, "decimal_places": 2,
    },
    "month_cost": {
        "enabled": True, "prefix": None, "currency_symbol": "$",
        "show_aic": False, "decimal_places": 2, "db_path": None,
        "budget_usd": None, "budget_warning_percent": 80,
    },
    "cache": {
        "enabled": True, "prefix": None, "show_as_percentage": True,
        "auto_hide_zero": True,
    },
    "reasoning": {
        "enabled": True, "prefix": None, "auto_hide_zero": True,
    },
    "total_tokens": {
        "enabled": True, "prefix": None,
    },
    "pr": {
        "enabled": True, "prefix": None, "hyperlinks": True,
        "cache_ttl_seconds": 60,
    },
}


def default_config_path() -> Path:
    return COPILOT_DIR / "copilot-cli-tracker.json"


def merge_defaults(base: dict, overrides: dict) -> dict:
    out = dict(base)
    for k, v in (overrides or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_defaults(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | None) -> dict:
    cfg_path = Path(path) if path else default_config_path()
    try:
        with open(cfg_path, "r") as f:
            user_cfg = json.load(f)
    except FileNotFoundError as exc:
        if path:
            raise ValueError(f"Cannot load config {cfg_path}: {exc}") from exc
        return json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load config {cfg_path}: {exc}") from exc
    if not isinstance(user_cfg, dict):
        raise ValueError(f"Config {cfg_path} must contain a JSON object")
    return merge_defaults(DEFAULT_CONFIG, user_cfg)


# --------------------------------------------------------------------------
# Themes / palettes
# --------------------------------------------------------------------------

def palette_for_theme(theme_name: str) -> dict:
    name = (theme_name or "").lower()
    if name in ("github", "default"):
        return dict(reset="\x1b[0m", dim="\x1b[90m", label="\x1b[90m",
                    sep="\x1b[90m  │  \x1b[0m", spend="\x1b[92m",
                    tokens_normal="\x1b[97m", tokens_alert="\x1b[1;31m",
                    badge="\x1b[93m", pr="\x1b[33m")
    if name == "nord":
        return dict(reset="\x1b[0m", dim="\x1b[90m", label="\x1b[90m",
                    sep="\x1b[90m  │  \x1b[0m", spend="\x1b[36m",
                    tokens_normal="\x1b[97m", tokens_alert="\x1b[1;31m",
                    badge="\x1b[93m", pr="\x1b[33m")
    if name == "tokyo-night":
        return dict(reset="\x1b[0m", dim="\x1b[90m", label="\x1b[90m",
                    sep="\x1b[90m  │  \x1b[0m", spend="\x1b[95m",
                    tokens_normal="\x1b[97m", tokens_alert="\x1b[1;31m",
                    badge="\x1b[93m", pr="\x1b[93m")
    if name == "plain":
        return dict(reset="", dim="", label="", sep="  |  ", spend="",
                    tokens_normal="", tokens_alert="", badge="", pr="")
    # "colorblind" or anything else
    return dict(reset="\x1b[0m", dim="\x1b[90m", label="\x1b[90m",
                sep="\x1b[90m  │  \x1b[0m", spend="\x1b[94m",
                tokens_normal="\x1b[97m", tokens_alert="\x1b[1;31m",
                badge="\x1b[93m", pr="\x1b[93m")


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------

_ICONS = {
    "tokens": {"plain": "Tokens:", "nerd": "\U000f0b9a", "emoji": "🪙"},
    "session": {"plain": "Session:", "nerd": "\U000f012c", "emoji": "💰"},
    "month": {"plain": "Month:", "nerd": "\U000f0820", "emoji": "📅"},
    "cache": {"plain": "Cache:", "nerd": "\U000f0638", "emoji": "⚡"},
    "reasoning": {"plain": "Think:", "nerd": "\U000f06a9", "emoji": "🧠"},
    "total_tokens": {"plain": "Total:", "nerd": "\U000f04c5", "emoji": "📊"},
    "pr": {"plain": "PR", "nerd": "\uf407", "emoji": "🔀"},
}


def icon(name: str, icon_set: str, custom: str | None) -> str:
    if custom:
        return custom
    return _ICONS[name].get(icon_set, _ICONS[name]["plain"])


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------

def format_tokens(n) -> str:
    if not n or n <= 0:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1_000:.0f}k"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(int(n))


def calculate_spend(total_nano_aiu: int) -> tuple[float, float]:
    # 1 AIC = $0.01 USD; 1 nano AIU = 1e-9 AIC = 1e-11 USD
    aic = total_nano_aiu / 1e9
    usd = aic * 0.01
    return usd, aic


# --------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------

def render_tokens_segment(ctx: dict, cfg: dict, icon_set: str, p: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    r, d, lbl = p["reset"], p["dim"], p["label"]
    curr = ctx.get("current_context_tokens") or 0
    maxv = ctx.get("displayed_context_limit") or 0
    pct = ctx.get("current_context_used_percentage")

    if pct is not None and maxv > 0 and abs(pct - curr / maxv * 100) > 2:
        color = p["tokens_alert"] if pct >= 90 else p["tokens_normal"]
        label = cfg.get("prefix") if cfg.get("prefix") is not None else "Context:"
        return f"{lbl}{label}{r} {color}{pct:g}%{r}"

    curr_str = format_tokens(curr)
    max_str = format_tokens(maxv)

    used_pct = pct if pct is not None else curr / maxv * 100 if maxv > 0 else None
    is_alert = used_pct >= 90 if used_pct is not None else curr > cfg.get("alert_threshold", 100_000)
    curr_color = p["tokens_alert"] if is_alert else p["tokens_normal"]
    curr_styled = f"{curr_color}{curr_str}{r}"

    max_styled = f"{d}{max_str}{r}"
    pct_styled = ""
    if cfg.get("show_percentage", True) and pct is not None:
        pct_int = int(round(pct)) if float(pct).is_integer() else pct
        pct_styled = f" ({d}{pct_int:g}%{r})" if isinstance(pct_int, float) else f" ({d}{pct_int}%{r})"

    bar = ""
    if cfg.get("show_bar", False) and maxv > 0:
        progress = max(0.0, min(100.0, used_pct))
        filled = min(10, int(progress / 10 + 0.5))
        color = p["tokens_alert"] if progress >= 90 else p["badge"] if progress >= 70 else p["tokens_normal"]
        bar = f" {color}[{'█' * filled}{'░' * (10 - filled)}]{r}"

    ico = icon("tokens", icon_set, cfg.get("prefix"))
    return f"{lbl}{ico}{r} {curr_styled}/{max_styled}{pct_styled}{bar}"


def render_session_cost_segment(total_nano_aiu: int, cfg: dict, icon_set: str, p: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    r, d, lbl = p["reset"], p["dim"], p["label"]
    usd, aic = calculate_spend(total_nano_aiu)
    dp = cfg.get("decimal_places", 2)
    cost_str = f"{p['spend']}{cfg.get('currency_symbol', '$')}{usd:.{dp}f}{r}"
    ico = icon("session", icon_set, cfg.get("prefix"))
    out = f"{lbl}{ico}{r} {cost_str}"
    if cfg.get("show_aic") and aic >= 0.1:
        out += f" ({d}{aic:.1f} AIC{r})"
    return out


def validate_month_budget(cfg: dict) -> None:
    budget = cfg.get("budget_usd")
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not math.isfinite(budget) or budget <= 0:
            raise ValueError("month_cost.budget_usd must be a positive number")
    threshold = cfg.get("budget_warning_percent", 80)
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold) or not 0 < threshold <= 100:
        raise ValueError("month_cost.budget_warning_percent must be between 0 and 100")


def render_month_cost_segment(total_month_nano: int, cfg: dict, icon_set: str, p: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    validate_month_budget(cfg)
    r, d, lbl = p["reset"], p["dim"], p["label"]
    usd, aic = calculate_spend(total_month_nano)
    dp = cfg.get("decimal_places", 2)
    currency = cfg.get("currency_symbol", "$")
    budget = cfg.get("budget_usd")
    color = p["spend"]
    limit_str = ""
    if budget is not None:
        threshold = cfg.get("budget_warning_percent", 80)
        if usd >= budget:
            color = p["tokens_alert"]
        elif usd >= budget * threshold / 100:
            color = p["badge"]
        limit_str = f"/{currency}{budget:.{dp}f}"
    cost_str = f"{color}{currency}{usd:.{dp}f}{limit_str}{r}"
    ico = icon("month", icon_set, cfg.get("prefix"))
    out = f"{lbl}{ico}{r} {cost_str}"
    if cfg.get("show_aic") and aic >= 1.0:
        out += f" ({d}{aic:.0f} AIC{r})"
    return out


def render_cache_segment(ctx: dict, cfg: dict, icon_set: str, p: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    cache_read = ctx.get("total_cache_read_tokens") or 0
    cache_write = ctx.get("total_cache_write_tokens") or 0
    if cfg.get("auto_hide_zero", True) and cache_read == 0 and cache_write == 0:
        return None

    ico = icon("cache", icon_set, cfg.get("prefix"))
    r, lbl = p["reset"], p["label"]

    if cfg.get("show_as_percentage", True):
        total_in = ctx.get("total_input_tokens") or 0
        denom = total_in if total_in > 0 else (cache_read + cache_write)
        if denom > 0:
            pct = max(0.0, min(100.0, (cache_read / denom) * 100.0))
            value_str = f"{pct:.0f}%"
        else:
            value_str = "0%"
    else:
        value_str = format_tokens(cache_read)

    return f"{lbl}{ico}{r} {p['tokens_normal']}{value_str}{r}"


def render_reasoning_segment(ctx: dict, cfg: dict, icon_set: str, p: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    reasoning = ctx.get("total_reasoning_tokens") or 0
    if cfg.get("auto_hide_zero", True) and reasoning == 0:
        return None
    ico = icon("reasoning", icon_set, cfg.get("prefix"))
    r, lbl = p["reset"], p["label"]
    return f"{lbl}{ico}{r} {p['tokens_normal']}{format_tokens(reasoning)}{r}"


def render_total_tokens_segment(ctx: dict, cfg: dict, icon_set: str, p: dict) -> str | None:
    if not cfg.get("enabled", True):
        return None
    total = ctx.get("total_tokens")
    if total is None:
        total = (ctx.get("total_input_tokens") or 0) + (ctx.get("total_output_tokens") or 0)
    ico = icon("total_tokens", icon_set, cfg.get("prefix"))
    r, lbl = p["reset"], p["label"]
    return f"{lbl}{ico}{r} {p['tokens_normal']}{format_tokens(total)}{r}"


_GITHUB_URL_RE = re.compile(r"^https://(www\.)?github\.com(/.*)?$")


def _is_safe_github_url(url: str) -> bool:
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in url):
        return False
    return bool(_GITHUB_URL_RE.match(url))


def render_pr_segment(pr_info: dict | None, cfg: dict, icon_set: str, p: dict) -> str | None:
    if not cfg.get("enabled", True) or not pr_info:
        return None
    ico = icon("pr", icon_set, cfg.get("prefix"))
    r = p["reset"]
    icon_part = f"{p['label']}{ico}{r} " if ico else ""
    pr_text = f"#{pr_info['number']}"

    if not r:  # plain theme, no escape codes
        formatted = pr_text
    elif cfg.get("hyperlinks", True) and _is_safe_github_url(pr_info["url"]):
        formatted = f"\x1b[4m{p['pr']}\x1b]8;;{pr_info['url']}\x1b\\{pr_text}\x1b]8;;\x1b\\{r}"
    else:
        formatted = f"{p['pr']}{pr_text}{r}"
    return f"{icon_part}{formatted}"


# --------------------------------------------------------------------------
# DB: month-to-date spend from other sessions
# --------------------------------------------------------------------------

def default_db_path() -> Path:
    return COPILOT_DIR / "session-store.db"


def get_month_usage_nano(db_path: Path, session_id: str | None, session_nano: int) -> int | None:
    if not db_path.exists():
        return None
    try:
        uri = f"file:{db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            cur = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN created_at >= strftime('%Y-%m-01', 'now')
                        THEN total_nano_aiu ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN session_id = ?
                        THEN total_nano_aiu ELSE 0 END), 0)
                FROM assistant_usage_events
                WHERE created_at >= strftime('%Y-%m-01', 'now') OR session_id = ?
                """,
                (session_id or "", session_id or ""),
            )
            month_logged, session_logged = cur.fetchone()
            live_delta = max(0, session_nano - session_logged) if session_id else 0
            return max(0, int(month_logged) + live_delta)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"Monthly usage unavailable: {exc}", file=sys.stderr)
        return None


# --------------------------------------------------------------------------
# Git / PR lookup (optional "pr" segment)
# --------------------------------------------------------------------------

def find_repo_root(start: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return None
        current = current.parent


def get_git_branch(repo_root: Path) -> str | None:
    git_dir = repo_root / ".git"
    if git_dir.is_file():
        try:
            content = git_dir.read_text().strip()
            if content.startswith("gitdir:"):
                target = Path(content.split(":", 1)[1].strip())
                git_dir = target if target.is_absolute() else (repo_root / target)
        except OSError:
            return None
    head_path = git_dir / "HEAD"
    try:
        line = head_path.read_text().strip()
    except OSError:
        return None
    if line.startswith("ref: refs/heads/"):
        return line[len("ref: refs/heads/"):]
    if len(line) >= 7:
        return line[:7]
    return None


def is_gh_available() -> bool:
    from shutil import which
    return which("gh") is not None


def _valid_pr(pr: object) -> bool:
    return (isinstance(pr, dict) and type(pr.get("number")) is int
            and pr["number"] > 0 and isinstance(pr.get("url"), str)
            and _is_safe_github_url(pr["url"]))


def get_pr_info(repo_dir: Path, branch: str, ttl_seconds: int = 60) -> dict | None:
    """Cache PR results per repository and branch so refreshes do not repeat `gh` calls."""
    key = hashlib.sha256(f"{repo_dir.resolve()}\0{branch}".encode()).hexdigest()[:24]
    cache_path = COPILOT_DIR / f".copilot-cli-tracker-pr-{key}.json"
    if ttl_seconds > 0:
        try:
            entry = json.loads(cache_path.read_text())
            if 0 <= time.time() - entry["checked_at"] < ttl_seconds:
                pr = entry["pr"]
                if pr is not None and not _valid_pr(pr):
                    raise ValueError("invalid cached PR")
                return pr
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            print(f"PR cache unreadable: {exc}", file=sys.stderr)
    if not is_gh_available():
        return None
    try:
        result = subprocess.run(
            ["gh", "pr", "list", "--head", branch, "--state", "open",
             "--limit", "1", "--json", "number,url"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=3,
        )
        if result.returncode != 0:
            print(f"gh pr list failed: {result.stderr.strip()}", file=sys.stderr)
            return None
        results = json.loads(result.stdout)
        if not isinstance(results, list):
            raise ValueError("invalid PR list from gh")
        pr = results[0] if results else None
        if pr is not None and not _valid_pr(pr):
            raise ValueError("invalid PR result from gh")
    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
        print(f"PR lookup failed: {exc}", file=sys.stderr)
        return None
    if ttl_seconds > 0:
        temp_path = None
        try:
            COPILOT_DIR.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=COPILOT_DIR, prefix=".pr-cache-", delete=False) as temp:
                temp_path = Path(temp.name)
                os.chmod(temp.name, 0o600)
                json.dump({"checked_at": time.time(), "pr": pr}, temp)
            os.replace(temp.name, cache_path)
        except OSError as exc:
            print(f"PR cache write failed: {exc}", file=sys.stderr)
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"PR cache cleanup failed: {exc}", file=sys.stderr)
    return pr


# --------------------------------------------------------------------------
# Rendering / layout
# --------------------------------------------------------------------------

def render_segments(segments: list[str], style: str, p: dict) -> str:
    if not segments:
        return ""
    if style == "minimal":
        return p["sep"].join(segments)
    if style == "plain":
        return "  |  ".join(segments)
    if style == "powerline":
        sep = f"{p['dim']}  {p['reset']}"
        return sep.join(segments)
    if style == "capsule":
        return " ".join(f"{p['dim']}{p['reset']}{s} {p['dim']}{p['reset']}" for s in segments)
    return p["sep"].join(segments)


# --------------------------------------------------------------------------
# Copilot stdin payload
# --------------------------------------------------------------------------

def read_stdin_payload() -> dict:
    if sys.stdin.isatty():
        return {}
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def detect_copilot_theme() -> str | None:
    settings_path = COPILOT_DIR / "settings.json"
    try:
        with open(settings_path) as f:
            data = json.load(f)
        theme = data.get("theme")
        return theme if isinstance(theme, str) else None
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="copilot-cli-tracker",
        description="Dependency-free statusLine for GitHub Copilot CLI",
    )
    parser.add_argument("-c", "--config", help="Path to JSON configuration file")
    parser.add_argument("-s", "--style", choices=["minimal", "powerline", "capsule", "plain"])
    parser.add_argument("-t", "--theme")
    parser.add_argument("-i", "--icon-set", dest="icon_set", choices=["plain", "nerd", "emoji"])
    parser.add_argument("--init", action="store_true", help="Write default config file")
    args = parser.parse_args()

    if args.init:
        cfg_path = default_config_path()
        if cfg_path.exists():
            print(f"Config file already exists at {cfg_path}", file=sys.stderr)
        else:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            cfg_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n")
            print(f"Initialized config at {cfg_path}")
        return

    try:
        config = load_config(args.config)
        validate_month_budget(config["month_cost"])
    except ValueError as exc:
        parser.error(str(exc))
    if args.style:
        config["style"] = args.style
    if args.icon_set:
        config["icon_set"] = args.icon_set
    if args.theme:
        config["theme"] = args.theme
    elif config.get("theme") == "colorblind":
        copilot_theme = detect_copilot_theme()
        if copilot_theme:
            config["theme"] = copilot_theme

    palette = palette_for_theme(config["theme"])
    icon_set = config["icon_set"]

    payload = read_stdin_payload()
    ctx = payload.get("context_window") or {}
    session_id = payload.get("session_id")
    session_nano = int((payload.get("ai_used") or {}).get("total_nano_aiu") or 0)

    db_path = Path(config["month_cost"].get("db_path") or default_db_path())
    total_month_nano = get_month_usage_nano(db_path, session_id, session_nano)

    pr_enabled = config["pr"].get("enabled", True) and "pr" in config["segments"]
    pr_info = None
    if pr_enabled:
        cwd = Path(os.getcwd())
        repo_root = find_repo_root(cwd)
        if repo_root:
            branch = get_git_branch(repo_root)
            if branch:
                pr_info = get_pr_info(repo_root, branch, config["pr"].get("cache_ttl_seconds", 60))

    rendered = []
    for seg in config["segments"]:
        out = None
        if seg == "tokens":
            out = render_tokens_segment(ctx, config["tokens"], icon_set, palette)
        elif seg == "session_cost":
            out = render_session_cost_segment(session_nano, config["session_cost"], icon_set, palette)
        elif seg == "month_cost":
            if config["month_cost"].get("enabled", True):
                if total_month_nano is None:
                    out = f"{palette['label']}{icon('month', icon_set, config['month_cost'].get('prefix'))}{palette['reset']} unavailable"
                else:
                    out = render_month_cost_segment(total_month_nano, config["month_cost"], icon_set, palette)
        elif seg == "cache":
            out = render_cache_segment(ctx, config["cache"], icon_set, palette)
        elif seg == "reasoning":
            out = render_reasoning_segment(ctx, config["reasoning"], icon_set, palette)
        elif seg == "total_tokens":
            out = render_total_tokens_segment(ctx, config["total_tokens"], icon_set, palette)
        elif seg == "pr":
            out = render_pr_segment(pr_info, config["pr"], icon_set, palette)
        if out:
            rendered.append(out)

    print(render_segments(rendered, config["style"], palette))


if __name__ == "__main__":
    main()
