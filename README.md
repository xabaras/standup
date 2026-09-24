<p align="center">
  <img src=".github/banner.svg" alt="standup — yesterday's commits, every repository, every branch" width="880">
</p>

<p align="center">
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-f5c518"></a>
  <img alt="Ruby 3.x" src="https://img.shields.io/badge/ruby-3.x-8fb7a0">
  <img alt="The reporter has no dependencies" src="https://img.shields.io/badge/reporter-no%20dependencies-9aa7b8">
</p>

A single Ruby file that reads the Git repositories sitting in your projects
directory and prints what you actually shipped yesterday, grouped by project.

That file is the reporter, and it needs nothing but Ruby and `git`. Alongside it
in `bin/` there is an optional publisher, which turns the report into a Telegram
message you approve with a button before anything is posted to X or wip.co. It
is described at the end, and you can ignore it entirely.

It looks one level down — `~/code/my-app/.git`, not `~/code/work/my-app/.git` —
so repositories filed inside a subdirectory are not picked up.

```console
$ standup
#weathercast
• Retry the forecast fetch once before falling back to cache
• Drop the hourly chart below 400px, it was unreadable

#ledgerly
• Reconcile split transactions against the imported statement
• 📝 2026-09-01 – Chose SQLite over Postgres for the desktop build
```

No dependencies, no daemon, no account. Ruby and `git`.

## Optional: publish to LinkedIn

The companion can post to LinkedIn through the [Buffer CLI](https://developers.buffer.com/guides/cli.html),
alongside X and wip.co. It is off unless you configure it.

```bash
npm install -g @bufferapp/cli
```

Then put both values in `~/.config/standup/buffer.env`, mode `600`:

```
BUFFER_API_KEY=<from https://publish.buffer.com/settings/api>
BUFFER_LINKEDIN_CHANNEL=<the channel id from: buffer channels list --organization-id <org>>
```

**Both keys are required.** A file with only one does not arm the destination, because a button that
fails hours after you press it is worse than a button that was never offered. `daily-standup.sh --check`
tells you which state you are in.

**Check where `buffer` actually lands.** `npm install -g` puts it in npm's global prefix, which is
not always on cron's PATH — and is not necessarily the same place your other global CLIs live. Run
`daily-standup.sh --check`: if it reports the binary as MISSING, put `BUFFER_BIN=/full/path/to/buffer`
on the cron line, the same way `BIRD_BIN` is already there. The publisher falls back to
`~/.npm-global/bin/buffer`, which is a guess and only right for one kind of npm setup.

With it configured the morning message carries a LinkedIn button and a `Tutti` button; without it,
the keyboard is unchanged. The post goes out with `--mode shareNow`, so it publishes immediately
rather than joining a Buffer queue — the press is the approval, and a post appearing at some later
slot is not what the button promised.

LinkedIn receives the same text as X: project names and website links rather than hashtags, which
are wip.co's attach mechanism and read as noise anywhere else.

Note that a Buffer API key reaches **every organisation and channel on your account** — Buffer has no
per-organisation scoping — so treat it like the other credentials here.

## Why it is not `git log`

Because `git log` reads the branch your checkout happens to be sitting on, and
that is usually not where the day's work is.

If you use worktrees, or feature branches, or you merge through pull requests
and rarely pull `main` back down, then a plain `git log` in each repository
reports a fraction of your day — silently, which is the worst part. On the day
this was found, the tool reported *"no commits"* for a day with **83 commits
across twelve repositories**.

`standup` reads `--branches --remotes` instead, so the work counts wherever its
branch lives. It also:

- skips merge commits, because *"Merge pull request #51"* is not a standup line
- skips the stash, which `--all` would otherwise walk into
- matches your author name literally, so a name holding `[` or `(` still works
- leaves out the commits of a repository that has no `user.name`, since an empty
  author filter matches **everyone's** commits. A dated `llm-context.md` entry
  in such a repository is still reported: it carries no author to filter on.

## Install

```bash
git clone https://github.com/hamen/standup.git
cd standup
chmod +x standup.rb
./standup.rb --help
```

Put it on your `PATH` if you want it everywhere:

```bash
ln -s "$PWD/standup.rb" ~/.local/bin/standup
```

## Usage

```bash
standup                        # yesterday (the default)
standup --today                # today
standup --projects-root ~/work # somewhere other than the configured root
standup --config ~/.standup.yml
standup --verbose              # say where it is looking, and how much it found
standup --help
```

## Configuration

Configuration is optional. With none at all, `standup` looks in `~/code`, then
`~/projects`.

**Which file it reads.** With `--config PATH`, that file and nothing else — if
it is not there, `standup` runs with no configuration rather than falling back.
Without the flag, the first of `~/.standup.yml` and `./standup.yml` that exists.

The Telegram publisher (`bin/daily-standup.sh`) reads `<repo>/standup.yml`
unless `STANDUP_CONFIG` points elsewhere. It still refuses to run without a
usable config — with no config there is no `exclude_repos`, and every repository
under your projects root would be published by directory name.

**Where it looks for repositories.** The first of these that is set:

1. `--projects-root PATH`
2. `projects_root` in the config file
3. `STANDUP_PROJECTS_ROOT` in the environment
4. `~/code`, then `~/projects`

```yaml
projects_root: ~/code

# Directory name -> the name the report prints. Hashtags are the common case.
repo_name_mapping:
  habit-tracker: "#habittracker"
  ebook-finder: "#ebookfinder"
  daily-journal: "#dailyjournal"

# Repositories to keep out of the report entirely.
exclude_repos:
  - some-client-work
  - unannounced-side-project

# Title, footer, and which LLM CLI formats the morning message.
# share_header: "📋 Building in public — {date}"
# share_footer: "#buildinpublic #indiehackers"
# formatter: cursor
# formatter_model: composer-2
```

### `repo_name_mapping`

Renames a repository in the output. That is *all* it does — it never decides
which repositories are scanned.

For the Telegram → X / wip.co pipeline, each published value must be a single
wip.co project hashtag on its own (e.g. `#myfoodmate`). A display title such as
`My Food Mate - myfoodmate.net` is fine for local reading, but the publisher
will refuse the report: it only recognises `#hashtag` project headers.

### `exclude_repos`

Names the repositories to hide. Deliberately a list of what to **hide** rather
than what to show: a repository you create next month appears on its own, and
hiding one stays a decision instead of becoming something you forget.

It matches the directory name, not the display name, so renaming a project
cannot quietly un-hide it. An entry that matches no repository is a warning on
stderr, because that typo does not look like a mistake — the repository it was
meant to hide is simply reported as usual.

### `share_header`

The title line of the morning message and of every published copy. `{date}` is
replaced with `YYYY-MM-DD`. If omitted, the default is
`📋 Daily Standup — {date}`.

### `share_footer`

Optional text appended after the report body (after the project-count line).
Put only the footer contents in the file — a blank line before it is added
automatically. The same footer goes to Telegram, X, wip.co, and LinkedIn.

### `formatter`

Which CLI summarises the raw standup into the morning message. `claude`
(default) runs Claude Code (`claude -p`); `cursor` runs the Cursor Agent CLI
(`agent -p --mode ask`). Override with `STANDUP_FORMATTER`. Point at a binary
with `FORMATTER_BIN` (or `CLAUDE_BIN` when using Claude).

### `formatter_model`

Model id passed to the formatter. Default `haiku` for Claude; for Cursor, omit
to use the account default. Override with `FORMATTER_MODEL`.

## Publishing the report

A hashtag in a [wip.co](https://wip.co) todo attaches it to that project, which
is exactly the shape `repo_name_mapping` produces. Give every repository you
publish an entry in the map: one without an entry prints its bare directory
name, which attaches to nothing and puts a private-looking name in a public
post.

Keep private work out with `exclude_repos` before you publish anywhere. A
repository name is a small thing to leak and an awkward one to take back.

### Framing the shared post

`share_header` and `share_footer` frame the message on every destination. On X,
only project hashtags in the *body* are rewritten as `Name — website`; the
title and the footer stay as written, so social hashtags belong in
`share_footer` rather than among the project lines.

### The publisher in `bin/`

`standup` itself only reads and prints. What is in `bin/` is the pipeline built
around it, kept here rather than in somebody's home directory so that it has a
history and can be reviewed:

- **`bin/daily-standup.sh`** runs the report, has an LLM summarise it, and sends
  it to Telegram with three buttons — X, wip.co, both.
- **`bin/standup-publish.py`** is run by cron every few minutes. It asks Telegram
  whether a button was pressed, and only then posts: to X with
  a `bird` CLI, to wip.co with `POST /v1/todos`.

**Nothing is published without a press.** An unpressed day leaves a state file
and expires quietly. Each destination is recorded separately, so a day that
reached X but not wip.co can be retried without tweeting it twice.

The two texts differ on purpose. wip.co needs the project hashtags, because that
is what attaches a todo to a project there; on X a row of those is just noise,
so each project header is swapped for that project's name and website, read from
wip.co at publish time. The title (`share_header`) and the footer
(`share_footer`) are left alone on every destination. The X form is sent to you
as a reply, so you approve the text you will actually post.

This half is not dependency-free: it needs `python3`, `curl`, a Claude CLI for
the summary, and `bird` only if you publish to X. `bin/daily-standup.sh` also
uses `readlink -f` to find itself, which is GNU coreutils — it works on Linux,
but not on a stock macOS.

#### Setting it up

```console
$ cp standup.yml.example standup.yml     # who to report on, and who to hide
$ mkdir -p ~/.config/standup
$ cp standup.env.example ~/.config/standup/telegram.env   # then edit it
```

Give the standup a bot of its own rather than sharing one. Telegram hands each
update to whichever process asks first, so two programs polling one bot means
the button looks fine and collects nothing.

For wip.co, put the API key alone in `~/.config/standup/wip-token`.

Credentials live outside the repository. Put the report config in
`<repo>/standup.yml` (gitignored), or set `STANDUP_CONFIG`; the sender
**refuses to run without a usable one** rather than falling back to an empty
default — with no config there is no `exclude_repos`, and every repository under
your projects root would be published by directory name.

Check what it resolved before trusting it to a scheduler:

```console
$ bin/daily-standup.sh --check    # prints every path it found, sends nothing
$ bin/daily-standup.sh --test     # sends one ping, to prove the bot works
```

#### Cron

```cron
# Send the standup. Set CLAUDE_BIN: cron's PATH is short.
30 7 * * * CLAUDE_BIN=/path/to/claude /path/to/standup/bin/daily-standup.sh >> /tmp/daily-standup.log 2>&1

# Collect the button press. Publishes nothing on its own. BIRD_BIN for the
# same reason — a global npm install is not on cron's PATH.
*/5 7-12 * * * BIRD_BIN=/path/to/bird /usr/bin/python3 /path/to/standup/bin/standup-publish.py >> /tmp/standup-publish.log 2>&1
```

Both lines redirect to a log on purpose. A scheduled job that exits non-zero
with nowhere to say so is indistinguishable from a quiet morning.

Everything is overridable by environment variable: `STANDUP_CONFIG`,
`STANDUP_CONFIG_DIR`, `STANDUP_STATE_DIR`, `STANDUP_FORMATTER`, `FORMATTER_BIN`,
`FORMATTER_MODEL`, `CLAUDE_BIN`, `CLAUDE_TOKEN_ENV`, `BIRD_BIN`.

## `llm-context.md`

If a repository contains an `llm-context.md`, `standup` also reports the
date-stamped entries in it:

```text
#### 2026-09-01 – Decided to drop the queue and call it inline
#### 2026‑09‑01 – En dashes in the date work too
```

A file changed on the target date but carrying no entry for it reports as
`llm-context.md was updated`. That check reads every branch as well, so an entry
written on a feature branch still counts.

## Tests

```bash
ruby test_standup.rb
```

One file, no framework. It builds throwaway repositories for the situations
that actually bit — a commit on an unmerged branch, a merge, a stash, a hostile
`user.name`, a repository with no name at all, a broken ref — and asserts the
report says the right thing about each.

## Licence

MIT — see [`LICENSE`](LICENSE).
