# Incidents

Why this codebase is shaped the way it is. Every rule in it that looks paranoid was bought
by one of these, and the comments in the source cite this file by name.

Names, member ids, post urns and host details are omitted throughout: the people involved
did not sign up to appear in a repository, and the failures are what matter.

The one sentence worth taking away, because four separate incidents share it:

> **A tool's report about what it did is not evidence about what the account did. Only
> reading the account back is evidence.**

Every one of these runs printed something that looked like success.

---

## 1. A prefix match sent a connection request to a stranger

A capture script located the "Connect" control by matching an accessible name with
`startswith("invite ")`. On a profile page that also matched a **sidebar recommendation
card** — "Invite ⟨someone else⟩ to connect" — and clicked it. A real connection request went
to a person nobody had chosen.

Two things turned a bug into an incident:

- Interception was armed *after* the click rather than before it, so the request left
  unobserved.
- The run therefore reported **"0 captured"**, which reads exactly like a harmless no-op.

It was found by reading the sent-invitations list, not from any output the tool produced.
Withdrawn about five minutes later; the recipient may have seen a notification.

**Rules adopted, and they are enforced in `tools/capture_payloads.py` rather than remembered:**

1. Arm interception **before the first click** of a flow, never before the last.
2. Never match a control by prefix or substring when a **person** is the object of the verb.
   Match the full accessible name exactly and fail loudly when it is absent.
3. A run that captures nothing is a **failure**, not a no-op.

## 2. A pattern that "looked broad" retracted a real invitation

The intercept pattern was `*/voyager/api/*`. That reads as "everything", and it is not:
**retracting an invitation does not post under `/voyager/api/`**. The request was never
paused, never recorded, and went to LinkedIn for real — while the run reported itself as
intercepting and printed that it had captured one harmless poll.

A real pending invitation to a third party was withdrawn. **Not recoverable**: LinkedIn
blocks re-inviting a withdrawn contact for up to three weeks.

Worse, the guard *enforced* the hole — it rejected the pattern `*` for not containing the
literal string `/voyager/api/`, so the only strictly-safe pattern was the one thing that
could not be used.

The cause was found later, and it is in `docs/sdui-migration.md`: the entire invitation
surface has moved off Voyager to LinkedIn's server-driven UI. It posts outside
`/voyager/api/` because it is not a Voyager screen any more.

**Fixed:** the default pattern is `*`, the guard accepts it as the one pattern nothing can
escape, and `is_write` no longer treats "not Voyager" as "not a write". And `invite withdraw`
is not implemented, permanently — not "not captured yet".

## 3. Three defects a green suite could not see

1476 tests passed. Live verification then failed three of twenty-nine steps, and two of the
three were in code written that same day to fix exactly this class of problem.

Every one was **a fixture that agreed with the parser instead of with the API**.

**`post get` reported a deleted post as alive.** The fixture for "content gone" was built by
reasoning about which fields a readable post carries and blanking them, which produced an
empty shell. LinkedIn does not send an empty shell — it fills `content` with a tombstone
whose title reads *"This post cannot be displayed"*, and fills `updateMetadata` with the urn
that was asked for while omitting `shareUrn`. So `content`, one of the things counted as
evidence that a post is readable, is the one field present **because** the post is gone.
Reading it as proof inverted the check on the single case the check exists for — and
`post get` is the oracle `post delete` sends a caller to.

**`invitations list` could not read an invitation.** The route had been verified against an
inbox holding zero, so no populated row had ever been seen and the projection guessed at flat
keys. The real answer is a normalized collection: the list is `*elements` — *references* —
with entities in `included`, three hops away. What saved it was the fail-loud rule: it
refused rather than returning `[]`, which would have been indistinguishable from "you have no
pending invitations", a false statement about the account that nothing downstream could
detect.

**`feed list --count=30` died on a socket frame limit.** One constant bounded both directions
of the supervisor socket, with a comment arguing it was "generous by three orders of
magnitude" — true of a request, which this package writes, and never true of an answer, which
LinkedIn writes.

**The lesson is about test suites, not about these three bugs.** A suite is a regression net.
It checks the code against fixtures somebody wrote, and wherever those fixtures were guesses,
it was checking a guess against itself. That is why the ledger, the postconditions and the
projections all prefer a loud failure to a plausible answer.

## 4. A refusal arrived inside an HTTP 200, and the flag for reading it was blind

`post create --visibility=CONNECTIONS` answered `200`. Nothing was published. LinkedIn had
put the refusal in a GraphQL `errors` array inside the body:

> `Invalid input for enum 'dash_contentcreation_VisibilityType'. No value found for name
> 'CONNECTIONS'`

The status line is the transport's report about itself, and on a GraphQL route it is not an
outcome. Three things came out of it.

**A `200` carrying `errors` is a refusal.** `posts`, `social` and `messaging` now read the
array at the top level *and* under `data` — LinkedIn uses both placements — and say "nothing
was applied", instead of sending a caller off to look for something that was never created.

**`--raw` did nothing on the failure path, which is the path it is most needed on.** The flag
exists so that an operator can see what LinkedIn actually said rather than writing a script to
find out, and it only ever applied to successes — where the answer is least interesting.
Diagnosing this took a hand-written HTTP script, which is precisely the work the flag was
added to remove. `--raw` now attaches the upstream body as `error.body`: scrubbed, *beside*
the error rather than instead of it, without moving the exit code, and only when the recorded
body belongs to the request that actually failed. The same reasoning is why the body is not
truncated the way an error *message* is, and why a payload asked for explicitly is not
redacted on the success path either.

**The enum was narrowed rather than left advertising a value that does not exist.**
`--visibility` takes one value, `ANYONE` — the one the capture carried and the only one
observed being accepted. An unconfirmed option in a help string is a promise the tool cannot
keep, and the two ways of keeping it here were publishing to the wrong audience or not
publishing at all.

## 5. A session cannot be copied to a second machine

Copying a working `li_at` to a second host worked — feed, profile, messages, everything — and
minutes later LinkedIn invalidated the session **everywhere**, including in the original
browser.

One credential presented from two devices looks exactly like theft. Two separate logins
coexist without complaint; one session on two hosts does not. This is why `auth seed` is
treated as a dangerous operation and why nothing in this CLI exports a cookie.

## 6. A browser killed with a signal loses the session it just acquired

Login succeeded three times — correct profile, real data — and every subsequent run was
logged out. Chromium writes its cookie store lazily, and the browser was being terminated
before it flushed. The session was genuinely acquired and evaporated three times before
anyone understood why.

This is why the supervisor closes the browser through CDP and waits, rather than sending a
signal and moving on.

## 7. A debug port is not a security boundary

An early design held the credential behind a CDP debug port bound to loopback, on the
argument that loopback is local-only. Tested in a real container: **an unprivileged user
enumerated the port out of `/proc/net/tcp`, connected with zero credentials**, and could have
read the session cookie straight out of `Network.getCookies` — or issued authenticated writes
past the write ledger, the pacer and the circuit breaker entirely.

Loopback TCP is not uid-gated on Linux. The debug channel now rides inherited file
descriptors (`--remote-debugging-pipe`, see `linkedin_cli/pipe.py`) with no listening port
anywhere, and the control socket is a unix socket whose permissions are asserted at bind time
rather than assumed.
