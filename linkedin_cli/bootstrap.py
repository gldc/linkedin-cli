"""One-time seeding of the managed browser profile, and the checks that make it
safe to trust afterwards.

The managed browser has never seen a login form. It is given a jar copied once
out of the operator's own running Chrome, after which it maintains and rotates
the session itself and nothing here runs again. That is the whole reason the
pivot could delete the cookie-decryption tower: `li_at` stops being something
this CLI reads and becomes something a browser holds.

There is deliberately no `export`. Its only possible output is a live `li_at` on
stdout, which under an agent gateway is permanent model context, and dispatch in `cli.py`
is granular to the verb - so a broker allowlist permitting `seed` could not have
denied `export`. Seeding a different host is an operator action over ssh, not a
verb an agent can reach.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import supervisor
from .transport import SessionExpired, UpstreamError

# The operator's own Chrome, which is the source of the one-time copy. Only ever
# read from, never launched or written to.
SOURCE_PROFILE_ENV = "LINKEDIN_SOURCE_PROFILE"
SOURCE_PROFILES = {
    "darwin": "~/Library/Application Support/Google/Chrome",
    "linux": "~/.config/google-chrome",
}

# Cookies without which the session does not authenticate. `liap` is here because
# it was the one missing from every disk read: Chrome keeps it in memory only, so
# a profile scraped off disk looked complete and failed on every data endpoint.
ESSENTIAL = ("li_at", "JSESSIONID", "liap")


def source_profile(explicit: str | None = None, system: str | None = None) -> Path:
    chosen = explicit or os.environ.get(SOURCE_PROFILE_ENV)
    if not chosen:
        import sys

        key = (system or sys.platform).lower()
        key = "darwin" if key.startswith("darwin") else "linux"
        chosen = SOURCE_PROFILES[key]
    return Path(chosen).expanduser()


def read_source_jar(profile: Path, reader=None) -> list[dict]:
    """Read the live jar out of the operator's running Chrome.

    Deliberately CDP rather than the cookie database: Chrome holds `li_at` and
    `liap` in memory and rewrites the on-disk rows while running, so a disk read
    intermittently finds a rotated token or no row at all - and both fail
    identically, much later, as a redirect to the request's own URL.
    """
    if reader is None:
        from .cdp import cookies_from_running_browser as reader

    jar = reader(profile)
    if isinstance(jar, dict):
        jar = [
            {"name": k, "value": v, "domain": ".linkedin.com", "path": "/"} for k, v in jar.items()
        ]
    names = {c.get("name") for c in jar}
    missing = [n for n in ESSENTIAL if n not in names]
    if missing:
        raise SessionExpired(
            f"the source browser has no {', '.join(missing)} cookie, so it is not "
            "signed in to LinkedIn. Sign in there first, then run `linkedin auth seed`."
        )
    return jar


# Asserted rather than assumed: an un-overridden headless browser reports
# HeadlessChrome on every request, a 1x 800x600 display, and - in a container -
# UTC. The browser has to claim a coherent identity that is consistent with how
# the account is actually used, and carrying bcookie/bscookie across from a
# different environment makes an inconsistency more visible, not less. See
# docs/voyager-headers.md.
def check_environment(probe: dict) -> list[str]:
    """Return the reasons this browser would look wrong to LinkedIn."""
    problems = []
    ua = str(probe.get("userAgent", ""))
    if "Headless" in ua:
        problems.append(f"the browser reports itself as headless in its user-agent ({ua!r})")
    if probe.get("webdriver") is True:
        problems.append("navigator.webdriver is true, which marks the page as automated")
    width = probe.get("screenWidth") or 0
    if width and width < 1280:
        problems.append(f"the reported screen is {width}px wide, which no desktop is")
    timezone = probe.get("timezone")
    expected = os.environ.get("TZ")
    if expected and timezone and timezone != expected:
        problems.append(f"the browser resolves timezone {timezone!r}, not {expected!r}")
    return problems


def seed(source_profile_path: str | None = None, *, request=None, reader=None) -> dict:
    """Copy the live jar into the managed profile and prove it authenticates."""
    call = request or supervisor.request
    jar = read_source_jar(source_profile(source_profile_path), reader=reader)

    result = call({"op": "seed", "cookies": jar})
    if result.get("error"):
        raise UpstreamError(f"the supervisor refused the seed: {result['error']}")

    problems = check_environment(result.get("environment") or {})
    if problems:
        raise UpstreamError(
            "the managed browser would be recognisable as automation, so the session "
            "was not seeded into it: " + "; ".join(problems)
        )

    # Not a bare 200: a flagged-but-not-yet-blocked session answers 200 too, which
    # is exactly what this account did shortly before it was invalidated.
    verified = result.get("verified") or {}
    if not verified.get("signed_in"):
        raise SessionExpired(
            "the seeded browser did not reach a signed-in page. If a checkpoint is "
            "waiting, clear it in your own Chrome and seed again."
        )
    return {
        "seeded": True,
        "cookies": len(jar),
        "member_urn": verified.get("member_urn"),
        "profile": result.get("profile"),
    }


def status(client) -> dict:
    """Whether the managed profile is still signed in, as a real call."""
    from .surfaces import profile as profile_surface

    me = profile_surface.get_me(client)
    return {
        "signed_in": True,
        "member_urn": me.get("profile_urn"),
        "public_id": me.get("public_id"),
    }
