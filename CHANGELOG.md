# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Report config: one file, or set `STANDUP_CONFIG`.** When both
  `~/.standup.yml` and `<repo>/standup.yml` exist and `STANDUP_CONFIG` is
  unset, the morning publisher refuses to run (AMBIGUOUS) instead of silently
  preferring home. An empty `STANDUP_CONFIG=` is treated like unset. Successful
  runs still log `using config: …`.

### Fixed

- **The standup could be sent and then leave a button that did nothing.** The
  message id and chat id were pulled out of Telegram's response with unguarded
  command substitutions, under `set -e`. A response Telegram accepted but shaped
  unexpectedly — `"ok":true` with no `result.message_id` — killed the script
  there, *after* the report was already in the chat: no pending state was
  written, so both publish buttons were dead, and the log carried neither a
  "sent" line nor an error. Parsing and the state write are now one guarded
  step, and a failure says plainly that the report went out but its buttons will
  not work.

- **The report crashed on any day with a non-ASCII commit subject, when run
  without a locale.** Ruby tags bytes from `git` with the locale's encoding;
  with no `LANG` — which is what cron provides — that is US-ASCII, and the first
  accented character raises *"invalid byte sequence in US-ASCII"*. A Romanian or
  Italian commit subject was enough to kill the whole standup. It survived only
  because the wrapper script sources a shell profile that happens to set `LANG`:
  protection by accident, and none at all for anything invoking `standup.rb`
  directly. Git output, the config and `llm-context.md` are now read as UTF-8
  regardless of the environment, and a stray byte is replaced with `?`
  rather than killing the day's report. The config is the exception: it is read
  exactly or refused, because a repaired byte inside an `exclude_repos` entry
  changes the name, the entry stops matching, and the repository it was written
  to hide gets published.

- **One underscore could break the whole Telegram message.** Legacy Markdown has
  no escape character, so a single unpaired `_` anywhere in the report is a
  syntax error for all of it. On 2026-09-09 the commit subject `Build config:
  dart_defines from production.env` carried exactly one; Telegram answered
  *"Can't find end of the entity starting at byte offset 896"* — the byte of that
  underscore — and the send fell back to plain text, every asterisk showing raw
  and nothing bold. The report now goes out as MarkdownV2, escaped here rather
  than hoped for, with the emphasis added afterwards. A commit subject can hold
  any character.
- **The X and wip.co text lost underscores too.** Stripping Telegram markup
  deleted every `_`, so that same subject would have been published as
  `dartdefines`. Only asterisks are stripped now.
- **A long day could exceed Telegram's 4096-character limit**, and escaping only
  inflates the text. The message is trimmed on whole project blocks, before
  escaping so a cut cannot split an escape pair, and says that it was trimmed.
  The published text stays complete.

### Added

- **The publisher lives here now.** `bin/daily-standup.sh` and
  `bin/standup-publish.py` turn the report into a Telegram message with an
  approval button, and post to X and wip.co once it is pressed. They worked for
  months from an unversioned directory, which meant two thirds of the project
  had no history and no review, and anyone cloning this repository got a report
  printer while the README told them publishing was "a few lines of shell". They
  are imported unchanged and then de-hardcoded: nothing names a home directory
  any more, and every path is resolved or set by environment variable.
- **`bin/daily-standup.sh --check`**, which prints every path it resolved and
  sends nothing. Nothing else answered that question without posting a real
  message, and a cron job with a short `PATH` fails in exactly the way an
  interactive shell hides.
- **Continuous integration.** The suites existed; nothing ran them but a person.
- **`test_privacy.rb`**, which fails on a committed credential, an absolute home
  directory, or a private repository name. This repository is public and its
  pipeline posts publicly. The example config and the README did once carry real
  project names, and removing them meant rewriting published history.

### Changed

- **A missing `standup.yml` is now refused, not defaulted.** That file is
  gitignored, so a fresh clone has none, and `standup.rb` answers a missing
  config with an empty one. The report would then have no `exclude_repos` — and
  this pipeline publishes. Every repository under the projects root would have
  been named, by directory name, in public.

## [1.0.0] — 2026-09-02

First tagged release. `standup` had been working for months, and reporting a
fraction of the truth for most of them. This release is the day that was found
and fixed.

### Fixed

- **The report only saw the checked-out branch.** `git log` ran with no ref
  scope, so it read whichever branch each repository happened to be sitting on.
  Work done in a worktree, or on a feature branch, or merged through a pull
  request without pulling `main` back down, was invisible. The failure was
  silent: a day with 83 commits across twelve repositories reported *"no
  commits"*. It now reads `--branches --remotes`, so the day counts wherever its
  branch lives.
- **`repo_name_mapping` doubled as an allowlist.** A repository missing from the
  map was dropped before the scan, so six active repositories were invisible
  because nobody had remembered to add them. The map now only renames.
- **`git config user.name` was interpolated into a shell string.** A repository
  configured with a name holding `$(...)` or a backtick executed it. Every git
  call now passes its arguments as `argv` through `Open3`, and nothing reaches a
  shell.
- **`--author` was read as a regular expression.** A name holding `[` matched a
  character class and found nothing. Matching is literal now.
- **A repository with no `user.name` reported everyone else's work as yours.**
  An empty `--author=` is not a narrow filter; it matches every commit. The
  commits of such a repository, and the `llm-context.md` change detection that
  uses the same filter, are now left out. Dated `llm-context.md` entries are
  still reported from the checkout, since they carry no author to filter on.
- **A failing `git` became a quiet empty day.** Errors were discarded. A git
  that has something to say now says it on stderr.

### Added

- **`exclude_repos`** — the repositories to keep out of the report, for when it
  is published somewhere public. A list of what to hide rather than what to
  show, so a repository created next month appears on its own. It matches the
  directory name rather than the display name, refuses anything that is not a
  list of names, and warns when an entry matches nothing, because that typo
  otherwise looks like success while the repository stays visible.
- **`--verbose`** — the repository scan lines, which used to print
  unconditionally and land in the report.
- **`test_standup.rb`** — a regression suite with no framework. It builds
  throwaway repositories for each situation that actually bit, and every
  assertion was checked against the mutation it exists to catch.

### Changed

- Merge commits are excluded. *"Merge pull request #51"* is not a standup line.
- The stash is excluded. `--all` walks `refs/stash`, so a stash made that day
  arrived as `index on main: …` dressed up as work.
- Output is bullets and blank lines between projects, rather than banners and
  indentation, so it reads on a phone and pastes into a post.
- `llm-context.md` history is read across every branch, so a file that exists
  only on a feature branch still counts as work.

[1.0.0]: https://github.com/hamen/standup/releases/tag/v1.0.0
