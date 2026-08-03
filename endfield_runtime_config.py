"""Portable configuration helpers for Endfield command-line tools.

The module has no data or Blender dependencies, so importing a public-source
checkout remains safe on machines that do not have any extracted game data.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from dataclasses import dataclass
from typing import Any, Mapping


TOKEN_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
RUNTIME_CONFIG_ENV = "ENDFIELD_RUNTIME_CONFIG"
RUNTIME_CONFIG_FORMAT = "EndfieldRuntimeConfig/1"
LOCAL_CONFIG_NAME = "endfield.runtime.local.json"


class ConfigurationError(RuntimeError):
    """Raised when a runtime-only path or setting has not been configured."""


@dataclass(frozen=True)
class RuntimeConfig:
    """Optional local runtime configuration and the directory that owns it."""

    paths: Mapping[str, Any]
    path: Path | None

    @property
    def directory(self) -> Path | None:
        return self.path.parent if self.path is not None else None

    def value(self, name: str) -> Any:
        return self.paths.get(name)


def user_runtime_config_path() -> Path:
    """Return the per-user local runtime config path used after first run."""

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data).expanduser().resolve() / "EndfieldExtractor" / LOCAL_CONFIG_NAME
    return Path(__file__).resolve().parent / LOCAL_CONFIG_NAME


def expand_tokens(value: Any, variables: Mapping[str, str] | None = None) -> Any:
    """Recursively expand ``${NAME}`` tokens without exposing host defaults."""

    available = dict(os.environ)
    available.update({str(key): str(item) for key, item in (variables or {}).items()})

    if isinstance(value, str):
        missing = sorted({name for name in TOKEN_RE.findall(value) if not available.get(name)})
        if missing:
            raise ConfigurationError(
                "Unresolved configuration variable(s): "
                + ", ".join(missing)
                + ". Set them in the environment or replace the tokens in the local config file."
            )
        return TOKEN_RE.sub(lambda match: available[match.group(1)], value)
    if isinstance(value, list):
        return [expand_tokens(item, available) for item in value]
    if isinstance(value, dict):
        return {str(key): expand_tokens(item, available) for key, item in value.items()}
    return value


def load_json_config(
    path: Path,
    *,
    expected_format: str | None = None,
    variables: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise ConfigurationError(f"Configuration file is missing: {resolved}")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError(f"Configuration file is not valid JSON: {resolved}: {error}") from error
    if not isinstance(payload, dict):
        raise ConfigurationError(f"Configuration root must be an object: {resolved}")
    payload = expand_tokens(
        payload,
        {
            "ENDFIELD_CONFIG_DIR": str(resolved.parent),
            "ENDFIELD_EXTRACTOR_ROOT": str(Path(__file__).resolve().parent),
            **dict(variables or {}),
        },
    )
    if expected_format and payload.get("format") != expected_format:
        raise ConfigurationError(
            f"Unsupported configuration format in {resolved}: {payload.get('format')!r}; "
            f"expected {expected_format!r}"
        )
    return payload


def load_runtime_config(
    cli_value: Path | str | None = None,
    *,
    root: Path | None = None,
    env_name: str = RUNTIME_CONFIG_ENV,
    local_name: str = LOCAL_CONFIG_NAME,
) -> RuntimeConfig:
    """Load CLI > environment > portable local runtime configuration.

    The portable local file is intentionally optional. An explicit CLI or
    environment configuration path is not optional, so spelling mistakes fail
    before a launcher can start a long-running process.
    """

    candidate: Path | None = None
    explicit = False
    if cli_value is not None and str(cli_value).strip():
        candidate = Path(str(cli_value)).expanduser()
        explicit = True
    elif os.environ.get(env_name, "").strip():
        candidate = Path(os.environ[env_name]).expanduser()
        explicit = True
    else:
        base_root = (root or Path(__file__).resolve().parent).resolve()
        candidates = [base_root / local_name]
        if base_root == Path(__file__).resolve().parent:
            user_candidate = user_runtime_config_path()
            if user_candidate not in candidates:
                candidates.append(user_candidate)
        candidate = next((item for item in candidates if item.is_file()), candidates[0])

    candidate = candidate.resolve()
    if not candidate.is_file():
        if explicit:
            raise ConfigurationError(
                f"Runtime configuration file is missing: {candidate}. "
                f"Pass a valid --runtime-config path or set {env_name}."
            )
        return RuntimeConfig(paths={}, path=None)

    payload = load_json_config(candidate, expected_format=RUNTIME_CONFIG_FORMAT)
    paths = payload.get("paths", {})
    if not isinstance(paths, dict):
        raise ConfigurationError(f"Runtime configuration paths must be an object: {candidate}")
    return RuntimeConfig(paths=paths, path=candidate)


def path_hint(
    *,
    cli_option: str,
    env_name: str,
    config_key: str,
) -> str:
    """Describe all supported settings without leaking host-specific defaults."""

    return (
        f"Pass {cli_option}, set {env_name}, or set paths.{config_key} in "
        f"{LOCAL_CONFIG_NAME} (or select it with --runtime-config)."
    )


def configured_path(
    cli_value: Path | str | None,
    *,
    env_name: str,
    label: str,
    config_value: Path | str | None = None,
    config_dir: Path | None = None,
    default: Path | str | None = None,
    kind: str | None = None,
    cli_option: str | None = None,
    config_key: str | None = None,
    env_aliases: tuple[str, ...] = (),
) -> Path:
    """Resolve CLI > environment > config > portable default and validate it."""

    raw: Path | str | None
    base: Path | None = None
    if cli_value is not None and str(cli_value).strip():
        raw = cli_value
    elif os.environ.get(env_name, "").strip():
        raw = os.environ[env_name]
    elif any(os.environ.get(alias, "").strip() for alias in env_aliases):
        raw = next(os.environ[alias] for alias in env_aliases if os.environ.get(alias, "").strip())
    elif config_value is not None and str(config_value).strip():
        raw = config_value
        base = config_dir
    else:
        raw = default
    if raw is None or not str(raw).strip():
        hint = (
            path_hint(
                cli_option=cli_option or "the matching CLI option",
                env_name=env_name,
                config_key=config_key or env_name.casefold(),
            )
            if cli_option
            else f"Set {env_name} or provide it in the local configuration file."
        )
        raise ConfigurationError(
            f"{label} is not configured. {hint}"
        )
    candidate = Path(expand_tokens(str(raw))).expanduser()
    if base is not None and not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve()
    if kind == "file" and not resolved.is_file():
        raise ConfigurationError(
            f"{label} file is missing: {resolved}. "
            + (
                path_hint(
                    cli_option=cli_option or "the matching CLI option",
                    env_name=env_name,
                    config_key=config_key or env_name.casefold(),
                )
                if cli_option
                else f"Configure it with the CLI option or {env_name}."
            )
        )
    if kind == "dir" and not resolved.is_dir():
        raise ConfigurationError(
            f"{label} directory is missing: {resolved}. "
            + (
                path_hint(
                    cli_option=cli_option or "the matching CLI option",
                    env_name=env_name,
                    config_key=config_key or env_name.casefold(),
                )
                if cli_option
                else f"Configure it with the CLI option or {env_name}."
            )
        )
    return resolved


def configured_optional_path(
    cli_value: Path | str | None,
    *,
    env_name: str,
    label: str,
    config_value: Path | str | None = None,
    config_dir: Path | None = None,
    kind: str | None = None,
    cli_option: str | None = None,
    config_key: str | None = None,
    env_aliases: tuple[str, ...] = (),
) -> Path | None:
    """Resolve an optional path with the same precedence as ``configured_path``."""

    if cli_value is not None and str(cli_value).strip():
        return configured_path(
            cli_value,
            env_name=env_name,
            env_aliases=env_aliases,
            label=label,
            kind=kind,
            cli_option=cli_option,
            config_key=config_key,
        )
    if os.environ.get(env_name, "").strip():
        return configured_path(
            os.environ[env_name],
            env_name=env_name,
            env_aliases=env_aliases,
            label=label,
            kind=kind,
            cli_option=cli_option,
            config_key=config_key,
        )
    for alias in env_aliases:
        if os.environ.get(alias, "").strip():
            return configured_path(
                os.environ[alias],
                env_name=env_name,
                env_aliases=env_aliases,
                label=label,
                kind=kind,
                cli_option=cli_option,
                config_key=config_key,
            )
    if config_value is not None and str(config_value).strip():
        return configured_path(
            None,
            env_name=env_name,
            env_aliases=env_aliases,
            label=label,
            config_value=config_value,
            config_dir=config_dir,
            kind=kind,
            cli_option=cli_option,
            config_key=config_key,
        )
    return None


def configured_path_list(
    cli_values: list[Path] | tuple[Path, ...] | None,
    *,
    env_name: str,
    label: str,
    config_values: list[str] | tuple[str, ...] | None = None,
    config_dir: Path | None = None,
    required: bool = False,
) -> tuple[Path, ...]:
    if cli_values:
        raw_values = [str(value) for value in cli_values]
        base = None
    elif os.environ.get(env_name, "").strip():
        raw_values = [value for value in os.environ[env_name].split(os.pathsep) if value]
        base = None
    elif config_values:
        raw_values = [str(value) for value in config_values]
        base = config_dir
    else:
        raw_values = []
        base = None
    if required and not raw_values:
        raise ConfigurationError(
            f"{label} is not configured. Repeat its CLI option, set {env_name} as an "
            f"{os.pathsep!r}-separated list, or add it to the local configuration file."
        )
    result = []
    for raw in raw_values:
        candidate = Path(expand_tokens(raw)).expanduser()
        if base is not None and not candidate.is_absolute():
            candidate = base / candidate
        resolved = candidate.resolve()
        if not resolved.is_file():
            raise ConfigurationError(f"{label} file is missing: {resolved}")
        result.append(resolved)
    return tuple(result)
