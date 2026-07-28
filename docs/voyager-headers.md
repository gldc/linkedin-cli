# What a real voyager-web XHR carries

A record of the request shape a voyager-web XHR presents on the wire: which
headers it carries, and the schema of the one non-trivial header a client has to
add for a request to be well-formed. This is a description of the request shape,
not advice on avoiding detection.

The headers below were observed from real in-app calls in a browser seeded with
a live session. LinkedIn's own `fetch` runs same-origin inside the page, so the
browser supplies `user-agent`, `referer` and every `sec-ch-*` header by itself;
only the `x-li-*` tracking headers have to be added by a client performing the
same in-page `fetch`.

## Header inventory

| header | who supplies it | when |
|---|---|---|
| `accept`, `csrf-token`, `x-restli-protocol-version`, `x-li-lang` | the client sends these | every call |
| `x-li-track`, `x-li-page-instance` | the browser does not send these; the client adds them | every call |
| `user-agent`, `referer`, `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`, `sec-ch-prefers-color-scheme` | browser-supplied | every call |
| `x-li-pem-metadata`, `content-type` | route-specific | some calls |
| `x-li-deco-include-micro-schema`, `x-http-method-override` | route-specific | some calls |

`x-li-track` and `x-li-page-instance` appear on every real call but are not
among the headers the browser adds on its own, so a client doing an in-page
`fetch` has to supply them itself. This is why capturing them is treated as
required rather than cosmetic.

## `x-li-track` schema

The header is a JSON object. Representative shape and field types (the display
and timezone values shown here are neutral placeholders, not any real device):

```json
{"clientVersion":"1.13.45452","mpVersion":"1.13.45452","osName":"web",
 "timezoneOffset":0,"timezone":"UTC","deviceFormFactor":"DESKTOP",
 "mpName":"voyager-web","displayDensity":1,"displayWidth":1920,"displayHeight":1080}
```

The `timezone`, `timezoneOffset` and `display*` fields report the browser's
resolved identity, so they follow whatever device metrics and timezone the
client has configured - see `Identity` in `linkedin_cli/supervisor.py`. They
have to be internally consistent with the rest of the request.

`clientVersion` moves on LinkedIn's deploys, so it is captured at page load
rather than pinned in source - the same failure mode as `queryId`.

`x-li-page-instance` is `urn:li:page:<pageKey>;<base64 id>`, minted per page
view.

## Consequence for the implementation

Because the fetch runs inside the page, the browser supplies `user-agent`,
`referer` and every `sec-ch-*` header automatically. Only `x-li-track` and
`x-li-page-instance` need adding for the request to match the shape LinkedIn's
own client sends.
