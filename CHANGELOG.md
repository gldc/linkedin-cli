# Changelog

No release has been tagged yet — `0.1.0` in `pyproject.toml` is the pre-release
version and everything below is unreleased. Entries collect under `Unreleased`
rather than under a version until the first tag.

The rule for this file matches the rule the rest of the repo is written to: an
entry says what an operator or an agent has to do differently, and a behaviour
that can refuse a write says so. Anything verified against the live account is
marked as such; anything unobserved is marked as that too.

## Unreleased

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
