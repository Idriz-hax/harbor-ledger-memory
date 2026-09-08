# Customizing Harbor Ledger Memory

This document describes the v1.1.0 server-wide declarative theme settings and
the stable Tide Atlas hooks exposed by the current client.

## Theme configuration

Theme settings live in the server TOML configuration under `[theme]`. The
default persistent file is `~/.config/harbor-ledger-memory/config.toml`:

```toml
[theme]
preset = "deepwater"
grid_size = 24
grid_opacity = 0.18
node_radius = 8
node_opacity = 1.0

[theme.palette]
accent = "#f0b956"
route = "#86c9c0"
```

`preset` is one of `deepwater`, `low-tide`, `moonlit`, or `salt-marsh`.
`grid_size` is an integer from 4 through 128. `grid_opacity` is 0 through 1.
`node_radius` is 2 through 32, and `node_opacity` is 0.1 through 1.0.

Palette names are alphanumeric identifiers containing `-` or `_`, at most 32
characters. There may be at most eight entries, and each value must be a
`#RRGGBB` or `#RRGGBBAA` color. Unknown theme keys are rejected.

The same object is returned as `theme` by the authenticated `GET
/api/v1/settings` endpoint. Theme changes may be submitted as an administrator
through `PUT /api/v1/settings` (or the equivalent POST) and require a service
restart. For example:

```json
{
  "theme": {
    "preset": "moonlit",
    "grid_size": 20,
    "palette": {"accent": "#e4bd7a", "route": "#a9c4eb"}
  }
}
```

The current Tide Atlas client reads `theme.preset` and maps it to its built-in
chart presets. The validated `palette` and numeric fields are server-owned and
persisted, but are not currently interpreted by the client as arbitrary CSS
tokens. Palette customization therefore does not override the client’s
built-in preset colors until a client version explicitly consumes those fields.
The client requests the settings from `GET /api/v1/settings` and saves the
preset selector with `PUT /api/v1/settings`.

## Tide Atlas hooks

The current client exposes these intentional CSS class hooks:

- `.tide-atlas` — the chart panel.
- `.tide-atlas .atlas-grid` — the decorative SVG grid.
- `.tide-atlas .index-port` — an index-type Cytoscape node.
- `.tide-atlas .landmark` — a regular Cytoscape node.

The chart container also has `role="application"` and the accessible label
`Tide Atlas memory chart. Pan and zoom to explore notes.` Chart elements carry
stable runtime data fields: cluster nodes use `id`, `label`, `kind`, `count`,
and `scope`; edges use `id`, `source`, `target`, and `weight`. These fields
are data used by the graph renderer, not user-editable CSS selectors.

## Security boundary

Customization is declarative and server-wide only. No user CSS is accepted,
stored, or executed. No user JavaScript is accepted, stored, or executed.
Theme configuration cannot contain CSS source, JavaScript source, HTML, URLs,
or executable expressions; arbitrary theme keys are rejected by validation.
