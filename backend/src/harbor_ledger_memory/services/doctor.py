"""Read-only, offline-first diagnostics for the local installation."""

from __future__ import annotations

import ipaddress
import json
import platform
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from sqlalchemy import inspect
from sqlalchemy.engine import make_url

from harbor_ledger_memory.catalog.database import CatalogSession
from harbor_ledger_memory.config import Settings, config_path
from harbor_ledger_memory.services.embeddings import embedding_runtime_available
from harbor_ledger_memory.services.status import CatalogStatusService
from harbor_ledger_memory.services.tokens import TokenService
from harbor_ledger_memory.services.validation import ValidationService

_HEAD_REVISION = "0018_proposal_base_diff"
_REQUIRED_TABLES = {
    "notes",
    "links",
    "scan_runs",
    "memory_write_proposals",
}


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: str
    message: str
    details: dict[str, object] | None = None


def _check(name: str, status: str, message: str, **details: object) -> DoctorCheck:
    return DoctorCheck(name, status, message, details or None)


def _installed_version() -> str:
    try:
        return version("harbor-ledger-memory")
    except PackageNotFoundError:
        return "unknown"


def _database_check(settings: Settings) -> DoctorCheck:
    try:
        parsed = make_url(settings.database_url)
        if parsed.get_backend_name() != "sqlite":
            return _check(
                "database", "warn", "database inspection supports SQLite only"
            )
        database = parsed.database
        if (
            not database
            or database != ":memory:"
            and not Path(database).expanduser().is_file()
        ):
            return _check("database", "fail", "database file is missing")
        from sqlalchemy import create_engine

        engine = create_engine(settings.database_url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            missing = sorted(_REQUIRED_TABLES - tables)
            if missing:
                return _check(
                    "database",
                    "fail",
                    "database schema is incomplete",
                    missing_tables=missing,
                )
            versions: set[str] = set()
            if "alembic_version" in tables:
                with engine.connect() as connection:
                    versions = {
                        str(row[0])
                        for row in connection.exec_driver_sql(
                            "SELECT version_num FROM alembic_version"
                        )
                    }
            if versions != {_HEAD_REVISION}:
                return _check(
                    "database",
                    "warn",
                    "database migration needed",
                    migration_needed=True,
                )
            return _check("database", "pass", "database schema is current")
        finally:
            engine.dispose()
    except Exception:
        return _check("database", "fail", "database could not be inspected")


def _catalog_checks(settings: Settings) -> tuple[DoctorCheck, DoctorCheck]:
    database = _database_check(settings)
    if database.status == "fail":
        return database, _check("index", "warn", "index checks skipped")
    try:
        from sqlalchemy import create_engine

        engine = create_engine(settings.database_url)
        session = CatalogSession(bind=engine)
        try:
            status = CatalogStatusService(session).read()
            validation = ValidationService(session).run()
            active_tokens = None
            if "api_tokens" in inspect(engine).get_table_names():
                token_service = TokenService(engine)
                try:
                    active_tokens = token_service.count_active()
                finally:
                    token_service.close()
        finally:
            session.close()
            engine.dispose()
        latest = status.latest_successful_scan
        index_status = "pass" if latest is not None else "warn"
        index_message = (
            "latest successful scan is available"
            if latest
            else "no successful scan recorded"
        )
        return database, _check(
            "index",
            index_status,
            index_message,
            indexed_notes=status.indexed_notes,
            scan_runs=status.scan_runs,
            validation_errors=len(validation.errors),
            validation_warnings=len(validation.warnings),
            active_tokens=active_tokens,
        )
    except Exception:
        return database, _check(
            "index", "fail", "index or validation could not be inspected"
        )


def _probe(url: str, installed: str) -> DoctorCheck:
    parsed = urlparse(url)
    try:
        hostname = parsed.hostname
    except ValueError:
        hostname = None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or not _is_loopback_host(hostname)
        or parsed.username
        or parsed.password
    ):
        return _check("server", "fail", "probe URL is malformed")
    try:
        safe_url = parsed._replace(query="", fragment="").geturl()
        health_request = Request(urljoin(safe_url.rstrip("/") + "/", "health"))
        with urlopen(health_request, timeout=3) as response:
            health = json.loads(response.read())
        openapi_request = Request(urljoin(safe_url.rstrip("/") + "/", "openapi.json"))
        with urlopen(openapi_request, timeout=3) as response:
            document = json.loads(response.read())
        server_version = document.get("info", {}).get("version")
        stale = bool(
            server_version and installed != "unknown" and server_version != installed
        )
        return _check(
            "server",
            "warn" if stale else "pass",
            "server is reachable"
            if not stale
            else "installed and server versions differ",
            health_ok=isinstance(health, dict),
            stale=stale,
            server_version=server_version,
        )
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return _check("server", "fail", "server health/openapi probe failed")


def _is_loopback_host(hostname: str) -> bool:
    """Accept only explicit localhost and loopback IP literals, never DNS."""
    if hostname.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback


def run_doctor(
    settings: Settings | None = None, url: str | None = None
) -> dict[str, object]:
    """Run diagnostics without changing configuration, vault, or catalog state."""
    checks: list[DoctorCheck] = []
    config_exists = config_path().is_file()
    try:
        effective = settings or Settings.load()
        checks.append(
            _check(
                "config",
                "pass" if config_exists else "warn",
                "configuration is valid"
                if config_exists
                else "configuration uses environment/defaults",
            )
        )
    except Exception:
        checks.append(_check("config", "fail", "configuration is missing or invalid"))
        checks.extend(
            (
                _check(
                    "vault",
                    "warn",
                    "vault check skipped because configuration is invalid",
                ),
                _check(
                    "database",
                    "warn",
                    "database check skipped because configuration is invalid",
                ),
                _check(
                    "index",
                    "warn",
                    "index check skipped because configuration is invalid",
                ),
                _check(
                    "embeddings",
                    "warn",
                    "embedding check skipped because configuration is invalid",
                ),
                _check(
                    "tokens",
                    "warn",
                    "token and MCP checks skipped because configuration is invalid",
                ),
                _check(
                    "install",
                    "pass",
                    "Python runtime and package are available",
                    python=platform.python_version(),
                    version=_installed_version(),
                ),
            )
        )
        report = _report(checks, url, _installed_version())
        return report

    vault_ok = effective.vault_path.expanduser().is_dir()
    checks.append(
        _check(
            "vault",
            "pass" if vault_ok else "fail",
            "vault is available" if vault_ok else "vault is missing",
        )
    )
    database, index = _catalog_checks(effective)
    checks.extend((database, index))
    configured_model = effective.memory.embedding_model
    if not configured_model:
        checks.append(
            _check(
                "embeddings",
                "pass",
                "embedding runtime disabled; lexical search is available",
            )
        )
    elif embedding_runtime_available():
        checks.append(
            _check("embeddings", "pass", "configured embedding runtime is available")
        )
    else:
        checks.append(
            _check("embeddings", "fail", "configured embedding runtime is unavailable")
        )
    checks.append(
        _check(
            "tokens",
            "pass",
            "token and MCP settings are readable",
            mcp_enabled=effective.mcp.enabled,
            api_enabled=effective.api.enabled,
        )
    )
    installed = _installed_version()
    checks.append(
        _check(
            "install",
            "pass",
            "Python runtime and package are available",
            python=platform.python_version(),
            version=installed,
        )
    )
    report = _report(checks, url, installed)
    return report


def _report(
    checks: list[DoctorCheck], url: str | None, installed: str
) -> dict[str, object]:
    if url:
        checks.append(_probe(url, installed))
    failures = sum(check.status == "fail" for check in checks)
    warnings = sum(check.status == "warn" for check in checks)
    return {
        "schema_version": 1,
        "ok": failures == 0,
        "exit_code": 2 if failures else (1 if warnings else 0),
        "checks": [asdict(check) for check in checks],
    }


__all__ = ["DoctorCheck", "run_doctor"]
