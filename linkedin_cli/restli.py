"""Encode Python values into RestLi 2.0 `variables` syntax.

LinkedIn's GraphQL endpoints do not take JSON variables; they take RestLi tuple
syntax: objects are `(k:v,k2:v2)`, arrays are `List(a,b)`, and scalars are bare.
Anything that could be mistaken for structure inside a value - most importantly
the parentheses and commas inside a `msg_conversation` URN - has to be
percent-encoded, or the server parses the value as nested structure.
"""

from __future__ import annotations

from urllib.parse import quote


def encode(value: object) -> str:
    """Render `value` as a RestLi 2.0 variables fragment."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dict):
        parts = [f"{k}:{encode(v)}" for k, v in value.items() if v is not None]
        return "(" + ",".join(parts) + ")"
    if isinstance(value, (list, tuple)):
        return "List(" + ",".join(encode(v) for v in value) + ")"
    # Scalars are percent-encoded with nothing safe, so ':' '(' ')' ',' inside a
    # URN can never be read as structure.
    return quote(str(value), safe="")


def query_string(query_id: str, variables: dict | None = None, **extra: object) -> str:
    """Build the query string for a voyager GraphQL call."""
    parts = []
    if extra.get("include_web_metadata"):
        parts.append("includeWebMetadata=true")
    if variables is not None:
        parts.append(f"variables={encode(variables)}")
    parts.append(f"queryId={query_id}")
    return "&".join(parts)
