# arkitekt-lsp

The Arkitekt language server. It shows what `arkitekt check` finds in your app, underlined in
the BSX of the blok it is about, in any editor that speaks LSP.

```bash
uv add --dev arkitekt-lsp   # in the app's project: the server imports the app with its environment
```

The editor starts `arkitekt-lsp` (stdio) from that environment. Then:

- **on save** of a Python file it runs `arkitekt check --json-out` for the app the file belongs
  to, in a child process, and publishes every finding at its blok's string literal: the node's
  tag, or the line and column of a parse error. Findings follow their blok as the code moves;
  a blok edited since the check drops them until the next save, and is checked as you type;
- **as you type** what its BSX says, against what the last check said the blok declares: its
  `deps.*`, `args.*` and locals. A blok no check has seen yet is judged by the language alone, and
  a query's value always waits for the next save.

All of this is local: the server talks to nothing.

## Who would satisfy your dependencies (`--connect`)

```bash
arkitekt-lsp --connect                     # or the setting arkitekt.server.connect
arkitekt-lsp --server https://my.hub       # or arkitekt.server.url; the default is https://go.arkitekt.live
```

A flag overrides what the editor configures.

With the flag, the server also logs in, as an app of its own, with a token that can only read
(`rekuest_read`). It registers no agent. For each dependency protocol your app declares it asks who
would satisfy it, nothing registered, so a protocol you just wrote is asked about on save:

- above the protocol, a count: `3 agents · 2 connected`, and the slots nobody offers;
- a warning (information, for an optional one) when too few do. Never an error: what is missing
  may simply not be running yet;
- on hover of the protocol, or of `deps.<key>` in a blok, the agents and the count per slot.

A login nobody approves or a server that is gone leaves the server local, and says so.

An editor can also write a dependency for you: the request `arkitekt/rekuest/offers` (`{uri?}`) lists
the agents with what each offers, and `arkitekt/rekuest/declare` (`{uri, agent, className, actions,
states}`) writes the `@app.declare` class of what was picked, as an edit the editor applies. A
structure, a model or an enum is written as the class your app's registry has for it, services
included (`arkitekt>=11.4`'s report lists them); one it has none for leaves that action as a comment
naming it. The next save re-reads the registry, so adding the service is enough.

An editor can start this itself: the notification `arkitekt/rekuest/login` (`{url?}`) connects now,
also after a failure, and the request `arkitekt/rekuest/logout` leaves, revokes the login and forgets
it (`{forgotten}`). One that sets `clientShowsLogin` in its initialization options is told
`arkitekt/rekuest/challenge` (`{url, code, link, expiresIn}`) instead of getting a plain message.

Which app: the saved file when it declares one (`App(`), else the `arkitekt.target` setting, else
the project's default (`ARKITEKT_APP`, or `app.py`). A project is the nearest directory with a
`pyproject.toml`. A file with several `App`s and none named `app` needs a target that names one
(`many.py:api`), which then wins for that file. Changing the target checks the open files again.
The request `arkitekt/apps` (`{}` → `{apps: [{target, file, line, project}], target}`) lists the
apps of the workspace, by reading its files without importing any, for a client to offer.

Settings (initialization options, or `workspace/didChangeConfiguration` under `arkitekt`):
`check.onSave` (default true), `check.timeout` (seconds, default 120), `check.warnOnFStrings`
(default true), `target`.

A blok written as an f-string with a field gets a warning at its opening quote (`blok.generated`):
Python fills it in once, when the app starts, so it can't follow the app as it runs the way
`<foreach>` and `<if>` can. `arkitekt check` still checks what it evaluates to. The warning is the
editor's, because a check only ever sees the finished string.

Besides the standard protocol it answers `bsx/regions` (`{textDocument: {uri}}` →
`{regions: [{start, end, value, root, context, kind, exact}]}`), the blok literals of a document,
which the VS Code extension uses to complete BSX inside Python strings. It notifies
`arkitekt/status` (`{state: checking|ok|failed, app, errors?, warnings?, message?}`) and takes the
notification `arkitekt/check` (`{textDocument: {uri}}`) to check now.
