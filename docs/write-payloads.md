# Verified write payloads

Captured from live traffic by driving the real web client with CDP
`Network.requestWillBeSent`, then each one reproduced over plain HTTP to confirm
it works outside the browser. Nothing here is inferred.

## Send / reply to a message

`POST voyagerMessagingDashMessengerMessages?action=createMessage`

```json
{
  "message": {
    "body": {"attributes": [], "text": "<the message>"},
    "renderContentUnions": [],
    "conversationUrn": "urn:li:msg_conversation:(urn:li:fsd_profile:<me>,2-<thread>)",
    "originToken": "<uuid4>"
  },
  "mailboxUrn": "urn:li:fsd_profile:<me>",
  "trackingId": "<16 raw bytes>",
  "dedupeByClientGeneratedToken": false
}
```

Returns `data["*value"]` = the new `urn:li:msg_message:(...)`.

Three details that cost several failed attempts, all of which return a bare `400`
with no field-level explanation:

- **`trackingId` is 16 raw bytes carried as a string**, not base64. In Python:
  `os.urandom(16).decode("latin-1")`.
- **`originToken` is required** and is a UUID4. It is **not** a dedupe key, and an
  earlier revision of this file said it was. The same captured body three lines
  above sends `"dedupeByClientGeneratedToken": false` — the server is being told
  explicitly *not* to collapse repeats of the client's token — so two calls
  carrying one `--idempotency-key` are two messages in the thread, and this CLI
  has no verb that unsends either of them. The claim was an inference contradicted
  by a field in the very payload it was written under, which is why the capture
  wins and the prose was corrected rather than the body.

  What `--idempotency-key` actually buys is **traceability**: it pins
  `originToken` to a caller-chosen value instead of the uuid4 that would otherwise
  be generated, so a send can be correlated afterwards. It buys nothing about
  safety, there is no client-side dedupe either, and a send whose answer was lost
  is resolved by reading the thread back — never by repeating it.
- **`conversationUrn` is required even for a "new" conversation.** The web client
  resolves or creates the conversation first and only then sends, so a
  `hostRecipientUrns`-style payload is not what the current API accepts.

## Mark a conversation read

`POST voyagerMessagingDashMessagingBadge?action=markAllMessagesAsSeen`

```json
{"until": <epoch-ms>}
```

## Typing indicator (optional, makes traffic look human)

`POST voyagerMessagingDashMessengerConversations?action=typing`

```json
{"conversationUrn": "urn:li:msg_conversation:(...)"}
```

## Delivery acknowledgement (the client sends this after receiving)

`POST voyagerMessagingDashMessengerMessageDeliveryAcknowledgements?action=sendDeliveryAcknowledgement`

```json
{"messageUrns": ["urn:li:msg_message:(...)"], "clientId": "voyager-web",
 "deliveryMechanism": "REALTIME", "clientConsumedAt": <epoch-ms>}
```

## Still to capture — superseded, kept for the reasoning

Written before the interception run. All five it lists — `react`,
`comment`, `post create`, `post delete` and `invite` — were captured and are
recorded further down this file, so nothing on the original list is outstanding.
The claim that guessing does not converge still holds and is why every one of
them was captured rather than inferred.

What is outstanding now is a smaller set. `notifications mark-read` still has no
captured payload. `comment delete` does have one — verified live, at the bottom of
this file.

Two entries that used to sit on this list are off it for opposite reasons:

- **Reading invitations is no longer route-less.**
  `relationships/invitationViews?q=receivedInvitation` was probed live, answered
  `200` with `data.elements` and `data.paging`, and agreed with two independent
  reads; `linkedin invitations list` ships on it. That is the *received* side
  only. See `docs/sdui-migration.md` for the probe table — thirteen spellings
  across both sides, of which this and `invitationsSummary` were the two that
  answered.
- **`invite withdraw` is not outstanding, it is impossible.** The invitation
  manager left Voyager for LinkedIn's server-driven UI, so there is no request
  body for anyone to record and no further capture run that could produce one.
  Two runs at this surface each cost a real stranger their pending invitation.
  Do not plan another. `docs/sdui-migration.md` is the evidence.

The reason a *finder* was treated as capturable at all still holds and is worth
keeping: the failure mode of a guessed read is the inverse of a guessed write's. A
guessed write body comes back as a bare 400; a guessed finder comes back as an
empty collection, which is indistinguishable from having no invitations at all.
That is why `invitations list` fails loudly on a page it cannot parse instead of
returning `[]`.

## React to a post

Captured live. Note that like and unlike are **different queryIds**, and
only like carries `entity.reactionType` — so this is one endpoint with two
distinct content hashes, not one call with a flag.

`POST graphql?action=execute&queryId=voyagerSocialDashReactions.b731222600772fd42464c0fe19bd722b`

```json
{
  "variables": {"entity": {"reactionType": "LIKE"}, "threadUrn": "urn:li:activity:<id>"},
  "queryId": "voyagerSocialDashReactions.b731222600772fd42464c0fe19bd722b",
  "includeWebMetadata": true
}
```

## Remove a reaction

`POST graphql?action=execute&queryId=voyagerSocialDashReactions.f68b48ae5bc0085d7a45c7003b772a39`

```json
{
  "variables": {"threadUrn": "urn:li:activity:<id>"},
  "queryId": "voyagerSocialDashReactions.f68b48ae5bc0085d7a45c7003b772a39",
  "includeWebMetadata": true
}
```

## Comment on a post

`POST voyagerSocialDashNormComments?decorationId=com.linkedin.voyager.dash.deco.social.NormComment-43`

```json
{
  "commentary": {
    "text": "<the comment>",
    "attributesV2": [],
    "$type": "com.linkedin.voyager.dash.common.text.TextViewModel"
  },
  "threadUrn": "urn:li:activity:<id>"
}
```

## The urn trap

These take `urn:li:activity:<id>`. A post's **URL** shows `urn:li:share:<id>`, and
the two ids are different — one post reads `urn:li:share:7486948402790400000` in
its URL and `urn:li:activity:7486948402790400001` in these payloads. They are not
interchangeable and one cannot be derived from the other, so the CLI rejects a
share urn rather than guessing.

## Still uncaptured — superseded, kept for the reasoning

Nothing this section calls uncaptured still is. `post create`, `post delete` and
`invite` were all recorded by interception and appear below; the closed-shadow-root
problem this section describes is real and was solved by
`Accessibility.queryAXTree`, which is shadow-agnostic, rather than by piercing.

The composer defeated four different approaches and the reason is worth writing
down: it renders in a **closed** shadow root. `document.querySelectorAll` finds no
editable element, there is no child iframe, and — the part that took longest —
`DOM.getDocument(pierce=true)` does not help either, because pierce exposes *open*
shadow roots only. Real input events do reach it, but that depends on screen
layout rather than on the DOM, so it is not something to build a capture tool
around.

`invite` was held back here because it sends a connection request to a real
person and there was no target it would have been reasonable to use. Interception
removed the premise: the request is recorded and then aborted, so learning the
payload no longer requires a willing recipient. It is transcribed below.

---

## Captured by interception — nothing was published or deleted

These were obtained by driving the real web client and pausing the request with CDP `Fetch`
at `requestStage: Request`, recording `request.postData`, then `Fetch.failRequest`. The
action never reached LinkedIn. This replaces the old method of having to perform an
irreversible, publicly visible action in order to learn its request body.

### Create a post

`POST graphql?action=execute&queryId=voyagerContentcreationDashShares.80089eb2e82a2dfa23cb621fb09eb7bf`

```json
{"variables": {"post": {"allowedCommentersScope": "ALL",
                        "intendedShareLifeCycleState": "PUBLISHED",
                        "origin": "FEED",
                        "visibilityDataUnion": {"visibilityType": "ANYONE"},
                        "commentary": {"text": "<the post>", "attributesV2": []}}},
 "queryId": "voyagerContentcreationDashShares.80089eb2e82a2dfa23cb621fb09eb7bf",
 "includeWebMetadata": true}
```

`visibilityDataUnion.visibilityType` is what `--visibility` sets, and `ANYONE` — the value
the capture carried — is the **only** value that works. `CONNECTIONS` was sent to the live
account and LinkedIn answered HTTP `200` with `Invalid input for enum
'dash_contentcreation_VisibilityType'. No value found for name 'CONNECTIONS'`. The enum was
narrowed to one member rather than left advertising an option that does not exist
(`docs/incidents.md`).

**There is no dedupe token in this payload at all** — no field a client-chosen key could go
in — so a create whose response is lost must never be retried automatically, or it publishes
twice. That is a stronger statement than the messaging payload's: `createMessage` at least
*has* an `originToken`, it just does not dedupe on it, because the same body switches
client-token dedupe off. Neither write is idempotent; only one of them looks as though it
might be.

### Delete a post

`POST graphql?action=execute&queryId=voyagerContentcreationDashShares.c459f081c61de601a90d103fbea46496`

```json
{"variables": {"updateUrn": "urn:li:fsd_update:(urn:li:activity:<id>,FEED_DETAIL,EMPTY,DEFAULT,false)"},
 "queryId": "voyagerContentcreationDashShares.c459f081c61de601a90d103fbea46496",
 "includeWebMetadata": true}
```

The `updateUrn` wraps an **activity** urn in a fixed five-field tuple, so it is buildable
from what `feed list` and `post get` already return. That settles an open worry that create
returns a `share` urn while delete needs an `activity` urn: delete needs the activity urn,
and the two are still not derivable from each other, so a create must be confirmed by
reading rather than by transforming its own response.

Transcribed into `surfaces/posts.py:delete`. Two things about the
transcription are easy to get wrong:

- **The tuple is a plain JSON string.** `restli.encode` is for *query strings*, where `(`,
  `)` and `,` parse as nested structure; this is a request body, where they do not, and the
  capture carries them raw. Percent-encoding it would send a body that was never observed
  being accepted.
- **The urn is validated with `social.activity_urn`, not `feed.activity_urn`.** The lenient
  reader accepts a bare number, and a post's share id and its activity id are both bare
  numbers — so a bare number would name a post by luck, and a delete cannot be undone.

### Driving controls that page JS cannot see

The composer and these menus render in **closed** shadow roots. `document.querySelectorAll`
finds nothing in any frame's isolated world, and `DOM.getDocument(pierce: true)` does not
reach them either. What works:

- `Accessibility.queryAXTree({nodeId: <document root>, role, accessibleName})` plus
  `DOM.getBoxModel({backendNodeId})` for the coordinates.
- The composer autofocuses its editor, so `Input.insertText` needs no selector at all.
- `DOM.performSearch` is a trap for bare words: given `button` it falls back to a
  plain-text search and returns hundreds of irrelevant nodes. With a real selector such as
  `[contenteditable="true"]` it does pierce correctly.
- `Page.captureScreenshot` is unreliable while `Fetch` is intercepting; it timed out and
  killed one capture run. Do not put it in the capture path.

Control names are exact enough to be safe: the withdraw control for an invitation is a
**link** named `Withdraw invitation sent to <Name>`, and a post's menu is
`Open control menu for post by <Author>`. Matching those in full — never by prefix — is what
keeps a capture from acting on the wrong person's card.

**Do not use the first of those.** It is recorded here because the naming rule it
illustrates is general, not because the control should be driven. Pressing Withdraw does not
withdraw — it navigates to a confirmation screen fetched from the server, so a capture run
cannot observe the retraction without performing the step that produces it, and the retraction
itself is an SDUI action this transport could never send anyway. Two runs at that control each
cost a real stranger their pending invitation. `docs/sdui-migration.md` has the full reasoning.

### Send a connection request

`POST voyagerRelationshipsDashMemberRelationships?action=verifyQuotaAndCreateV2`
`&decorationId=com.linkedin.voyager.dash.deco.relationships.InvitationCreationResultWithInvitee-2`

```json
{"invitee": {"inviteeUnion": {"memberProfile": "urn:li:fsd_profile:<target>"}}}
```

Captured by interception and **aborted** — the sent-invitations list was unchanged before and
after, with the target absent. The profile urn is what `profile get` already returns.
`verifyQuotaAndCreateV2` implies LinkedIn checks a server-side invitation quota, so a refusal
here is a real answer and must not be retried blindly.

Transcribed into `surfaces/invitations.py`. Four things about the
transcription are easy to get wrong:

- **The decorationId is part of the address, not a decoration of the answer only.** It goes
  in the query string next to `action`, and it is *versioned* (`-2`) rather than a content
  hash — so it does not rotate every deploy the way a queryId does, but it is retired
  eventually. It carries a `LINKEDIN_DECORATION_ID_INVITE` override and appears in `doctor`
  under its own list, because refreshing it is a different procedure from refreshing a
  queryId and one list holding both sends the operator to the wrong steps.
- **The action's own name contains the word `quota`.** Any detector that classifies a
  refusal by searching the error text has to strip `verifyQuotaAndCreateV2` first: it is in
  the URL of *every* failure from this route, so an unstripped search reports a rotated
  decoration as a spent invitation quota and tells the operator to wait a week.
- **The response was never captured**, because the capture aborted at the request. So the
  reader requires a created invitation urn rather than treating any 200 as a success. Note
  that the decoration is `...WithInvitee`, so the *target's own profile urn* is in the
  answer — reading the first urn found anywhere would report every refusal as a sent
  invitation, using the recipient's id as the evidence.
- **There is no note field and no dedupe token.** `--note` is refused rather than mapped
  onto an invented `message` key, and there is no retry anywhere on the path: two identical
  requests are two invitations in front of the same person, and this CLI cannot withdraw
  either of them.

Two bugs in the capture driver had to be fixed before this could be obtained, and both were
invisible to unit tests:

- **`Accessibility.queryAXTree` returns `backendDOMNodeId`**, while the DOM domain calls the
  same thing `backendNodeId`. Reading the DOM spelling yielded `None` for every node, so no
  control was ever locatable against a real browser — and because the "not found" diagnostic
  lists names without resolving boxes, the failure looked like *the control is absent* rather
  than *the finder is broken*. The fake transport used the wrong name too, so the suite
  agreed with the bug.
- **A profile renders the same control twice** — once in the page and once in a sticky header
  pinned to the top of the viewport. Both carry the same accessible name and both resolve a
  box, so taking the first match clicked the header copy at y=27, on the navigation bar. The
  driver now hit-tests a candidate's centre with `DOM.getNodeForLocation` and compares the
  accessible name of whatever is actually on top, which is what a real click will reach.

## Delete a comment

Verified live, and **not** by interception. This is the other way to learn a
write, and for this one it is strictly safer than a capture run:

```
DELETE /voyager/api/voyagerSocialDashNormComments/<url-quoted comment key>
  -> 2xx, empty body
```

The key is the **inner** urn, percent-encoded whole (`urllib.parse.quote(urn, safe="")`):

```
urn:li:fsd_comment:(<commentId>,urn:li:activity:<activityId>)
```

### The two collections are not the same resource

This is the whole difficulty, and it cost two probe rounds:

| | collection | key |
|---|---|---|
| create | `voyagerSocialDashNormComments` | n/a, it is a collection POST |
| **delete** | `voyagerSocialDashNormComments` | **inner** `urn:li:fsd_comment:(...)` |
| read back | `voyagerSocialDashComments` | **inner** `urn:li:fsd_comment:(...)` |

And `social.comment` reports a **doubled** urn, because that is what LinkedIn answers with:

```
urn:li:fsd_normComment:urn:li:fsd_comment:(7487…,urn:li:activity:7487…)
```

The `urn:li:fsd_normComment:` wrapper has to come off before the rest is used as a path key.
Round one sent the doubled urn to the write collection and got a 400; round two found the
right key but only ever sent deletes to the *read* collection, and got a 400. Neither
combination was the one that works, and each round's four failures looked like evidence the
route did not exist. Round one's own oracle - a GET of the comment - was also answering 400,
so its four probes never tested a route at all; they tested a key spelling. **Finding the
oracle first is what turned three rounds into an answer.**

### Why guessing was allowed here, when it is forbidden everywhere else

Everywhere else in this project a guessed write body is refused, because a wrong guess is a
real action against a real person and a bare `400` cannot be told from a fact. None of that
applies to this probe:

- **Every object was ours.** A post this CLI published seconds earlier, a comment it wrote on
  that post, and nobody else's content at any point.
- **The desired end state was "deleted" either way.** A probe that succeeds achieves exactly
  what was wanted. There is no outcome in which a wrong guess reaches a stranger.
- **The cleanup did not depend on any probe succeeding.** The post is removed at the end
  regardless, and removing a post removes its comments - so the backstop is unconditional.
  Round one's five 400s left nothing behind for precisely this reason.
- **The verdict came from reading the comment back**, not from the status code. `2xx` is what
  the request said about itself; `404` on the read afterwards is what the account said. Only
  the second is evidence, which is the lesson the whole incident record turns on.

That combination - our own object, "gone" as the goal, an unconditional cleanup, and an
independent read-back - is the test for when probing is legitimate. `invite withdraw` fails
every clause of it, which is why it was never probed and never will be.
