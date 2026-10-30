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
- **as you type** what its BSX says without the app (syntax, catalogs, expressions); what needs
  the app (`deps.*`, locals, queries) waits for the next save.

Which app: the saved file when it declares one (`App(`), else the `arkitekt.target` setting, else
the project's default (`ARKITEKT_APP`, or `app.py`). A project is the nearest directory with a
`pyproject.toml`.

Settings (initialization options, or `workspace/didChangeConfiguration` under `arkitekt`):
`check.onSave` (default true), `check.timeout` (seconds, default 120), `target`.

Besides the standard protocol it answers `bsx/regions` (`{textDocument: {uri}}` →
`{regions: [{start, end, value, root, context, kind, exact}]}`), the blok literals of a document,
which the VS Code extension uses to complete BSX inside Python strings. It notifies
`arkitekt/status` (`{state: checking|ok|failed, app, errors?, warnings?, message?}`) and takes the
notification `arkitekt/check` (`{textDocument: {uri}}`) to check now.
