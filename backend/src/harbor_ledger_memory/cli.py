"""Vault-scope CLI commands and local application management."""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast
from urllib.parse import urlparse

import typer
from argon2 import PasswordHasher
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from harbor_ledger_memory.api.app import create_app
from harbor_ledger_memory.catalog.database import CatalogSession, create_database
from harbor_ledger_memory.catalog.models import Note
from harbor_ledger_memory.config import (
    FolderAccess,
    FolderRule,
    Settings,
    config_path,
    config_payload,
    read_persistent_config,
    update_persistent_config,
)
from harbor_ledger_memory.domain.retrieval import QueryRequest
from harbor_ledger_memory.graph.builder import GraphBuilder
from harbor_ledger_memory.services.activity import ActivityService
from harbor_ledger_memory.services.query import QueryService
from harbor_ledger_memory.services.scan import ScanService
from harbor_ledger_memory.services.search import SearchService
from harbor_ledger_memory.services.status import (
    CatalogStatusService,
    canonical_status_payload,
)
from harbor_ledger_memory.services.tokens import TokenService
from harbor_ledger_memory.services.validation import ValidationService
from harbor_ledger_memory.vault.boundary import VaultBoundary, VaultPathError
from harbor_ledger_memory.watcher import VaultWatchService

app = typer.Typer(
    name="hlm",
    help=(
        "Commands for the configured Obsidian vault scope and local "
        "application management."
    ),
    no_args_is_help=True,
)
config_app = typer.Typer(name="config", help="Manage local application settings.")
app.add_typer(config_app, name="config")
token_app = typer.Typer(name="token", help="Create, list, and revoke API tokens.")
app.add_typer(token_app, name="token")


def sync_opencode_skill() -> None:
    """Synchronize the bundled OpenCode skill into the user's config."""
    bundled = files("harbor_ledger_memory").joinpath(
        "skills", "harbor-ledger-memory", "SKILL.md"
    )
    content = bundled.read_bytes()
    target = Path.home() / ".config/opencode/skills/harbor-ledger-memory/SKILL.md"
    if target.is_file() and target.read_bytes() == content:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_bytes(content)
    temporary.replace(target)


@app.callback()
def cli_options(
    vault_path: str | None = typer.Option(
        None, "--vault-path", help="Override the configured vault directory."
    ),
    index_root: str | None = typer.Option(
        None, "--index-root", help="Override the configured vault-relative root."
    ),
    folder_rules: str | None = typer.Option(
        None, "--folder-rules", help="Override folder rules with a JSON array."
    ),
    embedding_model: str | None = typer.Option(
        None, "--embedding-model", help="Override the configured embedding model."
    ),
) -> None:
    """Apply one-shot CLI configuration overrides before a command runs."""
    try:
        sync_opencode_skill()
    except OSError as exc:
        typer.echo(f"Warning: could not synchronize OpenCode skill: {exc}", err=True)
    overrides = {
        "HLM_VAULT_PATH": vault_path,
        "HLM_INDEX_ROOT": index_root,
        "HLM_FOLDER_RULES": folder_rules,
        "HLM_EMBEDDING_MODEL": embedding_model,
    }
    for name, value in overrides.items():
        if value is not None:
            os.environ[name] = value


@contextmanager
def _catalog(settings: Settings) -> Generator[Session, None, None]:
    engine = create_database(settings.database_url)
    session = CatalogSession(bind=engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _load_settings() -> Settings:
    try:
        return Settings.load()
    except (ValidationError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _emit(value: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    if isinstance(value, list):
        for item in cast(list[object], value):
            typer.echo(str(item))
        return
    for key, item in cast(dict[str, object], value).items():
        typer.echo(f"{key}: {item}")


def _token_service(settings: Settings) -> TokenService:
    return TokenService(settings.database_url)


RULE_LEVELS: tuple[str, ...] = (
    FolderAccess.NONE.value,
    FolderAccess.READ.value,
    FolderAccess.PROPOSE_WRITE.value,
    FolderAccess.AUTO_WRITE.value,
)


def _parse_rule(raw: str) -> FolderRule:
    """Parse one ``--rule PATH=LEVEL`` value into a validated rule."""
    path_text, separator, level = raw.partition("=")
    if not separator or not path_text.strip():
        raise typer.BadParameter(
            f"rule '{raw}' must be a vault-relative PATH=LEVEL; "
            f"valid levels: {', '.join(RULE_LEVELS)}"
        )
    if level not in RULE_LEVELS:
        raise typer.BadParameter(
            f"rule '{raw}' has invalid level '{level}'; "
            f"valid levels: {', '.join(RULE_LEVELS)}"
        )
    try:
        return FolderRule.model_validate({"path": path_text, "access": level})
    except ValidationError as exc:
        raise typer.BadParameter(
            f"rule '{raw}' has an invalid path; "
            f"valid levels: {', '.join(RULE_LEVELS)}"
        ) from exc


def _rule_summary(rules: Sequence[FolderRule]) -> str:
    """Compact policy summary: non-read rules, else ``read-only``."""
    entries = [
        f"{rule.path.as_posix()}: {rule.access.value}"
        for rule in rules
        if rule.access is not FolderAccess.READ
    ]
    if not entries:
        return "read-only"
    return ", ".join(entries) + " (default read)"


def _scan_payload(result: Any) -> dict[str, Any]:
    return {
        "files_indexed": result.files_indexed,
        "broken_links": result.broken_links,
        "ambiguous_links": result.ambiguous_links,
        "indexed_paths": list(result.indexed_paths),
        "diagnostics": [
            {
                "code": diagnostic.code,
                "message": diagnostic.message,
                "path": diagnostic.path,
                "severity": diagnostic.severity,
                "line": diagnostic.line,
            }
            for diagnostic in result.diagnostics
        ],
    }


@config_app.command("show")
def config_show(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show the effective vault-scope configuration."""
    settings = _load_settings()
    payload = config_payload(settings)
    payload.update(
        {
            "server_host": settings.server_host,
            "server_port": settings.server_port,
            "frontend": {
                "enabled": settings.frontend.enabled,
                "mode": settings.frontend.mode,
                "public_origin": settings.frontend.public_origin,
            },
            "api": {"enabled": settings.api.enabled},
            "mcp": {"enabled": settings.mcp.enabled},
            "network": {
                "enabled": settings.network.enabled,
                "external": settings.network.external,
                "host": settings.network.host,
                "port": settings.network.port,
                "allowed_cidrs": list(settings.network.allowed_cidrs),
                "tls_cert": settings.network.tls_cert,
                "tls_key": settings.network.tls_key,
                "insecure_http": settings.network.insecure_http,
            },
        }
    )
    _emit(payload, json_output)


@config_app.command("set")
def config_set(
    vault_path: str | None = typer.Option(
        None, "--vault-path", help="Existing Obsidian vault directory."
    ),
    index_root: str | None = typer.Option(
        None, "--index-root", help="Vault-relative indexed subtree (default: .)."
    ),
    folder_rules: str | None = typer.Option(
        None, "--folder-rules", help="Folder rule JSON array."
    ),
    embedding_model: str | None = typer.Option(
        None, "--embedding-model", help="Optional embedding model name."
    ),
    frontend_enabled: bool | None = typer.Option(
        None, "--frontend-enabled/--frontend-disabled", help="Enable the frontend."
    ),
    frontend_mode: Literal["loopback", "lan"] | None = typer.Option(
        None, "--frontend-mode", help="Frontend bind mode."
    ),
    api_enabled: bool | None = typer.Option(
        None, "--api-enabled/--api-disabled", help="Enable bearer REST."
    ),
    mcp_enabled: bool | None = typer.Option(
        None, "--mcp-enabled/--mcp-disabled", help="Enable bearer MCP."
    ),
    network_enabled: bool | None = typer.Option(
        None, "--network-enabled/--network-disabled", help="Enable LAN networking."
    ),
    network_external: bool | None = typer.Option(
        None,
        "--network-external/--network-internal",
        help="Allow external network exposure.",
    ),
    network_host: str | None = typer.Option(
        None, "--network-host", help="Configured network host."
    ),
    network_port: int | None = typer.Option(
        None, "--network-port", help="Configured network port."
    ),
    public_origin: str | None = typer.Option(
        None, "--public-origin", help="Canonical browser Origin for LAN UI."
    ),
    allowed_cidrs: str | None = typer.Option(
        None, "--allowed-cidrs", help="CIDR allowlist as a JSON array."
    ),
    insecure_http: bool | None = typer.Option(
        None, "--insecure-http/--no-insecure-http", help="Acknowledge LAN HTTP."
    ),
    tls_cert: str | None = typer.Option(
        None, "--tls-cert", help="TLS certificate path for serving."
    ),
    tls_key: str | None = typer.Option(
        None, "--tls-key", help="TLS private key path for serving."
    ),
    server_host: str | None = typer.Option(
        None, "--server-host", help="Default bind host."
    ),
    server_port: int | None = typer.Option(
        None, "--server-port", help="Default bind port."
    ),
) -> None:
    """Persist selected settings in the application TOML file.

    The running service is intentionally not mutated; restart it to apply the
    new vault scope.
    """
    updates: dict[str, Any] = {}
    if vault_path is not None:
        updates["vault_path"] = vault_path
    if index_root is not None:
        updates["index_root"] = index_root
    if folder_rules is not None:
        try:
            updates["folder_rules"] = json.loads(folder_rules)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter("folder-rules must be a JSON array") from exc
    if embedding_model is not None:
        updates["embedding_model"] = embedding_model
    frontend: dict[str, Any] = {}
    if frontend_enabled is not None:
        frontend["enabled"] = frontend_enabled
    if frontend_mode is not None:
        frontend["mode"] = frontend_mode
    if public_origin is not None:
        frontend["public_origin"] = public_origin
    if frontend:
        updates["frontend"] = frontend
    if api_enabled is not None:
        updates["api"] = {"enabled": api_enabled}
    if mcp_enabled is not None:
        updates["mcp"] = {"enabled": mcp_enabled}
    network: dict[str, Any] = {}
    if network_enabled is not None:
        network["enabled"] = network_enabled
    if network_external is not None:
        network["external"] = network_external
    if network_host is not None:
        network["host"] = network_host
    if network_port is not None:
        network["port"] = network_port
    if allowed_cidrs is not None:
        try:
            parsed_cidrs = json.loads(allowed_cidrs)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter("allowed-cidrs must be a JSON array") from exc
        if not isinstance(parsed_cidrs, list):
            raise typer.BadParameter("allowed-cidrs must be a JSON array")
        network["allowed_cidrs"] = parsed_cidrs
    if insecure_http is not None:
        network["insecure_http"] = insecure_http
    if tls_cert is not None:
        network["tls_cert"] = tls_cert
    if tls_key is not None:
        network["tls_key"] = tls_key
    if network:
        updates["network"] = network
    if server_host is not None:
        updates["server_host"] = server_host
    if server_port is not None:
        updates["server_port"] = server_port
    if not updates:
        raise typer.BadParameter("provide at least one setting to update")
    persisted = read_persistent_config()
    database_url = os.environ.get(
        "DATABASE_URL", str(persisted.get("database_url", "sqlite:///data/memory.db"))
    )
    target = update_persistent_config(updates)
    activity = ActivityService(database_url)
    try:
        activity.record(
            "config",
            {"changed_keys": sorted(updates), "restart_required": True},
        )
    finally:
        activity.close()
    typer.echo(f"saved {target}")
    typer.echo("restart required for the running service to apply changes")


@config_app.command("set-password")
def config_set_password() -> None:
    """Interactively set the Argon2id verifier for LAN frontend access."""
    target = config_path()
    if target.exists() and target.stat().st_mode & 0o077:
        raise typer.BadParameter(
            f"config file {target} must be owner-only (mode 0600)"
        )
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise typer.BadParameter("passwords do not match")
    if not password:
        raise typer.BadParameter("password must not be empty")
    verifier = PasswordHasher().hash(password)
    update_persistent_config({"frontend": {"password_verifier": verifier}}, target)
    typer.echo(f"saved password verifier to {target}")


@token_app.command("create")
def token_create(
    name: str = typer.Option(..., "--name", "-n", help="Human-readable token name."),
    admin: bool = typer.Option(
        False, "--admin", help="Allow managing tokens and settings."
    ),
    rule: list[str] = typer.Option(
        [],
        "--rule",
        help="Folder rule PATH=LEVEL (repeatable). LEVEL: "
        "none|read|propose-write|auto-write.",
    ),
) -> None:
    """Create a token; the plaintext is printed once and never stored.

    Without --rule flags the token is read-only everywhere.
    """
    settings = _load_settings()
    rules = [_parse_rule(raw) for raw in rule]
    service = _token_service(settings)
    try:
        created = service.create(name, rules, admin)
    finally:
        service.close()
    admin_note = " [admin]" if created.admin else ""
    typer.echo(created.plaintext)
    typer.echo(
        f"created token '{created.name}'{admin_note} — "
        f"{_rule_summary(created.rules)}; store it now, it is shown only once"
    )


@token_app.command("list")
def token_list(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """List tokens (name, admin flag, created date, rules — never the secret)."""
    settings = _load_settings()
    service = _token_service(settings)
    try:
        records = service.list()
    finally:
        service.close()
    if json_output:
        _emit([record.as_dict() for record in records], True)
        return
    typer.echo(f"{'NAME':<20} {'ADMIN':<7} {'CREATED':<11} RULES")
    for record in records:
        typer.echo(
            f"{record.name:<20} "
            f"{'[ADMIN]' if record.admin else '':<7} "
            f"{record.created_at.date().isoformat():<11} "
            f"{_rule_summary(record.rules)}"
        )


@token_app.command("revoke")
def token_revoke(name: str = typer.Argument(..., help="Token name to revoke.")) -> None:
    """Revoke a token by name (idempotent)."""
    settings = _load_settings()
    service = _token_service(settings)
    try:
        record = service.revoke(name)
    finally:
        service.close()
    if record is None:
        typer.echo(f"Error: no token named '{name}'", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"revoked token '{record.name}'")


@app.command()
def scan(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Rebuild the disposable catalog from admitted vault Markdown files."""
    settings = _load_settings()
    result = ScanService.from_settings(settings).full_scan()
    payload = _scan_payload(result)
    if json_output:
        _emit(payload, True)
        return
    typer.echo(f"indexed {result.files_indexed} files")
    typer.echo(f"broken links: {result.broken_links}")
    typer.echo(f"ambiguous links: {result.ambiguous_links}")
    for path in result.indexed_paths:
        typer.echo(path)


@app.command()
def status(
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show status for the disposable catalog projection."""
    settings = _load_settings()
    with _catalog(settings) as session:
        current = CatalogStatusService(
            session, path_filter=VaultBoundary(settings).is_admitted
        ).read()
    payload = canonical_status_payload(settings, current)
    _emit(payload, json_output)


@app.command()
def search(
    query: str = typer.Argument(..., help="Literal text to search for."),
    limit: int = typer.Option(20, min=1, max=100, help="Maximum results."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Search the catalog's literal-token FTS projection."""
    settings = _load_settings()
    with _catalog(settings) as session:
        hits = SearchService(
            session, path_filter=VaultBoundary(settings).is_admitted
        ).search(query, limit=limit)
    payload = [
        {
            "path": hit.path,
            "title": hit.title,
            "summary": hit.summary,
            "snippet": hit.snippet,
            "score": hit.score,
        }
        for hit in hits
    ]
    _emit(payload, json_output)


@app.command()
def validate(
    output_format: Literal["text", "json"] = typer.Option(
        "text", "--format", help="Output format."
    ),
) -> None:
    """Report validation findings without proposing or applying vault edits."""
    settings = _load_settings()
    with _catalog(settings) as session:
        report = ValidationService(session).run()
    if output_format == "json":
        _emit(
            {
                "findings": [
                    {
                        "code": finding.code,
                        "severity": finding.severity,
                        "path": finding.path,
                        "message": finding.message,
                        "evidence": dict(finding.evidence),
                    }
                    for finding in report.findings
                ],
                "errors": len(report.errors),
                "warnings": len(report.warnings),
            },
            True,
        )
        return
    typer.echo(f"errors: {len(report.errors)}")
    typer.echo(f"warnings: {len(report.warnings)}")
    for finding in report.findings:
        path = f" [{finding.path}]" if finding.path else ""
        typer.echo(f"{finding.severity}: {finding.code}{path}: {finding.message}")


def _identity_for_node(boundary: VaultBoundary, value: str) -> str:
    """Validate a vault-relative identity through the canonical boundary."""
    candidate = PurePosixPath(value)
    try:
        boundary.resolve_vault_path(candidate)
    except (ValueError, VaultPathError) as exc:
        raise typer.BadParameter(
            "node path must be an admitted vault-relative path"
        ) from exc
    return candidate.as_posix()


def _node_payload(settings: Settings, path: str) -> dict[str, Any]:
    boundary = VaultBoundary(settings)
    identity = _identity_for_node(boundary, path)
    with _catalog(settings) as session:
        note = session.scalar(select(Note).where(Note.path == identity))
        if note is None:
            raise typer.BadParameter(f"node does not exist in the catalog: {identity}")
        graph = GraphBuilder(session, path_filter=boundary.is_admitted).build()
        graph_data = dict(graph.nodes.get(identity, {}))
        return {
            "path": identity,
            "title": note.title,
            "type": note.type,
            "status": note.status,
            "summary": note.summary,
            "content": note.content,
            "node_type": graph_data.get("node_type", "File"),
        }


@app.command("show-node")
def show_node(
    path: str = typer.Argument(
        ..., help="Vault-relative path in the configured scope."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show one catalog node after boundary validation."""
    settings = _load_settings()
    _emit(_node_payload(settings, path), json_output)


@app.command("show-neighbours")
def show_neighbours(
    path: str = typer.Argument(..., help="Vault-relative node path."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON output."),
) -> None:
    """Show deterministic incoming and outgoing graph neighbours."""
    settings = _load_settings()
    boundary = VaultBoundary(settings)
    identity = _identity_for_node(boundary, path)
    with _catalog(settings) as session:
        neighbours = GraphBuilder(session, path_filter=boundary.is_admitted).build()
        if identity not in neighbours:
            raise typer.BadParameter(f"node does not exist in the catalog: {identity}")
        from harbor_ledger_memory.services.status import GraphService

        values = GraphService(neighbours).neighbours(identity)
    payload = [
        {
            "path": item.path,
            "edge_type": item.edge_type,
            "direction": item.direction,
            "node_type": item.node_type,
            "explicit": item.explicit,
            "inferred": item.inferred,
            "confidence": item.confidence,
            "weight": item.weight,
            "source": item.source,
        }
        for item in values
    ]
    _emit(payload, json_output)


@app.command()
def serve(
    host: str | None = typer.Option(
        None, "--host", help="Bind host (default: 127.0.0.1)."
    ),
    port: int | None = typer.Option(None, "--port", help="Bind port (default: 8765)."),
) -> None:
    """Start the local FastAPI server with uvicorn."""
    settings = _load_settings()
    bind_host = host or settings.server_host
    bind_port = port or settings.server_port
    if not settings.frontend.enabled:
        raise typer.BadParameter("frontend.enabled must be true before serving")
    loopback = _is_loopback_host(bind_host)
    if settings.frontend.mode == "loopback":
        if not loopback:
            raise typer.BadParameter(
                "loopback frontend mode requires a loopback serve host",
                param_hint="--host",
            )
        ui_origin = _loopback_origin(bind_host, bind_port)
        tls_cert = tls_key = None
    else:
        ui_origin = _validate_lan_config(settings, bind_host)
        tls_cert = settings.network.tls_cert
        tls_key = settings.network.tls_key
    service = _token_service(settings)
    try:
        service.backfill_legacy_tokens(settings.folder_rules)
    finally:
        service.close()
    application = create_app(settings, ui_origin=ui_origin)
    scheme = "https" if tls_cert else "http"
    typer.echo(f"serving on {scheme}://{bind_host}:{bind_port}")
    import uvicorn

    if tls_cert and tls_key:
        uvicorn.run(
            application,
            host=bind_host,
            port=bind_port,
            log_level="info",
            ssl_certfile=tls_cert,
            ssl_keyfile=tls_key,
        )
    else:
        uvicorn.run(application, host=bind_host, port=bind_port, log_level="info")


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_lan_config(settings: Settings, bind_host: str) -> str:
    network = settings.network
    if _is_loopback_host(bind_host):
        raise typer.BadParameter("LAN frontend mode requires a non-loopback host")
    if not network.enabled:
        raise typer.BadParameter("network.enabled must be true for LAN serving")
    if not settings.frontend.password_verifier:
        raise typer.BadParameter("LAN serving requires a password verifier")
    for cidr in network.allowed_cidrs:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise typer.BadParameter("invalid CIDR allowlist entry") from exc
    origin = settings.frontend.public_origin
    try:
        parsed = urlparse(origin or "")
        port = parsed.port
    except ValueError as exc:
        raise typer.BadParameter("LAN serving requires a valid public_origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise typer.BadParameter("LAN serving requires a valid public_origin")
    if any(
        not _readable_file(path) for path in (network.tls_cert, network.tls_key) if path
    ):
        raise typer.BadParameter("TLS certificate and key must be readable files")
    has_cert = network.tls_cert is not None
    has_key = network.tls_key is not None
    if has_cert != has_key:
        raise typer.BadParameter("TLS certificate and key must be configured together")
    if parsed.scheme == "https" and not has_cert:
        raise typer.BadParameter("HTTPS LAN serving requires TLS certificate and key")
    if parsed.scheme == "http" and (has_cert or not network.insecure_http):
        raise typer.BadParameter(
            "LAN HTTP requires insecure_http acknowledgement and no TLS files"
        )
    display_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    default_port = 443 if parsed.scheme == "https" else 80
    port_suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme}://{display_host}{port_suffix}"


def _readable_file(path: str | None) -> bool:
    return path is not None and Path(path).is_file() and os.access(path, os.R_OK)


def _loopback_origin(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"http://{display_host}:{port}"


@app.command()
def watch(
    debounce: float = typer.Option(
        0.25, min=0.05, max=60.0, help="Debounce interval in seconds."
    ),
) -> None:
    """Watch admitted vault Markdown changes and rebuild the projection safely."""
    settings = _load_settings()
    boundary = VaultBoundary(settings)
    service = VaultWatchService(
        boundary, ScanService.from_settings(settings), debounce_seconds=debounce
    )
    service.start()
    typer.echo("watching admitted vault Markdown files; press Ctrl-C to stop")
    try:
        while True:
            service.flush()
            import time

            time.sleep(min(debounce, 1.0))
    except KeyboardInterrupt:
        pass
    finally:
        service.stop()


@app.command()
def query(
    query_text: str = typer.Argument(..., help="Query text to search for."),
    project: str | None = typer.Option(
        None, "--project", help="Active project path filter."
    ),
    include_excluded: bool = typer.Option(
        False, "--include-excluded", help="Include excluded activated nodes."
    ),
    output_format: Literal["text", "json"] = typer.Option(
        "json", "--format", help="Output format (json or text)."
    ),
) -> None:
    """Run a transparent memory query over the catalog projection."""
    settings = _load_settings()
    with _catalog(settings) as session:
        service = QueryService(
            session,
            retrieval_settings=settings.retrieval,
            memory_settings=settings.memory,
            path_filter=VaultBoundary(settings).is_admitted,
        )
        request = QueryRequest(
            query=query_text,
            active_project=project,
            include_excluded=include_excluded,
        )
        result = service.query(request)

    if output_format == "json":
        payload = _query_payload(result, include_excluded)
        _emit(payload, True)
        return

    # Human-readable output
    typer.echo(f"trace_id: {result.trace_id}")
    typer.echo(f"query: {result.query}")
    typer.echo(f"selected: {len(result.selected_memories)} memories")
    typer.echo(f"tokens: {result.total_estimated_tokens}")
    if result.selected_memories:
        typer.echo("")
        for idx, mem in enumerate(result.selected_memories, start=1):
            typer.echo(f"  {idx}. {mem.path}")
            typer.echo(
                f"     score: retrieval={mem.retrieval_score}, "
                f"activation={mem.activation_score}"
            )
            if mem.summary:
                typer.echo(f"     summary: {mem.summary}")
            if mem.excerpt:
                excerpt = mem.excerpt[:120]
                suffix = "..." if len(mem.excerpt) > 120 else ""
                typer.echo(f"     excerpt: {excerpt}{suffix}")
            typer.echo("")
    else:
        typer.echo("  (no memories selected)")


def _query_payload(result: Any, include_excluded: bool) -> dict[str, Any]:
    """Serialize a QueryResult to a JSON-friendly dict."""
    payload: dict[str, Any] = {
        "trace_id": str(result.trace_id),
        "query": result.query,
        "selected_memories": [
            {
                "path": mem.path,
                "title": mem.title,
                "summary": mem.summary,
                "excerpt": mem.excerpt,
                "retrieval_score": mem.retrieval_score,
                "activation_score": mem.activation_score,
                "reasons": list(mem.reasons),
                "estimated_tokens": mem.estimated_tokens,
            }
            for mem in result.selected_memories
        ],
        "total_estimated_tokens": result.total_estimated_tokens,
        "short_term_evidence": result.short_term_evidence.model_dump(mode="json"),
    }
    if include_excluded and result.excluded_nodes:
        payload["excluded_nodes"] = [
            {
                "path": node.path,
                "activation_score": node.activation_score,
                "hop": node.hop,
                "via_path": node.via_path,
                "edge_type": node.edge_type,
            }
            for node in result.excluded_nodes
        ]
    return payload


@app.command()
def update(
    auto: bool = typer.Option(False, "--auto", help="Apply update without prompting."),
) -> None:
    """Check for and apply project updates."""
    import subprocess
    import sys

    typer.echo("Checking for updates...")
    try:
        subprocess.run(["git", "fetch"], check=True, capture_output=True)
        local = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True
        ).stdout.strip()
        remote = subprocess.run(
            ["git", "rev-parse", "origin/main"], capture_output=True, text=True
        ).stdout.strip()

        if local == remote:
            typer.echo("Already up to date.")
            return

        if auto:
            typer.echo("Applying update (--auto)...")
        else:
            typer.echo("Update available. Apply? (y/n)")
            if input().strip().lower() != "y":
                typer.echo("Update skipped.")
                return

        subprocess.run(["git", "pull"], check=True)
        typer.echo("Reinstalling...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--force-reinstall", "."],
            check=True,
        )
        typer.echo("Update applied successfully.")
    except subprocess.CalledProcessError as e:
        typer.echo(f"Update failed: {e}", err=True)
        raise typer.Exit(code=1)


@app.command()
def feedback(
    trace_id: str = typer.Argument(..., help="The trace ID to provide feedback for"),
    relevant: list[str] | None = typer.Option(
        None, "--relevant", "-r", help="Paths that were relevant"
    ),
    irrelevant: list[str] | None = typer.Option(
        None, "--irrelevant", "-i", help="Paths that were irrelevant"
    ),
) -> None:
    """Provide feedback on a query trace to adjust adaptive weights."""
    s = _load_settings()
    with _catalog(s) as session:
        from harbor_ledger_memory.services.adaptive import (
            AdaptiveService,
            FeedbackValidationError,
        )
        boundary = VaultBoundary(s)

        adaptive = AdaptiveService(session, s.memory)
        try:
            adjustments = adaptive.apply_trace_feedback(
                trace_uuid=trace_id,
                relevant_paths=relevant,
                irrelevant_paths=irrelevant,
                path_filter=boundary.is_admitted,
            )
        except FeedbackValidationError as exc:
            raise typer.BadParameter(str(exc)) from exc
        session.commit()
        typer.echo(f"Applied {adjustments} adjustment(s) for trace {trace_id}")


def main() -> None:
    """Console-script entry point."""
    app()


__all__ = ["app", "main"]
