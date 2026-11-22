"""Provider-owned native subscription state."""
from __future__ import annotations

import base64
import fcntl
import getpass
import hashlib
import json
import os
import subprocess
import stat
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    from .providers import Provider

ROOT = Path.home() / ".squid" / "provider-homes"

_AUTH_ENV = (
    "OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN",
    "OPENAI_BASE_URL", "CODEX_BASE_URL", "CHATGPT_BASE_URL",
    "OPENAI_FEDERATION_RULE_ID", "OPENAI_IDENTITY_TOKEN_FILE",
)


def home_for(provider_id: str, harness: str = "codex") -> Path:
    # Providers have already been validated, but this helper also accepts
    # provider IDs persisted in historical sessions.
    from .providers import _ID_RE
    if not _ID_RE.fullmatch(provider_id):
        raise ValueError("Invalid Codex provider ID")
    directory = ".claude" if harness == "claudecode" else ".codex"
    variable = "CLAUDE_CONFIG_DIR" if harness == "claudecode" else "CODEX_HOME"
    home = ROOT / provider_id / directory
    host = Path(os.environ.get(variable) or Path.home() / directory).expanduser().resolve()
    root = ROOT.parent.resolve() / ROOT.name
    if ROOT.resolve() != root or home.resolve() != root / provider_id / directory:
        raise ValueError(f"Provider {provider_id!r} has an invalid subscription home")
    if home.resolve() == host or home.resolve() == (Path.home() / directory).resolve():
        raise ValueError(f"Provider {provider_id!r} aliases the host Codex home or Claude configuration directory")
    return home


def ensure_home(provider_id: str, harness: str = "codex") -> Path:
    home = home_for(provider_id, harness)
    for directory in (ROOT, home.parent, home):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if directory.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError(f"Provider {provider_id!r} requires a private subscription home")
    auth = home / (".credentials.json" if harness == "claudecode" else "auth.json")
    if auth.is_symlink() or (auth.exists() and (
        not auth.is_file() or auth.stat().st_uid != os.geteuid()
        or auth.stat().st_mode & 0o077 or auth.stat().st_nlink != 1
    )):
        raise ValueError(f"Provider {provider_id!r} requires a private subscription auth file")
    return home


_CLAUDE_AUTH_ENV = (
    "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_REFRESH_TOKEN", "CLAUDE_CODE_OAUTH_SCOPES",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH", "CLAUDE_CODE_SKIP_VERTEX_AUTH",
    "CLAUDE_CODE_SKIP_FOUNDRY_AUTH", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_CODE_PROJECT_DIR_NAME", "CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST", "CLAUDE_CODE_BARE",
)


def environment(home: Path, harness: str = "codex") -> dict[str, str | None]:
    if harness == "claudecode":
        return {
            **dict.fromkeys(key for key in os.environ if key.startswith("ANTHROPIC_")),
            **dict.fromkeys(_CLAUDE_AUTH_ENV),
            "CLAUDE_CONFIG_DIR": str(home.resolve()),
            "ANTHROPIC_CONFIG_DIR": str(home.resolve() / ".no-console-profile"),
            # The directory survives reauthentication, including its project memory.
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        }
    return {
        **dict.fromkeys(_AUTH_ENV),
        "CODEX_HOME": str(home),
        "CODEX_SQLITE_HOME": str(home),
    }


def config_args(home: Path, harness: str = "codex") -> list[str]:
    if harness == "claudecode":
        return ["--setting-sources", "", "--settings", '{"forceLoginMethod":"claudeai"}']
    return [
        "-c", 'cli_auth_credentials_store="file"',
        "-c", 'forced_login_method="chatgpt"',
        "-c", 'model_provider="openai"',
        # This override sets the inference endpoint for the built-in provider.
        "-c", 'openai_base_url="https://chatgpt.com/backend-api/codex"',
        "-c", 'chatgpt_base_url="https://chatgpt.com/backend-api/"',
        "-c", f"sqlite_home={json.dumps(str(home))}",
    ]


CLAUDE_UNVERIFIED = "claude-signed-in-identity-unverified"


class ClaudeIdentityUnavailable(ValueError):
    """The store can be opened, but its cached login needs verification."""


class SubscriptionBusy(ValueError):
    """The credential store is locked by sign-in or a running turn."""


def account_verified(account: str | None) -> bool:
    return bool(account and account != CLAUDE_UNVERIFIED)


def _read_private_credentials(path: Path, label: str):
    # Validate the opened file. The CLI can replace credentials between the
    # directory check and this read, for example while refreshing its login.
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as credential:
        info = os.fstat(credential.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError(f"{label} credential file must be private to the provider")
        try:
            return json.load(credential)
        except (ValueError, UnicodeError):
            return None


def _claude_file_revision(home: Path) -> str | None:
    # On Linux the native credential comes from this file. On macOS the
    # file may be a fallback while a different credential lives in Keychain.
    if sys.platform != "linux":
        return None
    data = _read_private_credentials(home / ".credentials.json", "Claude")
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    keys = [oauth.get("accessToken"), oauth.get("refreshToken")]
    return "claude-file:" + hashlib.sha256(json.dumps(keys).encode()).hexdigest()


def _check_claude_profile(home: Path) -> None:
    if os.path.lexists(home / ".no-console-profile"):
        raise ValueError("Claude subscription store has an unexpected Console profile directory")
    settings = home / ".claude.json"
    if settings.is_symlink() or (settings.exists() and (not settings.is_file() or settings.stat().st_nlink != 1)):
        raise ValueError("Claude account metadata must be local to the provider")


def _claude_account(
    home: Path, *, allow_unidentified: bool = False, identify_managed: bool = False,
) -> str | None:
    from .config import CLAUDE_PATH

    if not CLAUDE_PATH:
        raise ValueError("Claude Code CLI not found in PATH")
    _check_claude_profile(home)
    credential_before = _claude_file_revision(home)
    env = os.environ.copy()
    for key, value in environment(home, "claudecode").items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    try:
        result = subprocess.run(
            [CLAUDE_PATH, *config_args(home, "claudecode"), "auth", "status", "--json"],
            cwd=home, env=env, capture_output=True, text=True, timeout=10,
        )
        status = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, ValueError) as exc:
        raise ValueError("Claude account status is unavailable") from exc
    if not isinstance(status, dict) or status.get("configDirectory") != str(home.resolve()):
        raise ValueError("Claude did not confirm its dedicated configuration directory")
    if status.get("apiProvider") != "firstParty" or status.get("forcedLoginMethod") != "claudeai":
        raise ValueError("Claude provider policy does not allow a native claude.ai subscription")
    if result.returncode == 1 and status.get("authMethod") == "none":
        return None
    if (result.returncode != 0 or status.get("loggedIn") is not True
            or status.get("authMethod") != "claude.ai"):
        raise ValueError("Claude did not confirm native claude.ai subscription authentication")
    email, organization = status.get("email"), status.get("orgId")
    if not all(isinstance(value, str) and value.strip() for value in (email, organization)):
        if allow_unidentified:
            return None
        raise ClaudeIdentityUnavailable("Claude did not report an account and organization")
    if credential_before != _claude_file_revision(home):
        raise ClaudeIdentityUnavailable("Claude credentials changed during the status check")
    if identify_managed:
        return hashlib.sha256(json.dumps([email.strip().casefold(), organization.strip()]).encode()).hexdigest()
    return credential_before or CLAUDE_UNVERIFIED


def _codex_tokens(home: Path) -> dict:
    data = _read_private_credentials(home / "auth.json", "Codex")
    if not isinstance(data, dict) or data.get("auth_mode") not in (None, "chatgpt"):
        return {}
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        return {}
    return tokens


def _codex_account(tokens: dict) -> str | None:
    if not tokens:
        return None
    try:
        claims = tokens["id_token"].split(".")[1]
        identity = json.loads(base64.urlsafe_b64decode(claims + "=" * (-len(claims) % 4)))
        subject = identity["sub"]
        if not isinstance(subject, str) or not subject:
            raise ValueError
        binding = json.dumps([identity.get("iss"), subject, tokens.get("account_id")])
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        # For legacy stores without an identifiable subject, treat a rewrite
        # conservatively as an account change.
        binding = json.dumps(tokens, sort_keys=True)
    return hashlib.sha256(binding.encode()).hexdigest()


def _read_generation(path: Path) -> str | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, encoding="ascii") as marker:
        info = os.fstat(marker.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("Subscription login generation must be a private regular file")
        return uuid.UUID(hex=marker.read(33)).hex


def _account_generation(home: Path, account: str | None) -> str | None:
    if not account_verified(account):
        return account
    generation = _read_generation(home / ".squid-generation")
    if generation is None:
        # Keep sessions and grants made before the first explicit replacement.
        return account
    return hashlib.sha256(f"{account}:{generation}".encode()).hexdigest()


def _write_private_marker(path: Path, contents: str) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="ascii", delete=False) as marker:
        temporary = Path(marker.name)
        try:
            marker.write(contents)
            marker.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def advance_generation(home: Path) -> None:
    """Advance the persistent login generation while holding the sign-in lock."""
    _write_private_marker(home / ".squid-generation", uuid.uuid4().hex)


_CLAUDE_MANAGED_GENERATION = ".squid-claude-managed-generation"


def uses_managed_claude_login(harness: str) -> bool:
    return harness == "claudecode" and sys.platform == "darwin"


def complete_managed_claude_login(home: Path) -> bool:
    """Verify native auth and record a new generation under the login lock."""
    identity = _claude_account(home, identify_managed=True)
    if not identity:
        return False
    _write_private_marker(home / _CLAUDE_MANAGED_GENERATION, json.dumps({
        "generation": uuid.uuid4().hex, "identity": identity,
    }))
    return True


def _managed_claude_account(home: Path, *, identity: str | None = None) -> str | None:
    _check_claude_profile(home)
    marker = _read_private_credentials(home / _CLAUDE_MANAGED_GENERATION, "Claude login marker")
    if not isinstance(marker, dict):
        return None
    generation, expected_identity = marker.get("generation"), marker.get("identity")
    if not isinstance(generation, str) or not isinstance(expected_identity, str):
        raise ValueError("Invalid Claude login marker")
    generation = uuid.UUID(hex=generation).hex
    if identity is not None and identity != expected_identity:
        raise ValueError("Claude account changed outside Squid. Sign in again to this provider.")
    return f"claude-managed:{generation}"


def account_identity(home: Path, harness: str = "codex", *, allow_unidentified: bool = False) -> str | None:
    """Read a credential binding or a signed-in marker without exposing secrets."""
    if uses_managed_claude_login(harness):
        identity = _claude_account(home, allow_unidentified=allow_unidentified, identify_managed=True)
        if identity is None:
            return None
        return _managed_claude_account(home, identity=identity) or CLAUDE_UNVERIFIED
    account = (_claude_account(home, allow_unidentified=allow_unidentified)
               if harness == "claudecode" else _codex_account(_codex_tokens(home)))
    return _account_generation(home, account)


def codex_quota_credentials(home: Path) -> tuple[dict, str | None]:
    tokens = _codex_tokens(home)
    return tokens, _account_generation(home, _codex_account(tokens))


def claude_quota_credentials(home: Path) -> tuple[str | None, str | None]:
    data = None
    if sys.platform == "darwin":
        suffix = hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:8]
        try:
            result = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-a", getpass.getuser(),
                 "-w", "-s", f"Claude Code-credentials-{suffix}"],
                capture_output=True, timeout=5,
            )
        except subprocess.TimeoutExpired as exc:
            raise ValueError("Claude Keychain lookup timed out") from exc
        if result.returncode == 0:
            raw = result.stdout.strip()
            if not raw.startswith(b"{"):
                raw = bytes.fromhex(raw.decode("ascii"))
            data = json.loads(raw)
        elif result.returncode != 44:  # errSecItemNotFound
            raise ValueError("Claude Keychain credentials unavailable")
    if data is None:
        data = _read_private_credentials(home / ".credentials.json", "Claude")
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token or not token.isascii() or not token.isprintable() or " " in token:
        return None, None
    revision = hashlib.sha256(json.dumps(oauth, sort_keys=True).encode()).hexdigest()
    return token, _account_generation(home, revision)


def lock(home: Path, *, login: bool) -> IO[bytes]:
    descriptor = os.open(
        home / ".squid.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600,
    )
    handle = os.fdopen(descriptor, "rb+")
    try:
        mode = fcntl.LOCK_EX if login else fcntl.LOCK_SH
        fcntl.flock(handle, mode | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise SubscriptionBusy("Subscription provider is busy. Try again after its active turns or sign-in finish.") from None
    except BaseException:
        handle.close()
        raise
    return handle


# Marks live agent bindings invalidated by a provider, Home, or agent change.
# Active session fingerprints carry the persistent account identity separately.
_agent_revisions: dict[str, int] = {}


def agent_revision(agent: str | None) -> int:
    return _agent_revisions.get(agent or "", 0)


def invalidate_agent(agent: str) -> None:
    _agent_revisions[agent] = agent_revision(agent) + 1


@dataclass(frozen=True)
class Binding:
    dedicated: bool
    revision: int
    home: Path | None = None
    account: str | None = None
    error: str | None = None
    harness: str = "codex"

    @property
    def can_reuse(self) -> bool:
        return not self.dedicated or (not self.error and account_verified(self.account))

    @property
    def recorded_account(self) -> str | None:
        return self.account if self.can_reuse else None


def bind(provider: Provider, agent: str | None, harness: str = "codex", *, revision: int | None = None,
         verify_claude: bool = True) -> Binding:
    if revision is None:
        revision = agent_revision(agent)
    if not provider.dedicated_login:
        return Binding(False, revision, harness=harness)
    try:
        home = ensure_home(provider.id, harness)
        with lock(home, login=False):
            # Rechecking an existing grant can happen in a SQLite write
            # transaction. Compare the already-verified credential on disk
            # here. The runner probes native status again before execution.
            if harness == "claudecode" and not verify_claude:
                account = (_managed_claude_account(home) if uses_managed_claude_login(harness)
                           else _account_generation(home, _claude_file_revision(home)))
            else:
                account = account_identity(home, harness)
        return Binding(True, revision, home, account, harness=harness)
    except (OSError, ValueError) as exc:
        directory = ".claude" if harness == "claudecode" else ".codex"
        return Binding(True, revision, ROOT / provider.id / directory, error=str(exc), harness=harness)


def session_fingerprint(runtime_fingerprint: str, binding: Binding) -> str:
    if not binding.dedicated:
        return runtime_fingerprint
    return f"{runtime_fingerprint}:{binding.home}:{binding.account}"


def invalidate_provider(provider_id: str) -> None:
    from .providers import get_provider
    from .resolve import split_agent_ref
    from .stats_db import clear_agent_sessions, list_agents

    provider = get_provider(provider_id)
    if provider is None or not provider.dedicated_login:
        return
    for agent in list_agents():
        harness, selected_provider = split_agent_ref(agent["harness"], agent.get("provider"))
        from .harnesses import default_provider_for
        if harness == provider.subscription_harness and (selected_provider or default_provider_for(harness)) == provider_id:
            invalidate_agent(agent["name"])
            clear_agent_sessions(agent["name"])
