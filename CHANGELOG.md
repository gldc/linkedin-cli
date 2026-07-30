# Changelog

No release has been tagged yet — `0.1.0` in `pyproject.toml` is the pre-release
version and everything below is unreleased. Entries collect under `Unreleased`
rather than under a version until the first tag.

The rule for this file matches the rule the rest of the repo is written to: an
entry says what an operator or an agent has to do differently, and a behaviour
that can refuse a write says so. Anything verified against the live account is
marked as such; anything unobserved is marked as that too.

## Unreleased

### Changed — `messages reply` no longer sends a reply it can see it already sent

- **The `originToken` is derived, not random.** `messages send` and
  `messages reply` compute it as `sha256(namespace, mailbox urn, conversation
  urn, text)` cast to a uuid4-shaped UUID, instead of a fresh `uuid.uuid4()` per
  call. Under an agent gateway a retry is a fresh process with no memory of the
  first attempt and the broker does not pass `--idempotency-key`, so a retry can
  only be recognised as one if its identity comes out of the arguments. On its
  own this buys traceability, not idempotency: the captured body still switches
  the server-side dedupe off and is unchanged.
- **A reply already visible in the thread is not sent twice.** Both verbs check
  the thread page `confirm_reply_target` already fetches, and a message from this
  account carrying the identical text short-circuits the write. The envelope
  gains `data.deduped` — `true` when that happened, `false` on a real send — so
  an agent branches on a field instead of parsing prose. **Zero extra requests:**
  it reads the page the membership check already paid for.
- **Best effort, and the docs say so rather than overclaiming.** The window is
  one server-default page of the newest messages; the match is exact text; and it
  cannot see a write that landed a moment ago and has not appeared yet. So
  `error.retryable` on an unconfirmed send stays `false`, and the advice is now
  "read the thread back, then re-run the identical command **once**" rather than
  "never retry".
- **A deduped reply still spends a `message` slot.** It is not free: it issues the
  live, browser-driven thread read, and the caps exist to pace exactly that. Had
  the slot been refunded, an identical-text loop could never reach the 40/day cap
  and the pre-round-trip refusal would never fire on this verb again.
  Consequence an operator should know: a *deliberate* identical re-run charges
  twice for one message.
- **`--idempotency-key` changed meaning.** It overrides the derived token *and*
  turns the repeat check off — it is now the escape hatch for deliberately
  sending the same text into the same thread twice, not a safety net. The
  credential broker does not expose the flag, so an agent cannot reach it.
  `post create` still refuses it outright.
- **A guard test replaces a checklist.** The claim "a retry is a second message"
  had drifted between prose and payload five times and needed sixteen edits
  across twelve files to correct. `tests/test_wire_field_stays_in_the_capture.py`
  now fails if `dedupeByClientGeneratedToken` is named anywhere but the capture,
  its two tests and the payload that sends it.

### Removed — the urllib transport, which nothing could reach

- **`transport.VoyagerClient` and `tools/resilient.py` are deleted.** The browser
  pivot removed `tools/acquire.py` and left the client and its one consumer
  behind; `import tools.resilient` had been raising `ModuleNotFoundError` ever
  since, and `cli.py` builds `browser.BrowserClient` unconditionally. **This is
  not a security fix and must not be reported as one** — a prompt-injected agent
  could not reach the client before (it is not in the credential broker's
  allowlist, there is no transport switch, and `LINKEDIN_TRANSPORT` is read by no
  code anywhere). What changes is audit surface: 221 lines of raw-cookie HTTP
  client no longer sit in the file every security review of this repo reads, and
  restoring a second, supervisor-free path to LinkedIn now fails three tests
  rather than none. **Breaking for an out-of-tree importer** of
  `linkedin_cli.transport`: `VoyagerClient`, `ESSENTIAL_COOKIES`, `USER_AGENT`,
  `NEVER_SENT` and `REDIRECT_CODES` are gone. Nothing installs this as a library.
- **`raise_for_status` no longer takes `location`, and no longer classifies 3xx.**
  The in-page `fetch` follows redirects, so `resp.status` is the *final*
  response's and can never be a 3xx; the only caller passed `location=None` as a
  literal. `method` is consequently the fifth parameter, not the sixth, and
  `browser.py` now calls it with keywords so a half-applied edit cannot shift
  `final_url` into the deleted slot. A future change that made the injected
  script decline redirects would need the arm back — which is why
  `test_the_injected_fetch_follows_redirects` fails the moment `SCRIPT` grows a
  `redirect:` key.
- **`linkedin_cli/state.py`'s docstring now says who its caps bind.** No rule
  changed and nothing was deleted: the caps, the cleanup ceiling, the throttle
  cooldown and the breaker are byte for byte what they were. The docstring read
  as though all five caps were the agent's boundary; as the credential broker's
  `policy.json` is written today the allowlist leaves the agent only `message`,
  via `messages reply`. `invite`, `post`, `comment`, `react` and the whole
  cleanup-exempt path are the operator's, by hand. Pacing, the cooldown and the
  breaker still bind both.

### Added — one command for the pre-merge gate

- **`tools/check.sh`** runs ruff, the suite, `--fail-under=100` coverage on
  `transport.py`, and `leakcheck` in that order. No staging step needed — the
  leak scan covers tracked *and* untracked-unignored files. There is no CI wired
  up here, so it is a discipline gate, not an enforced one.

### Fixed — a live credential on `doctor`'s success path

The first of four prerequisites for running under the credential broker. This one
and the membership guard below are live defects independent of that work.

- **`doctor` and every `--dry-run` printed the browser's full `location.href`.**
  A session parked on a LinkedIn checkpoint carries the csrf token back in the
  query string as `?ct=`, and that token **is** the `JSESSIONID` cookie value —
  so the one command an operator runs when something is wrong could print a live
  session onto stdout, which under an agent gateway is permanent model context.
  The supervisor's `status` now reports the URL's **path**, which is where both
  consumers inherit it: `doctor`'s `browser` block and the `runs_in` of every
  `--dry-run` preview. That second path was the worse one — a preview skips the
  membership read, the ledger claim, the cap and the breaker and makes no
  LinkedIn round trip at all, so `messages reply … --dry-run` was a credential
  oracle repeatable at whatever rate the caller is paced to. The diagnosis
  survives: `/checkpoint/challenge/…` still says a session is parked on a
  checkpoint, and only the query string carried the token. Nothing downstream
  would have caught it — `render.ok` does not scrub and cannot, because
  `profile get` legitimately returns an `ACoAA…` urn the patterns would eat, and
  the redactor runs from error constructors only.

### Changed — behaviour that can now refuse a write (confinement)

- **`messages reply` reads the thread before it writes into it, and checks whose
  mailbox answered.** Exit `4` / `not_found`, nothing sent, no quota spent, if
  the conversation urn is one this session cannot read a message out of, **or if
  the thread LinkedIn answers with is in another member's mailbox**.
  `messaging.send_message` drops the urn into the createMessage body with no
  lookup and `reply <urn>` builds the request `send --to=X --conversation=<urn>`
  builds, so nothing on that path previously tied a reply to a thread the account
  is in — which is precisely what an agent that has just read a stranger's DM can
  be talked into. **Callers must handle exit `4` from `reply`.** One extra
  request, on `reply` only: `messages read` and `messages list` are unchanged,
  and `--dry-run` skips it because a preview issues no requests and sends nothing
  to confine. The mailbox half is free — a conversation urn is
  `(<mailbox owner>,2-<thread>)` and the owner rides in the answer the read
  already paid for — and it is compared against LinkedIn's answer rather than
  against the caller's argument.
  What the two together establish is **read access and the mailbox, not
  membership** — the messages response carries no participant roster, so a thread
  this session cannot see and a thread whose messages were all deleted answer
  identically and the refusal names both. An answer carrying messages but naming
  no conversation at all is also refused, rather than skipping the check: that
  shape means the payload rotated, and a check that quietly stops running is
  worse than one that was never claimed.
- **`LINKEDIN_DEPLOYMENT` naming the confined deployment makes the built-in paths
  fail closed.** Under that deployment an unset `LINKEDIN_BROWSER_BINARY`,
  `LINKEDIN_BROWSER_PROFILE` or `LINKEDIN_STATE_FILE` raises instead of falling
  back. The default binary lives under a directory the untrusted agent uid can
  write, and the CLI runs as the uid holding every tenant's credential there — so
  a policy that lost a key used to exec whatever that uid last wrote, silently.
  The default ledger lives under a `HOME` that deployment rebuilds on every
  redeploy, so a lost `LINKEDIN_STATE_FILE` silently reset the 40/day message
  cap, any live throttle cooldown and an **open** circuit breaker to zero, and
  moved the supervisor socket with it. Outside that deployment nothing changes.
  The comparison is against one exact value — `supervisor.CONFINED_DEPLOYMENT` —
  and a `LINKEDIN_DEPLOYMENT` misspelled in the policy arms nothing.
- **A confined deployment missing a key now reads as a misconfiguration.** The
  refusal above was raised while `supervisor.request` was annotating a `status`
  answer, inside the handler that means "the connection died mid-request" — so a
  supervisor that had answered perfectly was reported as one that had stopped
  answering, and the key the policy dropped went unmentioned. It comes back as
  `kind: "config"` carrying the refusal. Separately, a 401/403 built that message
  while raising `SessionExpired`, so the same missing key turned exit `3` into
  exit `6`: exit `3` is what the circuit breaker counts, and three in an hour is
  what makes a rotted session degrade loudly rather than an agent loop on it.

### Fixed — the membership guard covered one of the two ways to send a message

- **`messages send --conversation=<urn>` posted into whatever mailbox the urn
  named.** The guard above went onto `messages reply`; `send` built the
  byte-identical createMessage body one branch down in the same function and
  reached `messaging.send_message` with the caller's urn and no check at all.
  Reproduced: `send --to=<a member> --conversation=<a stranger's thread>` exited
  `0` with the message delivered into the stranger's thread. **Callers must now
  handle exit `4` from `send` as well as from `reply`**, and `send` costs the
  same extra request. The two verbs reach the writer through **one** function
  now, and a test asserts `cli.py` contains exactly one call to it: this is the
  third time in this project that a guard landed on one call site while a
  byte-identical second path stayed open, and two call sites is what all three
  had in common.
- **`messages send` takes `--to` or `--conversation`, and refuses both.** Exit
  `2`. `--to` was read only when `--conversation` was absent, so passing both
  dropped the recipient entirely while making the invocation read as though it
  had been addressed and checked. Nothing on that path can reconcile the two —
  `find_conversation_with` scans forty threads and answers "none" past that, so a
  real thread beyond the scan would look like a disagreement — so the pair is
  refused rather than silently resolved, which is the rule
  `messages mark-all-read <urn>` and `invite --note` are already written to.
  `send --conversation=<urn>` and `reply <urn>` are the same send.

### Fixed — two supervisor diagnoses that named the wrong thing

- **`about:blank` was reported as a page called `blank`.** `urlsplit` calls the
  whole of an opaque URL its path, so the reduction that keeps a checkpoint's
  `?ct=` out of `status` was cutting the scheme off the one page this module
  writes its own error message about. Not a leak — a diagnosis. The daemon now
  reports `about:blank`, and any other scheme-only URL keeps its scheme too.
- **A lost policy key is `kind: "config"` wherever it is raised.** It was
  `config` from the client's annotation and `upstream` from the daemon's
  dispatcher, decided only by which op ran first, because the classifier had no
  branch for it; `upstream` sends an operator to restart a browser over a line
  missing from `policy.json`. The refusal has its own type now and exactly one
  function classifies it. The same key also escaped `supervisor.request` as an
  exception before anything was connected — the socket path is derived from the
  ledger's — which `browser.py` reported as `outcome_unknown`, telling an agent a
  message may have landed when nothing had been sent.
- **The "navigation did not take" refusal no longer interpolates a raw
  `location.href`.** It reaches the client as an error string, and it is the last
  member of the class the `status` reduction closed. Only reachable when the page
  is off `linkedin.com`, so a checkpoint's `?ct=` cannot arrive here — but the
  rule is "the query string is where the credential is", not "LinkedIn's query
  string is". The origin and the path both survive, because where it landed is
  the entire diagnosis.

### Added

- **`linkedin invitations list`** — the connection invitations the operator has
  **received**. Backed by `relationships/invitationViews?q=receivedInvitation`,
  verified live against three agreeing reads. Pages by a numeric offset.
  **The populated element shape is unobserved**: the inbox was empty when the
  route was verified, so the projection is written liberally and fails loudly — a non-empty page it cannot read raises exit `6`
  rather than returning `[]`, because an empty list is indistinguishable from an
  empty inbox and only one of those is a bug report.
- **`linkedin comment delete <comment-urn>`** — the inverse of `comment`, verified
  live. Takes the comment's own urn
  (`urn:li:fsd_comment:(<commentId>,urn:li:activity:<activityId>)`), and accepts
  the `urn:li:fsd_normComment:`-wrapped spelling that `comment` itself reports.
  Booked as a cleanup under `comment.cleanup`. Deliberately no `--yes`.
- **`error.body` under `--raw` on the failure path.** LinkedIn's own response,
  redacted, beside the error rather than instead of it, and without moving the exit
  code. Attached only when the recorded body belongs to the request that failed,
  and never to a refusal made without asking LinkedIn.
- **`cleanup_ceiling`, `cleanup_window_from`, `cleanup_used_in_window` and
  `cleanup_remaining`** in every `doctor` ledger row, so the bound on the undo
  channel can be read without being refused by it.

### Changed — behaviour that can now refuse a write

- **An undo is no longer unconditional, and the docs that said it was were wrong.**
  `unreact`, `post delete` and `comment delete` remain exempt from every daily and
  weekly cap, from a throttle cooldown, and from an open circuit breaker. They are
  now bounded by a **cleanup ceiling**: 5× the kind's daily cap per rolling 24h (50
  `post delete`s, 200 `comment delete`s, 500 `unreact`s), which is more than a full
  week at the cap could have created. An open breaker **tightens** that to one
  day's cap rather than closing the channel, counted from the moment of the block
  rather than the start of the day — measured over a flat 24h instead, ten
  legitimate deletions in a morning strand the eleventh.
  **Callers must handle exit `5` / `write_quota_exceeded` from an undo.** Before
  this, `unreact` in a retry loop was a write channel with no limit of any kind.
- **A write ledger that exists and cannot be parsed refuses every write, including
  an undo** — exit `9`, code `ledger_unreadable`. Reads are unaffected, and
  `doctor` still answers. A write claimed against `{}` is a write made with the
  caps, the cooldown and the breaker disarmed at once, and it then overwrites the
  only record of what had already been spent. The refusal survives the pacer
  rewriting the file and expires after a week, once nothing the lost file could
  have recorded is still inside an enforcement window. The remedy is one `mv`,
  named in full in the message.
  `ledger_unreadable` shares exit `9` with `blocked` and means the opposite thing:
  `blocked` says stop calling LinkedIn, this says a local file needs replacing.
  **Branch on `error.code`, not on the exit code alone.**
- **`--visibility` accepts exactly one value, `ANYONE`.** `CONNECTIONS` was sent to
  the live account and refused inside an HTTP `200` (`No value found for name
  'CONNECTIONS'`). It was removed rather than kept as an unconfirmed option, so
  there is no way to publish to connections only from this CLI.
- **A `200` carrying a GraphQL `errors` array is a refusal, not an unknown
  outcome.** `posts`, `social` and `messaging` all read both the top level and
  `data` — the placement LinkedIn actually uses — and report "nothing was applied"
  instead of sending a caller to look for something that was never created.
- **`post get` answers exit `4` for a hollow shell.** A post that is deleted, or
  not visible to this session, comes back as `200` with the update entity emptied
  out; every field the projection could still fill for one is computed from the urn
  that was *sent*, so a projected shell was the request echoed back wearing the
  shape of an answer. That is what makes `post get` a usable read-back after a
  `post delete` whose response was unclear. LinkedIn does not distinguish deleted
  from invisible anywhere in the body, and neither does this.

### Fixed — documentation that claimed a safety property the code does not have

- **`--idempotency-key` never made a retry safe.** It sets `createMessage`'s
  `originToken`, and the same captured body sends
  `dedupeByClientGeneratedToken: false` — the server being told explicitly not to
  collapse repeats of the token. Two calls with one key are two messages in the
  thread and nothing here unsends either. The flag buys traceability. SKILL.md,
  README.md, `docs/write-payloads.md` and `surfaces/messaging.py` all asserted the
  opposite; `docs/write-payloads.md` asserted it three lines under the payload that
  contradicts it.
- **SKILL.md promised that a published post is "always removable"** and that an
  undo passes "a spent cap, an open breaker and a cooldown". The second half is
  still true; the promise was not, and an agent that read it had no handler for
  either of the two ways an undo can now fail while it holds a live public post.
- **SKILL.md mapped exit `9` to "blocked (stop entirely)"** with no room for
  `ledger_unreadable`, whose correct response is the opposite.
- **Both agent-facing docs implied an exit code could mean "retry".** None does.
  `error.retryable` is the only field that says so, and it is true only for a
  throttled read.
- **README described `post get` as a route never observed answering.** It is
  verified.
- **README's `doctor` sample omitted the keys that bound the undo channel**, so the
  only way to learn the bound was to be refused by it.
- **README said "a corrupt ledger must never stop the CLI running."** True of
  reads, false of writes, since this release.

### Fixed — `invite withdraw` and the sent-invitations list are impossible, not uncaptured

Both were stubbed with "the route/body was never captured", which reads as one more
careful capture run away — and that is what the previous handoff was pointed at.

The invitation manager has migrated to LinkedIn's server-driven UI. Its rows are
rendered into the document, its controls post protobuf-shaped
`proto.sdui.actions.core.*` payloads to `/flagship-web/`, and the answer is a React
Server Components stream this CLI's transport cannot read. Pressing Withdraw does
not withdraw — it opens a confirmation screen that is itself fetched from the
server, so the retraction cannot be observed without performing it. There is no
Voyager request for anyone to record.

`docs/sdui-migration.md` is the evidence, gathered live and read-only. Every doc
that implied another capture run would close these has been corrected, because
**the last two runs at this surface each cost a real stranger their pending
invitation**, and an instruction to try again is aimed at exactly the reader who
has set out to close the gap.

That document's own probe count was also internally contradictory — a sent-side
subtotal quoted as the total, over a table enumerating thirteen. The table is the
authority:
**thirteen** spellings probed and two answered; **seven** of them aimed at the sent
side, of which three answered `404` and four `400`. Both numbers are now stated
alongside what each one counts.
