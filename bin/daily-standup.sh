#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# daily-standup.sh — Daily standup summary → Telegram
# ============================================================================
# Runs the standup script, passes the output to Claude for formatting,
# and sends it to Telegram.
#
# Usage:
#   ./daily-standup.sh              # yesterday's standup
#   ./daily-standup.sh --today      # today's standup
#   ./daily-standup.sh --test       # send a test ping
#   ./daily-standup.sh --check      # print what it resolved, send nothing
#
# Everything it needs is either beside it in this repository or named by an
# environment variable, so it runs from a clone rather than from one machine:
#
#   STANDUP_CONFIG_DIR   where the credentials live   (~/.config/standup)
#   STANDUP_CONFIG       the report config            (<repo>/standup.yml)
#   STANDUP_STATE_DIR    pending button presses       (~/.local/state/standup)
#   STANDUP_FORMATTER    claude | cursor              (or formatter: in standup.yml)
#   FORMATTER_BIN        path to the formatter CLI
#   FORMATTER_MODEL      model id for the formatter
#   CLAUDE_TOKEN_ENV     headless Claude Code auth    (~/.config/claude-code-token.env)
#   CLAUDE_BIN           Claude CLI (alias when formatter is claude)
# ============================================================================

# readlink -f, not dirname alone: the README teaches symlinking this into
# ~/.local/bin, and a bare dirname then resolves to ~/.local/bin, where neither
# standup.rb nor standup-publish.py is.
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# standup.rb sits at the repository root; this script sits in bin/ beneath it.
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# The PATH as the scheduler handed it over, before any profile below widens it.
# --check reports bird against this one: bird is run by standup-publish.py, which
# is invoked directly by cron and sources no profile at all. Resolving it against
# the widened PATH would report a binary that the poller cannot see.
CRON_PATH="$PATH"

# LinkedIn is armed only when buffer.env carries BOTH keys with a real value.
# A half-configured destination is worse than an absent one: it offers a button
# that fails hours later. One function, because the keyboard and --check have to
# agree with each other and with the publisher — and the publisher strips quotes
# before deciding, so BUFFER_API_KEY="" is empty there and must be empty here.
linkedin_armed() {
  local env_file="${STANDUP_CONFIG_DIR:-$HOME/.config/standup}/buffer.env" key value
  [ -f "$env_file" ] || return 1
  for key in BUFFER_API_KEY BUFFER_LINKEDIN_CHANNEL; do
    value=$(sed -nE "s/^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=[[:space:]]*//p" \
            "$env_file" | head -1)
    value="${value%"${value##*[![:space:]]}"}"
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    [ -n "$value" ] || return 1
  done
  return 0
}

# ---- Source shell profile (cron runs with minimal env) ----
#
# The profile is sourced for PATH, which cron does not give us. It is also a
# file that can export anything, and everything it exports lands on top of what
# the cron line set — including the variables the README tells you to set on
# the cron line. So the caller's values are put back afterwards: an override
# written where the documentation says to write it has to win over a profile
# that happens to mention the same name.
#
# Insurance rather than a repair: no profile on this machine exports any of
# these. It costs six lines and removes a class of failure that would look like
# the override simply being ignored.
# printf -v, never eval: eval re-parses the value, so a path holding $(...) or
# a backtick would be executed rather than stored — the same shell-injection
# shape standup.rb already avoids for git author names.
#
# The credentials are in the list because a profile exporting either of them
# silently changes WHERE the standup is posted, which is the worst version of
# this failure and the least visible.
_OVERRIDES=(CLAUDE_BIN BIRD_BIN BUFFER_BIN CLAUDE_TOKEN_ENV STANDUP_CONFIG STANDUP_CONFIG_DIR
            STANDUP_STATE_DIR STANDUP_FORMATTER FORMATTER_BIN FORMATTER_MODEL
            TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID)
for _v in "${_OVERRIDES[@]}"; do
  # ${!_v+set}, not -n: a caller that writes VAR= on the cron line means "empty",
  # and that has to win over a profile export too.
  [ -n "${!_v+set}" ] && printf -v "_caller_$_v" '%s' "${!_v}"
done

if [ -f "$HOME/.zshrc" ]; then
  export SHELL=/bin/zsh
  set +eu
  source "$HOME/.zshenv" 2>/dev/null || true
  source "$HOME/.zprofile" 2>/dev/null || true
  source "$HOME/.zshrc" 2>/dev/null || true
  set -eu
elif [ -f "$HOME/.bashrc" ]; then
  set +eu
  source "$HOME/.bash_profile" 2>/dev/null || true
  source "$HOME/.bashrc" 2>/dev/null || true
  set -eu
fi

# set +eu turns off only -e and -u, so -o pipefail survives the block above —
# measured, not assumed, because a review expected otherwise. Restated in full
# anyway so the script's options do not depend on that detail staying true.
set -euo pipefail

for _v in "${_OVERRIDES[@]}"; do
  _saved="_caller_$_v"
  [ -n "${!_saved+set}" ] && export "$_v=${!_saved}"
  unset "$_saved"
done
unset _v _saved _OVERRIDES

# ---- Load Telegram credentials ----
#
# The standup wants a bot of its own, not one shared with another program. Two
# processes reading one Telegram update queue means each button press goes to
# whichever asked first, so the button looks fine and collects nothing.
STANDUP_CONFIG_DIR="${STANDUP_CONFIG_DIR:-$HOME/.config/standup}"
TELEGRAM_CREDS="$STANDUP_CONFIG_DIR/telegram.env"
if [ -f "$TELEGRAM_CREDS" ]; then
  set -a; source "$TELEGRAM_CREDS"; set +a
fi

# ---- Claude Code headless auth (cron has no interactive OAuth session) ----
CLAUDE_TOKEN_ENV="${CLAUDE_TOKEN_ENV:-$HOME/.config/claude-code-token.env}"
if [ -f "$CLAUDE_TOKEN_ENV" ]; then
  set -a; source "$CLAUDE_TOKEN_ENV"; set +a
fi

# Resolve which LLM CLI formats the report. Env wins over standup.yml; defaults
# keep the historical Claude + haiku behaviour when nothing is set.
#
# Sets STANDUP_FORMATTER, FORMATTER_BIN, FORMATTER_MODEL. Safe with a missing
# config: --check must still work on a fresh clone.
resolve_formatter() {
  local cfg="${1:-}" yml_formatter="" yml_model=""
  if [ -n "$cfg" ] && [ -s "$cfg" ]; then
    eval "$(RUBYOPT="-Eutf-8:utf-8" ruby -ryaml -rshellwords -e '
path = ARGV[0]
raw = File.read(path)
cfg = YAML.safe_load(raw, permitted_classes: [], permitted_symbols: [], aliases: true) || {}
cfg = {} unless cfg.is_a?(Hash)
fmt = (cfg["formatter"] || "").to_s.strip
model = (cfg["formatter_model"] || "").to_s.strip
puts "yml_formatter=#{Shellwords.escape(fmt)}"
puts "yml_model=#{Shellwords.escape(model)}"
' "$cfg")"
  fi
  local fmt
  fmt="${STANDUP_FORMATTER:-${yml_formatter:-claude}}"
  fmt=$(printf '%s' "$fmt" | tr '[:upper:]' '[:lower:]')
  case "$fmt" in
    claude|cursor) ;;
    *)
      echo "WARNING: unknown formatter '$fmt'; using claude" >&2
      fmt=claude
      ;;
  esac
  local bin model
  case "$fmt" in
    claude)
      model="${FORMATTER_MODEL:-${yml_model:-haiku}}"
      bin="${FORMATTER_BIN:-${CLAUDE_BIN:-$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")}}"
      ;;
    cursor)
      model="${FORMATTER_MODEL:-${yml_model:-}}"
      bin="${FORMATTER_BIN:-$(command -v agent 2>/dev/null || echo "$HOME/.local/bin/agent")}"
      ;;
  esac
  STANDUP_FORMATTER="$fmt"
  FORMATTER_BIN="$bin"
  FORMATTER_MODEL="$model"
}

run_with_timeout() {
  # GNU timeout is not on stock macOS. Prefer timeout, then gtimeout (Homebrew
  # coreutils), then perl alarm — Perl ships on macOS and keeps the 120s cap
  # without requiring brew. SECS first, then the command argv.
  local secs="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$secs" "$@"
  elif command -v gtimeout >/dev/null 2>&1; then
    gtimeout "$secs" "$@"
  else
    perl -e 'alarm shift; exec @ARGV' "$secs" "$@"
  fi
}

timeout_backend() {
  if command -v timeout >/dev/null 2>&1; then
    echo "timeout ($(command -v timeout))"
  elif command -v gtimeout >/dev/null 2>&1; then
    echo "gtimeout ($(command -v gtimeout))"
  elif command -v perl >/dev/null 2>&1; then
    echo "perl-alarm ($(command -v perl))"
  else
    echo "NONE"
  fi
}

run_formatter() {
  local prompt="$1" out="" err rc=0
  err=$(mktemp 2>/dev/null) || err=""
  case "$STANDUP_FORMATTER" in
    claude)
      if [ -n "$err" ]; then
        out=$(printf '%s' "$prompt" | run_with_timeout 120 "$FORMATTER_BIN" -p --model "$FORMATTER_MODEL" 2>"$err") || rc=$?
      else
        out=$(printf '%s' "$prompt" | run_with_timeout 120 "$FORMATTER_BIN" -p --model "$FORMATTER_MODEL" 2>/dev/null) || rc=$?
      fi
      ;;
    cursor)
      if [ -n "$FORMATTER_MODEL" ]; then
        if [ -n "$err" ]; then
          out=$(run_with_timeout 120 "$FORMATTER_BIN" -p --output-format text --model "$FORMATTER_MODEL" --mode ask -- "$prompt" 2>"$err") || rc=$?
        else
          out=$(run_with_timeout 120 "$FORMATTER_BIN" -p --output-format text --model "$FORMATTER_MODEL" --mode ask -- "$prompt" 2>/dev/null) || rc=$?
        fi
      else
        if [ -n "$err" ]; then
          out=$(run_with_timeout 120 "$FORMATTER_BIN" -p --output-format text --mode ask -- "$prompt" 2>"$err") || rc=$?
        else
          out=$(run_with_timeout 120 "$FORMATTER_BIN" -p --output-format text --mode ask -- "$prompt" 2>/dev/null) || rc=$?
        fi
      fi
      ;;
  esac
  if [ -z "$out" ] && [ -n "$err" ] && [ -s "$err" ]; then
    echo "  Formatter stderr: $(head -c 300 "$err" | tr '\n' ' ')" >&2
  fi
  [ -n "$err" ] && rm -f "$err"
  printf '%s' "$out"
}

# A non-empty YAML with at least one non-comment line. Blank / "---"-only files
# parse to an empty config and would publish every repository by directory name.
config_is_usable() {
  local path="${1:-}"
  [ -n "$path" ] && [ -s "$path" ] && grep -qE '^[[:space:]]*[^#[:space:]-]' "$path"
}

# Match standup.rb without --config: ~/.standup.yml, then <repo>/standup.yml.
# STANDUP_CONFIG in the environment wins and is not searched past.
# On success sets STANDUP_CONFIG to the chosen path. On failure leaves it alone
# when it was set by the caller; when searching, leaves it unset.
resolve_standup_config() {
  if [ -n "${STANDUP_CONFIG+set}" ]; then
    config_is_usable "$STANDUP_CONFIG"
    return $?
  fi
  local candidate
  for candidate in "$HOME/.standup.yml" "$REPO_DIR/standup.yml"; do
    if config_is_usable "$candidate"; then
      STANDUP_CONFIG="$candidate"
      return 0
    fi
  done
  return 1
}

# ---- Check mode: what did all of that resolve to? ----
#
# Placed before the credential check on purpose, and this is the whole point of
# the flag: --check has to work on a machine that has nothing set up yet, or it
# cannot report what is missing. Put it after, and it dies on the first thing
# absent instead of listing all of them — which is what happened, and what CI
# caught. It sends nothing and always exits 0.
#
# Resolution order here mirrors what actually runs: the environment variable
# first, then PATH, then the documented default. Printing PATH first would
# name a binary the cron job will never call.
if [ "${1:-}" = "--check" ]; then
  resolve_standup_config || true
  if config_is_usable "${STANDUP_CONFIG:-}"; then
    cfg="$STANDUP_CONFIG"
    cfg_note=""
  elif [ -n "${STANDUP_CONFIG+set}" ]; then
    cfg="$STANDUP_CONFIG"
    cfg_note=" (MISSING or EMPTY — copy standup.yml.example)"
  else
    cfg="$HOME/.standup.yml or $REPO_DIR/standup.yml"
    cfg_note=" (MISSING or EMPTY — copy standup.yml.example)"
  fi
  resolve_formatter "${STANDUP_CONFIG:-}"
  echo "repository:    $REPO_DIR"
  echo "standup.rb:    $REPO_DIR/standup.rb $([ -f "$REPO_DIR/standup.rb" ] || echo '(MISSING)')"
  # -s, matching the guard below. With -f an empty config reports as present
  # here and is then refused at run time, so --check would describe a run that
  # cannot happen.
  echo "report config: $cfg$cfg_note"
  echo "credentials:   $TELEGRAM_CREDS $([ -f "$TELEGRAM_CREDS" ] || echo '(MISSING — copy standup.env.example)')"
  echo "bot token:     $([ -n "${TELEGRAM_BOT_TOKEN:-}" ] && echo set || echo 'NOT SET')"
  echo "chat id:       $([ -n "${TELEGRAM_CHAT_ID:-}" ] && echo set || echo 'NOT SET')"
  bird_at="${BIRD_BIN:-$(PATH="$CRON_PATH" command -v bird 2>/dev/null || echo "$HOME/.npm-global/bin/bird")}"
  formatter_note=""
  if [ ! -x "$FORMATTER_BIN" ]; then
    if [ "$STANDUP_FORMATTER" = claude ]; then
      formatter_note=" (MISSING — set FORMATTER_BIN or CLAUDE_BIN)"
    else
      formatter_note=" (MISSING — set FORMATTER_BIN)"
    fi
  fi
  model_note="—"
  [ -n "$FORMATTER_MODEL" ] && model_note="$FORMATTER_MODEL"
  echo "formatter:     $STANDUP_FORMATTER  $FORMATTER_BIN  model=$model_note$formatter_note"
  echo "timeout:       $(timeout_backend)"
  if config_is_usable "${STANDUP_CONFIG:-}"; then
    RUBYOPT="-Eutf-8:utf-8" ruby -ryaml -e '
path = ARGV[0]
cfg = YAML.safe_load(File.read(path), permitted_classes: [], permitted_symbols: [], aliases: true) || {}
map = cfg["repo_name_mapping"]
exit 0 unless map.is_a?(Hash)
bad = map.select { |_k, v| v.is_a?(String) && !v.strip.match?(/\A#[A-Za-z0-9][A-Za-z0-9_-]*\z/) }
bad.each { |k, v| warn "mapping warn: #{k} -> #{v.inspect} (need a #hashtag for publish)" }
' "$STANDUP_CONFIG" 2>&1 | while IFS= read -r line; do echo "  $line"; done
  fi
  echo "bird:          $bird_at $([ -x "$bird_at" ] || echo '(MISSING — set BIRD_BIN; only needed to post to X)')"
  if linkedin_armed; then
    # The same fallback the publisher uses, or --check calls a binary missing
    # that the publisher would have found.
    buffer_at="${BUFFER_BIN:-$(PATH="$CRON_PATH" command -v buffer 2>/dev/null || echo "$HOME/.npm-global/bin/buffer")}"
    echo "LinkedIn:      armed $([ -x "$buffer_at" ] || echo "(but $buffer_at is MISSING — set BUFFER_BIN)")"
  else
    echo "LinkedIn:      not armed (${STANDUP_CONFIG_DIR:-$HOME/.config/standup}/buffer.env needs BUFFER_API_KEY and BUFFER_LINKEDIN_CHANNEL)"
  fi
  echo "publisher:     $SCRIPT_DIR/standup-publish.py $([ -f "$SCRIPT_DIR/standup-publish.py" ] || echo '(MISSING)')"
  echo "state dir:     ${STANDUP_STATE_DIR:-$HOME/.local/state/standup}"
  exit 0
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "ERROR: Telegram credentials not found at $TELEGRAM_CREDS"
  exit 1
fi

# Render a report as MarkdownV2, or hand back what we were given.
#
# Guarded at every call site, and this is not defensive habit: the script runs
# under `set -euo pipefail`, send_telegram below is the function that reports
# THAT standup.rb just failed, and the publisher this pipes through is the file
# most likely to be mid-edit. An unguarded command substitution there would let
# a broken publisher silence the one message whose whole job is to be loud.
#
# On failure the caller sends the unescaped text with no parse_mode, which is
# ugly and correct, rather than nothing at all — and it says why, because a
# renderer that silently stops working would look like Telegram being slow.
# PYTHONUTF8/PYTHONIOENCODING because cron often runs with LANG unset or LANG=C.
# Modern CPython coerces that to a UTF-8 locale and the emoji in the title
# survives, but that coercion is a build option, and the failure it prevents is
# precisely the one this file exists to fix: the renderer dies, the report goes
# out unformatted, and it looks like Telegram being slow.
PY_UTF8=(env PYTHONUTF8=1 PYTHONIOENCODING=utf-8)

telegram_markdown() {
  local out err rc
  # No /dev/null fallback here: rm -f /dev/null fails for an ordinary user, and
  # under set -e that would abort a run that was going perfectly well. If mktemp
  # cannot give us a file we simply lose the diagnostic, which is the smaller
  # loss by far.
  err=$(mktemp 2>/dev/null) || err=""
  # `|| rc=$?` rather than a bare assignment: under set -e a bare one aborts
  # the caller before the error path below can run, and it only looks safe
  # today because every call site happens to be a conditional. That dependency
  # is invisible from here, which is how it would eventually be broken.
  rc=0
  if [ -n "$err" ]; then
    out=$(printf '%s' "$1" | "${PY_UTF8[@]}" python3 "$SCRIPT_DIR/standup-publish.py" --telegram-markdown 2>"$err") || rc=$?
  else
    out=$(printf '%s' "$1" | "${PY_UTF8[@]}" python3 "$SCRIPT_DIR/standup-publish.py" --telegram-markdown 2>/dev/null) || rc=$?
  fi
  if [ "$rc" -eq 0 ]; then
    [ -n "$err" ] && rm -f "$err"
    printf '%s' "$out"
    return 0
  fi
  if [ -n "$err" ]; then
    echo "  Could not render MarkdownV2: $(head -c 300 "$err" | iconv -f utf-8 -t utf-8 -c 2>/dev/null || head -c 300 "$err")" >&2
    rm -f "$err"
  else
    echo "  Could not render MarkdownV2 (no temp file for the reason)" >&2
  fi
  return 1
}

# The same report, trimmed to Telegram's limit but not escaped: what the
# plain-text retry should carry. Sending the untrimmed text there means a busy
# day plus any rejection produces no message at all — which is worse than the
# unformatted message this fallback exists to guarantee. Falls back to the
# original if even this cannot run.
telegram_plain() {
  local out
  if out=$(printf '%s' "$1" | "${PY_UTF8[@]}" python3 "$SCRIPT_DIR/standup-publish.py" --telegram-plain 2>/dev/null) \
     && [ -n "$out" ]; then
    printf '%s' "$out"
    return 0
  fi
  # The renderer is the thing that failed, so the last resort cannot use it.
  # A blunt cut in the shell, well inside the 4096 limit — this is the path
  # where the alternative is no message at all, and a truncated standup beats
  # silence. 3800 leaves room for the notice and for any character Telegram
  # counts as two.
  if [ "${#1}" -gt 3800 ]; then
    printf '%s\n\n… truncated; the renderer is not working.' "${1:0:3800}"
  else
    printf '%s' "$1"
  fi
}

send_telegram() {
  local message="$1"
  local resp escaped
  resp=""
  # Only attempt MarkdownV2 when there is something escaped to send. Sending the
  # raw text with parse_mode=MarkdownV2 is a request that cannot succeed — an
  # unescaped "." is a syntax error there — so it would buy a guaranteed
  # rejection and a line of noise before the retry below does the real work.
  if escaped=$(telegram_markdown "$message") && [ -n "$escaped" ]; then
    resp=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "text=${escaped}" \
      --data-urlencode "parse_mode=MarkdownV2") || resp=""
    if echo "$resp" | grep -q '"ok":true'; then
      return 0
    fi
  fi
  # Retry as plain text so the message still arrives, and say why rather than
  # hiding it. The retry sends the UNESCAPED text: resending the escaped payload
  # without a parse_mode would display \#alpha and dart\_defines, which is a
  # worse failure than the one being recovered from.
  #
  # Only report a rejected send when one was actually attempted — when the
  # renderer failed, telegram_markdown has already said so, and blaming Telegram
  # here would send the reader looking in the wrong place.
  [ -n "$resp" ] && echo "  Telegram MarkdownV2 send failed: $resp"
  resp=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$(telegram_plain "$message")") || resp=""
  # Return the truth. The final echo used to be the last command, so this
  # function returned 0 after both attempts had failed — and --test then printed
  # "Test message sent" having sent nothing at all, which is the one thing that
  # flag exists to tell you.
  if echo "$resp" | grep -q '"ok":true'; then
    return 0
  fi
  echo "  Plain-text send also failed: $resp"
  return 1
}

# ---- Test mode ----
if [ "${1:-}" = "--test" ]; then
  if send_telegram "✅ Daily standup bot is working. $(date '+%Y-%m-%d %H:%M')"; then
    echo "Test message sent."
    exit 0
  fi
  echo "Test message NOT sent — see the error above."
  exit 1
fi

# ---- Config ----
TODAY=$(date '+%Y-%m-%d')
STANDUP_BIN="$REPO_DIR/standup.rb"

# A missing report config is a leak, not an inconvenience, which is why this
# refuses to run rather than carrying on with a default.
#
# standup.yml is gitignored, so a fresh clone has none, and standup.rb answers a
# missing config with an empty one rather than an error. The report would then
# have no repo_name_mapping and no exclude_repos — and this pipeline publishes
# to X and wip.co. Every private repository under the projects root would be
# named, under its own directory name, in public.
#
# Lookup matches standup.rb without --config: ~/.standup.yml, then the clone's
# standup.yml. STANDUP_CONFIG in the environment still wins and is not searched
# past — an explicit path that is missing stays an error.
#
# Checked here and not earlier on purpose: --test and --check must still work on
# a fresh clone, because proving the bot works is the first thing anyone does.
# Not just non-empty: a file of blank lines, or one holding nothing but "---",
# is a zero-byte config as far as the report is concerned, and the whole point
# of this guard is what an empty config publishes.
_CONFIG_FROM_ENV=0
[ -n "${STANDUP_CONFIG+set}" ] && _CONFIG_FROM_ENV=1
if ! resolve_standup_config; then
  if [ "$_CONFIG_FROM_ENV" -eq 1 ]; then
    echo "ERROR: no usable report config at $STANDUP_CONFIG"
    echo "Copy standup.yml.example to that path and edit it, or set STANDUP_CONFIG to a usable file."
  else
    echo "ERROR: no usable report config"
    echo "Looked in: $HOME/.standup.yml and $REPO_DIR/standup.yml"
    echo "Copy standup.yml.example to either path and edit it, or set STANDUP_CONFIG."
  fi
  # -s, not -f: an empty file parses to an empty config, which is exactly the
  # publish-everything case this guard exists to stop.
  echo "Refusing to run: without it, every repository would be published by directory name."
  exit 1
fi
unset _CONFIG_FROM_ENV

# share_header / share_footer from standup.yml. {date} → TODAY. Footer is plain
# text only — the blank line before it is added when appending, not in the file.
# Shellwords so a header with apostrophes or spaces cannot break `eval`.
# RUBYOPT: cron/tests may run with LC_ALL=C; the default header holds emoji and
# an ASCII default external encoding refuses the -e script before it can run.
# Defaults + || true: a failed eval must not leave SHARE_* unset under set -u
# (cron would abort before Telegram ever sees the report).
SHARE_HEADER="📋 Daily Standup — $TODAY"
SHARE_FOOTER=""
eval "$(RUBYOPT="-Eutf-8:utf-8" ruby -ryaml -rshellwords -e '
path, date = ARGV[0], ARGV[1]
raw = File.read(path)
cfg = YAML.safe_load(raw, permitted_classes: [], permitted_symbols: [], aliases: true) || {}
cfg = {} unless cfg.is_a?(Hash)
header = (cfg["share_header"] || "📋 Daily Standup — {date}").to_s
footer = (cfg["share_footer"] || "").to_s.strip
header = header.gsub("{date}", date)
puts "SHARE_HEADER=#{Shellwords.escape(header)}"
puts "SHARE_FOOTER=#{Shellwords.escape(footer)}"
' "$STANDUP_CONFIG" "$TODAY")" || true

resolve_formatter "$STANDUP_CONFIG"

# Build standup args
# An array, not a string: word splitting would turn a config path containing a
# space into two arguments and the run would fail on a path that is perfectly
# legal.
STANDUP_ARGS=(--config "$STANDUP_CONFIG")
if [ "${1:-}" = "--today" ]; then
  STANDUP_ARGS+=(--today)
  DATE_LABEL="today"
else
  DATE_LABEL="yesterday"
fi

# ---- Run standup ----
echo "[$TODAY] Running daily standup ($DATE_LABEL)..."
RAW_STANDUP=$(ruby "$STANDUP_BIN" "${STANDUP_ARGS[@]}" 2>&1) || {
  echo "  Standup script failed: $RAW_STANDUP"
  send_telegram "⚠️ Daily standup failed: ${RAW_STANDUP:0:200}"
  exit 1
}

if [ -z "$RAW_STANDUP" ] || echo "$RAW_STANDUP" | grep -q "^No activity found"; then
  send_telegram "$SHARE_HEADER

No commits $DATE_LABEL. Rest day? 🏖️"
  echo "[$TODAY] No activity — message sent."
  exit 0
fi

# ---- Format with the configured LLM CLI ----
echo "  Formatting with $STANDUP_FORMATTER ($FORMATTER_BIN)..."
PROMPT="You are formatting a daily developer standup for Telegram.

Date: $TODAY (this is $DATE_LABEL's activity)

Here is the raw standup output (each section is a project hashtag, bullets are commits):

$RAW_STANDUP

Format this as a concise, scannable Telegram message:
- Do not write a title line. Start directly with the first project hashtag.
- Group by project hashtag (bold the hashtag)
- Summarize related commits into one bullet where possible (don't repeat noise like 'chore: bump version')
- Use plain language, not commit-speak
- Add a brief one-line summary at the end with total project count
- Keep it short — this is a standup, not a changelog
- Bold ONLY the project hashtag on its own line, with single asterisks (*#project*), never double. Use no other markup anywhere — no italics, no inline bold, no code spans. Every other character is escaped before sending, so a stray marker is published literally to X and wip.co rather than rendered.
- Output ONLY the formatted message, nothing else"

ANALYSIS=$(run_formatter "$PROMPT") || ANALYSIS=""

# Fallback: if the formatter failed, send raw standup (title is prepended after strip)
if [ -z "$ANALYSIS" ]; then
  echo "  $STANDUP_FORMATTER formatting failed, using raw fallback"
  ANALYSIS="$RAW_STANDUP"
fi

# The hashtags are data, not prose: wip.co attaches a todo to a project BY the hashtag, and the X
# text swaps it for the project name and website. The prompt above asks an LLM to keep one
# character, and on 2026-09-09 it did not — it bolded the names and dropped every "#". The X post
# went out with no links and the wip.co todo would have attached to nothing, and neither failure
# is visible downstream, because a dropped "#" reads exactly like a project that never had one.
# So restore them from what standup.rb actually emitted. If a whole project is missing, the
# formatter lost work: publish the raw report, which is uglier and correct.
REPAIRED=$(printf '%s' "$ANALYSIS" | RAW_STANDUP="$RAW_STANDUP" \
  python3 "$SCRIPT_DIR/standup-publish.py" --repair-headers) && ANALYSIS="$REPAIRED" || {
  echo "  Formatter lost a project; publishing the raw standup instead"
  ANALYSIS="$RAW_STANDUP"
}

# Security lines never leave this machine, on any destination — not X, not
# wip.co, not the Telegram message you approve. Placed AFTER every fallback
# above resolves, because the two fallback branches replace ANALYSIS wholesale:
# one with the Claude failure text, one with the raw report. The raw report is
# the worse of the two to leak, since it carries the commit subjects
# unsummarised, and a filter that sits inside the repair pipe never sees it.
#
# Fail closed. A filter that cannot run must stop the standup, not publish what
# it failed to read. Every other fallback in this script degrades to sending
# something; this one degrades to sending nothing, and that asymmetry is the
# whole point of it.
# set -e would abort here on exit 2 before the case ever ran, and an aborted
# script sends nothing — the right outcome for the wrong reason, and silent.
# Refusing to send is not enough on its own. A pending state for today may
# already exist — from an earlier run, or from before this filter existed — and
# its buttons carry the same date id, so a press on that older message would
# still publish the text we are refusing to send now. Disarm it first.
disarm_today() {
  rm -f "${STANDUP_STATE_DIR:-$HOME/.local/state/standup}/pending-${TODAY}.json"
}

set +e
STRIPPED=$(printf '%s' "$ANALYSIS" | python3 "$SCRIPT_DIR/standup-publish.py" --strip-private)
STRIP_STATUS=$?
set -e
case $STRIP_STATUS in
  0) ANALYSIS="$STRIPPED" ;;
  2)
    echo "[$TODAY] Every project was dropped by the private-line filter; nothing sent."
    disarm_today
    send_telegram "🔒 $SHARE_HEADER

Niente da pubblicare: ogni riga era di sicurezza. Nessun report inviato."
    exit 0
    ;;
  3)
    # The filter ran and refused: the report has no project blocks at all.
    # Often the formatter failed AND repo_name_mapping uses display titles
    # instead of #hashtags — the raw fallback then has nothing the pipeline
    # can treat as a project header.
    echo "[$TODAY] The report has no project blocks; nothing sent."
    if ! printf '%s' "$RAW_STANDUP" | grep -qE '^[[:space:]]*#'; then
      echo "  Hint: repo_name_mapping values must be wip.co hashtags (e.g. #myfoodmate), not display names."
    fi
    disarm_today
    if ! printf '%s' "$RAW_STANDUP" | grep -qE '^[[:space:]]*#'; then
      send_telegram "⚠️ $SHARE_HEADER

Il report non contiene nessun progetto: in standup.yml ogni mapping pubblicato deve essere un hashtag wip.co (es. #myfoodmate), non un titolo. Non ho inviato niente."
    else
      send_telegram "⚠️ $SHARE_HEADER

Il report non contiene nessun progetto: la formattazione è fallita a monte. Non ho inviato niente. Controlla il log."
    fi
    exit 1
    ;;
  *)
    echo "[$TODAY] The private-line filter itself failed; nothing sent."
    disarm_today
    send_telegram "⚠️ $SHARE_HEADER

Il filtro delle righe di sicurezza non ha funzionato. Non ho inviato niente, per non pubblicare un report non filtrato. Controlla il log."
    exit 1
    ;;
esac

# Configured title is the only source: drop whatever title the formatter wrote
# (if the first line is not a project hashtag), then prepend SHARE_HEADER.
# Same placement as the footer — after strip-private, so a share_header that is
# itself a lone #hashtag is never mistaken for an empty project and dropped.
ANALYSIS=$(printf '%s' "$ANALYSIS" | python3 "$SCRIPT_DIR/standup-publish.py" --drop-title) || ANALYSIS=""
ANALYSIS="${SHARE_HEADER}

${ANALYSIS}"

# share_footer is config text only: trim already done when loading, and the
# blank line before it is added here so the yml never needs a leading newline.
if [ -n "$SHARE_FOOTER" ]; then
  ANALYSIS="${ANALYSIS}

${SHARE_FOOTER}"
fi

# ---- Send to Telegram, with the button that publishes it ----
#
# The button does not publish. It records that you pressed it; standup-publish.py
# runs from cron, sees the press, and posts to X and wip.co. Nothing reaches
# either of them without that press.
echo "  Sending to Telegram..."

STATE_DIR="${STANDUP_STATE_DIR:-$HOME/.local/state/standup}"
mkdir -p "$STATE_DIR"
# A kill between writing the temporary state and renaming it leaves a .partial
# behind that nothing else ever removes. The poller ignores them; they just
# accumulate. Swept here rather than at write time, because the write is
# precisely when the process may not survive to clean up after itself.
rm -f "$STATE_DIR"/pending-*.json.partial
PENDING_ID="$TODAY"
# One button per destination, so a day already published to one place can still
# be sent to the other. Each destination is recorded on its own, so pressing the
# same button twice is a no-op rather than a duplicate.
# LinkedIn is armed only when buffer.env carries BOTH keys. A file with one of
# them is a half-configured destination, which is worse than an absent one: it
# offers a button that fails hours later. The publisher applies the same rule,
# so the keyboard and the publish path cannot disagree.
ARMED='["x","wip"]'
LINKEDIN_ARMED=0
if linkedin_armed; then
  LINKEDIN_ARMED=1
  ARMED='["x","wip","linkedin"]'
fi

# "Tutti" carries `all` and replaces "Entrambi" on new keyboards. `both` is not
# offered any more but is still honoured on receipt, so a press on a message
# sent before LinkedIn existed still does what its label promised.
ROW1="[{\"text\":\"🐦 X\",\"callback_data\":\"publish:${PENDING_ID}:x\"},{\"text\":\"📋 wip.co\",\"callback_data\":\"publish:${PENDING_ID}:wip\"}]"
if [ "$LINKEDIN_ARMED" = 1 ]; then
  KEYBOARD="{\"inline_keyboard\":[${ROW1},[{\"text\":\"💼 LinkedIn\",\"callback_data\":\"publish:${PENDING_ID}:linkedin\"}],[{\"text\":\"🚀 Tutti\",\"callback_data\":\"publish:${PENDING_ID}:all\"}]]}"
else
  KEYBOARD="{\"inline_keyboard\":[${ROW1},[{\"text\":\"🚀 Entrambi\",\"callback_data\":\"publish:${PENDING_ID}:both\"}]]}"
fi

# The state file keeps ANALYSIS unescaped — that text is what X and wip.co
# receive when the button is pressed, and backslashes are not prose. Only what
# Telegram sees is escaped.
RESP=""
if ESCAPED=$(telegram_markdown "$ANALYSIS") && [ -n "$ESCAPED" ]; then
  RESP=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${ESCAPED}" \
    --data-urlencode "parse_mode=MarkdownV2" \
    --data-urlencode "reply_markup=${KEYBOARD}") || RESP=""
else
  echo "  Sending the report unformatted"
fi

if ! echo "$RESP" | grep -q '"ok":true'; then
  # Retry as plain text so the report and its buttons still arrive. The
  # UNESCAPED text, not the payload above: resending that without a parse_mode
  # would show \#alpha and dart\_defines to the reader.
  # Only when a send was actually attempted — see send_telegram above.
  [ -n "$RESP" ] && echo "  Telegram MarkdownV2 send failed: $RESP"
  RESP=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=$(telegram_plain "$ANALYSIS")" \
    --data-urlencode "reply_markup=${KEYBOARD}") || RESP=""
fi

if echo "$RESP" | grep -q '"ok":true'; then
  # Parse the response and write the state file in one guarded step.
  #
  # These were two unguarded command substitutions, and the script runs under
  # set -e. A response Telegram accepted but shaped unexpectedly — "ok":true
  # with no result.message_id — made the extraction raise and killed the script
  # right here, AFTER the report was already in the chat. The reader got a
  # standup with two buttons that could never do anything, because no pending
  # state was ever written, and the log said nothing at all: no "sent" line, no
  # error, just a run that stopped.
  #
  # Together in one call because the two have to agree: a message id without a
  # state file is a dead button, and a state file without the id it belongs to
  # cannot be replied to. Text through the environment, not argv, because it is
  # long and multi-line and that is where quoting breaks.
  if ! MESSAGE_ID=$(RESP="$RESP" ANALYSIS="$ANALYSIS" ARMED="$ARMED" "${PY_UTF8[@]}" python3 - \
      "$STATE_DIR/pending-${PENDING_ID}.json" "$PENDING_ID" <<'PYEOF'
import json, os, sys

# encoding="utf-8" explicitly, and PY_UTF8 on the interpreter above. The report
# is Italian and Romanian and starts with an emoji; cron has no locale, so
# writing it through the platform default raises UnicodeEncodeError — after
# Telegram has already accepted the message. That is the failure this whole
# block exists to prevent, and it would have arrived through the fix for it.
#
# Written to a temporary file and renamed, so the poller can never read a
# half-written state: os.replace is atomic within a filesystem.
path, pending_id = sys.argv[1:3]
result = json.loads(os.environ["RESP"])["result"]
state = {"id": pending_id,
         "message_id": int(result["message_id"]),
         "chat_id": int(result["chat"]["id"]),
         "text": os.environ["ANALYSIS"],
         # What was armed THIS morning. Completion is judged against this list,
         # not against whatever config exists when the button is finally
         # pressed — a day whose keyboard never offered LinkedIn must not wait
         # for a LinkedIn post that can never happen.
         "armed": json.loads(os.environ["ARMED"]),
         "posted_x": False, "posted_wip": False}
if "linkedin" in state["armed"]:
    state["posted_linkedin"] = False

tmp = path + ".partial"
try:
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
except BaseException:
    # A disk that fills part-way through would otherwise leave .partial behind
    # for good. The poller ignores it, but nobody ever cleans it up either.
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise
print(result["message_id"])
PYEOF
  ); then
    echo "  The report was sent, but its state could not be recorded."
    echo "  The publish buttons on that message will do nothing. Response was: ${RESP:0:300}"
    echo "[$TODAY] Daily standup sent, but WITHOUT a working publish button."
    exit 1
  fi

  # Show the X form too, as a reply. The message above is the wip.co form: the
  # hashtags are what attach a todo to a project there. On X they are just a row
  # of words, so standup-publish.py swaps them for the project name and site —
  # and until now that swap only happened after the button was pressed, which
  # meant approving a text nobody had seen. Sent separately on purpose: the
  # state file holds the wip.co text, and that is what wip.co must receive.
  X_PREVIEW=$(python3 "$SCRIPT_DIR/standup-publish.py" --preview "$PENDING_ID" 2>/dev/null || true)
  if [ -n "$X_PREVIEW" ]; then
    PREVIEW_RESP=$(curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
      --data-urlencode "reply_to_message_id=${MESSAGE_ID}" \
      --data-urlencode "disable_web_page_preview=true" \
      --data-urlencode "text=🐦 Su X esce così:

${X_PREVIEW}") || PREVIEW_RESP=""
    # The body, not just curl's exit code. Telegram rejects with HTTP 200 and
    # {"ok":false}, so curl succeeds and the rejection would go unmentioned —
    # the preview is how you approve what goes to X, and its silent absence is
    # the one failure nobody would think to look for.
    echo "$PREVIEW_RESP" | grep -q '"ok":true' ||
      echo "  The X preview could not be sent; the button still works: ${PREVIEW_RESP:0:200}"
  else
    echo "  The X preview could not be sent; the button still works"
  fi
  echo "[$TODAY] Daily standup sent, waiting for the publish button."
else
  echo "  Plain-text send also failed: $RESP"
  echo "[$TODAY] Daily standup NOT sent."
  exit 1
fi
