"""Conservative OpenClaw setup; JSON5 configuration is left for manual merge."""

from pathlib import Path


def setup() -> str:
    """Return actionable manual steps; JSON5 and memory slots remain untouched."""
    config = Path.home() / ".openclaw" / "openclaw.json"
    return (
        "From the HLM repository root run `cd integrations/openclaw && "
        "npm pack`, then `openclaw plugins install "
        "npm-pack:./harbor-ledger-memory-openclaw-2.0.0.tgz --force`. "
        f"Manually merge into {config}, preserving other settings:\n"
        '{ plugins: { allow: ["harbor-ledger-memory-openclaw"], entries: '
        '{ "harbor-ledger-memory-openclaw": { enabled: true, config: '
        '{ baseUrl: "http://127.0.0.1:8765", token: '
        '{ source: "env", provider: "default", id: "HLM_TOKEN" } } } }, '
        'slots: { memory: "harbor-ledger-memory-openclaw" } } }\n'
        "Selecting this slot replaces memory-core. Set HLM_TOKEN in OpenClaw's "
        "host secret store; no token or host config was written."
    )
