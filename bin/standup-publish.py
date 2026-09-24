#!/usr/bin/env python3
"""Publish the daily standup to X and wip.co, but only after the button is pressed.

daily-standup.sh sends the morning message with an inline "Pubblica" button and
leaves the text in a state file. This runs from cron a few times an hour, asks
Telegram whether the button was pressed, and publishes if it was.

Nothing here publishes on its own. No button, no post.
"""

import contextlib
import datetime
import fcntl
import io
import json
import os
import pathlib
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

STATE_DIR = pathlib.Path(
    os.environ.get("STANDUP_STATE_DIR") or pathlib.Path.home() / ".local/state/standup"
)
OFFSET_FILE = STATE_DIR / "telegram-offset"
# The standup wants a bot of its own. Two processes reading one Telegram update
# queue means each press goes to whichever asked first — so the button would
# look fine and collect nothing.
CONFIG_DIR = pathlib.Path(
    os.environ.get("STANDUP_CONFIG_DIR") or pathlib.Path.home() / ".config/standup"
)
TELEGRAM_ENVS = [CONFIG_DIR / "telegram.env"]
WIP_TOKEN_FILE = CONFIG_DIR / "wip-token"

# Resolved, not hardcoded — but not left to PATH alone either. This runs from
# cron, where PATH is short and holds none of the places a global npm install
# puts its binaries, and --selftest never calls bird, so a PATH-only lookup
# fails at the one moment nothing is watching: the post to X.
BIRD = (
    os.environ.get("BIRD_BIN")
    or shutil.which("bird")
    or str(pathlib.Path.home() / ".npm-global/bin/bird")
)
# The npm-global fallback matters for the same reason it does for bird: this
# runs under cron, whose PATH is short, and the README says npm install -g.
# Without it every LinkedIn press would fail with FileNotFoundError — reported
# rather than fatal, but never able to succeed.
BUFFER = (
    os.environ.get("BUFFER_BIN")
    or shutil.which("buffer")
    or str(pathlib.Path.home() / ".npm-global/bin/buffer")
)
BUFFER_ENV = CONFIG_DIR / "buffer.env"


def log(msg):
    print(msg, flush=True)


def warn(msg):
    """Diagnostics that must never reach stdout.

    daily-standup.sh captures --preview's stdout as the text it posts to X, so
    anything printed there lands inside the tweet — and for_x would then return
    different bytes at publish time than the preview showed.
    """
    # Suppressed: a closed or full stderr must not become an exception on the
    # publish path, which is the one thing this whole change promised not to do.
    with contextlib.suppress(Exception):
        print(msg, file=sys.stderr, flush=True)


def die(msg):
    log(f"ERROR: {msg}")
    sys.exit(1)


def read_env(paths, key):
    """Read a key from the first of these files that carries it."""
    for path in paths:
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{key}=") or line.startswith(f"export {key}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    die(f"{key} not found in any of {', '.join(str(p) for p in paths)}")


def optional_env(path, *keys):
    """Every key, or None. Never fatal, unlike read_env.

    read_env calls die() on a missing key, so reusing it for optional config
    would exit on every cron tick of a machine that has no buffer.env.
    """
    if not path.exists():
        return None
    found = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        key, sep, value = line.partition("=")
        if sep and key.strip() in keys:
            found[key.strip()] = value.strip().strip('"').strip("'")
    if any(not found.get(k) for k in keys):
        return None
    return found


def linkedin_config():
    """The Buffer key and channel, or None when LinkedIn is not armed.

    Both are required. A file with one of them is a half-configured destination,
    which is worse than an absent one because it only fails at publish time.
    """
    return optional_env(BUFFER_ENV, "BUFFER_API_KEY", "BUFFER_LINKEDIN_CHANNEL")


def wip_key():
    if not WIP_TOKEN_FILE.exists():
        die(f"{WIP_TOKEN_FILE} is missing")
    raw = WIP_TOKEN_FILE.read_text().strip()
    # The file has been seen holding WIP_TOKEN=<key> rather than the bare key,
    # which the API rejects exactly like a made-up one. Accept both shapes.
    return raw.split("=", 1)[1] if raw.startswith("WIP_TOKEN=") else raw


def telegram(token, method, **params):
    data = urllib.parse.urlencode(params).encode()
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        with urllib.request.urlopen(url, data=data, timeout=30) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code != 409:
            body = e.read().decode("utf-8", "replace")[:300]
            die(f"telegram {method} failed: HTTP {e.code}: {body}")
        if e.code == 409:
            # Somebody else is polling this bot. Telegram hands each update to
            # whichever process asks first, so the button would keep looking
            # fine and keep collecting nothing. Say it where it will be read,
            # rather than dying quietly in a cron log.
            warn_conflict(token)
            die("another process is polling this bot (HTTP 409); the publish button is dead until it stops")
    if not body.get("ok"):
        die(f"telegram {method} failed: {body}")
    return body["result"]


def warn_conflict(token):
    """Send the alarm with sendMessage, which no conflict blocks."""
    chat_id = read_env(TELEGRAM_ENVS, "TELEGRAM_CHAT_ID")
    text = ("⚠️ Standup: the publish button is not working.\n\n"
            "Another process is reading this bot's updates (HTTP 409), so button "
            "presses never arrive. Usually claude-telegram-bot.service was started: "
            "stop it, or give the standup a bot of its own in "
            f"{TELEGRAM_ENVS[0]}.")
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=30
        ).close()
    except Exception as e:  # noqa: BLE001 - the alarm must not hide the failure
        log(f"could not send the conflict warning: {e}")


def ack(token, callback_query_id, text):
    """The little toast on the button. Cosmetic, and allowed to fail.

    Telegram rejects a callback id that is too old, and it did: the 400 raised
    through the publish and the post never went out, with the offset already
    advanced so the press was gone. Nothing about a toast is worth a lost day.
    """
    try:
        telegram(token, "answerCallbackQuery", callback_query_id=callback_query_id, text=text)
    # SystemExit too: telegram() calls die() on a bad response, and SystemExit is
    # not an Exception, so catching Exception alone would still have exited here.
    except (Exception, SystemExit) as e:  # noqa: BLE001 - the publish is the point
        log(f"could not acknowledge the button press ({e}); publishing anyway")


HASHTAG = re.compile(r"#([a-z0-9]+)")

# A header as standup.rb emits it: the mapped hashtag, alone on its line.
RAW_HEADER = re.compile(r"#([A-Za-z0-9][A-Za-z0-9_-]*)")
# The same header after the formatter, which may have bolded it and may have
# eaten the "#". Anything with a space in it is a title or a trailer, not a header.
FMT_HEADER = re.compile(r"\*?#?([A-Za-z0-9][A-Za-z0-9_-]*)\*?")


# Every character MarkdownV2 gives a meaning to. All of them are escaped,
# without asking whether this one looks like markup: guessing is what legacy
# Markdown does, and guessing is the bug.
MDV2_SPECIAL = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_mdv2(text):
    return MDV2_SPECIAL.sub(r"\\\1", text)


def to_markdown_v2(text):
    """Render the report for Telegram, with the emphasis put in here.

    Legacy Markdown has no escape character, so a single unpaired "_" anywhere
    in the message is a syntax error for the whole message. On 2026-09-09 the
    commit subject "Build config: dart_defines from production.env" carried
    exactly one, Telegram answered "Can't find end of the entity starting at
    byte offset 896" — the byte of that underscore — and the send fell back to
    plain text. The report arrived with every asterisk showing raw and nothing
    bold, which reads as "the formatting is broken", and it hid a repair that
    had in fact worked.

    So: escape everything as MarkdownV2, then add the two emphases that are ours
    to add — the title, and each project header. A commit subject can then hold
    any character it likes, because none of them are markup any more.

    The input must be unescaped text. This is not idempotent and cannot be: a
    report legitimately containing a backslash has to have it escaped, so
    escaped output is a different kind of value from source text, not a fixed
    point.
    """
    out = []
    seen_content = False
    for line in text.split("\n"):
        stripped = line.strip()
        # Title: first non-empty line unless a project starts the report.
        # share_header need not begin with 📋.
        is_title = bool(stripped) and not seen_content and not RAW_HEADER.fullmatch(stripped)
        if stripped:
            seen_content = True
        header = RAW_HEADER.fullmatch(stripped) or is_title
        out.append(f"*{escape_mdv2(stripped)}*" if header and stripped else escape_mdv2(line))
    return "\n".join(out)


# Telegram rejects a message over 4096 characters. Escaping only inflates the
# text — every "." and "-" gains a backslash — so a busy day that fitted before
# can stop fitting exactly when the report matters most.
TELEGRAM_LIMIT = 4096


def tg_len(text):
    """Length as Telegram counts it: UTF-16 code units, not code points.

    The 📋 in the title is one Python character and two of these. Counting
    Python characters gives a budget that is quietly too generous for exactly
    the reports that carry emoji, which is all of them.
    """
    return len(text.encode("utf-16-le")) // 2


def fit_telegram(text, limit=TELEGRAM_LIMIT):
    """Trim the report to what Telegram will accept, on a boundary that reads.

    Trimming happens BEFORE escaping, never after: an escape is a two-character
    pair, and a cut landing between the backslash and its character leaves a
    dangling backslash — which Telegram rejects, which is the very failure this
    module exists to remove, reintroduced by its own guard.

    Whole project blocks go first, because half a project is worse than a named
    omission. If one block alone is too big, that block is cut by line.
    """
    # Measured on the text a reader sees, not on the payload.
    #
    # Telegram applies the limit after it parses entities, so the backslashes
    # and the emphasis asterisks this module adds do not count toward it. An
    # earlier revision measured the escaped payload and then the rendered one;
    # both trim reports that Telegram would have accepted, and on a subject full
    # of full stops the escape inflates the count by a tenth or more.
    #
    # The failure modes are not symmetrical, which is why this is worth getting
    # right rather than being conservative: trimming early silently drops a
    # project from the report, while measuring too generously produces a
    # rejection that the plain-text retry below already handles.
    def payload(t):
        return tg_len(t)

    marker = "\n\n… trimmed to fit Telegram; the published version is complete."
    if payload(text) <= limit:
        return text

    room = limit - payload(marker)

    # Split on project headers, not on blank lines. A blank line is wherever the
    # formatter felt like one — put one between two bullets and a "block" is
    # half a project, so half a project is what gets dropped.
    blocks, current = [], []
    for line in text.split("\n"):
        if RAW_HEADER.fullmatch(line.strip()) and current:
            blocks.append("\n".join(current).strip("\n"))
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip("\n"))

    kept = []
    for block in blocks:
        candidate = kept + [block]
        if payload("\n\n".join(candidate)) <= room:
            kept = candidate
            continue

        # This block does not fit whole. Cut it by line rather than dropping it:
        # an oversized first project used to leave the reader with a title and a
        # trim marker and no commits at all, because the by-line path could only
        # run when nothing had been kept, and a report had a title to keep.
        # Since strip_private stopped exempting frame lines, a title carrying a
        # matched word is dropped and the report can start with a project
        # header — so "nothing kept yet" no longer implies "first block".
        lines = block.split("\n")
        while lines and payload("\n\n".join(kept + ["\n".join(lines)])) > room:
            lines.pop()
        # If even its first line is too long, cut the line itself.
        if not lines:
            first = block.split("\n")[0]
            while first and payload("\n\n".join(kept + [first])) > room:
                first = first[:-1]
            lines = [first] if first else []
        if lines:
            kept.append("\n".join(lines))
        break  # nothing after an overflowing block can fit either

    return "\n\n".join(kept) + marker


def strip_telegram_markup(text):
    """The morning message is Telegram Markdown. X and wip.co are not.

    Only the asterisks go. An underscore in a commit subject is a character in
    an identifier, not italics: on 2026-09-09 this deleted the one in
    "Build config: dart_defines from production.env", and the X post would have
    read "dartdefines". The formatter is asked for *bold* and rarely writes
    _italics_, so deleting every underscore to catch a case that mostly does not
    happen costs more than it saves.
    """
    out = []
    seen_content = False
    for line in text.splitlines():
        bare = line.strip()
        # Asterisks are removed only where they can only be markup: the title,
        # and a project header, both of which are re-emphasised downstream
        # anyway. Everywhere else an asterisk is a character somebody committed
        # — "*.rb", "2 * 3" — and deleting it is the same data loss as the
        # underscore this function exists to stop deleting.
        # Tested with the asterisks removed, so **#alpha** is recognised as the
        # header it is. FMT_HEADER allows one asterisk a side, and a formatter
        # that reaches for ** despite the prompt used to have its stars deleted
        # by the old blanket replace; matching on the bare token restores that
        # without deleting asterisks anywhere else.
        #
        # The title is the first non-empty line unless a project starts the
        # report — share_header need not begin with 📋.
        is_title = bool(bare) and not seen_content and not FMT_HEADER.fullmatch(
            bare.replace("*", ""))
        if bare:
            seen_content = True
        if is_title or bare.startswith("📋") or FMT_HEADER.fullmatch(bare.replace("*", "")):
            line = line.replace("*", "")
        out.append(line)
    return "\n".join(out).strip()


def wip_projects():
    """The projects as wip.co holds them, keyed by hashtag."""
    url = "https://api.wip.co/v1/users/me/projects?" + urllib.parse.urlencode(
        {"api_key": wip_key(), "limit": "100"}
    )
    with urllib.request.urlopen(url, timeout=30) as r:
        body = json.load(r)
    items = body if isinstance(body, list) else body.get("data", [])
    return {p["hashtag"]: p for p in items if p.get("hashtag")}


def repair_headers(raw, formatted):
    """Put back the hashtags the formatter dropped, against what standup.rb emitted.

    The headers are data, not prose. wip.co attaches a todo to a project BY the
    hashtag, and the X text swaps that hashtag for the project name and website.
    The prompt asks an LLM to keep one character, and on 2026-09-09 it did not:
    it bolded the names and dropped every "#". The post went out with no links,
    and the wip.co todo would have attached to nothing. Neither failure is
    visible downstream, because a missing "#" reads exactly like a project that
    never had one.

    So the hashtags are restored from the raw report rather than hoped for in
    the prompt. Returns (repaired_text, missing_tags); a non-empty second value
    means the formatter lost a whole project and its output must not be used.
    """
    tags = [m.group(1) for line in raw.split("\n")
            if (m := RAW_HEADER.fullmatch(line.strip()))]
    out = []
    for line in formatted.split("\n"):
        m = FMT_HEADER.fullmatch(line.strip())
        out.append(f"*#{m.group(1)}*" if m and m.group(1) in tags else line)
    repaired = "\n".join(out)
    return repaired, [t for t in tags if f"#{t}" not in repaired]


# A project block's first line IS the hashtag, optionally in Telegram bold.
# Deliberately not "a line that contains one": a bullet opening
# "\u2022 #123 was the culprit" would otherwise become a project and write "123"
# into the rotation history.
SLOT_HEADER = re.compile(r"\*?#([a-z0-9]+)\*?")
DATE_KEY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _lead_files():
    """Resolved at call time, never at import: selftest rebinds STATE_DIR."""
    return STATE_DIR / "lead-history.json", STATE_DIR / "lead-history.lock"


@contextlib.contextmanager
def _lead_lock():
    """Yields True when the lock is held, False when it could not be taken.

    A caller that gets False must read nothing and write nothing. Carrying on
    unlocked would let two processes overwrite each other's history, which is
    the whole reason the lock is here — and losing a day of rotation is a much
    smaller price than a corrupted one.

    The lock lives on a sidecar that is never replaced. Locking the JSON file
    itself would not work: the atomic write installs a new inode, so the next
    process would lock a different file and the two updates would race.
    """
    _, lock_path = _lead_files()
    handle = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+")
        fcntl.flock(handle, fcntl.LOCK_EX)
    except Exception as e:  # noqa: BLE001 - a lock we cannot take must not stop a publish
        warn(f"lead rotation lock unavailable: {e}")
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()
        handle = None
    try:
        yield handle is not None
    finally:
        if handle is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(handle, fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                handle.close()


def _is_date(value):
    """A real calendar date, not just the shape of one. 2026-99-99 is neither."""
    if not (isinstance(value, str) and DATE_KEY.fullmatch(value)):
        return False
    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _set_aside(path):
    """Keep an unusable file. True when it is safely out of the way.

    False means the caller must not write a fresh history over it: that would
    destroy the only copy of whatever went wrong, which is the opposite of the
    point. Nanoseconds, not seconds, because two renders in the same second
    would collide and the rename would fail.
    """
    try:
        path.rename(path.with_suffix(f".json.bad-{time.time_ns()}"))
        return True
    except Exception as e:  # noqa: BLE001
        warn(f"could not set the unusable lead rotation file aside: {e}")
        return False


def _read_history():
    """(decisions, last_led, writable).

    Anything unusable starts an empty history: a decode error, a top level that
    is not a dict, a missing or non-dict section, or any value that is not a
    real calendar date. The file is written by this code alone, so a shape it
    never writes means something else got to it, and trusting the parts that
    still parse is a guess.

    `writable` is False when the old file could not be set aside. Writing then
    would destroy the only copy of the evidence, so the caller does nothing at
    all and the day falls back to the seeded shuffle.
    """
    path, _ = _lead_files()
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, {}, True
    except Exception as e:  # noqa: BLE001 - unreadable, or not JSON
        warn(f"lead rotation file unusable ({e}); starting a new one")
        return {}, {}, _set_aside(path)

    def usable(section, check):
        return isinstance(section, dict) and all(
            isinstance(k, str) and isinstance(v, str) and check(k, v)
            for k, v in section.items())

    if not (isinstance(raw, dict)
            and "decisions" in raw and "last_led" in raw
            and usable(raw["decisions"], lambda k, v: _is_date(k))
            and usable(raw["last_led"], lambda k, v: _is_date(v))):
        warn("lead rotation file has a shape it was never written with; "
             "starting a new one")
        return {}, {}, _set_aside(path)
    return dict(raw["decisions"]), dict(raw["last_led"]), True


def _write_history(decisions, last_led):
    """Best effort. A rotation file we cannot write is not worth a lost publish."""
    path, _ = _lead_files()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"decisions": decisions, "last_led": last_led},
                                  ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001
        warn(f"could not write the lead rotation file: {e}")
        with contextlib.suppress(Exception):
            path.with_suffix(".json.tmp").unlink(missing_ok=True)


def choose_lead(tags, seed, date):
    """Which project leads today: the one that has gone longest without leading.

    The lead slot is the promotion — X builds the link card from the first URL
    in the post — so drawing it at random every day was never fair. An
    independent draw clusters, and it did: one project led three mornings
    running while three others had never led at all.

    Pinned per date, because --preview renders this text at 07:30 and the
    publish re-renders it when the button is finally pressed, hours later. A
    preview that does not match what goes out is not an approval of anything.
    The pin is validated against today's projects: a second daily-standup.sh run
    the same morning rewrites the pending file, possibly with a different set.
    """
    if not tags:
        return None
    with _lead_lock() as locked:
        if not locked:
            return None
        decisions, last_led, writable = _read_history()
        pinned = decisions.get(date)
        if pinned in tags:
            return pinned
        # Never-led sorts first; the hashtag breaks the sort stably. Only the
        # group tied at the front is shuffled, and only with the date seed:
        # hash() and set order follow PYTHONHASHSEED, and preview and publish
        # are two processes.
        ranked = sorted(tags, key=lambda t: (last_led.get(t) or "", t))
        front_key = last_led.get(ranked[0]) or ""
        front = [t for t in ranked if (last_led.get(t) or "") == front_key]
        random.Random(seed).shuffle(front)
        lead = front[0]
        # The pin is only written under a date this file can read back. A key
        # that fails validation would make the whole file unusable on the next
        # read, and take every project's turn with it.
        if writable and _is_date(date):
            decisions[date] = lead
            _write_history(decisions, last_led)
        return lead


def should_record_lead(x_before, state, lead):
    """A turn is spent by a fresh X success on this press, and nothing else.

    Not the "already posted, skipped" path, and not a wip.co-only press: only X
    carries the link card that makes the lead slot worth having.
    """
    return bool(not x_before and state.get("posted_x") and lead.get("tag"))


def record_lead(date, project):
    """Mark the turn as spent. Only after X actually accepted the post.

    A standup that is previewed and never approved, or whose publish fails, must
    not consume a project's turn. A wip.co-only press records nothing: only X
    has the card that makes the lead slot worth anything.
    """
    if not (_is_date(date) and project):
        return
    with _lead_lock() as locked:
        if not locked:
            return
        decisions, last_led, writable = _read_history()
        if not writable:
            return
        # Never backwards: pending_states() can publish an older day after a
        # newer one, and an older date here would make that project look
        # least-recently-led and lead again tomorrow.
        if (last_led.get(project) or "") >= date:
            return
        last_led[project] = date
        _write_history(decisions, last_led)


# Lines that must not leave this machine, on any destination. Every stem ends
# in \w* on purpose: "\bvulnerabilit\b" can never match "vulnerability", because
# a word character follows the stem. The first draft of this list was written
# that way and caught none of the four lines it was written for.
# The formatter's closing count, which is never anybody's commit.
TRAILER_LINE = re.compile(r"(?i)\s*\d+\s+projects?\b.*")
PRIVATE_LINE = [re.compile(p) for p in (
    # The word, not the identifier, and the plural: "fixed rubyzip CVE" and
    # "3 CVEs fixed" are the disclosure — naming the library and the fact of it.
    # The number adds nothing. The id-only form missed all five of the lines it
    # was supposed to catch on 2026-09-16.
    r"(?i)\bcves?\b",
    # Security work under another name.
    r"(?i)\bharden\w*",
    # \w* on the stems: "\bvulnerabilit\b" can never match "vulnerability",
    # because a word character follows the stem. The first version of this list
    # was written that way and caught none of the lines it was written for.
    r"(?i)\b(vulnerabilit|vuln|advisor|exploit|breach)\w*",
    r"(?i)\b(zero[ -]?day|rce|xss|csrf|sqli)\b",
    # Qualified only. Bare "injection" is dependency injection and bare "leak"
    # is a memory leak — both ordinary commits, and dropping a project's only
    # bullet would turn an ordinary day into a silent one.
    r"(?i)\b(sql|command|code|template|prompt)[ -]?injection\b",
    r"(?i)\b(data|token|key|secret|credential|password)[ -]?leaks?\b",
    # Unanchored, and it replaces the old start-of-line form, which caught a
    # "Security:" label and nothing else. A trailer reading "security
    # hardening" is the same disclosure in the middle of a line.
    r"(?i)\bsecurity\b",
    # Plural only, never \w*: "secret\w*" would drop a bullet about a secretary.
    r"(?i)\b(passwords?|secrets?|credentials?)\b",
    # A key or a token is only interesting when something qualifies it.
    r"(?i)\b(api|signing|private|public|ssh|gpg|jwt|auth|access|refresh|session)"
    r"[ _-]?(keys?|tokens?)\b",
)]


# Deliberately wider than SLOT_HEADER, which the rotation uses. The rotation
# must not hand the lead to a project the URL swap cannot resolve; the filter
# has the opposite duty — miss a header here and a valid report is refused as
# having no projects at all. repo_name_mapping allows uppercase, "_" and "-".
FILTER_HEADER = re.compile(r"\*?#([A-Za-z0-9][A-Za-z0-9_-]*)\*?")


def _is_header(line):
    return bool(FILTER_HEADER.fullmatch(line.strip()))


def strip_private(text):
    """Remove security lines, and any project they leave with nothing to say.

    Applied per line. Only **headers** are exempt: dropping one would orphan its
    bullets into the project above it, which is worse than publishing the name.
    The title and the closing count are matched like any other line — neither
    has anything below it to orphan, and on 2026-09-16 the count line published
    "security hardening" because it was exempt.

    Frame identity still decides grouping, and still keeps a frame line from
    counting as body: a trailer glued to the last project must not keep that
    project alive once its bullets are gone.

    The frame is the title and the closing count, and it takes BOTH position and
    shape to be one. The title is the first non-empty line unless a header
    starts the report. The trailer is the last non-empty line AND has to look
    like the formatter's count.

    Both halves are load-bearing, and each was wrong on its own once. Shape
    alone made any body line beginning "3 projects ..." exempt from every
    pattern — a hole, not an exemption. Position alone made the last bullet of a
    count-less report into a trailer, which cost that project its header.

    The distinction matters more since a frame line stopped counting as body: a
    bullet wrongly classed as frame would earn its project no body at all, and
    the project would be dropped as empty.

    Blocks are grouped under their header rather than judged one at a time,
    because standup.rb writes the project name, a blank line, and then the
    bullets. In a blank-line split the header is therefore its own block with no
    body in it, and a per-block rule would leave a bare project name behind
    whenever a project's only bullet was dropped.

    Returns (text, projects_before, projects_after).
    """
    raw = text.split("\n")
    filled = [i for i, line in enumerate(raw) if line.strip()]
    frame = set()
    if filled:
        # The title is whatever comes first, unless a project starts the report
        # — which is the shape standup.rb's own output has.
        if not _is_header(raw[filled[0]].strip()):
            frame.add(filled[0])
        # The trailer has to be last AND look like the formatter's count. Last
        # alone is wrong: the raw report ends on a bullet, and exempting it
        # dropped that project's header. Shape alone is worse: it made every
        # body line beginning "3 projects ..." immune to all four patterns.
        last = filled[-1]
        if not _is_header(raw[last].strip()) and TRAILER_LINE.fullmatch(raw[last]):
            frame.add(last)

    # Split keeping the separators, so every line's absolute index falls out of
    # the arithmetic. Re-finding each line by value was fragile: two identical
    # bullets in one report would resync onto the wrong one.
    numbered, index = [], 0
    for position, part in enumerate(re.split(r"(\n\s*\n)", text)):
        if position % 2:
            index += part.count("\n")
            continue
        rows = [(index + offset, line) for offset, line in enumerate(part.split("\n"))]
        numbered.append(rows)
        index += len(rows) - 1

    FRAME, HEADED, LOOSE = "frame", "headed", "loose"
    groups = []
    for rows in numbered:
        if rows and _is_header(rows[0][1].strip()):
            groups.append({"kind": HEADED, "rows": rows})
        elif all(i in frame or not line.strip() for i, line in rows):
            groups.append({"kind": FRAME, "rows": rows})
        elif groups and groups[-1]["kind"] == HEADED:
            groups[-1]["rows"].append((-1, ""))
            groups[-1]["rows"].extend(rows)
        else:
            groups.append({"kind": LOOSE, "rows": rows})

    before = sum(g["kind"] == HEADED for g in groups)
    out, after = [], 0
    for group in groups:
        if group["kind"] == FRAME:
            # Filtered too. The exemption used to live here as well as in the
            # loop below, and changing only one left a glued trailer going out.
            # Headers are still exempt on this path as well as the other: a
            # title-less report whose first line is a header lands here.
            kept_lines = [line for _, line in group["rows"]
                          if _is_header(line.strip())
                          or not (line.strip() and any(r.search(line) for r in PRIVATE_LINE))]
            if any(line.strip() for line in kept_lines):
                out.append("\n".join(kept_lines))
            continue
        kept_rows, body = [], 0
        for i, line in group["rows"]:
            if _is_header(line.strip()):
                kept_rows.append((i, line))
                continue
            if line.strip() and any(r.search(line) for r in PRIVATE_LINE):
                continue
            kept_rows.append((i, line))
            # A frame line that survives is still not body. Counting a glued
            # trailer as body would leave a project header with nothing under
            # it but the count.
            body += bool(line.strip()) and i not in frame
        lines = [line for _, line in kept_rows]
        if not body:
            # The project is gone, but a frame line glued to it is not
            # collateral: it is dropped only when it matches a pattern itself,
            # and it already survived that test above.
            survivors = [line for i, line in kept_rows if i in frame and line.strip()]
            if survivors:
                out.append("\n".join(survivors))
            continue
        after += group["kind"] == HEADED
        out.append("\n".join(lines).strip("\n"))
    return "\n\n".join(b for b in out if b.strip()), before, after


def split_share_frame(text):
    """Split a report into (head, body, tail) for public rewrite.

    head — share_header / title (everything before the first project)
    body — project blocks only; shuffle and hashtag→URL run here
    tail — the "N projects" trailer plus share_footer (never rewritten)

    Without a trailer, trailing blocks that are not real projects (no body under
    a hashtag header) are treated as the footer — so a lone `#foo` after the
    last project stays a social tag, not a project slot.
    """
    if not text or not text.strip():
        return text, "", ""

    blocks = re.split(r"\n\s*\n", text)

    def first_line(block):
        return block.split("\n")[0].strip()

    def is_slot(block):
        return bool(SLOT_HEADER.fullmatch(first_line(block)))

    def is_trailer_block(block):
        return bool(TRAILER_LINE.fullmatch(block.strip()))

    def is_real_project(block):
        if not is_slot(block):
            return False
        lines = block.split("\n")
        return any(ln.strip() for ln in lines[1:])

    def join(parts):
        return "\n\n".join(parts)

    trailer_i = None
    for i, block in enumerate(blocks):
        if is_trailer_block(block):
            trailer_i = i

    # Leading matter that is not a real project (hashtag + bullets) is the
    # title / share_header. A lone #hashtag as the first block is the configured
    # title, not a project — otherwise for_x would turn it into "Name — URL".
    if trailer_i is not None:
        i = 0
        head = []
        while i < trailer_i and not is_real_project(blocks[i]):
            head.append(blocks[i])
            i += 1
        body = blocks[i:trailer_i]
        tail = blocks[trailer_i:]
        return join(head), join(body), join(tail)

    rest = list(blocks)
    tail = []
    while len(rest) > 1 and not is_real_project(rest[-1]):
        tail.insert(0, rest.pop())

    i = 0
    head = []
    while i < len(rest) and not is_real_project(rest[i]):
        head.append(rest[i])
        i += 1
    return join(head), join(rest[i:]), join(tail)


def _join_share_frame(head, body, tail):
    return "\n\n".join(p for p in (head, body, tail) if p)


def _shuffle_body(body, seed, lead_out=None):
    """Reorder project slots inside the body only. See shuffle_projects."""
    if not body or not body.strip():
        return body

    blocks = re.split(r"\n\s*\n", body)
    slots, tags = [], []
    for i, b in enumerate(blocks):
        m = SLOT_HEADER.fullmatch(b.split("\n")[0].strip())
        if m:
            slots.append(i)
            tags.append(m.group(1))
    picked = [blocks[i] for i in slots]

    lead = None
    random.Random(seed).shuffle(picked)
    try:
        if tags:
            lead = choose_lead(tags, seed, date=seed)
        if lead is not None and lead in tags:
            at = next(i for i, b in enumerate(picked)
                      if (m := SLOT_HEADER.fullmatch(b.split("\n")[0].strip()))
                      and m.group(1) == lead)
            picked.insert(0, picked.pop(at))
            if lead_out is not None:
                lead_out["tag"] = lead
    except Exception as e:  # noqa: BLE001 - fairness is never worth a lost day
        warn(f"lead rotation unavailable, falling back to the shuffle: {e}")

    for i, block in zip(slots, picked):
        blocks[i] = block
    return "\n\n".join(blocks)


def shuffle_projects(text, seed, lead_out=None):
    """Reorder the project blocks, the same way all day, differently each day.

    X builds the link card from the first URL in the tweet. The standup lists
    the projects in a stable order, so the same site was always first and the
    card carried the same image every morning.

    Seeded by the day on purpose: --preview renders this text hours before the
    button is pressed, and a preview that does not match what gets posted is
    not an approval of anything.

    The title, the closing count, and the share_footer keep their place —
    only project blocks in the body move.

    `lead_out`, when given, receives the chosen hashtag under "tag". The caller
    needs it to record the turn afterwards, and it cannot be read back out of
    the finished text: for_x has replaced every hashtag with a name and a URL by
    then, so the key would never match.

    Nothing here may raise. This runs inside post_to_x, which catches nothing,
    and the update offset is already advanced by the time it does — so an
    exception would lose the press and the day. Any failure falls back to the
    plain seeded shuffle.
    """
    try:
        head, body, tail = split_share_frame(text)
        if not body:
            return text
        shuffled = _shuffle_body(body, seed, lead_out=lead_out)
    except Exception as e:  # noqa: BLE001 - never lose the day
        warn(f"shuffle failed, keeping original order: {e}")
        return text
    return _join_share_frame(head, shuffled, tail)


def _rewrite_project_headers(body, projects):
    """Swap project-header lines only — not inline tags, not header/footer."""
    out = []
    for line in body.split("\n"):
        m = SLOT_HEADER.fullmatch(line.strip())
        if not m:
            out.append(line)
            continue
        project = projects.get(m.group(1))
        site = (project or {}).get("website_url")
        if not project or not site:
            out.append(line)
            continue
        out.append(f"{project.get('name') or m.group(1)} — {site}")
    return "\n".join(out)


def for_x(text, seed, lead_out=None):
    """Rewrite project hashtags as names and links, for X.

    A hashtag is the attach mechanism on wip.co and nothing but text on X, where
    a row of them reads as spam. The name and the site say more.

    Only project headers in the body are rewritten. share_header and share_footer
    stay verbatim — including any hashtags they carry — so social tags at the
    end of the post are not turned into project links.

    The names and URLs come from wip.co itself, at publish time, rather than a
    second list here that would drift from it — the same duplication that made
    a standup silently hide six repositories, and made a command report a cache
    it did not keep.

    If wip.co cannot be reached, the hashtags stay. A post that reads a little
    worse beats no post at all.

    Nothing here may raise. Same contract as shuffle_projects: the update offset
    is already advanced by the time this runs.
    """
    try:
        head, body, tail = split_share_frame(text)
        if body:
            body = _shuffle_body(body, seed, lead_out=lead_out)
    except Exception as e:  # noqa: BLE001 - never lose the day
        warn(f"for_x frame/shuffle failed, keeping original text: {e}")
        return text
    try:
        projects = wip_projects()
    except Exception as e:  # noqa: BLE001 - never let this block a publish
        log(f"could not read wip.co projects, keeping the hashtags: {e}")
        return _join_share_frame(head, body, tail)

    if body:
        body = _rewrite_project_headers(body, projects)
    return _join_share_frame(head, body, tail)


def post_to_x(text):
    """`text` is already rendered for a public timeline — see render_public."""
    result = subprocess.run([BIRD, "tweet", text], capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[:300]
    return True, (result.stdout or "").strip()[:300]


def post_to_linkedin(text, config):
    """Through the Buffer CLI, the same shape X already has with bird.

    shareNow rather than the queue: the press is the approval, and a post that
    appears at some later slot is not what the button promised. The key reaches
    the child through the environment; a file it never reads would not.
    """
    result = subprocess.run(
        [BUFFER, "posts", "create",
         "--channel-id", config["BUFFER_LINKEDIN_CHANNEL"],
         "--text", text,
         "--mode", "shareNow",
         "--scheduling-type", "automatic"],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ, BUFFER_API_KEY=config["BUFFER_API_KEY"]))
    if result.returncode != 0:
        # The CLI answers in JSON, and what Buffer refused is the useful part.
        return False, (result.stderr or result.stdout).strip()[:300]
    return True, (result.stdout or "").strip()[:300]


def post_to_wip(text):
    data = urllib.parse.urlencode({"body": text}).encode()
    url = "https://api.wip.co/v1/todos?" + urllib.parse.urlencode({"api_key": wip_key()})
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = json.load(r)
        return True, body.get("url") or str(body)[:200]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
    except Exception as e:  # noqa: BLE001 - the reason has to reach the log
        return False, str(e)[:300]


LEGACY_BOTH = ("x", "wip")
# Which flag records each destination. A state written before LinkedIn existed
# has no "armed" list, and must not wait for a post its keyboard never offered.
DESTINATION_FLAG = {"x": "posted_x", "wip": "posted_wip", "linkedin": "posted_linkedin"}


def day_complete(state):
    """Every destination armed THAT MORNING has been resolved.

    Resolved means posted, or waived. Judged against the armed list rather than
    the config that exists now: a day whose keyboard never offered LinkedIn must
    not wait for a LinkedIn post, and one whose LinkedIn was unconfigured before
    the button was pressed must not wait for ever either.

    A waiver is deliberately not a success. Recording one as posted would show a
    green tick for a post that never happened, and would let a retry be skipped
    as "already posted" if the configuration came back.
    """
    armed = state.get("armed") or list(LEGACY_BOTH)
    waived = state.get("waived") or []
    return all(state.get(DESTINATION_FLAG[a]) or a in waived
               for a in armed if a in DESTINATION_FLAG)


def publish_pending(state, target, posters, save):
    """Post to each requested destination, writing down each success at once.

    The write has to happen after every individual post, not once at the end,
    because everything after a successful post can fail: the next destination,
    the status reply, the process. The update offset was already advanced before
    any of this ran, so a failure that loses the record also loses the press —
    and the next press starts again from "nothing has been posted". For X that
    means the same standup tweeted twice, and a duplicate on a public timeline
    is not something an apology undoes.

    So: post, remember, then move on. `save` is called with the state after each
    destination, and the caller decides where that goes.
    """
    lines = []
    for name, code, already, fn in posters:
        # `all` is every armed destination. `both` is the two that existed
        # before LinkedIn and means exactly those, so a press on a message sent
        # back then still does what its label promised — and does not silently
        # acquire a third destination the person never saw.
        if not (target == code or target == "all"
                or (target == "both" and code in LEGACY_BOTH)):
            continue
        if state.get(already):
            lines.append(f"✅ {name} — already posted, skipped")
            continue
        try:
            ok, detail = fn()
        except subprocess.TimeoutExpired as e:
            # Ambiguous: the post may well have gone out. It is recorded as
            # failed and is therefore retryable, so the reply has to say so
            # rather than leave a second public post to chance.
            ok, detail = False, (f"timed out after {e.timeout:g}s — it MAY have "
                                 "been published; check before pressing again")
        # SystemExit too, and not by accident: wip_key() calls die() on a
        # missing token, die() raises SystemExit, and SystemExit is not an
        # Exception. Catching Exception alone would have left that one escaping
        # exactly the way this guard exists to stop.
        except (Exception, SystemExit) as e:  # noqa: BLE001 - see below
            # bird runs with timeout=120 and nothing caught it, and the
            # getUpdates offset was advanced before this loop began. A raise
            # here used to lose the press and the whole day.
            ok, detail = False, f"{type(e).__name__}: {e}"[:300]
        state[already] = ok
        save(state)
        lines.append(f"{'✅' if ok else '❌'} {name} — {detail or 'posted'}")
    return lines


def pending_states():
    if not STATE_DIR.exists():
        return []
    out = []
    for p in sorted(STATE_DIR.glob("pending-*.json")):
        try:
            out.append((p, json.loads(p.read_text())))
        except json.JSONDecodeError:
            log(f"skipping unreadable state file {p}")
    return out


def _reset_rotation():
    """Every case starts from an empty history, or it passes for the wrong reason."""
    path, lock = _lead_files()
    path.unlink(missing_ok=True)
    lock.unlink(missing_ok=True)
    for bad in STATE_DIR.glob("lead-history.json.bad-*"):
        bad.unlink()


def selftest():
    """Isolated, always.

    STATE_DIR is bound at import, so setting STANDUP_STATE_DIR in here would do
    nothing. The 28-day loop below runs real dates, and without this rebind a
    test run writes fake projects into the live rotation file.
    """
    global STATE_DIR
    real, tmp = STATE_DIR, tempfile.mkdtemp(prefix="standup-selftest-")
    STATE_DIR = pathlib.Path(tmp)
    try:
        _selftest_body()
    finally:
        STATE_DIR = real
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_body():
    global STATE_DIR
    text = ("\U0001F4CB Daily Standup \u2014 2026-09-06\n\n"
            "#alpha\n\u2022 one\n\n#beta\n\u2022 two\n\n#gamma\n\u2022 three\n\n"
            "3 projects, 9 commits shipped")
    day = shuffle_projects(text, "2026-09-06")
    assert day == shuffle_projects(text, "2026-09-06"), "one day must render one order"
    assert day.startswith("\U0001F4CB Daily Standup"), "the title has to stay first"
    assert day.endswith("9 commits shipped"), "the closing line has to stay last"
    assert sorted(HASHTAG.findall(day)) == ["alpha", "beta", "gamma"], "no project may be lost"
    firsts = {HASHTAG.search(shuffle_projects(text, f"2026-09-{d:02d}")).group(1)
              for d in range(1, 29)}
    assert len(firsts) > 1, f"the first project never changes: {firsts}"

    # The 2026-09-09 failure, and the shapes around it.
    raw = "#alpha\n\u2022 one\n#beta\n\u2022 two"
    dropped = "\U0001F4CB *Daily Standup*\n\n*alpha*\n\u2022 one\n\n*beta*\n\u2022 two\n\n2 projects \u2014 fine."
    fixed, missing = repair_headers(raw, dropped)
    assert not missing, missing
    assert "*#alpha*" in fixed and "*#beta*" in fixed, fixed
    assert "\U0001F4CB *Daily Standup*" in fixed, "the title is not a header"
    assert "2 projects \u2014 fine." in fixed, "the trailer is not a header"
    assert "\u2022 one" in fixed, "bullets must not be touched"
    # Already correct: repairing twice must not double the hash.
    again, _ = repair_headers(raw, fixed)
    assert again == fixed and "##" not in again, again
    # A project the formatter deleted cannot be repaired, and must be reported.
    _, lost = repair_headers(raw, "*alpha*\n\u2022 one")
    assert lost == ["beta"], lost
    # A project this standup never had must not be invented into a header.
    kept, _ = repair_headers(raw, "*gamma*\n\u2022 three\n\n*alpha*\n\u2022 one")
    assert "*gamma*" in kept and "#gamma" not in kept, kept
    # A success must be on disk before the next thing that can fail runs.
    # Without that, X posting and wip.co failing loses the X success, and the
    # next press tweets the same standup again.
    saved = []
    st = {"id": "d"}
    def explode():
        raise RuntimeError("wip.co is down")
    # The exception used to be allowed to escape, on the reasoning that the
    # process dying was not the problem. It was: the getUpdates offset is
    # advanced before this loop runs, so a raise here consumed the press and
    # lost the whole day — and bird runs with timeout=120 and nothing caught it.
    # A poster that raises is a failed destination now, and the ones after it
    # still run.
    lines = publish_pending(
        st, "both",
        (("X", "x", "posted_x", lambda: (True, "https://x.com/i/1")),
         ("wip.co", "wip", "posted_wip", explode)),
        lambda s: saved.append(dict(s)),
    )
    assert st["posted_wip"] is False, st
    assert any("wip.co is down" in l for l in lines), lines
    assert any("RuntimeError" in l for l in lines), lines

    # A timeout is the ambiguous one: the post may well have gone out, and it is
    # recorded as failed and therefore retryable, so the reply has to say so.
    def stall():
        raise subprocess.TimeoutExpired(cmd="bird", timeout=120)
    timed = publish_pending(
        {"id": "t"}, "x", (("X", "x", "posted_x", stall),), lambda s: None)
    assert any("MAY have been" in l for l in timed), timed
    assert saved and saved[0].get("posted_x") is True, \
        "the X success was not written down before wip.co was attempted"
    # posted_wip is False rather than absent now: a poster that raises is a
    # failed destination, which is recorded, reported and retryable. It used to
    # be absent because the raise happened before the write — and took the
    # press with it.
    assert st["posted_x"] is True and st["posted_wip"] is False, st

    # And the ordinary path still records both, and skips what is already done.
    saved.clear()
    st = {"id": "d", "posted_x": True}
    def must_not_run():
        raise AssertionError("a destination already posted must not be posted again")
    lines = publish_pending(
        st, "both",
        (("X", "x", "posted_x", must_not_run),
         ("wip.co", "wip", "posted_wip", lambda: (True, "ok"))),
        lambda s: saved.append(dict(s)),
    )
    assert any("already posted" in l for l in lines), lines
    assert st["posted_wip"] is True and saved[-1]["posted_wip"] is True

    # A destination not asked for is not touched.
    st = {"id": "d"}
    publish_pending(st, "wip",
                    (("X", "x", "posted_x", must_not_run),
                     ("wip.co", "wip", "posted_wip", lambda: (True, "ok"))),
                    lambda s: None)
    assert "posted_x" not in st, st

    # --- LinkedIn ---------------------------------------------------------
    # Armed only when both keys are there. A half-configured destination is
    # worse than an absent one: it offers a button that fails hours later.
    box = pathlib.Path(tempfile.mkdtemp(prefix="standup-buffer-"))
    try:
        assert optional_env(box / "nothing.env", "A") is None
        half = box / "half.env"
        half.write_text("BUFFER_API_KEY=k\n")
        assert optional_env(half, "BUFFER_API_KEY", "BUFFER_LINKEDIN_CHANNEL") is None
        blank = box / "blank.env"
        blank.write_text("BUFFER_API_KEY=k\nBUFFER_LINKEDIN_CHANNEL=\n")
        assert optional_env(blank, "BUFFER_API_KEY", "BUFFER_LINKEDIN_CHANNEL") is None
        full = box / "full.env"
        full.write_text('export BUFFER_API_KEY="k"\nBUFFER_LINKEDIN_CHANNEL=c\n')
        assert optional_env(full, "BUFFER_API_KEY", "BUFFER_LINKEDIN_CHANNEL") == \
            {"BUFFER_API_KEY": "k", "BUFFER_LINKEDIN_CHANNEL": "c"}
    finally:
        shutil.rmtree(box, ignore_errors=True)

    # `all` reaches every destination; `both` reaches the two that existed
    # before LinkedIn, so a press on an older message still does what its label
    # promised; a target nobody serves produces no lines, and main() turns that
    # into a reply rather than an empty sendMessage.
    def served(target, names):
        st = {"id": "s"}
        posters = tuple((n, c, f"posted_{c}", (lambda ok=n: (True, "ok")))
                        for n, c in names)
        return publish_pending(st, target, posters, lambda s: None)
    three = (("X", "x"), ("wip.co", "wip"), ("LinkedIn", "linkedin"))
    assert len(served("all", three)) == 3
    assert [l.split(" — ")[0] for l in served("both", three)] == ["✅ X", "✅ wip.co"]
    assert served("linkedin", (("X", "x"), ("wip.co", "wip"))) == []

    # A destination armed that morning but no longer configured is still in the
    # poster list, as one that fails and says why. Left out, `all` would post to
    # the others and omit it in silence, while the pending file waited for a
    # post nobody was going to attempt.
    # A waiver is not a success: no flag is set, so no green tick and no
    # "already posted, skipped" if the configuration comes back.
    st = {"id": "g", "armed": ["x", "wip", "linkedin"],
          "posted_x": True, "posted_wip": True}
    assert not day_complete(st), "an armed destination cannot just be ignored"
    st["waived"] = ["linkedin"]
    assert day_complete(st), f"a waived destination must not stall the day: {st}"
    assert "posted_linkedin" not in st, "a waiver must not look like a post"

    # The argv the CLI actually receives: a list, never a shell string, with
    # shareNow and the report as one argument however many spaces it has.
    box = pathlib.Path(tempfile.mkdtemp(prefix="standup-buffer-bin-"))
    try:
        stub = box / "buffer"
        stub.write_text("#!/usr/bin/env python3\n"
                        "import json,sys,os\n"
                        "print(json.dumps({'argv': sys.argv[1:],\n"
                        "                  'key': os.environ.get('BUFFER_API_KEY')}))\n")
        stub.chmod(0o755)
        real_buffer = globals()["BUFFER"]
        globals()["BUFFER"] = str(stub)
        try:
            ok, detail = post_to_linkedin(
                "a report\nwith two lines",
                {"BUFFER_API_KEY": "k", "BUFFER_LINKEDIN_CHANNEL": "chan"})
        finally:
            globals()["BUFFER"] = real_buffer
        assert ok, detail
        seen = json.loads(detail)
        assert seen["key"] == "k", "the key reaches the child through its environment"
        argv = seen["argv"]
        assert argv[:3] == ["posts", "create", "--channel-id"], argv
        assert "chan" in argv and "--mode" in argv, argv
        assert argv[argv.index("--mode") + 1] == "shareNow", argv
        assert "a report\nwith two lines" in argv, "the report is one argument"
    finally:
        shutil.rmtree(box, ignore_errors=True)

    # die() raises SystemExit, which is not an Exception. wip_key() calls it.
    def exits():
        die("no wip.co token")
    st = {"id": "e"}
    out = publish_pending(st, "wip", (("wip.co", "wip", "posted_wip", exits),),
                          lambda s: None)
    assert st["posted_wip"] is False, st
    assert any("SystemExit" in l for l in out), out

    # Completion is judged against what was armed that morning, never against
    # the config that exists when the button is finally pressed.
    assert day_complete({"armed": ["x", "wip"], "posted_x": True, "posted_wip": True})
    assert not day_complete({"armed": ["x", "wip", "linkedin"],
                             "posted_x": True, "posted_wip": True})
    assert day_complete({"armed": ["x", "wip", "linkedin"], "posted_x": True,
                         "posted_wip": True, "posted_linkedin": True})
    # A state written before LinkedIn existed has no armed list and must not
    # wait for a post its keyboard never offered.
    assert day_complete({"posted_x": True, "posted_wip": True})
    # And a destination that was armed and then unconfigured is resolved, not
    # pending: "skipped" is truthy, so the file can finally be deleted.
    assert day_complete({"armed": ["x", "wip", "linkedin"], "posted_x": True,
                         "posted_wip": True, "posted_linkedin": "skipped"})

    # --- The 2026-09-09 underscore, both halves of it ---------------------
    subject = "• Build config: dart_defines from production.env"
    assert "dart_defines" in strip_telegram_markup(f"*#alpha*\n{subject}"), \
        "an underscore in an identifier is not italics"
    assert "*" not in strip_telegram_markup("*#alpha*"), "a header's asterisks are markup and do go"
    # But an asterisk in a commit subject is a character somebody committed.
    kept_star = strip_telegram_markup("*#alpha*\n• Rename every *.rb under lib/")
    assert "*.rb" in kept_star, kept_star
    assert "*#alpha*" not in kept_star and "#alpha" in kept_star, kept_star
    assert "\\*\\.rb" in to_markdown_v2(kept_star), "a literal asterisk must be escaped, not dropped"

    tg = to_markdown_v2(f"\U0001F4CB Daily Standup — 2026-09-09\n\n#alpha\n{subject}")
    assert "dart\\_defines" in tg, tg
    assert "*\\#alpha*" in tg, "a project header is bold"
    assert tg.splitlines()[0] == "*\U0001F4CB Daily Standup — 2026\\-09\\-09*", tg.splitlines()[0]
    assert "\\." in tg, "a full stop is reserved in MarkdownV2 and must be escaped"

    # share_header need not begin with 📋 — title is the first non-empty line
    # that is not a project hashtag.
    custom_title = "Morning notes — 2026-09-24"
    custom = f"{custom_title}\n\n#alpha\n{subject}"
    tg_c = to_markdown_v2(custom)
    assert tg_c.splitlines()[0] == f"*{escape_mdv2(custom_title)}*", tg_c.splitlines()[0]
    stripped_c = strip_telegram_markup(f"*{custom_title}*\n\n*#alpha*\n{subject}")
    assert stripped_c.splitlines()[0] == custom_title, stripped_c.splitlines()[0]
    assert stripped_c.splitlines()[2] == "#alpha", stripped_c

    # Every reserved character, including a literal backslash. Four assertions
    # would not support "a commit subject can hold anything".
    for ch in "_*[]()~`>#+-=|{}.!\\":
        rendered = to_markdown_v2(f"• a subject with {ch} in it")
        assert f"\\{ch}" in rendered, f"{ch!r} was not escaped: {rendered!r}"

    # A bullet that opens with "#" is an issue reference, not a header. Headers
    # are re-detected by pattern after markup is stripped, so this is the
    # plausible false positive.
    issue = to_markdown_v2("#alpha\n• #123 was the culprit")
    assert issue.splitlines()[0].startswith("*"), "the header lost its emphasis"
    assert not issue.splitlines()[1].startswith("*"), "a bullet was turned into a header"

    # No idempotence assertion: escaping a raw backslash and treating escaped
    # output as already-escaped are mutually exclusive, and the loop above
    # requires the former.

    # The CLI composes these three, and only the pieces were tested. A header
    # arrives from the formatter as *#alpha*, and must survive stripping and
    # come back bold — with its "#" escaped, which is what the earlier
    # by-hand rendering got wrong.
    cli = to_markdown_v2(fit_telegram(strip_telegram_markup(
        "\U0001F4CB Daily Standup — 2026-09-09\n\n*#alpha*\n• one dart_defines here")))
    assert "*\\#alpha*" in cli, cli
    assert "dart\\_defines" in cli, cli
    assert "**" not in cli, "the formatter's asterisks were not stripped first"

    # --- The length guard --------------------------------------------------
    small = "\U0001F4CB Daily Standup\n\n#alpha\n• one\n\n#beta\n• two"
    assert fit_telegram(small) == small, "a report that fits must not be touched"

    big = "\U0001F4CB Daily Standup\n\n" + "\n\n".join(
        f"#p{i}\n" + "\n".join(f"• a commit subject, number {j}." for j in range(20))
        for i in range(30))
    trimmed = fit_telegram(big)
    rendered = to_markdown_v2(trimmed)
    assert tg_len(trimmed) <= TELEGRAM_LIMIT, tg_len(trimmed)
    assert "trimmed to fit Telegram" in trimmed, "a trim has to say so"
    assert not re.search(r"(?<!\\)\\$", rendered), "the render ends in a dangling escape"
    # Trimming drops whole projects, never half of one.
    assert trimmed.count("#p0") == 1 and "• a commit subject, number 19." in trimmed

    # Escapes and emphasis are entities, not text: a report whose escaped form
    # is over the limit but whose visible text is not must go out untouched.
    dotted = "\U0001F4CB Daily Standup\n\n#alpha\n" + "\n".join(
        "• a subject. with. a lot. of. full. stops." for _ in range(90))
    assert tg_len(dotted) < TELEGRAM_LIMIT < tg_len(escape_mdv2(dotted)), \
        "the fixture must be legal as text and over the limit once escaped"
    assert fit_telegram(dotted) == dotted, \
        "a report Telegram would accept was trimmed because the escape was measured"

    # One block larger than the whole budget still has to come back inside it.
    single = "#solo\n" + "\n".join(f"• subject number {j}." for j in range(600))
    assert tg_len(fit_telegram(single)) <= TELEGRAM_LIMIT

    # The case opencode found: a title, then a first project bigger than the
    # whole budget. The by-line cut used to be unreachable once anything had
    # been kept, so the reader got a title and a marker and no commits.
    fat = ("\U0001F4CB Daily Standup\n\n#alpha\n"
           + "\n".join(f"• subject number {j}, with some words." for j in range(300))
           + "\n\n#beta\n• a later project")
    cut = fit_telegram(fat)
    assert tg_len(cut) <= TELEGRAM_LIMIT, tg_len(cut)
    assert "subject number 0" in cut, "the oversized project was dropped, not cut"
    assert cut.count("•") > 20, f"only {cut.count(chr(8226))} bullets survived"

    # A blank line inside a project is not a project boundary. Splitting on one
    # drops half a project and keeps the rest.
    spaced = ("\U0001F4CB Daily Standup\n\n#alpha\n• one\n\n• two after a blank line\n\n"
              + "\n\n".join(f"#p{i}\n" + "\n".join(f"• filler {j} here." for j in range(40))
                             for i in range(20)))
    trimmed_spaced = fit_telegram(spaced)
    if "#alpha" in trimmed_spaced:
        assert "• two after a blank line" in trimmed_spaced, \
            "a project was split at a blank line and half of it dropped"

    # A header the formatter wrote with double asterisks.
    assert strip_telegram_markup("**#alpha**\n• one").startswith("#alpha"), \
        "a **bold** header kept its asterisks"
    assert "*\\#alpha*" in to_markdown_v2(strip_telegram_markup("**#alpha**\n• one")), \
        "a **bold** header was not re-emphasised"

    # And one LINE longer than the budget is cut, not dropped: dropping it left
    # a message consisting of the trim marker and nothing else.
    overlong = "• " + "a very long subject. " * 400
    fitted = fit_telegram(overlong)
    assert tg_len(fitted) <= TELEGRAM_LIMIT, tg_len(fitted)
    assert "a very long subject" in fitted, "the only line was dropped instead of cut"

    # ---- lead rotation ----------------------------------------------------
    three = ("\U0001F4CB Daily Standup\n\n#alpha\n\u2022 one\n\n"
             "#beta\n\u2022 two\n\n#gamma\n\u2022 three\n\n3 projects")

    def lead_of(rendered):
        for block in re.split(r"\n\s*\n", rendered):
            m = SLOT_HEADER.fullmatch(block.split("\n")[0].strip())
            if m:
                return m.group(1)
        return None

    # A render pins the day and leaves last_led alone: previewing is not publishing.
    _reset_rotation()
    out = {}
    first = shuffle_projects(three, "2026-09-20", lead_out=out)
    decisions, last_led, _ = _read_history()
    assert decisions == {"2026-09-20": out["tag"]}, decisions
    assert last_led == {}, "a render must not spend a turn"
    assert lead_of(first) == out["tag"], (first, out)

    # The same day renders the same order and rewrites nothing.
    lead_path, _ = _lead_files()
    before = lead_path.read_bytes()
    assert shuffle_projects(three, "2026-09-20") == first, "one day, one order"
    assert lead_path.read_bytes() == before, "a repeat render must not rewrite the file"

    # Only record_lead spends the turn, and only for the project handed to it.
    record_lead("2026-09-20", out["tag"])
    _, last_led, _ = _read_history()
    assert last_led == {out["tag"]: "2026-09-20"}, last_led

    # The pin wins even after last_led moves under it. This is the whole
    # preview-equals-publish promise: the 07:30 render decides, and a press
    # hours later — with other days recorded in between — must not re-decide.
    _reset_rotation()
    pinned = {}
    shown = shuffle_projects(three, "2026-09-25", lead_out=pinned)
    record_lead("2026-09-24", pinned["tag"])
    record_lead("2026-09-23", "beta")
    later = {}
    assert shuffle_projects(three, "2026-09-25", lead_out=later) == shown, \
        "a recorded turn must not move a day that was already pinned"
    assert later["tag"] == pinned["tag"], (pinned, later)

    # Yesterday's leader does not lead today.
    second = {}
    shuffle_projects(three, "2026-09-21", lead_out=second)
    assert second["tag"] != out["tag"], (out, second)

    # Over many days everyone leads, and nobody leads twice running.
    _reset_rotation()
    seen, previous = [], None
    for d in range(1, 16):
        day = f"2026-10-{d:02d}"
        got = {}
        shuffle_projects(three, day, lead_out=got)
        record_lead(day, got["tag"])
        assert got["tag"] != previous, f"{day} repeated {previous}"
        previous = got["tag"]
        seen.append(got["tag"])
    assert set(seen) == {"alpha", "beta", "gamma"}, seen

    # A project that is not in today's standup is skipped, and is not marked led
    # even once the day is actually recorded.
    _reset_rotation()
    two = "\U0001F4CB Daily Standup\n\n#alpha\n\u2022 one\n\n#beta\n\u2022 two"
    got = {}
    shuffle_projects(two, "2026-11-01", lead_out=got)
    assert got["tag"] in ("alpha", "beta"), got
    record_lead("2026-11-01", got["tag"])
    _, last_led, _ = _read_history()
    assert "gamma" not in last_led, last_led
    assert last_led == {got["tag"]: "2026-11-01"}, last_led

    # record_lead credits what it is handed, never what the file happens to say.
    # The old shape of this test recorded the same project the pin already
    # named, so a regression to reading decisions[D] would have passed.
    _reset_rotation()
    _write_history({"2026-11-09": "delta"}, {})
    record_lead("2026-11-09", "alpha")
    _, last_led, _ = _read_history()
    assert last_led == {"alpha": "2026-11-09"}, last_led

    # The gate in main(): only a fresh X success on this press spends a turn.
    assert should_record_lead(False, {"posted_x": True}, {"tag": "alpha"})
    assert not should_record_lead(True, {"posted_x": True}, {"tag": "alpha"}), \
        "an already-posted X must not spend the turn again"
    assert not should_record_lead(False, {"posted_wip": True}, {"tag": "alpha"}), \
        "a wip.co-only press must not spend a turn"
    assert not should_record_lead(False, {"posted_x": True}, {}), \
        "no lead captured means nothing to record"

    # Every unusable shape is unusable whole: a fresh history, the old file
    # kept, and a render that still returns every block.
    for broken in ('{ not json',
                   json.dumps({"decisions": [], "last_led": {"alpha": "2026-01-01"}}),
                   json.dumps({"decisions": {}, "last_led": {"alpha": "nope"}}),
                   json.dumps(["not", "a", "dict"]),
                   json.dumps({"decisions": {}}),
                   json.dumps({"last_led": {}}),
                   json.dumps({"decisions": {"2026-99-99": "alpha"}, "last_led": {}}),
                   json.dumps({"decisions": {}, "last_led": {"alpha": "2026-02-30"}})):
        _reset_rotation()
        lead_path, _ = _lead_files()
        lead_path.write_text(broken)
        decisions, last_led, _ = _read_history()
        assert (decisions, last_led) == ({}, {}), (broken, decisions, last_led)
        assert _is_date("2026-09-15") and not _is_date("2026-99-99"), "calendar, not shape"
        rendered = shuffle_projects(three, "2026-11-04")
        assert sorted(HASHTAG.findall(rendered)) == ["alpha", "beta", "gamma"], rendered
        assert list(STATE_DIR.glob("lead-history.json.bad-*")), \
            f"the unusable file must be kept: {broken}"

    # A lock that cannot be taken must read nothing and write nothing. Carrying
    # on unlocked is how two processes overwrite each other.
    _reset_rotation()
    real_flock = fcntl.flock
    fcntl.flock = lambda *a, **k: (_ for _ in ()).throw(OSError("no lock for you"))
    try:
        assert choose_lead(["alpha", "beta"], "2026-11-11", date="2026-11-11") is None
        record_lead("2026-11-11", "alpha")
        assert not lead_path.exists(), "nothing may be written without the lock"
        blind = {}
        rendered = shuffle_projects(three, "2026-11-11", lead_out=blind)
        assert sorted(HASHTAG.findall(rendered)) == ["alpha", "beta", "gamma"], rendered
        assert blind == {}, "no lead may be claimed without the lock"
    finally:
        fcntl.flock = real_flock

    # Never-led beats led-long-ago.
    _reset_rotation()
    _write_history({}, {"alpha": "2020-01-01", "beta": "2020-01-02"})
    got = {}
    shuffle_projects(three, "2026-11-02", lead_out=got)
    assert got["tag"] == "gamma", got

    # last_led never moves backwards: pending_states() can publish an older day
    # after a newer one, and the older date would hand that project the lead again.
    _reset_rotation()
    record_lead("2026-11-10", "alpha")
    record_lead("2026-11-05", "alpha")
    _, last_led, _ = _read_history()
    assert last_led["alpha"] == "2026-11-10", last_led

    # A pin naming a project that is not here today is ignored, not obeyed.
    _reset_rotation()
    _write_history({"2026-11-03": "delta"}, {})
    got = {}
    shuffle_projects(three, "2026-11-03", lead_out=got)
    assert got["tag"] in ("alpha", "beta", "gamma"), got

    # A first line that merely mentions a hashtag is not a project header — and
    # this has to go through shuffle_projects, not just lead_of. A regression to
    # collecting slots with HASHTAG.search would sail past a lead_of-only check.
    _reset_rotation()
    mention = "\U0001F4CB Daily Standup\n\n\u2022 #123 was the culprit\n\u2022 two"
    caught = {}
    assert shuffle_projects(mention, "2026-11-05", lead_out=caught) == mention
    assert caught == {}, caught
    decisions, _, _ = _read_history()
    assert decisions == {}, f"a bullet must not be pinned as a project: {decisions}"
    assert lead_of("\u2022 #123 was the culprit\n\u2022 two") is None

    # The live first line is Telegram-bold, which is what SLOT_HEADER is for.
    _reset_rotation()
    bold = ("\U0001F4CB *Daily Standup*\n\n*#alpha*\n\u2022 one\n\n"
            "*#beta*\n\u2022 two\n\n2 projects")
    got = {}
    rendered = shuffle_projects(bold, "2026-11-12", lead_out=got)
    assert got.get("tag") in ("alpha", "beta"), got
    assert rendered.startswith("\U0001F4CB *Daily Standup*"), rendered
    assert rendered.endswith("2 projects"), rendered
    assert rendered.split("\n\n")[1] == f"*#{got['tag']}*\n\u2022 " + \
        ("one" if got["tag"] == "alpha" else "two"), rendered

    # share_footer and share_header hashtags must not be rewritten for X — only
    # project headers in the body. A footer that is a lone #tag matching a wip
    # project still stays a hashtag.
    _reset_rotation()
    catalog = {
        "alpha": {"hashtag": "alpha", "name": "Alpha", "website_url": "https://alpha.example"},
        "beta": {"hashtag": "beta", "name": "Beta", "website_url": "https://beta.example"},
        "foo": {"hashtag": "foo", "name": "Foo", "website_url": "https://foo.example"},
    }
    mod = sys.modules[__name__]
    real_wip = mod.wip_projects
    mod.wip_projects = lambda: catalog
    try:
        framed = (
            "Building #inpublic — 2026-09-23\n\n"
            "#alpha\n\u2022 one\n\n"
            "1 projects\n\n"
            "#buildinpublic #indiehackers"
        )
        x_text = for_x(framed, "2026-09-23")
        assert x_text.startswith("Building #inpublic — 2026-09-23"), x_text
        assert "Alpha — https://alpha.example" in x_text, x_text
        assert "#buildinpublic #indiehackers" in x_text, x_text
        assert "Indie" not in x_text and "#indiehackers" in x_text, x_text

        lone = (
            "\U0001F4CB Daily Standup\n\n"
            "#alpha\n\u2022 one\n\n"
            "1 projects\n\n"
            "#foo"
        )
        x_lone = for_x(lone, "2026-09-24")
        assert x_lone.rstrip().endswith("#foo"), x_lone
        assert "Foo — https://foo.example" not in x_lone, x_lone
        assert "Alpha — https://alpha.example" in x_lone, x_lone

        # No trailer: a trailing lone hashtag is still footer, not a project.
        no_trail = (
            "\U0001F4CB Daily Standup\n\n"
            "#alpha\n\u2022 one\n\n"
            "#foo"
        )
        x_nt = for_x(no_trail, "2026-09-25")
        assert x_nt.rstrip().endswith("#foo"), x_nt
        assert "Foo —" not in x_nt, x_nt

        # Inline hashtags in bullets stay put.
        bullet = (
            "\U0001F4CB Daily Standup\n\n"
            "#alpha\n\u2022 fixed #foo in logs\n\n"
            "1 projects"
        )
        x_b = for_x(bullet, "2026-09-26")
        assert "fixed #foo in logs" in x_b, x_b
        assert "Alpha — https://alpha.example" in x_b, x_b

        # A share_header that is itself a lone #hashtag must stay a title —
        # not become "Alpha — URL" on X.
        tag_title = (
            "#alpha\n\n"
            "#beta\n\u2022 one\n\n"
            "1 projects"
        )
        x_tt = for_x(tag_title, "2026-09-27")
        assert x_tt.startswith("#alpha"), x_tt
        assert "Alpha —" not in x_tt, x_tt
        assert "Beta — https://beta.example" in x_tt, x_tt
    finally:
        mod.wip_projects = real_wip

    # Footer survives the shuffle in place (after the trailer).
    _reset_rotation()
    with_foot = (
        "\U0001F4CB Daily Standup\n\n"
        "#alpha\n\u2022 one\n\n"
        "#beta\n\u2022 two\n\n"
        "2 projects\n\n"
        "#buildinpublic #indiehackers"
    )
    shuf = shuffle_projects(with_foot, "2026-11-13")
    assert shuf.startswith("\U0001F4CB Daily Standup"), shuf
    assert shuf.endswith("#buildinpublic #indiehackers"), shuf
    assert "2 projects" in shuf, shuf

    # Shapes that must not explode.
    _reset_rotation()
    assert shuffle_projects("#solo\n\u2022 one", "2026-11-06").startswith("#solo")
    assert shuffle_projects("no projects here", "2026-11-07") == "no projects here"

    # A state directory it cannot write still renders, raises nothing, and says
    # nothing on stdout — stdout is the tweet.
    _reset_rotation()
    blocked = STATE_DIR / "afile"
    blocked.write_text("not a directory")
    saved_dir = STATE_DIR
    STATE_DIR = blocked / "nested"
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rendered = shuffle_projects(three, "2026-11-08")
        assert sorted(HASHTAG.findall(rendered)) == ["alpha", "beta", "gamma"], rendered
        assert buf.getvalue() == "", f"nothing may reach stdout: {buf.getvalue()!r}"
    finally:
        STATE_DIR = saved_dir

    # Two processes, two hash seeds, one lead. The tie-break must not follow
    # PYTHONHASHSEED: --preview and the publish are different interpreters, and
    # on a day when every project ties they must still agree. Each child gets its
    # own empty history, or the pin would decide it for them.
    driver = ("import importlib.util,sys;"
              "spec=importlib.util.spec_from_file_location('sp',sys.argv[1]);"
              "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
              "out={};m.shuffle_projects(sys.argv[2],'2026-12-01',lead_out=out);"
              "print(out.get('tag',''))")
    picks = []
    for hash_seed in ("0", "12345"):
        box = tempfile.mkdtemp(prefix="standup-tie-")
        try:
            child = subprocess.run(
                [sys.executable, "-c", driver, __file__, three],
                capture_output=True, text=True, timeout=60,
                env=dict(os.environ, PYTHONHASHSEED=hash_seed, STANDUP_STATE_DIR=box))
            assert child.returncode == 0, child.stderr[:300]
            picks.append(child.stdout.strip())
        finally:
            shutil.rmtree(box, ignore_errors=True)
    assert picks[0] and picks[0] == picks[1], f"the tie-break follows the hash seed: {picks}"

    # ---- private lines ----------------------------------------------------
    # Invented lines, never the real report: this repository is public, the two
    # lines that prompted this were never published, and a fixture would be the
    # first public copy of them.
    def stripped(t):
        return strip_private(t)[0]

    for gone in ("\u2022 bump rubyzip for CVE-2026-11111",
                 "\u2022 also cve-2026-11111 lowercase",
                 "\u2022 Security: remove the demo account",
                 "\u2022 Fixed a security vulnerability",
                 "\u2022 Published an advisory for it",
                 "\u2022 rotate the signing keys",
                 "\u2022 removed hardcoded passwords",
                 "\u2022 stop logging the api key"):
        body = f"\U0001F4CB Daily Standup\n\n#alpha\n\u2022 Tests: ok\n{gone}\n\n1 projects"
        assert gone not in stripped(body), gone
        assert "\u2022 Tests: ok" in stripped(body), gone

    # Anchored and word-bounded, not a substring hunt.
    for kept in ("\u2022 Made the upload more secure",
                 "\u2022 securely stores nothing",
                 "\u2022 Auth: split the exercise lookup"):
        body = f"\U0001F4CB Daily Standup\n\n#alpha\n{kept}\n\n1 projects"
        assert kept in stripped(body), kept

    # A block that loses its last body line loses its header too.
    two = ("\U0001F4CB Daily Standup\n\n*#alpha*\n\u2022 Security: drop it\n\n"
           "*#beta*\n\u2022 Tests: keep it\n\n2 projects")
    out, before, after = strip_private(two)
    assert "alpha" not in out and "*#beta*" in out, out
    assert (before, after) == (2, 1), (before, after)
    assert out.startswith("\U0001F4CB Daily Standup"), out
    assert out.endswith("2 projects"), "a trailer that matches nothing is kept"

    # standup.rb writes the name, a blank line, then the bullets — so the header
    # is its own block. A per-block rule would leave the name behind alone.
    raw = "#alpha\n\n\u2022 bump rubyzip for CVE-2026-11111\n\n#beta\n\n\u2022 Tests: ok"
    out, before, after = strip_private(raw)
    assert "#alpha" not in out, f"orphaned header: {out!r}"
    assert "#beta" in out and (before, after) == (2, 1), (out, before, after)

    # A header is never dropped by a line match. A title is: it has nothing
    # below it to orphan, and the report still composes without one.
    framed = "\U0001F4CB Daily Standup \u2014 security review\n\n#alpha\n\u2022 Tests: ok\n\n1 projects"
    out = stripped(framed)
    assert "security review" not in out, out
    assert out.startswith("#alpha"), out
    assert "\u2022 Tests: ok" in out, out

    # Everything dropped: no projects left, which the shell turns into "send nothing".
    _, before, after = strip_private(
        "\U0001F4CB Daily Standup\n\n#alpha\n\u2022 Security: all of it\n\n1 projects")
    assert (before, after) == (1, 0), (before, after)

    # A report with no project at all is a formatter failure, not a quiet
    # security day, and must not be reported as one.
    assert strip_private("just some prose\n\nand more")[1:] == (0, 0)

    # The trailer's shape is not an exemption. Matching it anywhere made any
    # body line beginning "3 projects ..." immune to every pattern.
    sneaky = ("\U0001F4CB Daily Standup\n\n#alpha\n"
              "\u2022 3 projects now authenticate with the api key\n\u2022 Tests: ok\n\n1 projects")
    assert "api key" not in stripped(sneaky), stripped(sneaky)

    # Hyphens, bold labels, and a word that only looks like one of the stems.
    assert "signing-key" not in stripped(
        "\U0001F4CB T\n\n#alpha\n\u2022 rotate the signing-key\n\u2022 Tests: ok\n\n1 projects")
    assert "drop it" not in stripped(
        "\U0001F4CB T\n\n#alpha\n\u2022 *Security:* drop it\n\u2022 Tests: ok\n\n1 projects")
    assert "secretary" in stripped("\U0001F4CB T\n\n#alpha\n\u2022 ask the secretary\n\n1 projects")

    # Every header shape repo_name_mapping allows must be recognised, or a valid
    # report is refused as having no projects at all.
    for shape in ("#alpha", "#My-App", "#my_app", "*#Alpha*", "#3thingsaday"):
        one = f"\U0001F4CB T\n\n{shape}\n\u2022 Tests: ok\n\n1 projects"
        assert strip_private(one)[1:] == (1, 1), (shape, strip_private(one)[1:])

    # Qualified only: an unqualified injection or leak is an ordinary commit,
    # and dropping a project's only bullet would fake a quiet day.
    for kept in ("\u2022 Fixed a memory leak", "\u2022 Refactor dependency injection",
                 "\u2022 vulgar language filter"):
        one = f"\U0001F4CB T\n\n#alpha\n{kept}\n\n1 projects"
        assert kept in stripped(one), kept
    for gone in ("\u2022 Fix the SQL injection", "\u2022 a credential leak in logs",
                 "\u2022 patch the vuln in upload", "\u2022 a zero-day in the parser"):
        one = f"\U0001F4CB T\n\n#alpha\n\u2022 Tests: ok\n{gone}\n\n1 projects"
        assert gone not in stripped(one), gone

    # The 2026-09-16 leak: the word with no identifier, the plural, and work
    # described as hardening rather than as a fix.
    for gone in ("\u2022 Fixed rubyzip CVE",
                 "\u2022 Upgraded Rails with CVE fixes",
                 "\u2022 fixed two CVEs this week",
                 "\u2022 Hardened CI gate with Ruby requirement",
                 "\u2022 more hardening of the gem audit",
                 "\u2022 improved security of the upload path"):
        one = f"\U0001F4CB T\n\n#alpha\n\u2022 Tests: ok\n{gone}\n\n1 projects"
        assert gone not in stripped(one), gone
    # The identifier form still matches, and ordinary words still survive.
    assert "CVE-2026-11111" not in stripped(
        "\U0001F4CB T\n\n#alpha\n\u2022 bump for CVE-2026-11111\n\u2022 Tests: ok\n\n1 projects")
    for kept in ("\u2022 Made the upload more secure", "\u2022 ask the secretary",
                 "\u2022 Analytics: fixed the funnel ordering"):
        one = f"\U0001F4CB T\n\n#alpha\n{kept}\n\n1 projects"
        assert kept in stripped(one), kept

    # A trailer that matches is dropped, on both exemption paths: separated by a
    # blank line, and glued to the last project.
    sep = ("\U0001F4CB T\n\n#alpha\n\u2022 Tests: ok\n\n"
           "3 projects \u2014 security hardening, CI improvements")
    assert "security hardening" not in stripped(sep), stripped(sep)
    assert "\u2022 Tests: ok" in stripped(sep), stripped(sep)
    glued = "\U0001F4CB T\n\n#alpha\n\u2022 Tests: ok\n3 projects \u2014 security hardening"
    assert "security hardening" not in stripped(glued), stripped(glued)
    assert "\u2022 Tests: ok" in stripped(glued), "an empty result would pass the line above"

    # A title-less report — its title was dropped — still trims for Telegram.
    titleless = strip_private(
        "\U0001F4CB Daily Standup \u2014 security review\n\n#alpha\n\u2022 Tests: ok")[0]
    assert titleless.startswith("#alpha"), titleless
    assert fit_telegram(titleless) == titleless, "a short title-less report must not be trimmed"
    assert to_markdown_v2(titleless), "a title-less report still composes"

    # And one that does not fit. The by-line path used to assume a report always
    # had a title, so "nothing kept yet" meant "still on the first block". A
    # dropped title breaks that, and the first block is now a project header.
    fat = ("\U0001F4CB Daily Standup \u2014 security review\n\n#alpha\n"
           + "\n".join(f"\u2022 a commit subject, number {n}." for n in range(400)))
    trimmed = fit_telegram(strip_private(fat)[0])
    assert tg_len(trimmed) <= TELEGRAM_LIMIT, tg_len(trimmed)
    assert trimmed.startswith("#alpha"), trimmed[:60]
    assert "security review" not in trimmed, "the title was dropped before trimming"
    assert "\u2022 a commit subject, number 0." in trimmed, "it must keep what it can"

    # A kept trailer glued to a project must not keep that project alive once
    # its own bullets are gone — and must not be dragged down with it either.
    dead = "\U0001F4CB T\n\n#alpha\n\u2022 Fixed rubyzip CVE\n2 projects"
    out, before, after = strip_private(dead)
    assert (before, after) == (1, 0), (out, before, after)
    assert "#alpha" not in out, out
    assert "2 projects" in out, f"a frame line is not collateral: {out!r}"

    # A report with no count line at all: the last bullet is the last non-empty
    # line, and must NOT be taken for a trailer. It would earn its project no
    # body and the project would be dropped as empty. Both report shapes.
    for countless in ("\U0001F4CB T\n\n#alpha\n\u2022 Tests: ok",
                      "#alpha\n\n\u2022 Tests: ok",
                      "#alpha\n\n\u2022 only one"):
        out, before, after = strip_private(countless)
        assert (before, after) == (1, 1), (countless, before, after)
        assert "#alpha" in out and out.strip().endswith(countless.strip().split("\n")[-1]), out

    # A glued trailer that matches nothing stays, and so does its project.
    alive = "\U0001F4CB T\n\n#alpha\n\u2022 Tests: ok\n2 projects"
    out, before, after = strip_private(alive)
    assert (before, after) == (1, 1), (out, before, after)
    assert "#alpha" in out and "\u2022 Tests: ok" in out and out.endswith("2 projects"), out

    # The 2026-09-16 shape end to end: three projects that did nothing but
    # security work vanish, the others are untouched, and the count line goes
    # because it describes the work that was removed. Survivors named, not
    # counted — the count line was already unreliable before any of this.
    incident = (
        "\U0001F4CB *Daily Standup*\n\n"
        "*#keeper*\n- Overhauled account handling and analytics\n\n"
        "*#wallpapers*\n- Hardened CI gem check gate, fixed rubyzip CVE\n"
        "- Hardened CI gate with Ruby requirement\n\n"
        "*#second*\n- Analytics fixes for transition tracking\n\n"
        "*#mygoo*\n- Fixed rubyzip CVE\n\n"
        "*#phototoss*\n- Upgraded Rails with CVE fixes\n\n"
        "5 projects \u2014 security hardening, CI improvements")
    out, before, after = strip_private(incident)
    assert (before, after) == (5, 2), (before, after)
    for survivor in ("*#keeper*", "- Overhauled account handling and analytics",
                     "*#second*", "- Analytics fixes for transition tracking"):
        assert survivor in out, (survivor, out)
    for gone in ("*#wallpapers*", "*#mygoo*", "*#phototoss*", "CVE", "Hardened",
                 "security hardening"):
        assert gone not in out, (gone, out)

    # A header carrying a matching word is exempt, or its bullets would orphan.
    hdr = "\U0001F4CB T\n\n#security\n\u2022 Tests: ok\n\n1 projects"
    assert "#security" in stripped(hdr), stripped(hdr)

    # The filter runs before the rotation, so a project stripped to nothing
    # cannot be handed the lead slot.
    _reset_rotation()
    mixed = ("\U0001F4CB Daily Standup\n\n*#alpha*\n\u2022 Security: gone\n\n"
             "*#beta*\n\u2022 Tests: ok\n\n2 projects")
    picked = {}
    shuffle_projects(strip_private(mixed)[0], "2027-01-01", lead_out=picked)
    assert picked.get("tag") == "beta", picked

    # --strip-private says everything it has to say on stderr. stdout is the
    # report, and daily-standup.sh publishes whatever lands there.
    child = subprocess.run(
        [sys.executable, __file__, "--strip-private"],
        input="\U0001F4CB T\n\n#alpha\n\u2022 Security: all of it\n\n1 projects",
        capture_output=True, text=True, timeout=60)
    assert child.returncode == 2, child.returncode
    assert child.stdout == "", f"stdout must stay clean: {child.stdout!r}"
    assert "dropped" in child.stderr, child.stderr

    # A report with no project at all exits 1, not 2. Checked through the CLI,
    # because swapping the two guards would still pass a function-level test.
    child = subprocess.run(
        [sys.executable, __file__, "--strip-private"],
        input="just some prose\n\nand more", capture_output=True, text=True, timeout=60)
    assert child.returncode == 3, child.returncode
    assert child.stdout == "", f"stdout must stay clean: {child.stdout!r}"

    # And the ordinary path really does put the report on stdout.
    child = subprocess.run(
        [sys.executable, __file__, "--strip-private"],
        input="\U0001F4CB T\n\n#alpha\n\u2022 Security: gone\n\u2022 Tests: ok\n\n1 projects",
        capture_output=True, text=True, timeout=60)
    assert child.returncode == 0, child.stderr[:200]
    assert "Tests: ok" in child.stdout and "Security: gone" not in child.stdout, child.stdout

    # The vocabulary the first list missed.
    for gone in ("\u2022 patch the vuln in upload", "\u2022 fix the SQL injection",
                 "\u2022 rotate the JWT token", "\u2022 a zero-day in the parser",
                 "\u2022 Security: no bullet marker needed",
                 "\u2022 **Security:** double bold"):
        body = f"\U0001F4CB T\n\n#alpha\n\u2022 Tests: ok\n{gone}\n\n1 projects"
        assert gone not in stripped(body), gone

    print("selftest ok")


def main():
    # The morning message shows the wip.co form, where the hashtags do the
    # attaching. What goes to X is a different text, and approving a text you
    # cannot see is not approving anything: --preview renders it.
    if "--preview" in sys.argv:
        pending = sys.argv[sys.argv.index("--preview") + 1]
        state = json.loads((STATE_DIR / f"pending-{pending}.json").read_text())
        print(for_x(strip_telegram_markup(state["text"]), pending))
        return

    # Reads the report on stdin and writes what Telegram should receive. Kept
    # out of the state file on purpose: X and wip.co get the unescaped text, and
    # backslashes are not prose.
    if "--telegram-markdown" in sys.argv:
        sys.stdout.write(to_markdown_v2(fit_telegram(strip_telegram_markup(sys.stdin.read()))))
        return

    # The same text, trimmed but not escaped: what the plain-text retry should
    # send. Without it a report over the limit plus a renderer failure means no
    # message arrives at all, which is the busy day fit_telegram exists for.
    if "--telegram-plain" in sys.argv:
        sys.stdout.write(fit_telegram(strip_telegram_markup(sys.stdin.read())))
        return

    if "--selftest" in sys.argv:
        selftest()
        return

    # Reads the formatted report on stdin and the raw one from RAW_STANDUP, so
    # neither has to survive argv quoting. Exits 1 when a project went missing,
    # which is daily-standup.sh's signal to publish the raw report instead.
    # Reads the report on stdin. 0 = filtered report on stdout, 2 = nothing of
    # it survived, anything else = it failed. daily-standup.sh must treat every
    # non-zero as "send nothing": a privacy filter that fails open is not one.
    if "--strip-private" in sys.argv:
        stripped, before, after = strip_private(sys.stdin.read())
        if not before:
            # No project headers at all. That is a formatter failure, not a
            # quiet security day, and reporting it as one would hide it.
            # 3, not 1: a crashed interpreter also exits 1, and the two must
            # not collapse into one message again.
            warn("the report has no project blocks; refusing to publish it")
            sys.exit(3)
        if not after:
            warn("every project was dropped by the private-line filter")
            sys.exit(2)
        sys.stdout.write(stripped)
        return

    if "--repair-headers" in sys.argv:
        repaired, missing = repair_headers(os.environ.get("RAW_STANDUP", ""),
                                           sys.stdin.read())
        if missing:
            log(f"ERROR: the formatter dropped {', '.join('#' + t for t in missing)}")
            sys.exit(1)
        sys.stdout.write(repaired)
        return

    token = read_env(TELEGRAM_ENVS, "TELEGRAM_BOT_TOKEN")

    states = {s.get("id"): (p, s) for p, s in pending_states()}
    if not states:
        return  # nothing waiting; say nothing, this runs every few minutes

    offset = 0
    if OFFSET_FILE.exists():
        offset = int(OFFSET_FILE.read_text().strip() or 0)

    updates = telegram(token, "getUpdates", offset=offset, timeout=0,
                       allowed_updates=json.dumps(["callback_query"]))
    if updates:
        OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
        OFFSET_FILE.write_text(str(updates[-1]["update_id"] + 1))

    for update in updates:
        cb = update.get("callback_query")
        if not cb:
            continue
        data = cb.get("data", "")
        if not data.startswith("publish:"):
            continue
        # publish:<id>:<target>, where target is x, wip, or both. Separate
        # buttons exist so a day already posted to one place can still be sent
        # to the other without risking a duplicate on the first.
        parts = data.split(":")
        key = parts[1]
        target = parts[2] if len(parts) > 2 else "both"
        if key not in states:
            ack(token, cb["id"], "Quel messaggio non è più in attesa.")
            continue

        path, state = states[key]
        text = strip_telegram_markup(state["text"])
        ack(token, cb["id"], "Pubblico…")

        # Each destination is remembered on its own, the moment it succeeds, so
        # a half-done day — X posted, wip.co refused — is retryable without
        # tweeting it twice.
        # The hashtag that led, caught on the way past. It cannot be read back
        # out of the finished text: for_x has already replaced every hashtag
        # with a project name and a URL by then.
        lead = {}
        x_before = bool(state.get("posted_x"))
        linkedin = linkedin_config()

        # Rendered on first use, not up front: for_x fetches the wip.co project
        # list and takes the rotation lock, and a wip.co-only press should do
        # neither. Cached, so X and LinkedIn cannot receive different bytes if
        # that fetch fails on a second call.
        public = {}

        def render_public():
            if "text" not in public:
                public["text"] = for_x(text, key, lead_out=lead)
            return public["text"]

        def post_x():
            ok, detail = post_to_x(render_public())
            # Recorded here rather than after the loop. publish_pending saves
            # posted_x the moment X succeeds, so anything that raises later —
            # the state write, the next destination — would skip the record,
            # and the next press sees posted_x and never posts X again. The
            # turn would be lost with nothing to show for it.
            try:
                if should_record_lead(x_before, {"posted_x": ok}, lead):
                    record_lead(key, lead["tag"])
            except Exception as e:  # noqa: BLE001 - the tweet is already live
                warn(f"could not record the lead rotation: {e}")
            return ok, detail

        posters = [("X", "x", "posted_x", post_x),
                   ("wip.co", "wip", "posted_wip", lambda: post_to_wip(text))]
        # Armed that morning, not configured right now. If buffer.env went away
        # in between, LinkedIn still belongs in this list — as a destination
        # that fails and says why. Leaving it out instead made `all` post to X
        # and wip.co and omit LinkedIn in silence, while the pending file waited
        # for a post nobody was going to attempt.
        # Armed that morning, never "configured right now": a press on a state
        # whose keyboard never offered LinkedIn must be refused, not quietly
        # served, for the same reason `both` does not reach a third destination.
        armed_today = state.get("armed") or list(LEGACY_BOTH)
        if "linkedin" in armed_today and linkedin:
            posters.append(("LinkedIn", "linkedin", "posted_linkedin",
                            lambda: post_to_linkedin(render_public(), linkedin)))

        lines = publish_pending(
            state, target, tuple(posters),
            lambda s: path.write_text(json.dumps(s, ensure_ascii=False, indent=2)),
        )

        # Armed this morning, unconfigured by the time the button was pressed.
        # Waived rather than posted: no green tick for a post that never
        # happened, and no "already posted, skipped" if the file comes back.
        # Waiving rather than leaving it pending, because a destination that no
        # longer exists cannot be waited for — the file would be re-read on
        # every tick for ever and the button would stay dead on the message.
        if "linkedin" in armed_today and not linkedin and \
                (target in ("linkedin", "all")):
            waived = state.setdefault("waived", [])
            if "linkedin" not in waived:
                waived.append("linkedin")
                path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
            lines.append(f"⚠️ LinkedIn — skipped: {BUFFER_ENV} no longer carries both "
                         "keys, so it was not attempted")

        # An empty result means the target matched nothing we can post to — a
        # linkedin press with no buffer.env, or a target from a newer sender.
        # sendMessage refuses empty text with a 400, and telegram() dies on it,
        # after the offset was already advanced. Another lost press.
        if not lines:
            lines = [f"❌ nothing to publish for target {target!r}: "
                     "that destination is not configured on this machine"]

        log(f"[{key}] " + " | ".join(lines))

        # Complete when everything ARMED THAT MORNING has posted. Deciding it
        # from the config that exists now would never complete a day whose
        # message predates LinkedIn, and pending_states() would re-read that
        # file on every tick for ever.
        if day_complete(state):
            path.unlink(missing_ok=True)

        telegram(token, "sendMessage", chat_id=state["chat_id"],
                 reply_to_message_id=state["message_id"],
                 text="\n".join(lines))


if __name__ == "__main__":
    main()
