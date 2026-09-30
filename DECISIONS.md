# Decisions

## The two settings are `forge_host` and `instance_url`, read from config.yaml with `GITMON_*` env overrides

Named after what they are, not after the thing that reads them, and kept in
`config.yaml` beside every other operational knob so the existing hot-reload
("picked up on the next scan -- no restart") covers them for free.
`GITMON_FORGE_HOST` / `GITMON_INSTANCE_URL` follow the `GITMON_CONFIG`,
`GITMON_DB`, `GITMON_PORT`, `GITMON_GATE_SECRET` naming already in `app.py`, so
a deployment that prefers compose env over a config file has the same door.
An env var that is set but blank counts as absent, so a stray `FOO=` cannot
silently mask a good config line.

## Both settings are parsed in `collector.py`, the module that already loads the config

`collector.load_config` is the single place the runtime config is read, so
`collector.forge_host(config)` and `collector.instance_url(config)` sit next to
it and every caller gets the same parse and the same validation. No new module
was added for two strings, and `projects.py` does not learn to read files.

## `projects._forge_tail` takes the host as an argument; `build_projects` reads it from the module-level `projects.FORGE_HOST`, which `app.py` re-applies on each config read

`storage.get_projects` is outside this order's bound, so the value cannot be
threaded config -> storage -> `build_projects` as a parameter. `_forge_tail`
and `_keys_for` therefore take it explicitly (pure, unit-testable, no global),
and `build_projects` reads `FORGE_HOST` exactly ONCE at the top so every row in
one build is keyed against the same host. `app._config_or_empty` sets it from
`collector.forge_host` on every config read, which preserves hot-reload and
resets it to unset when the config will not parse rather than leaving a stale
value behind. Threading it through `storage.get_projects` would be tidier and
is the obvious follow-up once that file is in scope.

## A malformed setting is IGNORED (reads as unset), never raised

`forge_host` must be a bare hostname -- no scheme, port or path, since
`_forge_tail` compares against an already-normalized URL's host and strips any
port the URL carried, so a configured port could only ever fail to match.
`instance_url` must be `http(s)://` with no whitespace or quote characters.
Anything else returns `None`, exactly as if nothing were configured. The read
side already degrades rather than fails (`app._config_or_empty` swallows an
unparseable config), and these are an optional grouping hint and an optional
link: refusing to start over a typo in one would be the larger outage, and the
symptom announces itself (forge repos stop merging with their bare; the 403
loses its link).

## Unset means NO forge keying at all, and NO link in the control-plane 403

With `forge_host` unset a forge-shaped origin keys like any other non-hosting
remote, via `_tail2` -- which is the correct behaviour for the majority of
installations, which have no forge. With `instance_url` unset the 403 reads
"the control plane is reachable only through lanauth" and stops there rather
than pointing at a guessed address. Both are covered by tests that fail if the
default ever starts keying or linking on its own.

## Two occurrences of the private host remain, in files outside this order's bound

The order's private-identifier grep still matches `LICENSE:3` (the copyright
line, which is deliberate) and `archived.py:8-40` (a docstring quoting real
`github.com/<owner>/...` origin keys as examples). The order's bound is
`projects.py`, `app.py`, the config-loading module, `tests/` and `README.md`,
so neither was touched here. The in-bound test fixtures that used the same
owner handle (`tests/test_projects.py`, `tests/test_signals.py`) were moved to
`example-owner`. `config.example.yaml` is likewise out of bound, so the two new
settings are documented in the README only.
