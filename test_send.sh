#!/usr/bin/env bash
# Regression test: run it with `bash test_send.sh`.
#
# The renderer has its own tests. This covers the wiring, which is where the
# 2026-09-09 failure lived: the report was rendered correctly and then sent with
# the wrong parse_mode. Every assertion reads what the script actually handed to
# curl, through a stub, so nothing reaches Telegram.
#
# Everything runs from a COPY of the checkout. An earlier version replaced
# bin/standup-publish.py in place to test the broken-renderer path, which left
# the repository damaged if it was interrupted between the two moves.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
failures=()

WORK="$TMP/work"
mkdir -p "$WORK/bin" "$TMP/stub" "$TMP/fakehome"
cp "$ROOT/bin/daily-standup.sh" "$ROOT/bin/standup-publish.py" "$WORK/bin/"
cp "$ROOT/standup.rb" "$WORK/"
printf 'projects_root: %s\n' "$TMP/projects" > "$WORK/standup.yml"

# curl: records every argument, then answers as Telegram would. FAIL_FIRST makes
# the first call fail, which is how the plain-text retry gets exercised.
cat > "$TMP/stub/curl" <<'SH'
#!/usr/bin/env bash
printf '%s\n' "$@" >> "$CURL_LOG"
printf -- '--- end of call ---\n' >> "$CURL_LOG"
if [ -n "${FAIL_FIRST:-}" ] && [ ! -f "$CURL_LOG.first" ]; then
  : > "$CURL_LOG.first"
  echo '{"ok":false,"description":"Bad Request: stubbed failure"}'
  exit 0
fi
# The X preview is a reply, and the only call carrying reply_to_message_id.
if [ -n "${FAIL_LAST:-}" ] && printf '%s\n' "$@" | grep -q reply_to_message_id; then
  echo '{"ok":false,"description":"Bad Request: stubbed preview failure"}'
  exit 7
fi
# The same rejection the way Telegram actually sends one: HTTP 200 with a body
# saying no, which curl reports as complete success.
if [ -n "${REJECT_PREVIEW:-}" ] && printf '%s\n' "$@" | grep -q reply_to_message_id; then
  echo '{"ok":false,"description":"Bad Request: message to reply not found"}'
  exit 0
fi
# Accepted, but not the shape the script expects. This is what killed it.
if [ -n "${OK_BUT_ODD:-}" ]; then
  echo '{"ok":true,"result":{"chat":{"id":1}}}'
  exit 0
fi
echo '{"ok":true,"result":{"message_id":1,"chat":{"id":1}}}'
SH
chmod +x "$TMP/stub/curl"

# GNU timeout is what the morning run wraps the formatter with. env -i on macOS
# has none on PATH, so without a stub every formatter call fails instantly and
# the suite only ever exercises the raw fallback.
cat > "$TMP/stub/timeout" <<'SH'
#!/usr/bin/env bash
shift
exec "$@"
SH
chmod +x "$TMP/stub/timeout"

# Real ruby still handles -ryaml / -e for share_* loading. Captured before PATH
# is narrowed by env -i in run().
REAL_RUBY="$(command -v ruby)"

# As above, but with the locale and the Python UTF-8 variables removed — the
# environment cron actually provides. The script is supposed to supply those
# itself; nothing else here proves it does.
run_no_utf8() {
  local log="$1"; shift
  : > "$log"; rm -f "$log.first"
  # LC_ALL=C and PYTHONCOERCECLOCALE=0: the environment cron gives the script,
  # with CPython's own C-locale promotion turned off as well.
  #
  # Even so, this build reports getpreferredencoding()=utf-8, so removing the
  # script's encoding="utf-8" and PYTHONUTF8 does NOT make this test fail here.
  # Measured, both ways round. What it pins is that the state file round-trips
  # non-ASCII under the environment cron provides; on a build where the default
  # is not UTF-8 it would also catch their removal. Keeping them is explicitness
  # that costs nothing, not something this suite can prove locally.
  env -i HOME="$TMP/fakehome" PATH="$TMP/stub:/usr/bin:/bin" CURL_LOG="$log" \
      LC_ALL=C PYTHONCOERCECLOCALE=0 \
      TELEGRAM_BOT_TOKEN=not-a-token TELEGRAM_CHAT_ID=not-a-chat \
      bash "$WORK/bin/daily-standup.sh" "$@" 2>&1
}

# ${CLAUDE_BIN+...}, not ${CLAUDE_BIN:+...}: the colon form drops an empty
# value, so a test for "empty beats the profile" would never hand the child
# the empty value it is testing. That is how the first version of test 11
# failed against correct code.
run() { # run <log> <args...>
  local log="$1"; shift
  : > "$log"; rm -f "$log.first"
  # LC_ALL because env -i drops it: CPython coerces a C locale to UTF-8 on this
  # build, so the emoji in the title survives, but that is a build option and
  # not something a test should rely on.
  env -i HOME="$TMP/fakehome" PATH="$TMP/stub:/usr/bin:/bin" CURL_LOG="$log" \
      LC_ALL=C.UTF-8 PYTHONUTF8=1 PYTHONIOENCODING=utf-8 \
      ${FAIL_FIRST:+FAIL_FIRST=1} ${OK_BUT_ODD:+OK_BUT_ODD=1} ${FAIL_LAST:+FAIL_LAST=1} \
      ${REJECT_PREVIEW:+REJECT_PREVIEW=1} ${TMPDIR:+TMPDIR=$TMPDIR} \
      ${CLAUDE_BIN+CLAUDE_BIN=$CLAUDE_BIN} \
      TELEGRAM_BOT_TOKEN=not-a-token TELEGRAM_CHAT_ID=not-a-chat \
      bash "$WORK/bin/daily-standup.sh" "$@" 2>&1
}

# The text of the Nth curl call, so an assertion can say which request it means.
call() { awk -v n="$2" 'BEGIN{c=1} /^--- end of call ---$/{c++; next} c==n' "$1"; }

# --- 1. --test goes out as MarkdownV2 -------------------------------------
out=$(run "$TMP/test.log" --test)
call "$TMP/test.log" 1 | grep -q 'parse_mode=MarkdownV2' ||
  failures+=("--test did not send MarkdownV2")
call "$TMP/test.log" 1 | grep -qx 'parse_mode=Markdown' &&
  failures+=("--test still sends legacy Markdown")
echo "$out" | grep -q 'Test message sent' ||
  failures+=("--test did not report success through the stub")

# --- 2. The whole report path, with the underscore that caused all this ----
# Stub ruby and claude so the script reaches its real send with known text.
# Real ruby still handles -ryaml / -e: daily-standup.sh loads share_* that way,
# and a stub that answers every ruby call with a standup body makes `eval`
# try to run "•" as a command.
cat > "$TMP/stub/ruby" <<SH
#!/usr/bin/env bash
case " \$* " in
  *" -ryaml "*|*" -e "*) exec "$REAL_RUBY" "\$@" ;;
esac
printf '#alpha\n• Build config: dart_defines from production.env\n'
SH
cat > "$TMP/stub/claude" <<'SH'
#!/usr/bin/env bash
cat > /dev/null
printf '📋 *Daily Standup*\n\n*#alpha*\n• Build config: dart_defines from production.env\n\n1 project.\n'
SH
chmod +x "$TMP/stub/ruby" "$TMP/stub/claude"

out=$(run "$TMP/report.log")
first=$(call "$TMP/report.log" 1)
echo "$first" | grep -q 'parse_mode=MarkdownV2' ||
  failures+=("the report was not sent as MarkdownV2")
echo "$first" | grep -q 'dart\\_defines' ||
  failures+=("the underscore reached Telegram unescaped")
echo "$first" | grep -q 'inline_keyboard' ||
  failures+=("the report lost its publish buttons")

# The state file must hold the UNESCAPED text: that is what X and wip.co get.
state=$(cat "$TMP/fakehome/.local/state/standup/pending-"*.json 2>/dev/null)
[ -n "$state" ] || failures+=("no pending state was written")
case "$state" in
  *'dart\\_defines'*) failures+=("the state file holds escaped text; X would publish backslashes") ;;
  *dart_defines*) : ;;
  *) failures+=("the state file lost the commit subject") ;;
esac

# --- 3. The retry, when Telegram rejects the first request -----------------
export FAIL_FIRST=1
out=$(run "$TMP/retry.log")
unset FAIL_FIRST   # it would otherwise stub the next test's only call into failing
retry=$(call "$TMP/retry.log" 2)
[ -n "$retry" ] || failures+=("a rejected send was not retried")
echo "$retry" | grep -q 'parse_mode' &&
  failures+=("the retry still carried a parse_mode")
echo "$retry" | grep -q 'dart\\_defines' &&
  failures+=("the retry sent escaped text, which would display backslashes")
echo "$retry" | grep -q 'dart_defines' ||
  failures+=("the retry did not carry the report")

# --- 3b. send_telegram's own retry, which test 3 does not reach ------------
# Test 3 drives the report path. This drives the other send site, which carries
# --test, the rest-day message and the standup-failed alarm.
export FAIL_FIRST=1
out=$(run "$TMP/test-retry.log" --test)
unset FAIL_FIRST
[ -n "$(call "$TMP/test-retry.log" 2)" ] ||
  failures+=("a rejected --test was not retried")
call "$TMP/test-retry.log" 2 | grep -q 'parse_mode' &&
  failures+=("the --test retry still carried a parse_mode")
echo "$out" | grep -q 'Test message sent' ||
  failures+=("--test reported failure after a successful retry")

# --- 3c. A report over Telegram's limit ------------------------------------
# The trim has to reach the wire, and the plain-text retry has to be trimmed
# too — otherwise a busy day plus any rejection means no message at all.
cat > "$TMP/stub/claude" <<'SH'
#!/usr/bin/env bash
cat > /dev/null
printf '📋 *Daily Standup*\n\n*#alpha*\n'
for i in $(seq 1 400); do printf '• a long commit subject number %s, with words in it.\n' "$i"; done
printf '\n1 project.\n'
SH
chmod +x "$TMP/stub/claude"

export FAIL_FIRST=1
out=$(run "$TMP/big.log")
unset FAIL_FIRST
big_first=$(call "$TMP/big.log" 1)
big_retry=$(call "$TMP/big.log" 2)
echo "$big_first" | grep -q 'trimmed to fit Telegram' ||
  failures+=("the oversized report was sent untrimmed")
[ -n "$big_retry" ] || failures+=("the oversized report was not retried after rejection")
echo "$big_retry" | grep -q 'trimmed to fit Telegram' ||
  failures+=("the plain-text retry sent the untrimmed report, which Telegram also rejects")
# Without the keyboard there is nothing to press, so the day cannot be published.
echo "$big_retry" | grep -q 'inline_keyboard' ||
  failures+=("the retry lost the publish buttons")

# Restore the ordinary formatter for anything added after this point.
cat > "$TMP/stub/claude" <<'SH'
#!/usr/bin/env bash
cat > /dev/null
printf '📋 *Daily Standup*\n\n*#alpha*\n• Build config: dart_defines from production.env\n\n1 project.\n'
SH
chmod +x "$TMP/stub/claude"

# --- 3d. The retry keeps its buttons on an ordinary report -----------------
export FAIL_FIRST=1
out=$(run "$TMP/retry2.log")
unset FAIL_FIRST
call "$TMP/retry2.log" 2 | grep -q 'inline_keyboard' ||
  failures+=("the ordinary retry lost the publish buttons")

# --- 4. A broken renderer must not stop the message -----------------------
cp "$WORK/bin/standup-publish.py" "$TMP/publisher.good"
printf 'not python\n' > "$WORK/bin/standup-publish.py"
out=$(run "$TMP/broken.log" --test)
cp "$TMP/publisher.good" "$WORK/bin/standup-publish.py"   # a later test must not inherit this
call "$TMP/broken.log" 1 | grep -q 'parse_mode' &&
  failures+=("a doomed MarkdownV2 request was sent with unescaped text")
call "$TMP/broken.log" 1 | grep -q 'text=' ||
  failures+=("a broken renderer stopped the message going out at all")
echo "$out" | grep -q 'Test message sent' ||
  failures+=("a broken renderer made --test fail instead of falling back")
echo "$out" | grep -q 'Could not render MarkdownV2' ||
  failures+=("a broken renderer failed silently")

cp "$TMP/stub/claude" "$TMP/claude.good"

# --- 4b. A broken publisher on the REPORT path now stops the report ---------
# This asserted the opposite until the private-line filter landed, and the
# reversal is the point. Every other fallback in the sender degrades to sending
# something; this one degrades to sending nothing. A publisher that cannot run
# cannot strip the security lines either, and a report nobody filtered must not
# reach X, wip.co, or the message you approve.
cp "$WORK/bin/standup-publish.py" "$TMP/publisher.good2"
printf 'not python\n' > "$WORK/bin/standup-publish.py"
out=$(run "$TMP/broken-report.log") && \
  failures+=("a broken publisher let the report path exit 0")
cp "$TMP/publisher.good2" "$WORK/bin/standup-publish.py"
first=$(call "$TMP/broken-report.log" 1)
echo "$first" | grep -q 'dart_defines' &&
  failures+=("an unfiltered report went out when the filter could not run")
echo "$first" | grep -q 'inline_keyboard' &&
  failures+=("a report nobody filtered was sent with publish buttons")
echo "$out" | grep -q 'private-line filter itself failed' ||
  failures+=("the report path did not say why it sent nothing")

# There is deliberately no end-to-end case for exit 3 ("no project blocks").
# The repair step runs first, and when the formatter returns prose the repair
# fails and the raw report — which does carry headers — replaces it. So the
# pipeline cannot reach that branch, and a test that pretended otherwise would
# be asserting a fiction. The CLI path is covered in --selftest.

# --- 4b-iii. LinkedIn arms the keyboard, and only when fully configured -----
# fakehome is the config dir under env -i, so buffer.env lands there.
BUF="$TMP/fakehome/.config/standup/buffer.env"
mkdir -p "$(dirname "$BUF")"

out=$(run "$TMP/no-buffer.log")
kb=$(call "$TMP/no-buffer.log" 1 | grep -o 'inline_keyboard.*')
echo "$kb" | grep -q ':both' ||
  failures+=("without buffer.env the keyboard lost its Entrambi button")
echo "$kb" | grep -q 'LinkedIn' &&
  failures+=("LinkedIn was offered with no buffer.env at all")

# Half-configured must not arm: a button that fails hours later is worse than
# one that was never offered.
printf 'BUFFER_API_KEY=k\n' > "$BUF"
out=$(run "$TMP/half-buffer.log")
call "$TMP/half-buffer.log" 1 | grep -q 'LinkedIn' &&
  failures+=("a buffer.env missing the channel id still armed LinkedIn")

# An empty value in quotes is empty once the quotes come off, which is what the
# publisher does. The keyboard must not claim otherwise.
printf 'BUFFER_API_KEY="k"\nBUFFER_LINKEDIN_CHANNEL=""\n' > "$BUF"
out=$(run "$TMP/quoted-empty.log")
call "$TMP/quoted-empty.log" 1 | grep -q 'LinkedIn' &&
  failures+=("an empty quoted value armed LinkedIn; the publisher would disagree")

printf 'BUFFER_API_KEY=k\nBUFFER_LINKEDIN_CHANNEL=c\n' > "$BUF"
out=$(run "$TMP/full-buffer.log")
kb=$(call "$TMP/full-buffer.log" 1 | grep -o 'inline_keyboard.*')
echo "$kb" | grep -q 'LinkedIn' ||
  failures+=("a complete buffer.env did not arm the LinkedIn button")
echo "$kb" | grep -q ':all' ||
  failures+=("the armed keyboard has no Tutti button")
echo "$kb" | grep -q ':both' &&
  failures+=("Entrambi is still offered alongside Tutti")
state=$(cat "$TMP/fakehome/.local/state/standup/pending-"*.json 2>/dev/null)
echo "$state" | grep -q '"linkedin"' ||
  failures+=("the pending state did not record linkedin as armed")
echo "$state" | grep -q '"posted_linkedin": false' ||
  failures+=("the pending state has no posted_linkedin flag")
rm -f "$BUF" "$TMP/fakehome/.local/state/standup/pending-"*.json

# --- 4b-iv. --check answers the question the README sends people to it for --
# The function it calls used to be defined below the --check block, which exits
# first, so bash never reached the definition and --check always said "not
# armed" — on every machine, armed or not. test_send.sh passed throughout,
# because the send path runs after the definition.
printf 'BUFFER_API_KEY=k\nBUFFER_LINKEDIN_CHANNEL=c\n' > "$BUF"
out=$(run "$TMP/check-armed.log" --check)
echo "$out" | grep -q 'command not found' &&
  failures+=("--check called a function that was not defined yet")
echo "$out" | grep -qE 'LinkedIn: +armed' ||
  failures+=("--check did not report LinkedIn as armed with a complete buffer.env")
rm -f "$BUF"
out=$(run "$TMP/check-bare.log" --check)
echo "$out" | grep -qE 'LinkedIn: +not armed' ||
  failures+=("--check did not report LinkedIn as unarmed without buffer.env")

# --- 4c. A report that is nothing but security lines ------------------------
# Not a rest day, and not a title with a trailer and no content: both of those
# would be a lie about what happened. Say so, send no report, arm no button.
cp "$TMP/stub/claude" "$TMP/claude.good"
cat > "$TMP/stub/claude" <<'SH'
#!/usr/bin/env bash
cat > /dev/null
printf '\xf0\x9f\x93\x8b *Daily Standup*\n\n*#alpha*\n\xe2\x80\xa2 Security: remove the demo account\n\n1 project.\n'
SH
chmod +x "$TMP/stub/claude"
out=$(run "$TMP/all-dropped.log") ||
  failures+=("an all-security report should exit 0, not fail")
cp "$TMP/claude.good" "$TMP/stub/claude"
first=$(call "$TMP/all-dropped.log" 1)
echo "$first" | grep -q 'inline_keyboard' &&
  failures+=("an empty report was sent with publish buttons")
echo "$first" | grep -q 'demo account' &&
  failures+=("the security line reached Telegram")
echo "$out" | grep -q 'dropped by the private-line filter' ||
  failures+=("nothing said why the report was empty")

# --- 5. Accepted by Telegram, but not the shape we expected ----------------
# The report is already in the chat at this point. The script used to die here
# on an unguarded substitution: no pending state, two buttons that could never
# work, and a log with neither a "sent" line nor an error in it.
# The state directory is emptied first. Earlier successful runs leave their own
# pending files here, and a check that globs the directory would have matched
# those and passed no matter what this run did.
rm -f "$TMP/fakehome"/.local/state/standup/pending-*.json
export OK_BUT_ODD=1
out=$(run "$TMP/odd.log")
rc=$?
unset OK_BUT_ODD
[ "$rc" -ne 0 ] ||
  failures+=("a report with no usable state reported success")
echo "$out" | grep -q 'WITHOUT a working publish button' ||
  failures+=("a dead publish button was not reported")
# Nothing at all may be left for the poller: not a half-written file, and not
# the .partial the atomic write uses on its way there.
compgen -G "$TMP/fakehome/.local/state/standup/pending-*" > /dev/null &&
  failures+=("a pending state was left behind for a report whose button is dead")

# --- 6. A cron-line override beats a profile that exports the same name ----
# The README documents setting these on the cron line. The profile is sourced
# after they are already in the environment, so without the restore it wins and
# the override looks ignored.
#
# Asserted on what reached curl, not on --check: --check prints "set" for a
# credential rather than its value, so it cannot tell which one won — which is
# exactly how the first version of this test passed with the restore removed.
# One profile at a time. The script prefers .zshrc when it exists, so writing
# both meant the .bashrc branch — a different block, with its own set +eu —
# was never taken.
for rc_file in .zshrc .bashrc; do
  rm -f "$TMP/fakehome/.zshrc" "$TMP/fakehome/.bashrc"
  printf 'export CLAUDE_BIN=/profile/wins/claude\nexport TELEGRAM_CHAT_ID=profile-chat\n' \
    > "$TMP/fakehome/$rc_file"
  rm -f "$TMP/fakehome"/.local/state/standup/pending-*.json
  out=$(CLAUDE_BIN=/cron/line/claude run "$TMP/override-$rc_file.log")

  grep -q 'chat_id=not-a-chat' "$TMP/override-$rc_file.log" ||
    failures+=("$rc_file redirected where the standup is posted")
  grep -q 'chat_id=profile-chat' "$TMP/override-$rc_file.log" &&
    failures+=("$rc_file's chat id was used for the report")

  out=$(CLAUDE_BIN=/cron/line/claude run "$TMP/override-$rc_file-check.log" --check)
  echo "$out" | grep -q 'claude:.*/cron/line/claude' ||
    failures+=("$rc_file overrode the CLAUDE_BIN set on the cron line")
done
rm -f "$TMP/fakehome/.zshrc" "$TMP/fakehome/.bashrc"

# --- 7. The X preview failing must not cost the run ------------------------
# It is the last thing the script does, and a bare curl under set -e would
# abort after the report, the state file and the buttons were all in place.
rm -f "$TMP/fakehome"/.local/state/standup/pending-*.json
export FAIL_LAST=1
out=$(run "$TMP/preview.log")
rc=$?
unset FAIL_LAST
[ "$rc" -eq 0 ] ||
  failures+=("a failed X preview aborted a run that had otherwise succeeded")
echo "$out" | grep -q 'waiting for the publish button' ||
  failures+=("the run did not report success after a failed X preview")
compgen -G "$TMP/fakehome/.local/state/standup/pending-*.json" > /dev/null ||
  failures+=("the pending state was lost when the X preview failed")

# --- 8. No temporary file for the renderer's diagnostic --------------------
# mktemp fails when TMPDIR points nowhere. That must cost the diagnostic and
# nothing else.
#
# 8a: a WORKING renderer with no temp file must still produce MarkdownV2. An
# earlier version fell back to /dev/null here, and the success path then ran
# `rm -f /dev/null`, which fails for an ordinary user — so the function
# returned non-zero and the report quietly went out unformatted. Delivered,
# and wrong, which is the hardest kind of failure to notice.
out=$(TMPDIR=/nonexistent-tmpdir run "$TMP/notmp-ok.log" --test)
call "$TMP/notmp-ok.log" 1 | grep -q 'parse_mode=MarkdownV2' ||
  failures+=("a broken TMPDIR silently downgraded the report to plain text")
# And it must do it quietly. An earlier version fell back to /dev/null, whose
# cleanup then failed for an ordinary user and put "cannot remove" in the cron
# log every single morning — harmless, and exactly the kind of daily noise that
# trains everyone to stop reading that log.
echo "$out" | grep -qi 'cannot remove' &&
  failures+=("the run put a spurious rm failure in the log")

# 8b: a BROKEN renderer with no temp file must still deliver, and still say so.
cp "$WORK/bin/standup-publish.py" "$TMP/publisher.good3"
printf 'not python\n' > "$WORK/bin/standup-publish.py"
out=$(TMPDIR=/nonexistent-tmpdir run "$TMP/notmp.log" --test)
rc=$?
cp "$TMP/publisher.good3" "$WORK/bin/standup-publish.py"
[ "$rc" -eq 0 ] ||
  failures+=("a missing TMPDIR turned a deliverable message into a failure")
call "$TMP/notmp.log" 1 | grep -q 'text=' ||
  failures+=("no message was sent when mktemp could not provide a scratch file")
echo "$out" | grep -q 'Could not render MarkdownV2' ||
  failures+=("the renderer failure went unreported without a temp file")

# --- 9. A preview Telegram rejects with HTTP 200 ---------------------------
rm -f "$TMP/fakehome"/.local/state/standup/pending-*.json
export REJECT_PREVIEW=1
out=$(run "$TMP/reject.log")
unset REJECT_PREVIEW
echo "$out" | grep -q 'The X preview could not be sent' ||
  failures+=("a preview Telegram refused was reported as sent")
echo "$out" | grep -q 'waiting for the publish button' ||
  failures+=("a refused preview cost the run its success")

# --- 10. The state file, written with no locale at all ---------------------
# Every other test hands the script a UTF-8 environment. This one does not, and
# asserts the pending file the poller depends on comes back correct — text,
# integer ids, and both posted flags false — parsed rather than grepped.
rm -f "$TMP/fakehome"/.local/state/standup/pending-*.json
cat > "$TMP/stub/claude" <<'SH'
#!/usr/bin/env bash
cat > /dev/null
printf '📋 *Daily Standup*\n\n*#alpha*\n• Somnoroase păsărele, e un più lungo\n\n1 project.\n'
SH
chmod +x "$TMP/stub/claude"
out=$(run_no_utf8 "$TMP/nolocale.log")
rc=$?
[ "$rc" -eq 0 ] ||
  failures+=("the report could not be written with no locale: $out")

state_file=$(compgen -G "$TMP/fakehome/.local/state/standup/pending-*.json" | head -1) || state_file=""
if [ -z "$state_file" ]; then
  failures+=("no pending state was written under a C locale")
else
  # Parsed, not grepped: the fields are what the poller depends on, and the
  # writer was rewritten wholesale in this branch.
  python3 - "$state_file" <<'PYEOF' || echo "STATE_BAD"
import json, sys
s = json.load(open(sys.argv[1], encoding="utf-8"))
assert "păsărele" in s["text"] and "più lungo" in s["text"], "text lost its characters"
assert isinstance(s["message_id"], int) and isinstance(s["chat_id"], int), "ids are not integers"
assert s["posted_x"] is False and s["posted_wip"] is False, "a fresh state claims to be posted"
assert s["id"], "no pending id"
PYEOF
  [ $? -eq 0 ] || failures+=("the pending state written under a C locale is wrong")
fi
compgen -G "$TMP/fakehome/.local/state/standup/pending-*.partial" > /dev/null &&
  failures+=("a .partial file was left behind by a successful write")

# --- 11. An override deliberately set to empty ----------------------------
# VAR= on the cron line means "empty", and has to beat a profile export just
# like any other value. Only non-empty ones used to be saved.
printf 'export CLAUDE_BIN=/profile/wins/claude\n' > "$TMP/fakehome/.zshrc"
out=$(CLAUDE_BIN= run "$TMP/empty.log" --check)
rm -f "$TMP/fakehome/.zshrc"
echo "$out" | grep -q 'claude:.*/profile/wins/claude' &&
  failures+=("an empty CLAUDE_BIN on the cron line did not clear the profile export")

# --- 12. share_header from config is the only title after strip -----------
# A share_header that is itself a #hashtag used to be prepended BEFORE
# strip-private and then dropped as an empty project. Prepend after strip.
printf 'projects_root: %s\nshare_header: "#alpha"\n' "$TMP/projects" > "$WORK/standup.yml"

# Raw standup must name the same projects the formatter emits, or
# --repair-headers falls back to raw and the assertions below never see them.
cat > "$TMP/stub/ruby" <<SH
#!/usr/bin/env bash
case " \$* " in
  *" -ryaml "*|*" -e "*) exec "$REAL_RUBY" "\$@" ;;
esac
printf '#beta\n• one\n'
SH
chmod +x "$TMP/stub/ruby"

# 12a. Formatter wrote no title — body starts with a project.
cat > "$TMP/stub/claude" <<'SH'
#!/usr/bin/env bash
cat > /dev/null
printf '*#beta*\n• one\n'
SH
chmod +x "$TMP/stub/claude"
out=$(run "$TMP/share-no-title.log")
first=$(call "$TMP/share-no-title.log" 1)
echo "$first" | grep -q 'text=\*\\#alpha\*' ||
  failures+=("share_header #alpha did not lead Telegram when the formatter omitted a title: $first")
echo "$first" | grep -q '\\#beta' ||
  failures+=("project #beta was lost when share_header was prepended")

# 12b. Formatter wrote the old default title — must be dropped, not stacked.
cat > "$TMP/stub/claude" <<'SH'
#!/usr/bin/env bash
cat > /dev/null
printf '📋 *Daily Standup*\n\n*#beta*\n• one\n'
SH
chmod +x "$TMP/stub/claude"
out=$(run "$TMP/share-wrong-title.log")
first=$(call "$TMP/share-wrong-title.log" 1)
echo "$first" | grep -q 'Daily Standup' &&
  failures+=("LLM title survived after share_header enforce: $first")
echo "$first" | grep -q 'text=\*\\#alpha\*' ||
  failures+=("share_header #alpha did not replace the LLM title: $first")

# 12c. Formatter started with *#alpha* (project) — still prepend config header.
# Raw must match so repair keeps the formatted body.
cat > "$TMP/stub/ruby" <<SH
#!/usr/bin/env bash
case " \$* " in
  *" -ryaml "*|*" -e "*) exec "$REAL_RUBY" "\$@" ;;
esac
printf '#alpha\n• one\n'
SH
cat > "$TMP/stub/claude" <<'SH'
#!/usr/bin/env bash
cat > /dev/null
printf '*#alpha*\n• one\n'
SH
chmod +x "$TMP/stub/ruby" "$TMP/stub/claude"
out=$(run "$TMP/share-hash-project.log")
first=$(call "$TMP/share-hash-project.log" 1)
# First text= line must start with the config header.
echo "$first" | grep -q 'text=\*\\#alpha\*' ||
  failures+=("share_header #alpha missing when body already started with *#alpha*: $first")

# Restore default yml and ruby stub for clarity.
printf 'projects_root: %s\n' "$TMP/projects" > "$WORK/standup.yml"
cat > "$TMP/stub/ruby" <<SH
#!/usr/bin/env bash
case " \$* " in
  *" -ryaml "*|*" -e "*) exec "$REAL_RUBY" "\$@" ;;
esac
printf '#alpha\n• Build config: dart_defines from production.env\n'
SH

if [ ${#failures[@]} -eq 0 ]; then
  echo 'ok: the report reaches Telegram escaped, retries unescaped, and refuses to send what it could not filter'
else
  printf 'FAIL: %s\n' "${failures[@]}" >&2
  exit 1
fi
