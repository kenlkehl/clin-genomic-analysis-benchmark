"""Fail-closed filesystem isolation for model-controlled coding-agent CLIs.

The benchmark adapter is trusted orchestration code. The Codex, Claude, or
Antigravity process it launches is not: it can run shell commands chosen by the
model. This module places that process in a bubblewrap mount namespace
containing only the current cohort, its dictionary, a per-question scratch
directory, an ephemeral home, and the minimal software runtime needed to do the
analysis.

This is a confidentiality boundary.  The CLIs' own ``--sandbox`` and
``--add-dir`` flags remain useful mutation controls, but are not used as a
substitute for the outer mount namespace.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import REPO_ROOT, RUNS_DIR, gold_root


BWRAP = Path("/usr/bin/bwrap")
SANDBOX_SCHEMA_VERSION = "1"
SANDBOX_COHORT_DIR = Path("/data/cohort")
SANDBOX_DICTIONARY_DIR = Path("/data/dictionary")
SANDBOX_SCRATCH_DIR = Path("/work")
SANDBOX_HOME_DIR = Path("/home/agent")


class AgentIsolationError(RuntimeError):
    """The mandatory outer sandbox could not be constructed or verified."""


@dataclass(frozen=True)
class SandboxedCommand:
    """Command and scrubbed environment to pass to ``subprocess.run``."""

    command: list[str]
    environment: dict[str, str]
    cohort_dir: str
    data_dictionary_path: str
    scratch_dir: str
    host_ephemeral_home: Path


_SUPPORTED_ADAPTER_DIRS = {
    "antigravity_gemini",
    "claude_code",
    "codex_gpt",
    "codex_qwen_3.6_35B_A3B_GGUF_Unsloth_q4bitxl",
}


def supported_adapter(agent_cmd: str) -> str | None:
    """Return the adapter name when its model CLI uses this sandbox module."""
    try:
        parts = shlex.split(agent_cmd)
    except ValueError:
        return None
    if len(parts) == 1:
        script_value = parts[0]
    elif len(parts) == 2 and Path(parts[0]).name in {"bash", "sh"}:
        script_value = parts[1]
    else:
        return None
    script = Path(script_value).expanduser()
    resolved = script.resolve() if script.is_absolute() else (REPO_ROOT / script).resolve()
    for adapter in _SUPPORTED_ADAPTER_DIRS:
        expected = (REPO_ROOT / "adapters" / adapter / "run.sh").resolve()
        if resolved == expected:
            return adapter
    return None


def require_supported_adapter(agent_cmd: str) -> str:
    """Fail closed instead of silently running an unisolated coding agent."""
    adapter = supported_adapter(agent_cmd)
    if adapter is None:
        supported = ", ".join(sorted(_SUPPORTED_ADAPTER_DIRS))
        raise AgentIsolationError(
            "agent adapter is not registered as bubblewrap-isolated; "
            f"supported adapters: {supported}. Do not run with an untrusted "
            "adapter until it uses agent.isolation.sandboxed_agent_command."
        )
    return adapter


def sandbox_question_view(question: Mapping[str, Any]) -> dict[str, Any]:
    """Replace host landmarks with the only paths visible to the model CLI."""
    view = dict(question)
    dictionary_name = Path(str(question["data_dictionary_path"])).name
    view["cohort_dir"] = str(SANDBOX_COHORT_DIR)
    view["data_dictionary_path"] = str(SANDBOX_DICTIONARY_DIR / dictionary_name)
    view["scratch_dir"] = str(SANDBOX_SCRATCH_DIR)
    return view


_PASSTHROUGH_ENV = {
    # Locale, terminal, TLS, and explicitly configured proxies.
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TERM",
    "TZ",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    # Azure/OpenAI/Codex providers.
    "AZURE_OPENAI_API_KEY",
    "AZURE_OPENAI_ENDPOINT",
    "AZURE_OPENAI_API_VERSION",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "VLLM_TOKEN",
    "UNSLOTH_STUDIO_AUTH_TOKEN",
    "API_TOKEN",
    # Anthropic and Google Vertex.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "ANTHROPIC_VERTEX_REGION",
    "CLAUDE_CODE_USE_VERTEX",
    "CLOUD_ML_REGION",
    "GOOGLE_CLOUD_PROJECT",
    "GCLOUD_PROJECT",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    # Resource controls and non-secret CLI behavior.
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "TOKENIZERS_PARALLELISM",
    "NO_COLOR",
    "DISABLE_TELEMETRY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    "AGY_CLI_DISABLE_AUTO_UPDATE",
}


def _sandbox_environment(source: Mapping[str, str]) -> dict[str, str]:
    env = {name: source[name] for name in _PASSTHROUGH_ENV if source.get(name)}
    env.update({
        "HOME": str(SANDBOX_HOME_DIR),
        "USER": "agent",
        "LOGNAME": "agent",
        "TMPDIR": "/tmp",
        "XDG_CONFIG_HOME": str(SANDBOX_HOME_DIR / ".config"),
        "XDG_CACHE_HOME": str(SANDBOX_HOME_DIR / ".cache"),
        "XDG_STATE_HOME": str(SANDBOX_HOME_DIR / ".local/state"),
        "CODEX_HOME": str(SANDBOX_HOME_DIR / ".codex"),
        "CLAUDE_CONFIG_DIR": str(SANDBOX_HOME_DIR / ".claude"),
        "PYTHONNOUSERSITE": "1",
        "PATH": "/home/agent/.gemini/antigravity-cli/bin:"
                "/opt/benchmark-python-wrappers:/opt/benchmark-venv/bin:"
                "/home/linuxbrew/.linuxbrew/bin:"
                "/usr/local/bin:/usr/bin:/bin",
    })
    return env


def _toml_key(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_-]+", value) else json.dumps(value)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(
            f"{_toml_key(str(key))} = {_toml_value(item)}"
            for key, item in value.items()
        ) + " }"
    raise TypeError(f"unsupported TOML value: {type(value).__name__}")


_CODEX_TOP_LEVEL_KEYS = {
    "model",
    "model_provider",
    "model_reasoning_effort",
    "service_tier",
    "oss_provider",
}


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        loaded = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_sanitized_codex_config(
    *, destination: Path, source_home: Path, source_env: Mapping[str, str]
) -> None:
    """Copy runtime settings without projects, histories, memories, or logs."""
    base = _read_toml(source_home / "config.toml")
    profile_name = source_env.get("CODEX_PROFILE", "").strip()
    external_profile = (
        _read_toml(source_home / f"{profile_name}.config.toml")
        if profile_name and Path(profile_name).name == profile_name
        else {}
    )
    embedded_profiles = base.get("profiles") if isinstance(base.get("profiles"), dict) else {}
    embedded_profile = (
        embedded_profiles.get(profile_name, {})
        if profile_name and isinstance(embedded_profiles.get(profile_name), dict)
        else {}
    )

    effective: dict[str, Any] = {}
    for layer in (base, embedded_profile, external_profile):
        for key in _CODEX_TOP_LEVEL_KEYS:
            if key in layer:
                effective[key] = layer[key]
    explicit = {
        "model": source_env.get("CODEX_MODEL"),
        "model_provider": source_env.get("CODEX_MODEL_PROVIDER"),
        "model_reasoning_effort": source_env.get("CODEX_REASONING_EFFORT"),
    }
    effective.update({key: value for key, value in explicit.items() if value})

    providers: dict[str, Any] = {}
    for layer in (base, external_profile):
        candidate = layer.get("model_providers")
        if isinstance(candidate, dict):
            providers.update({
                str(name): config
                for name, config in candidate.items()
                if isinstance(config, dict)
            })
    selected_provider = str(effective.get("model_provider") or "").strip()
    if selected_provider and selected_provider in providers:
        providers = {selected_provider: providers[selected_provider]}
    elif selected_provider:
        providers = {}

    lines = [
        f"{_toml_key(key)} = {_toml_value(value)}"
        for key, value in effective.items()
        if isinstance(value, (str, bool, int, float, list, dict))
    ]
    for name, provider in providers.items():
        lines.append("")
        lines.append(f"[model_providers.{_toml_key(name)}]")
        for key, value in provider.items():
            if isinstance(value, (str, bool, int, float, list, dict)):
                lines.append(f"{_toml_key(str(key))} = {_toml_value(value)}")

    if profile_name:
        lines.append("")
        lines.append(f"[profiles.{_toml_key(profile_name)}]")
        for key, value in effective.items():
            if isinstance(value, (str, bool, int, float, list, dict)):
                lines.append(f"{_toml_key(key)} = {_toml_value(value)}")

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(lines).rstrip() + "\n")


def _seed_codex_home(home: Path, source_env: Mapping[str, str]) -> None:
    configured = source_env.get("CODEX_HOME", "").strip()
    source_home = Path(configured).expanduser() if configured else Path.home() / ".codex"
    codex_home = home / ".codex"
    _write_sanitized_codex_config(
        destination=codex_home / "config.toml",
        source_home=source_home,
        source_env=source_env,
    )
    # API-backed OpenAI runs may use Codex's auth file.  It is copied into the
    # disposable home, never accompanied by sessions/history/state databases.
    auth = source_home / "auth.json"
    if auth.is_file():
        shutil.copy2(auth, codex_home / "auth.json")


def _seed_claude_home(home: Path, source_env: Mapping[str, str]) -> None:
    claude_home = home / ".claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    settings: dict[str, Any] = {}
    effort = source_env.get("CLINGEN_CLAUDE_EFFORT", "").strip()
    if effort:
        settings["effortLevel"] = effort
    (claude_home / "settings.json").write_text(json.dumps(settings, indent=2))

    credential_value = source_env.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    source_credential = (
        Path(credential_value).expanduser()
        if credential_value
        else Path.home() / ".config/gcloud/application_default_credentials.json"
    )
    if source_credential.is_file():
        copied = home / ".config/gcloud/application_default_credentials.json"
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_credential, copied)


def _seed_google_adc(home: Path, source_env: Mapping[str, str]) -> None:
    """Copy ADC into a disposable home without exposing its host path."""
    credential_value = source_env.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    source_credential = (
        Path(credential_value).expanduser()
        if credential_value
        else Path.home() / ".config/gcloud/application_default_credentials.json"
    )
    if source_credential.is_file():
        copied = home / ".config/gcloud/application_default_credentials.json"
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_credential, copied)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _seed_antigravity_home(home: Path, source_env: Mapping[str, str]) -> None:
    """Seed only Antigravity runtime settings, never persistent agent state.

    Antigravity normally keeps trusted workspace paths, conversations, implicit
    context, memories, and logs under ``~/.gemini/antigravity-cli``.  Copying
    that tree would reintroduce the exact cross-run data channel the outer
    namespace is intended to close, so this builds a fresh configuration from
    a small allow-list instead.
    """
    source_value = source_env.get("CLINGEN_AGY_CONFIG_DIR", "").strip()
    source = (
        Path(source_value).expanduser()
        if source_value
        else Path.home() / ".gemini/antigravity-cli"
    )
    destination = home / ".gemini/antigravity-cli"
    destination.mkdir(parents=True, exist_ok=True)

    original = _read_json_object(source / "settings.json")
    original_gcp = original.get("gcp") if isinstance(original.get("gcp"), dict) else {}
    project = source_env.get("AGY_GCP_PROJECT", "").strip() or str(
        original_gcp.get("project", "")
    ).strip()
    location = source_env.get("AGY_GCP_LOCATION", "").strip() or str(
        original_gcp.get("location", "")
    ).strip()
    model = source_env.get("AGY_MODEL", "").strip() or str(
        original.get("model", "")
    ).strip()

    settings: dict[str, Any] = {
        "allowNonWorkspaceAccess": False,
        "artifactReviewPolicy": "always-proceed",
        # Antigravity 1.1.10's Linux nsjail cannot start /usr/bin/bash when
        # nested inside the mandatory outer bubblewrap namespace. The outer
        # namespace is the benchmark's authoritative containment boundary.
        "enableTerminalSandbox": False,
        "enableTelemetry": False,
        "toolPermission": "always-proceed",
        "permissions": {
            "allow": ["command(*)"],
            "ask": [],
            "deny": [
                "read_file(/home/agent/.gemini)",
                "read_file(/home/agent/.config/gcloud)",
                "read_url(*)",
                "execute_url(*)",
                "mcp(*)",
            ],
        },
        "trustedWorkspaces": [str(SANDBOX_SCRATCH_DIR)],
    }
    if model:
        settings["model"] = model
    gcp = {key: value for key, value in (("project", project), ("location", location)) if value}
    if gcp:
        settings["gcp"] = gcp
    (destination / "settings.json").write_text(json.dumps(settings, indent=2) + "\n")

    onboarding_source = _read_json_object(source / "cache/onboarding.json")
    onboarding = {
        key: bool(onboarding_source[key])
        for key in (
            "consumerOnboardingComplete",
            "enterpriseOnboardingComplete",
            "onboardingComplete",
        )
        if isinstance(onboarding_source.get(key), bool)
    }
    if onboarding:
        cache = destination / "cache"
        cache.mkdir()
        (cache / "onboarding.json").write_text(json.dumps(onboarding, indent=2) + "\n")
    try:
        default_project_id = (source / "cache/default_project_id.txt").read_text().strip()
    except OSError:
        default_project_id = ""
    if re.fullmatch(r"[A-Za-z0-9_-]{1,128}", default_project_id):
        cache = destination / "cache"
        cache.mkdir(exist_ok=True)
        (cache / "default_project_id.txt").write_text(default_project_id + "\n")

    # OAuth profiles can be bound to Antigravity's installation identifier.
    # This UUID is not conversation/context state and contains no host path.
    try:
        installation_id = (source / "installation_id").read_text().strip()
    except OSError:
        installation_id = ""
    if re.fullmatch(r"[0-9a-fA-F-]{36}", installation_id):
        (destination / "installation_id").write_text(installation_id + "\n")
    try:
        jetski_state = (source / "jetski_state.pbtxt").read_text()
    except OSError:
        jetski_state = ""
    uuid_match = re.search(
        r'^installation_uuid:\s*"([0-9a-fA-F-]{36})"\s*$',
        jetski_state,
        re.MULTILINE,
    )
    if uuid_match:
        (destination / "jetski_state.pbtxt").write_text(
            f'installation_uuid: "{uuid_match.group(1)}"\n'
        )

    # Some Antigravity integrations invoke this helper by name.  The installed
    # helper embeds the host user's absolute path, so generate a canonical shim.
    helper_dir = destination / "bin"
    helper_dir.mkdir()
    helper = helper_dir / "agentapi"
    helper.write_text('#!/bin/sh\nexec /opt/agent-cli/agy agentapi "$@"\n')
    helper.chmod(0o755)
    _seed_google_adc(home, source_env)

    # Antigravity's own keyring client is not portable across mount/user
    # namespaces, but it has a supported file fallback. Trusted setup reads one
    # exact keyring item and writes only that OAuth profile into the disposable
    # home. The desktop session bus and unrelated keyring items stay outside.
    oauth_profile = _lookup_antigravity_keyring_secret(source_env)
    if oauth_profile:
        try:
            parsed_profile = json.loads(oauth_profile)
            token = parsed_profile.get("token")
            valid_profile = (
                isinstance(parsed_profile.get("auth_method"), str)
                and isinstance(token, dict)
                and any(
                    isinstance(token.get(key), str) and token[key]
                    for key in ("access_token", "refresh_token")
                )
            )
        except (UnicodeDecodeError, ValueError, TypeError):
            valid_profile = False
        if valid_profile:
            token_file = destination / "antigravity-oauth-token"
            token_file.write_bytes(oauth_profile)
            token_file.chmod(0o600)


_ANTIGRAVITY_KEYRING_LOOKUP = r"""
import gi
import sys

gi.require_version("Secret", "1")
from gi.repository import Secret

schema = Secret.Schema.new(
    "org.freedesktop.Secret.Generic",
    Secret.SchemaFlags.NONE,
    {
        "service": Secret.SchemaAttributeType.STRING,
        "username": Secret.SchemaAttributeType.STRING,
    },
)
password = Secret.password_lookup_sync(
    schema, {"service": "gemini", "username": "antigravity"}, None
)
if password:
    sys.stdout.buffer.write(password.encode())
"""


def _lookup_antigravity_keyring_secret(
    source_env: Mapping[str, str],
) -> bytes | None:
    """Read only Antigravity's OAuth profile from the host Secret Service."""
    if not source_env.get("DBUS_SESSION_BUS_ADDRESS", "").strip():
        return None
    proc = subprocess.run(
        [
            "/usr/bin/python3",
            "-c",
            _ANTIGRAVITY_KEYRING_LOOKUP,
        ],
        env=dict(source_env),
        capture_output=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def _seed_home(home: Path, kind: str, source_env: Mapping[str, str]) -> None:
    for relative in (".cache", ".config", ".local/state"):
        (home / relative).mkdir(parents=True, exist_ok=True)
    if kind in {"codex", "codex_qwen"}:
        _seed_codex_home(home, source_env)
    elif kind == "claude":
        _seed_claude_home(home, source_env)
    elif kind == "antigravity":
        _seed_antigravity_home(home, source_env)


def _add_ro_bind(command: list[str], source: Path, destination: str | Path) -> None:
    if source.exists():
        command.extend(["--ro-bind", str(source), str(destination)])


def _runtime_binds(command: list[str], *, python_wrappers: Path | None = None) -> None:
    for path in (Path("/usr"), Path("/bin"), Path("/lib"), Path("/lib64")):
        _add_ro_bind(command, path, path)
    for path in (
        Path("/etc/alternatives"),
        Path("/etc/ca-certificates"),
        Path("/etc/ssl"),
        Path("/etc/hosts"),
        Path("/etc/localtime"),
        Path("/etc/nsswitch.conf"),
        Path("/etc/passwd"),
        Path("/etc/group"),
        Path("/etc/resolv.conf"),
    ):
        _add_ro_bind(command, path, path)

    linuxbrew = Path("/home/linuxbrew/.linuxbrew")
    _add_ro_bind(command, linuxbrew, linuxbrew)

    venv = Path(sys.prefix).resolve()
    if venv != Path(sys.base_prefix).resolve() and (venv / "bin").is_dir():
        _add_ro_bind(command, venv, "/opt/benchmark-venv")
        base_runtime = Path(sys.executable).resolve().parent.parent
        if not any(base_runtime.is_relative_to(root) for root in (Path("/usr"), Path("/bin"))):
            _add_ro_bind(command, base_runtime, "/opt/benchmark-python")
        if python_wrappers is not None:
            _add_ro_bind(command, python_wrappers, "/opt/benchmark-python-wrappers")


def _prepare_python_wrappers(temporary: Path) -> Path | None:
    """Expose the benchmark venv without retaining its host-home symlinks."""
    venv = Path(sys.prefix).resolve()
    if venv == Path(sys.base_prefix).resolve():
        return None
    site_packages = sorted((venv / "lib").glob("python*/site-packages"))
    if not site_packages:
        return None
    version_dir = site_packages[0].parent.name
    executable_name = Path(sys.executable).resolve().name
    wrappers = temporary / "python-wrappers"
    wrappers.mkdir()
    script = (
        "#!/bin/sh\n"
        f"export PYTHONHOME=/opt/benchmark-python\n"
        f"export PYTHONPATH=/opt/benchmark-venv/lib/{version_dir}/site-packages\n"
        f'exec /opt/benchmark-python/bin/{executable_name} "$@"\n'
    )
    for name in ("python", "python3", executable_name):
        wrapper = wrappers / name
        wrapper.write_text(script)
        wrapper.chmod(0o755)
    return wrappers


def _map_external_executable(command: list[str], bwrap: list[str]) -> list[str]:
    if (
        not Path(command[0]).is_absolute()
        and Path(sys.prefix).resolve() != Path(sys.base_prefix).resolve()
        and command[0] in {"python", "python3", Path(sys.executable).resolve().name}
    ):
        # Resolve through the canonical wrapper inside the namespace, not the
        # host venv's absolute symlink into the user's home directory.
        return command
    executable = shutil.which(command[0]) if not Path(command[0]).is_absolute() else command[0]
    if not executable:
        raise AgentIsolationError(f"agent CLI executable not found: {command[0]}")
    resolved = Path(executable).resolve()
    covered = (
        Path("/usr"),
        Path("/bin"),
        Path("/home/linuxbrew/.linuxbrew"),
        Path(sys.prefix).resolve(),
    )
    if any(resolved.is_relative_to(root) for root in covered if root.exists()):
        return command
    destination_dir = Path("/opt/agent-cli")
    _add_ro_bind(bwrap, resolved.parent, destination_dir)
    return [str(destination_dir / resolved.name), *command[1:]]


def _validate_host_mounts(
    *, cohort_dir: Path, data_dictionary_path: Path, scratch_dir: Path
) -> tuple[Path, Path, Path]:
    cohort = cohort_dir.expanduser().resolve()
    dictionary = data_dictionary_path.expanduser().resolve()
    scratch = scratch_dir.expanduser().resolve()
    if not cohort.is_dir():
        raise AgentIsolationError(f"cohort directory is missing: {cohort}")
    if not dictionary.is_file():
        raise AgentIsolationError(f"data dictionary is missing: {dictionary}")
    if not scratch.is_dir():
        raise AgentIsolationError(f"scratch directory is missing: {scratch}")

    protected = gold_root().expanduser().resolve()
    if (
        cohort == protected
        or cohort.is_relative_to(protected)
        or protected.is_relative_to(cohort)
    ):
        raise AgentIsolationError(
            f"refusing a cohort mount that overlaps the gold root: {cohort}"
        )
    if dictionary == protected or dictionary.is_relative_to(protected):
        raise AgentIsolationError(
            f"refusing a dictionary mount inside the gold root: {dictionary}"
        )
    if scratch == protected or scratch.is_relative_to(protected):
        raise AgentIsolationError(f"refusing a scratch mount inside the gold root: {scratch}")
    return cohort, dictionary, scratch


@contextmanager
def sandboxed_agent_command(
    command: Sequence[str],
    *,
    cohort_dir: str | Path,
    data_dictionary_path: str | Path,
    scratch_dir: str | Path,
    environment: Mapping[str, str] | None = None,
    home_kind: str,
) -> Iterator[SandboxedCommand]:
    """Yield a bubblewrap command for one model-controlled CLI invocation.

    There is intentionally no unsandboxed fallback.  A missing/failed bwrap
    executable becomes a technical failure and the benchmark run cannot be
    certified.
    """
    if not BWRAP.is_file() or not os.access(BWRAP, os.X_OK):
        raise AgentIsolationError(f"mandatory bubblewrap executable missing: {BWRAP}")
    if not command:
        raise AgentIsolationError("empty agent CLI command")

    cohort, dictionary, scratch = _validate_host_mounts(
        cohort_dir=Path(cohort_dir),
        data_dictionary_path=Path(data_dictionary_path),
        scratch_dir=Path(scratch_dir),
    )
    preexisting_findings = audit_agent_artifacts(scratch)
    if preexisting_findings:
        raise AgentIsolationError(
            "refusing to reuse scratch containing forbidden-artifact markers: "
            f"{preexisting_findings[:3]}"
        )
    source_env = dict(os.environ if environment is None else environment)

    with tempfile.TemporaryDirectory(prefix="clingen_agent_home_") as temporary:
        temporary_root = Path(temporary)
        home = temporary_root / "home"
        home.mkdir(mode=0o700)
        _seed_home(home, home_kind, source_env)
        python_wrappers = _prepare_python_wrappers(temporary_root)
        sandbox_env = _sandbox_environment(source_env)
        copied_adc = home / ".config/gcloud/application_default_credentials.json"
        if copied_adc.is_file():
            sandbox_env["GOOGLE_APPLICATION_CREDENTIALS"] = str(
                SANDBOX_HOME_DIR / ".config/gcloud/application_default_credentials.json"
            )

        bwrap = [
            str(BWRAP),
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--share-net",
            "--cap-drop", "ALL",
        ]
        _runtime_binds(bwrap, python_wrappers=python_wrappers)
        bwrap.extend([
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--ro-bind", str(cohort), str(SANDBOX_COHORT_DIR),
            "--ro-bind", str(dictionary),
            str(SANDBOX_DICTIONARY_DIR / dictionary.name),
            "--bind", str(scratch), str(SANDBOX_SCRATCH_DIR),
            "--bind", str(home), str(SANDBOX_HOME_DIR),
            "--chdir", str(SANDBOX_SCRATCH_DIR),
        ])
        inner = _map_external_executable(list(command), bwrap)
        bwrap.extend(["--", *inner])
        yield SandboxedCommand(
            command=bwrap,
            environment=sandbox_env,
            cohort_dir=str(SANDBOX_COHORT_DIR),
            data_dictionary_path=str(SANDBOX_DICTIONARY_DIR / dictionary.name),
            scratch_dir=str(SANDBOX_SCRATCH_DIR),
            host_ephemeral_home=home,
        )


def export_agent_session_audit(
    launch: SandboxedCommand,
    *,
    destination: Path,
    home_kind: str,
    max_total_bytes: int = 100 * 1024 * 1024,
) -> list[Path]:
    """Preserve tool/session logs without copying config or credentials.

    The destination must live outside the mounted scratch directory so a later
    retry cannot inspect prior transcripts.  Symlinks and special files are
    ignored to prevent a model-created link from escaping the ephemeral home.
    """
    candidates = {
        "antigravity": (
            Path(".gemini/antigravity-cli/annotations"),
            Path(".gemini/antigravity-cli/brain"),
            Path(".gemini/antigravity-cli/cache"),
            Path(".gemini/antigravity-cli/conversations"),
            Path(".gemini/antigravity-cli/crashes"),
            Path(".gemini/antigravity-cli/implicit"),
            Path(".gemini/antigravity-cli/knowledge"),
            Path(".gemini/antigravity-cli/log"),
        ),
        "claude": (Path(".claude/projects"), Path(".claude/debug")),
        "codex": (Path(".codex/sessions"), Path(".codex/log")),
        "codex_qwen": (Path(".codex/sessions"), Path(".codex/log")),
    }.get(home_kind, ())
    copied: list[Path] = []
    total = 0
    for relative_root in candidates:
        source_root = launch.host_ephemeral_home / relative_root
        if source_root.is_symlink():
            raise AgentIsolationError(
                f"agent session-audit root unexpectedly became a symlink: {relative_root}"
            )
        if not source_root.is_dir():
            continue
        for source in sorted(source_root.rglob("*")):
            if source.is_symlink():
                raise AgentIsolationError(
                    f"agent session-audit artifact is a symlink: "
                    f"{source.relative_to(launch.host_ephemeral_home)}"
                )
            if not source.is_file():
                continue
            try:
                size = source.stat().st_size
            except OSError:
                continue
            if size < 0 or total + size > max_total_bytes:
                return copied
            relative = source.relative_to(launch.host_ephemeral_home)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append(target)
            total += size
    return copied


def sandbox_backend_provenance() -> dict[str, Any]:
    if not BWRAP.is_file() or not os.access(BWRAP, os.X_OK):
        raise AgentIsolationError(f"mandatory bubblewrap executable missing: {BWRAP}")
    proc = subprocess.run(
        [str(BWRAP), "--version"], capture_output=True, text=True, timeout=10
    )
    if proc.returncode != 0:
        raise AgentIsolationError(f"bubblewrap version check failed: {proc.stderr.strip()}")
    return {
        "backend": "bubblewrap",
        "backend_version": proc.stdout.strip(),
        "schema_version": SANDBOX_SCHEMA_VERSION,
        "mode": "required_fail_closed",
        "network": "shared_host_network",
        "agent_readable_mounts": ["current_cohort", "current_data_dictionary", "runtime"],
        "agent_writable_mounts": ["current_question_scratch", "ephemeral_home", "tmpfs_tmp"],
        "host_home_visible": False,
        "repository_visible": False,
        "prior_runs_visible": False,
        "gold_root_visible": False,
    }


def run_isolation_preflight() -> dict[str, Any]:
    """Exercise the same namespace builder and prove forbidden roots are absent."""
    with tempfile.TemporaryDirectory(prefix="clingen_preflight_") as temporary:
        root = Path(temporary)
        cohort = root / "cohort"
        scratch = root / "scratch"
        dictionary = root / "dictionary.xlsx"
        canary = root / "outside-canary.txt"
        cohort.mkdir()
        scratch.mkdir()
        dictionary.write_text("public dictionary placeholder")
        canary.write_text("CLINGEN_ISOLATION_CANARY")

        forbidden = [
            str(gold_root().expanduser().resolve()),
            str(RUNS_DIR.resolve()),
            str(canary.resolve()),
        ]
        script = (
            "import os,sys; "
            "required=sys.argv[1:4]; forbidden=sys.argv[4:]; "
            "assert all(os.path.exists(p) for p in required), required; "
            "assert all(not os.path.exists(p) for p in forbidden), forbidden"
        )
        with sandboxed_agent_command(
            [
                "/usr/bin/python3", "-c", script,
                str(SANDBOX_COHORT_DIR),
                str(SANDBOX_DICTIONARY_DIR / dictionary.name),
                str(SANDBOX_SCRATCH_DIR),
                *forbidden,
            ],
            cohort_dir=cohort,
            data_dictionary_path=dictionary,
            scratch_dir=scratch,
            environment={},
            home_kind="none",
        ) as launch:
            proc = subprocess.run(
                launch.command,
                env=launch.environment,
                capture_output=True,
                text=True,
                timeout=20,
            )
        if proc.returncode != 0:
            diagnostic = (proc.stderr or proc.stdout).strip()
            raise AgentIsolationError(
                f"bubblewrap isolation preflight failed (exit {proc.returncode}): {diagnostic}"
            )
    return {
        "passed": True,
        "checks": {
            "allowed_mounts_visible": True,
            "gold_root_hidden": True,
            "prior_runs_hidden": True,
            "outside_canary_hidden": True,
        },
    }


_FORBIDDEN_MARKERS = (
    "chatbpc_benchmark_gold",
    "bpc_benchmark_review_6-19-26.xlsx",
    "gold_standard/",
    "scorecard.json",
    "scorecard.md",
)


def audit_agent_artifacts(root: Path, *, max_findings: int = 50) -> list[dict[str, str]]:
    """Scan model-visible outputs for evidence of forbidden artifact access."""
    if not root.exists():
        return []
    dynamic = (
        str(gold_root().expanduser().resolve()),
        str(RUNS_DIR.resolve()),
    )
    markers = tuple(marker.encode() for marker in (*_FORBIDDEN_MARKERS, *dynamic))
    findings: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if len(findings) >= max_findings:
            break
        try:
            mode = path.lstat().st_mode
        except OSError:
            findings.append({
                "path": str(path.relative_to(root)),
                "marker": "<unstatable-artifact>",
            })
            continue
        if stat.S_ISLNK(mode):
            findings.append({
                "path": str(path.relative_to(root)),
                "marker": "<symlink-artifact>",
            })
            continue
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            findings.append({
                "path": str(path.relative_to(root)),
                "marker": "<special-file-artifact>",
            })
            continue
        if path.name.endswith(".question.json"):
            continue
        try:
            with path.open("rb") as handle:
                tail = b""
                while chunk := handle.read(1024 * 1024):
                    data = tail + chunk
                    matched = next((marker for marker in markers if marker in data), None)
                    if matched is not None:
                        findings.append({
                            "path": str(path.relative_to(root)),
                            "marker": matched.decode("utf-8", "replace"),
                        })
                        break
                    tail = data[-512:]
        except OSError:
            findings.append({
                "path": str(path.relative_to(root)),
                "marker": "<unreadable-artifact>",
            })
    return findings
