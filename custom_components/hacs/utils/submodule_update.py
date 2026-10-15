"""Bridge HACS installation to an explicitly configured Git-managed checkout.

Called by integration repositories; policy/installer errors must not trigger ZIP
fallback. Non-default branches cannot use HACS's default-branch update metadata.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import signal
import sys

INSTALL_TIMEOUT = 600
TERMINATE_GRACE = 30


def is_managed(root: Path, domain: str) -> bool:
    """Fail closed for invalid policies and unregistered Git-managed symlinks."""
    root = root.resolve()
    if not re.fullmatch(r"[a-z][a-z0-9_]*", domain or ""):
        return False
    policy = root / "integration-sources.json"
    if policy.is_file():
        try:
            policies = json.loads(policy.read_text())
            if not isinstance(policies, dict):
                raise TypeError("expected a mapping")
            if domain in policies:
                if not isinstance(policies[domain], dict):
                    raise TypeError("expected an integration mapping")
                return True
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("Invalid Git integration update policy") from exc
    deployed = root / "custom_components" / domain
    if deployed.is_symlink() and deployed.resolve().is_relative_to((root / "submodules").resolve()):
        raise RuntimeError(f"{domain}: Git-managed integration has no update policy")
    return False


def validate_ui_ref(root: Path, domain: str, ref: str, default_branch: str) -> None:
    """HACS update detection only tracks its default branch and published releases."""
    try:
        policy = json.loads((root / "integration-sources.json").read_text())[domain]
        branches = {policy.get("branch", "main"), policy.get("protected_branch")}
        protected = policy.get("protected_branch")
        if (ref in branches and ref != default_branch) or (
            ref == default_branch and protected is not None and protected != default_branch
        ):
            raise RuntimeError(
                "Non-default branch installs are unsupported in HACS; select a release "
                "or the repository default branch. Protected forks remain enforced."
            )
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise RuntimeError("Invalid Git integration update policy") from exc


async def stop_installer(process, communication):
    """Give the installer a rollback window, then reap its entire process group."""
    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(asyncio.shield(communication), TERMINATE_GRACE)
    except TimeoutError:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(communication, 5)
        except TimeoutError:
            # A daemonized Git hook may retain a pipe after the child was reaped.
            process._transport.close()


async def async_install_managed(
    root: Path, domain: str, repository: str, ref: str, ha_version: str
) -> dict:
    """Invoke the checked-in installer; failure never triggers ZIP fallback."""
    installer = root / "scripts/ha_submodule_update.py"
    if not installer.is_file():
        raise RuntimeError("Configured Git update installer is missing")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(installer),
        "--root",
        str(root),
        "--domain",
        domain,
        "--repository",
        repository,
        "--ref",
        ref,
        "--ha-version",
        ha_version,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    communication = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), INSTALL_TIMEOUT)
    except TimeoutError as exc:
        await stop_installer(process, communication)
        raise RuntimeError(
            "Git update installer timed out; inspect its transaction backup"
        ) from exc
    except asyncio.CancelledError:
        await stop_installer(process, communication)
        raise
    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace").strip() or "Git update installer failed")
    try:
        result = json.loads(stdout)
        if (
            not result.get("managed")
            or not result.get("version")
            or not re.fullmatch(r"[0-9a-f]{40}", result.get("commit", ""))
        ):
            raise ValueError("missing update metadata")
        return result
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("Invalid Git update installer response") from exc
