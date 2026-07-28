# The invitation surface has left Voyager

Observed live, read-only. No control was activated, no `Fetch` interception existed in any
script that produced this, and nothing about the account changed. Every claim below is
something the browser was seen to do, not something inferred from a name.

## Why this document exists

Two verbs were stubbed with the reason "the route/body was never captured". That reason
implies one more careful capture run would close them, and it is the reason the last session
ended pointed at another armed run against a real pending invitation.

It is wrong. The route was never captured because **there is no Voyager route to capture**.
The invitation surface has migrated to LinkedIn's server-driven UI stack, which this CLI's
transport cannot speak at all. That is a fact about LinkedIn, not a gap in the tooling, and
no amount of capture care would have produced the payload.

It also explains incident 2 exactly. The record said "retracting an invitation posts outside
`/voyager/api/`" as an observation. This is the cause: the entire screen posts outside
`/voyager/api/`, because the screen is no longer a Voyager screen.

## What was observed

A cold load of `/mynetwork/invitation-manager/` renders a non-empty list of pending sent
invitations while issuing **zero** invitation requests — the rows are on screen and no call
went out to fetch them. The data is server-rendered into the document. This
repo already documents the general fact - `doctor`'s own `query_id_recipe` says "a cold page
load issues no Voyager calls, navigate *within* the app" - and it holds here.

Switching tabs inside the app issues the real request:

```
POST https://www.linkedin.com/flagship-web/mynetwork/invitation-manager/sent
     -> 200 application/octet-stream   (80 KB React Server Components stream)

{"$type":"proto.sdui.actions.core.NavigateToScreen",
 "screenId":"com.linkedin.sdui.flagshipnav.mynetwork.invitations.InvitationManagerSent",
 "pageKey":"people_sent_invitations", ...}
```

Not JSON, not Voyager, not `application/vnd.linkedin.normalized+json+2.1`. `transport.parse`
cannot read an RSC flight stream, and `BASE` is `https://www.linkedin.com/voyager/api/`.

## What a Withdraw button actually does

The screen stream describes each row's controls, including the action bound to Withdraw. It
can be read without pressing one - the server hands over the button's behaviour as data:

```json
{"buttonProps": {"text": ["Withdraw"],
                 "aria-label": "Withdraw invitation sent to Ada Lovelace"},
 "triggers": [{"type": {"$case": "click"},
   "action": {"actions": [{"$type": "proto.sdui.actions.core.Navigate",
     "value": {"content": {"screen": {
       "screenId": "com.linkedin.sdui.flagshipnav.mynetwork.invitations.WithdrawConfirmationDialog",
       "pageKey": "people_invitations_withdraw_friction",
       "presentationStyle": "PresentationStyle_MODAL",
       "requestedArguments": {"payload": {
          "inviteeUrn": {"memberId": "<redacted>"},
          "profileUrn": "<redacted>",
          "queryName": "ProfileMemberRelationshipRefreshById"}}}}}}]}}]}
```

Three values in that block are substitutions. The two identifiers were a real
member's and are `<redacted>`; the name in the `aria-label` is a placeholder for a
real person's. The identifiers had a scanner behind them - `tools/leakcheck.py`
fails a tracked file carrying a member id - and the name did not, which is why a
name has to be caught by reading rather than by tooling. Nothing structural was
altered: every key, every `$type` and the nesting are as captured, and the point
of the block is the *shape* of an SDUI action, which survives all three
substitutions intact.

Two things follow.

**Clicking Withdraw does not withdraw.** It navigates to a confirmation *screen* which is
itself fetched from the server. The actual retraction is a further SDUI action on a screen
that does not exist in the page until the first click has already been made. A capture run
therefore cannot see the withdrawal without performing the step that produces it.

**The payload is a protobuf-shaped SDUI action, not a request body this CLI could send.**
Reproducing it would mean implementing an undocumented server-driven-UI protocol - screen
ids, `requestedArguments`, an RSC stream parser - against a surface LinkedIn changes without
notice, to answer in a format the transport would still have to be taught to read.

## Consequences

**`invite withdraw` is not implementable in this CLI.** Not "not captured yet". The stub says
so, and points at the browser, which is where withdrawing lives now. Nothing is gained by
another armed capture run, and the last two of those cost a stranger's invitation each.

**`invitations list` splits in two.** The received side survives on Voyager and ships; the
sent side does not exist outside SDUI and is refused with this document as the reason.

## The Voyager routes that do survive

Probed with GETs. A wrong guess on a read costs a status code, which is why probing a read is
legitimate where probing a write never is. **Thirteen** spellings were tried and two
answered:

| route | spellings | result |
|---|---|---|
| `relationships/invitationViews?count=N&q=receivedInvitation&start=0` | 1 | **200**, `data.elements`, `paging` |
| `relationships/invitationsSummary` | 1 | **200**, `numPendingInvitations`, `numNewInvitations` |
| `relationships/sentInvitationViews` (3 finder spellings) | 3 | 404 - the collection is gone |
| `voyagerRelationshipsDashInvitations` (q=received, q=sent) | 2 | 400 |
| `voyagerRelationshipsDashInvitationViews`, `...SentInvitationViews` | 2 | 400 |
| `voyagerRelationshipsDashInvitationsSummary`, `...MemberRelationships?q=invitations` | 2 | 400 |
| `relationships/invitations?q=sent`, `growth/normInvitations?q=sent` | 2 | 400 |

The counts are spelled out per row because the two numbers this document has to keep straight
are easy to conflate, and an earlier revision quoted one of them under the other's meaning
while its own table enumerated thirteen. The table is the authority. Both numbers are real
and they count different things:

* **Thirteen** is every spelling probed, across both sides of the surface — the sum of the
  `spellings` column above. Two answered.
* **Seven** is the subset aimed at the invitations you have *sent*: the three
  `sentInvitationViews` finders (404), plus `voyagerRelationshipsDashInvitations?q=sent`,
  `...SentInvitationViews`, `relationships/invitations?q=sent` and
  `growth/normInvitations?q=sent` (400). Three 404s and four 400s, none of them an answer.
  That is the count `invitations.NO_SENT_LIST` reports to an operator, and it is the right one
  for that message: what an operator asking for `invitations sent` needs is the sent-side
  tally, not the total.

The inbox it was verified against was empty, and all three surfaces said so: the received
route answered an empty `elements`, the summary answered a zero `numPendingInvitations`, and
the page's own tab count agreed. Three independent reads agreeing is what makes this a
verified route rather than a URL that happened to return 200.

`invitationsSummary` returns `null` for `numTotalSentInvitations` and
`numSingleSentInvitations` - the sent-side fields are vestigial, which is the same migration
showing through the summary.

**A limitation to state plainly:** the inbox was empty, so the *populated* element shape has
never been observed. The parser is written to fail loudly
rather than quietly - a non-empty `elements` that projects to nothing is an error, not an
empty list - for the same reason a zero-capture run is a failure and not a no-op.

## How this was learned, and why it was safe

Three scripts, none of which can act:

* Neither sends `Fetch.enable`. Nothing is paused, so nothing can be aborted or escape.
* The only clicks are tab labels matched by full equality against a frozen two-element
  allowlist (`{"Sent", "Received"}`), so no control named after a person is reachable. A tab
  changes what is on screen and nothing about the account.
* The route probes are GETs.

The read-back that mattered: the sent list is unchanged in length, the authorised test
invitation present and the retracted one absent. The retraction incident 2 caused is
confirmed from the account rather than from a run's own report of itself, which is the
distinction the whole of this project's incident record turns on.
