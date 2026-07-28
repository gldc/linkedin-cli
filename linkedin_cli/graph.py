"""Lazy index over LinkedIn's normalized response format.

With `accept: application/vnd.linkedin.normalized+json+2.1` a response is
`{"data": …, "included": [...]}`: `data` holds the skeleton, `included` holds
every entity flattened out, and the two are stitched together by keys prefixed
with `*` whose values are `entityUrn`s.

That structure is a graph, not a tree. It contains cycles, references to urns
that were never included, and the same `entityUrn` more than once. An earlier
revision resolved it eagerly and either hung or exploded; nothing here recurses
without both a depth budget and a visited path, and nothing here raises on a
malformed payload - a missing reference is a `None`, never an exception.
"""

from __future__ import annotations

from typing import Any


class _Drop:
    """Sentinel for a dangling urn inside a list ref, distinct from a real None."""


_DROP = _Drop()


def _type_suffix(obj: dict) -> str:
    return str(obj.get("$type") or "").rsplit(".", 1)[-1]


class Graph:
    def __init__(self, payload: dict):
        self.payload = payload if isinstance(payload, dict) else {}
        self.index: dict[str, dict] = {}
        included = self.payload.get("included")
        if isinstance(included, list):
            for entry in included:
                if isinstance(entry, dict):
                    urn = entry.get("entityUrn")
                    # Duplicates are routine: the same profile arrives with
                    # different decorations. The later copy is the richer one.
                    if isinstance(urn, str) and urn:
                        self.index[urn] = entry

    @property
    def data(self) -> dict:
        block = self.payload.get("data")
        return block if isinstance(block, dict) else {}

    def resolve(self, urn: str, want_type: str | None = None) -> dict | None:
        """Look up one entity from `included`; None if absent or the wrong type."""
        if not isinstance(urn, str):
            return None
        obj = self.index.get(urn)
        if obj is None or want_type is None:
            return obj
        if want_type in (obj.get("$type"), _type_suffix(obj)):
            return obj
        return None

    def deref(self, obj: dict, key: str) -> dict | list | None:
        """Follow a `*key` reference. Single ref -> entity, list ref -> list."""
        if not isinstance(obj, dict) or not isinstance(key, str):
            return None
        ref = obj.get("*" + key.lstrip("*"))
        if isinstance(ref, str):
            return self.resolve(ref)
        if isinstance(ref, list):
            # Dangling entries are dropped rather than emitted as None, so a
            # caller can iterate the list without a per-item guard.
            return [e for e in (self.resolve(r) for r in ref if isinstance(r, str)) if e]
        return None

    def by_type(self, type_suffix: str) -> list[dict]:
        return [obj for obj in self.index.values() if _type_suffix(obj) == type_suffix]

    def expand(self, obj: dict, depth: int = 2) -> dict:
        """Inline `*` references up to `depth` hops, dropping the `*` from the key."""
        if not isinstance(obj, dict):
            return {}
        return self._expand_obj(obj, depth, ())

    # ----------------------------------------------------------------- internals

    def _expand_obj(self, obj: dict, depth: int, path: tuple[str, ...]) -> dict:
        own = obj.get("entityUrn")
        if isinstance(own, str):
            path = path + (own,)

        out: dict[str, Any] = {}
        refs: dict[str, Any] = {}
        for key, value in obj.items():
            if isinstance(key, str) and key.startswith("*"):
                refs[key[1:]] = value
            else:
                out[key] = self._expand_value(value, depth, path)
        # Applied last: an object carrying both `author` and `*author` holds the
        # bare urn in the first and the entity in the second, so the ref wins.
        for name, ref in refs.items():
            out[name] = self._expand_ref(ref, depth, path)
        return out

    def _expand_ref(self, ref: Any, depth: int, path: tuple[str, ...]) -> Any:
        if isinstance(ref, str):
            return self._hop(ref, depth, path, dangling=None)
        if isinstance(ref, list):
            hops = (self._hop(r, depth, path, dangling=_DROP) for r in ref if isinstance(r, str))
            return [h for h in hops if h is not _DROP]
        return ref

    def _hop(self, urn: str, depth: int, path: tuple[str, ...], dangling: Any) -> Any:
        # Budget exhausted or the urn is already on this path: hand back the urn
        # itself. The caller keeps a usable handle and the recursion stops, which
        # is what makes a cyclic payload safe to expand at all.
        if depth <= 0 or urn in path:
            return urn
        target = self.index.get(urn)
        if target is None:
            return dangling
        return self._expand_obj(target, depth - 1, path)

    def _expand_value(self, value: Any, depth: int, path: tuple[str, ...]) -> Any:
        # Plain nesting is not an entity hop, so it does not spend depth.
        if isinstance(value, dict):
            return self._expand_obj(value, depth, path)
        if isinstance(value, list):
            return [self._expand_value(v, depth, path) for v in value]
        return value

    def graphql_root(self, query_name: str) -> dict | None:
        """Dig the real payload out of a GraphQL response's `data.data.<query>`."""
        data = self.data
        for container in (data.get("data"), data):
            if isinstance(container, dict):
                value = container.get(query_name)
                if isinstance(value, dict):
                    return value
        return None
