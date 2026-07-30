# linkedin-cli

A zero-dependency LinkedIn CLI, built so an AI agent can actually use it.

```
linkedin messages list --unread-only
linkedin messages reply "urn:li:msg_conversation:(...)" --text="on it, thanks"
linkedin feed list --count=10
linkedin notifications list
```

No SDK, no config file, no telemetry — just `python3` and the stdlib. It drives
LinkedIn's internal Voyager API with your own browser session.

> **Read this before running it.** There is no public LinkedIn API behind this.
> It drives the private `/voyager/api/` endpoints the web client uses, with your
> own logged-in session, which is very likely against LinkedIn's terms of service
> and can get an account restricted. Every request payload here was learned by
> watching a real browser, so LinkedIn can change any of it without notice and
> some of it will already be stale. Writes reach real people and most cannot be
> undone. It ships a write ledger, daily caps, a pacer and a circuit breaker
> because those turned out to be necessary, not as decoration — and none of them
> is a guarantee. Use it on an account you can afford to lose, or don't use it.

## What it does

Reads the feed, your conversations and their messages, your notifications, any
profile, and the connection invitations you have been sent. Writes reactions,
comments, messages, posts and connection invitations.

Three of those writes have an inverse today: `unreact` undoes `react`,
`post delete` takes down a post — including one this CLI published — and
`comment delete` removes a comment. Only `invite` has none, and that is not a gap
waiting on work: LinkedIn's invitation manager left the Voyager API for its
server-driven UI, so there is no request body for anyone to record
([the evidence](docs/sdui-migration.md)). Withdrawing an invitation is a browser
action, permanently. The full list of what refuses is under
[Not implemented](#not-implemented), and the CLI refuses those verbs rather than
pretending; `linkedin --help` prints the same list.

Output is a single JSON envelope on stdout, so an agent branches on one shape:

```json
{ "ok": true, "data": [ ... ], "next_cursor": "...", "has_more": false }
{ "ok": false, "error": { "code": "session_expired", "message": "...", "retryable": false } }
```

Exit codes are stable: `0` ok · `2` usage · `3` auth · `4` not found ·
`5` throttled · `6` upstream · `7` stale queryId · `9` blocked.

Two of them cover outcomes whose right responses are opposites, so `error.code`
qualifies the exit code rather than repeating it. Exit `9` is `blocked` — LinkedIn
has flagged the client, stop — or `ledger_unreadable`, which is a local file that
will not parse: reads still work, writes are refused, and the fix is one `mv` that
the message spells out. Exit `6` is `upstream` or `outcome_unknown`, and exit `5`
separates a local cap (`write_quota_exceeded`) from LinkedIn's own answers
(`rate_limited`, `invite_quota_exceeded`).

**No exit code means "retry".** `error.retryable` is the only field that says so,
and it is true only for a throttled read. Every write reports `retryable: false`,
because none of them carries a dedupe token LinkedIn honours.

`--raw` on a failure adds `error.body`: LinkedIn's own response, redacted,
alongside the error rather than instead of it. The exit code does not move. It is
attached only when the recorded body belongs to the request that failed, and never
to a refusal this CLI made by itself — a bad flag, an open breaker, a spent cap, an
unreadable ledger — because those never reached the wire and have no upstream
response to show.

## Read this before you install it

This talks to LinkedIn's **private** API using your own session cookies. That is
contrary to LinkedIn's User Agreement, which prohibits automated access. The
realistic risk is that your account gets restricted, and that risk scales with
how fast you write, not how much you read.

The tool is built accordingly: pacing is enforced *across processes* (a token
bucket in process memory would be meaningless for a CLI), writes are capped per
rolling window, there are no bulk flags anywhere, and an HTTP `999` — LinkedIn's
"you look automated" response — trips a sticky breaker instead of being retried.

Your call. It is your account.

## Install

```bash
uv tool install git+https://github.com/gldc/linkedin-cli
# or
pipx install git+https://github.com/gldc/linkedin-cli
```

There are no Python dependencies. There is a dependency, and it is not Python:

- **Python 3.11+**, stdlib only.
- **A Chromium-family browser on the machine that runs the CLI.** Every API call
  is an in-page `fetch()` inside a browser the supervisor keeps resident, not a
  `urllib` request wearing a copied user-agent — that is the whole point of the
  design, so there is no mode that skips it. `supervisor.resolve_binary` looks in
  a fixed order: `$LINKEDIN_BROWSER_BINARY` if set, then the deployment default
  `/opt/linkedin-cli/browser/chrome`, then the first of `chromium`,
  `chromium-browser`, `google-chrome`, `google-chrome-stable`, `chrome` found on
  `PATH`. An explicit `$LINKEDIN_BROWSER_BINARY` wins even when it names nothing,
  because a silently substituted browser is worse than a launch failure that
  prints the path it was handed. Chromium is tried before Chrome because this
  package launches with `--remote-debugging-pipe`, which both support and several
  Chromium *forks* do not.
- **For `auth seed` only: your own Chrome, running, signed in, with a
  linkedin.com tab open and a remote-debugging port.** The seed reads the live
  cookie jar out of it over CDP; without the port there is nothing to read from.
  This is a one-time step, not a running requirement — see below.

Nothing else needs starting by hand. The first command that needs the managed
browser autostarts the supervisor and waits for it.

## Authentication

`auth seed` copies the session out of *your* Chrome once. Your Chrome has to be
reachable over CDP for the length of that one command, which means starting it
with a debugging port:

```bash
# macOS
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 &
# Linux
google-chrome --remote-debugging-port=9222 &
```

Sign in to LinkedIn in that window and leave the tab open — the seed attaches to
a `linkedin.com` page target, and fails with "no linkedin.com tab is open" if
there is none. Then:

```bash
linkedin auth seed      # copy your live Chrome session into the managed profile
linkedin auth status    # verify it with a real API call
```

**Quit Chrome completely first.** Launching it again while it is already running
hands the flag to the existing process, which opens a window and no port — and
the failure arrives later as a missing `DevToolsActivePort`, which reads like a
bug in this tool.

The port number is never passed in. `auth seed` reads `DevToolsActivePort`, the
file Chrome writes into its own profile directory on startup, so it finds
whichever port was chosen. Which profile it reads is `--from-profile=PATH`, or
`$LINKEDIN_SOURCE_PROFILE`, defaulting to Chrome's own — `~/Library/Application
Support/Google/Chrome` on macOS, `~/.config/google-chrome` on Linux. That profile
is only ever read from: never launched, never written to.

The jar is copied into a separate profile that a resident supervisor owns, and
every API call afterwards is an in-page `fetch()` in *that* browser, so the TLS
fingerprint, the header order and the cookie jar are all a real Chrome's.

Quit the debugging Chrome when the seed is done, and read **"A Chrome debug port
is not a security boundary"** below before deciding to leave it running. The
managed browser deliberately never opens one — it speaks CDP over inherited fds
instead — and this is the one moment the whole design makes an exception.

There is no `auth export`, and no session file: the credential never leaves the
supervisor's process, which is reachable only over a `0600` unix socket inside a
`0700` directory. The CLI can ask that socket for a fetch by name and for the
browser's status — there is no request it can make that returns a cookie.

Sessions rot on their own schedule — `JSESSIONID` rotates from ordinary browsing,
and the CSRF token is derived from it — so `auth status` verifies by making a
real call rather than by trusting an expiry date. When a command reports
`session_expired`, the managed profile is signed out: run `auth seed` again.
Three of those within an hour trip the breaker, and `linkedin doctor
--clear-breaker` is what clears it once the profile is signed in.

## Commands

Every value flag accepts `--flag=value` and `--flag value`. There are no short
flags, by design: an agent composing commands should never have to guess whether
`-c` means `--count` or `--cursor`.

```
linkedin me
linkedin auth seed [--from-profile=PATH]
linkedin auth status
linkedin profile get [<public-id-or-url>]

linkedin feed list [--count=N] [--cursor=C]
linkedin post get <urn>
linkedin post create --text=... [--visibility=ANYONE]
linkedin post delete <urn> --yes
linkedin react <urn> [--type=LIKE]
linkedin unreact <urn>
linkedin comment <urn> --text=...
linkedin comment delete <comment-urn>

linkedin messages list [--unread-only] [--count=N] [--cursor=C]
linkedin messages read <conversation-urn> [--count=N] [--cursor=C]
linkedin messages counts
linkedin messages send (--to=<urn|profile-url> | --conversation=<urn>) --text=...
linkedin messages reply <conversation-urn> --text=...
linkedin messages mark-all-read --yes

linkedin invite <public-id-or-url>
linkedin invitations list [--count=N] [--cursor=C]

linkedin notifications list [--unread-only] [--count=N] [--cursor=C]

linkedin doctor [--clear-breaker]
```

`invite` sends a connection request. It takes the public id or profile URL you
have and resolves it to the member urn the write is addressed by — or takes the
`profile_urn` from `profile get` directly, which is the only form `--dry-run` can
preview, since resolving a public id is itself a request a preview may not make.

It is capped at 15 a day like every other write, but LinkedIn also keeps its own
count: the endpoint is `verifyQuotaAndCreateV2`, and a refusal from it exits `5`
with `invite_quota_exceeded` rather than the generic upstream `6`. That is a real
answer about the account, not a failed request — retrying it is what turns a
spent quota into a restricted account. There is no `--note`: no request body
carrying one has been captured, and the flag is refused rather than dropped, so
a bare invitation never goes out under a flag that promised a note.

`invitations list` reads the invitations you have **received**, from
`relationships/invitationViews?q=receivedInvitation` — verified live
against three agreeing reads. It pages by a numeric offset, so its `next_cursor`
is a number. The limitation worth stating plainly is the sample size: the route
was verified against an inbox holding **zero** invitations, and the populated row
has since been read exactly **once**, against a single real invitation, which is
what the committed fixture and its test pin.

That one read paid for itself. The collection is normalized — the list is
`data["*elements"]`, holding `urn:li:fs_relInvitationView:` *references* whose
entities live in `included`, and the invitation reaches its sender through a
second reference — and the projection written before it had been seen guessed at
flat keys that do not exist. It would have projected every row to nothing.

So the projection is written liberally and still **fails loudly**: a non-empty
page it cannot read raises exit `6` rather than returning an empty list, and one
unreadable row among readable ones is an error too. One invitation is not a
sample that retires that rule — the next shape it meets may be the second kind of
row, and an empty list is indistinguishable from an empty inbox while only one of
those is a bug report. The invitations you have **sent** are a different matter;
see [Not implemented](#not-implemented).

**Sending a message costs one extra request, whichever verb sends it,** and it
buys the only thing that ties a message to a thread this account is in. The
createMessage body carries the conversation urn with no lookup — `reply <urn>`
and `send --conversation=<urn>` build the identical request, and they now reach
it through one function for exactly that reason — so the urn is read back first,
and the answer has to clear two bars or it exits `4` with nothing sent: this
session was served at least one message for it, and every conversation urn in
that answer names **this account's own mailbox**. A conversation urn is
`(<mailbox owner>,2-<thread>)`, so the second bar is what keeps a message inside
this inbox, and it is read off LinkedIn's answer rather than off the argument.
It costs no second request.

The guard used to be on `reply` alone, and `send --conversation=<urn>` — the
byte-identical request one branch down — was reachable with any urn at all.

**`messages send` takes `--to` or `--conversation`, never both.** They name the
destination twice and nothing here can reconcile them: the urn decides where the
message lands on its own, so the recipient was simply dropped, and a `--to` that
contributes nothing reads like a check that happened. Passing both is exit `2`.
`send --conversation=<urn>` and `reply <urn>` are the same send.

What that establishes is read access and the mailbox, not membership: the
messages response carries no participant roster to check an owner against, so a
thread this session cannot see and a thread whose messages were all deleted
answer identically and that refusal names both instead of picking one.
`--dry-run` skips the read, because a preview issues no requests and sends
nothing to confine.

`messages mark-all-read` is the whole mailbox, which is why it wants `--yes`.
LinkedIn's captured payload is `markAllMessagesAsSeen` and carries a timestamp
rather than a conversation, so there is no per-thread read receipt to call.

`post delete` wants `--yes` for the same reason: it removes the post's reactions
and every comment on it too, and LinkedIn offers no undo. No daily cap, no
cooldown and no open circuit breaker can refuse it — a run that publishes and then
trips its own cap would otherwise leave a live public post with nothing in this
CLI able to remove it. It is booked under its own `post.cleanup` budget so that a
publish-and-undo pair does not cost two posts. `post create` does not share any of
that: the exemption is keyed to the sub-verb.

It is not, however, unconditional, and the docs used to say it was. Two things
still refuse an undo, and both are described under
[Write limits](#write-limits): the **cleanup ceiling** (exit `5`), which is the
arithmetic that stops the exemption from being an unmetered write channel, and an
**unreadable ledger** (exit `9`), whose own ceiling is counted out of the file
that will not parse. Anything driving this CLI needs a handler for both, because
the failure arrives while it is holding a live public post.

`comment delete` is the third undo and deliberately does **not** want `--yes`. The
opt-in on `post delete` guards a write that takes a post's whole comment thread
and every reaction on it down with it; this removes one comment this account
wrote. Gating the cheaper half of that trade behind a flag pushes a caller who
cannot spell it toward leaving the mess live, which is the outcome the exemption
exists to prevent. It takes the **comment** urn, not the post's:
`urn:li:fsd_comment:(<commentId>,urn:li:activity:<activityId>)`. `comment` reports
that urn wrapped in a second one (`urn:li:fsd_normComment:…`) because that is what
LinkedIn answers with, and this accepts either spelling — but nothing looser: a
comment id without its activity, or an activity urn, is refused locally, because
deleting the wrong comment is undone by nothing.

`post get` is a verified route: exercised live against a real post
via `q=backendUrnOrNss`. It answers exit `4` when LinkedIn returns the update
entity with nothing in it — no author, no text, no counts — which is what a
deleted post looks like and equally what a post this session may not see looks
like. LinkedIn does not distinguish the two anywhere in the body, so neither does
this. That behaviour is what makes `post get` usable as the read-back after a
`post delete` whose response was unclear.

### Not implemented

These dispatch and then refuse, whatever arguments you give them. They are named
here, and in `linkedin --help`, because a silent omission reads as a typo.

Missing a captured request body — a gap that could close:

```
linkedin notifications mark-read <urn>  not implemented - body never captured
linkedin messages mark-read <urn>       not implemented - renamed mark-all-read
```

Not a gap, and not closeable. LinkedIn's invitation manager has migrated to its
server-driven UI: the screen renders its data into the document and its controls
post protobuf-shaped SDUI actions to `/flagship-web/`, which answers with a React
Server Components stream. This CLI's transport speaks Voyager JSON at
`/voyager/api/` and cannot speak that at all, so there is nothing to record. The
observation is in [docs/sdui-migration.md](docs/sdui-migration.md):

```
linkedin invite withdraw <urn>          not implemented - withdraw in a browser
linkedin invitations sent               not implemented - no Voyager route survives
```

- `messages mark-read` is not implemented and never did what its name says: it
  accepted a conversation urn, dropped it, and marked the *entire* mailbox seen.
  It now refuses and names its replacement instead of obeying.
- The first group has no recorded request body, and this CLI does not ship
  guessed ones — a guessed write is one you find out about from the people who
  received it.
- The second group is worth being blunt about: **another capture run cannot close
  it**, and two attempts to capture this surface each cost a real stranger their
  pending invitation. Any plan that starts "capture the withdraw payload" is a
  plan to repeat that. Read the migration note first.
- Reading the invitations you were *sent* is a probe of a *read*, which is
  legitimate where probing a write is not — thirteen route spellings were tried,
  and the received-side finder that answered is what `invitations list` ships on.
  Seven spellings of a sent-side finder were tried and none of them answered:
  three `404`, four `400`.

### Flags

Global, and read by every command: `--format=json|text` (default json) ·
`--raw` · `--dry-run` · `--rate=N` (0 < N ≤ 1.0, requests per second) ·
`--help`.

Global in the sense that every command *accepts* them, but read by only some:

| flag | read by |
|---|---|
| `--count=N`, `--cursor=C` | `feed list`, `messages list`, `messages read`, `notifications list`, `invitations list` |
| `--idempotency-key=K` | `messages send`, `messages reply` |

Everywhere else those parse and are ignored.

`--idempotency-key` overrides `createMessage`'s `originToken`, which `messages
send` and `messages reply` otherwise derive from the mailbox, the thread and the
text. **It is the escape hatch, not the safety net.** Left unset, an identical
re-run is checked against the newest page of the thread before anything is sent
and will not put a second copy of a reply this account can see it already sent
there — the envelope reports `data.deduped`. Passing the key turns that check off,
which is the point: it is the one way to send the same text into the same thread
twice on purpose. The check is best effort — one page of the newest messages, an
exact text match, and it cannot see a write that landed a moment ago and has not
appeared yet — so read the thread back on an unknown outcome rather than assuming.
There is no unsend. `post create` refuses the flag outright, because its payload
has no field to put a token in at all and no thread to read back.

`--raw` prints the upstream payload instead of the envelope on success, and on a
failure attaches LinkedIn's own response under `error.body` — redacted, beside the
error rather than in place of it, and without moving the exit code. It is the flag
for diagnosing a refusal that arrives inside an HTTP `200`, which is the class that
has no other evidence: a `4xx` already carries its body in the error message, while
a `200` carrying a GraphQL `errors` array gets reduced by the surface to a single
sentence. It is attached only when the recorded body belongs to the failing request,
and never to a refusal this CLI made without asking LinkedIn.

`--verbose` is accepted and does nothing. Nothing in the package reads it.

Per-command flags: `--from-profile` (`auth seed`) · `--text` (`post create`,
`comment`, `messages send`, `messages reply`) · `--visibility` (`post create`) ·
`--type` (`react`) · `--to` **or** `--conversation`, never both (`messages send`) · `--yes`
(`messages mark-all-read`, `post delete`) · `--unread-only` (`messages list`,
`notifications list`) · `--clear-breaker` (`doctor`) · `--note` (`invite`, and
refused there — see above).

`--type` and `--visibility` are enumerated locally so a typo is refused here
rather than arriving as a bare `400`. `--type` accepts six reaction values, of
which only `LIKE` was on the wire in the capture; the rest are what the web client
sends and are unconfirmed.

`--visibility` accepts exactly **one** value, `ANYONE`. It used to advertise
`ANYONE|CONNECTIONS`, and `CONNECTIONS` was tried against the live account:
LinkedIn answered HTTP `200` with `Invalid input for enum
'dash_contentcreation_VisibilityType'. No value found for name 'CONNECTIONS'`. It
was removed rather than kept as an unconfirmed option, so **there is no way to
publish to connections only from this CLI**. An unrecognised value is still
refused rather than defaulted: a flag meant to *restrict* who sees a post must
not quietly publish it to everyone.

A flag aimed at the wrong command is refused, not dropped: `feed list --text=x`
exits 2 and names the commands that do take it. A misspelling is refused with a
suggestion — `--limit` was accepted and swallowed for a long time, so a page
came back at the default size and looked correct.

The allowlist is granular to the *verb*, not to the sub-verb, so a flag belonging
to one action of a verb parses on its siblings. `post get --visibility=…`,
`post delete --text=…` and `post create --idempotency-key=…` are refused by hand
for that reason; assume other sub-verb combinations are not.

`--dry-run` prints the exact request a write would send and exits without
sending it — and issues no other request either. Where a preview would need a
lookup first (resolving a recipient to a urn, finding your own member urn), it
refuses and tells you which command caches that, rather than quietly going to
the network under a flag that says it does not. Header redaction in the preview
is an allowlist rather than a denylist, because the `csrf-token` header *is* your
`JSESSIONID` value — a denylist that stripped only `cookie` would print a live
credential into your agent's transcript.

### Output

`--format=json` is the default and is the one to use for writes: it carries
every urn the write produced. `--format=text` renders one indented block per
item, keyed on the first identifier it recognises, and that key is the *subject*
of the write rather than its result — so a comment prints the post's
`activity_urn` and drops `comment_urn`, and a sent message prints
`conversation_urn` and drops `message_urn`. Read back with `--format=json` if
you need the id of the thing you just created.

### Write limits

Writes are counted in an on-disk ledger shared across invocations, because every
command is a fresh process: 10 posts, 40 messages, 40 comments, 100 reactions and
15 connection invitations per rolling 24 hours, and five times each of those per
7 days. LinkedIn enforces an invitation quota of its own on top of that one, and
a refusal from it is exit `5` under `invite_quota_exceeded`, which no local reset
affects. A `429` or `503` is remembered as a cooldown that outlives the process,
refusing further writes and slowing the reads a cooldown still allows.

The three undos — `unreact`, `post delete` and `comment delete` — are recorded
under `react.cleanup`, `post.cleanup` and `comment.cleanup`, because undoing is
still traffic and still counted, but none of them is ever refused **by a spent
cap, an open breaker or a cooldown**. An undo those can withhold strands exactly
what they were meant to prevent: a run that publishes and then trips its own cap
would be left holding a live public post. `post create` and `comment` are not
exempt — the exemption is keyed to the sub-verb, not to the verb, so publishing
stays bounded.

"Exempt from the caps" and "exempt from arithmetic" are different claims, and only
the first was ever argued for. Two things still refuse an undo:

- **The cleanup ceiling**, exit `5` under `write_quota_exceeded`. Five times the
  kind's daily cap of undos per rolling 24h — 50 `post delete`s, 200
  `comment delete`s, 500 `unreact`s — which is more than a full week at the cap
  could have created, so reaching it
  means a loop rather than a cleanup. An open breaker tightens it to one day's cap
  (10, 40 and 100) counted from the moment of the block rather than from the start of
  the day, because the argument for the tightened number is about what this client
  could have created *since* LinkedIn began refusing it. Counted over a flat 24h
  instead, ten legitimate deletions in the morning would strand the eleventh.
- **An unreadable ledger**, exit `9` under `ledger_unreadable`. An undo is exempt
  from an open breaker and never from a file that will not parse, because its own
  ceiling is counted out of that same file — exempting it there would restore
  precisely the unmetered channel the ceiling exists to close. The trade is
  defensible only because the refusal costs no waiting: it is one `mv` on this
  host, and the error message carries the exact command.

Anything driving this CLI needs a handler for both. They arrive while it is holding
something already published.

`doctor` reports the breaker, any cooldown in force, and the ledger itself under
`ledger.kinds` — per write kind, what the rolling day and week have spent
(`used`, `used_7d`), the caps, and what is left. Reading it changes nothing: it
neither spends a slot nor clears one, so an agent can check its budget without
the only way to find out being to exhaust it.

Undo traffic is reported beside the capped count rather than inside it —
`cleanup_used`, with `cleanup_counts_against_cap: false` — because `react.cleanup`
and `post.cleanup` are recorded and paced but never refused *by a cap*, so they
are not subtracted from `remaining`. The number that does bound them is in the
same row, because `cleanup_counts_against_cap: false` reads as "not bounded" and
the only other way to learn the real bound is to be refused by it:

```json
"post": { "used": 3, "cap": 10, "remaining": 7,
          "used_7d": 3, "cap_7d": 50, "remaining_7d": 47,
          "cleanup_bucket": "post.cleanup", "cleanup_used": 1,
          "cleanup_counts_against_cap": false,
          "cleanup_ceiling": 50,
          "cleanup_window_from": 1785194000.0, "cleanup_used_in_window": 1,
          "cleanup_remaining": 49 }
```

`cleanup_ceiling` is reported against the breaker as it stands, not against the
relaxed number, and `cleanup_remaining` is counted from wherever the next claim
will count it — from the moment of the block while the breaker is open, from the
rolling day otherwise. A `cleanup_remaining` measured over the wrong window would
report a channel as spent that the next undo would in fact find open, which is the
reading that sends an operator to the browser for a post this CLI would have
deleted.

Those two windows are why the row carries `cleanup_window_from` and
`cleanup_used_in_window` as well. `cleanup_used` is always the rolling day; while
the breaker is open the ceiling is not, so `cleanup_ceiling - cleanup_used` can go
negative and read as a spent channel that is in fact open. Subtract like for like:
`cleanup_ceiling - cleanup_used_in_window` is `cleanup_remaining`, clamped at zero
exactly as `remaining` is.

Reading it costs no traffic — the ledger is a local file, and `doctor` already
spends a live call per surface without adding one for its own report.

A host that has never written has no state file, and its zeros are true. A state
file that exists and will not parse is different, and says so: `readable: false`,
an **empty** `kinds`, and a `problem` explaining that the day's usage is unknown
rather than zero.

The two are treated differently where it counts. **Reads still run against an
unreadable ledger** — a corrupt file must not take the CLI down, and `doctor`,
the command that tells you which file to move aside, reads through the same path.
**Writes do not.** A write claimed against a ledger this client cannot parse is a
write made with the caps, the cooldown and the breaker disarmed at once, and it
then overwrites the only record of what had already been spent — so every write,
including an undo, is refused with exit `9` and `ledger_unreadable` until the file
is replaced. The refusal survives the pacer rewriting the file, because the
replacement carries a marker saying the history was lost; the marker expires after
a week, once nothing it could have recorded is still inside an enforcement window.

## Notes from building it

The endpoints and wire format were mapped against a live session rather than
taken from documentation. A few things that cost real time, in case they save
you some:

- **Send exactly six cookies.** `li_at`, `JSESSIONID`, `liap`, `lidc`,
  `bcookie`, `bscookie`. Several other LinkedIn cookies contain `;` or raw JSON,
  which truncates the `Cookie` header and silently drops `li_at` — an auth
  failure that looks like anything but a cookie bug.
- **A 302 to the request's own URL means the session is stale**, not that
  something is looping. Unless the response carried a `Set-Cookie` you hadn't
  applied yet, in which case retrying once fixes it.
- **`queryId`s are content hashes** that rotate on LinkedIn's deploys. They are
  pinned here and overridable with `LINKEDIN_QUERY_ID_<NAME>`; `doctor` tells you
  which surface broke. `invite` is addressed by a versioned `decorationId`
  instead, which survives an ordinary deploy but is retired eventually — that one
  is overridable with `LINKEDIN_DECORATION_ID_INVITE` and `doctor` reports it in
  its own list, because the two are refreshed by different steps.
- **LinkedIn pages are server-rendered.** A cold load of `/feed/` makes zero
  client-side API calls, so watching the network tab after a refresh shows you
  nothing. Navigate *within* the app.
- **You cannot observe traffic by patching `window.fetch`.** LinkedIn restores a
  pristine `fetch` from a fresh iframe. Use CDP's `Network` domain.

- **A Chrome debug port is not a security boundary.** It listens on TCP, and on
  Linux any local uid can enumerate it from `/proc/net/tcp` and connect — which
  is `Network.getCookies` for `li_at` to anyone who finds it. The supervisor
  therefore speaks CDP over inherited fds instead (`--remote-debugging-pipe`)
  and listens on a unix socket, which the filesystem *can* gate by uid.

## Development

```bash
python3 -m pytest tests/ -q      # the suite
git add -A && tools/check.sh     # the pre-merge gate
```

`tools/check.sh` runs, in order, `ruff check`, `ruff format --check`, the suite,
`coverage --fail-under=100` on `linkedin_cli/transport.py`, and
`tools/leakcheck.py`; it exits non-zero on the first failure. There is no CI
here, so that script is a discipline gate rather than an enforced one; promoting
it to a workflow is the highest-value follow-up.

The suite never touches the network, never launches a browser and never sleeps
on a real clock: `tests/conftest.py` makes `subprocess.Popen`, `socket.connect`
and the urllib opener raise, and every component takes an injected seam instead.

The fixtures in `tests/fixtures/*.json` are committed, and every person, urn and
message body in them is invented. They are written by hand against the shape of
a live response rather than scrubbed from one, because a scrub is a filter that
has to be right every time and an invented payload cannot leak what it never
contained. Real captures stay in `tests/fixtures/raw/`, which is gitignored; each
loader prefers the committed fixture and falls back to a raw one, so a clone
runs the whole suite and a capture on disk changes nothing. Three tests in
`test_messaging.py` pin a projection field-by-field against a real entity and
skip, by name, when no raw capture is present.

`.gitignore` is not a security boundary - it does nothing about `git add -f` or a
file that was already tracked - so `python3 tools/leakcheck.py` scans for anything
shaped like a live token, member id or address. It scans two sets: what git
tracks, and what is untracked and *unignored*, because the second is what the next
`git add -A` turns into the first. Scanning only tracked files checks a file for
the first time one commit after it mattered.

## License

MIT
