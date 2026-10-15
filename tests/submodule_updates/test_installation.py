"""Managed updates exercise the real HACS integration class and child process."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.hacs.exceptions import HacsException
from custom_components.hacs.repositories.integration import HacsIntegrationRepository


async def executor(function, *args):
    return function(*args)


def repository(tmp_path, script):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/ha_submodule_update.py").write_text(script)
    (tmp_path / "integration-sources.json").write_text(
        json.dumps({"demo": {"repository": "owner/demo"}})
    )
    hacs = SimpleNamespace(
        core=SimpleNamespace(config_path=str(tmp_path)),
        hass=SimpleNamespace(async_add_executor_job=executor),
    )
    repo = HacsIntegrationRepository(hacs, "owner/demo")
    repo.data.domain = "demo"
    repo.data.installed_version = "1.0"
    repo.data.installed_commit = "b" * 40
    repo.data.last_version = "v2.0"
    repo.update_repository = AsyncMock()  # GitHub metadata refresh is outside this test.
    return repo


@pytest.mark.asyncio
@pytest.mark.parametrize("release", ["v2.0", "2.0"])
async def test_hacs_installer_records_the_git_installed_version_and_commit(tmp_path, release):
    repo = repository(
        tmp_path,
        'import json\nprint(json.dumps({"managed":True,"commit":"a"*40,"version":"2.0","selected_ref":"v2.0"}))\n',
    )
    repo.data.releases = True
    repo.data.last_version = release
    await repo.async_install_repository(version=release)
    assert repo.data.installed_version == release
    assert repo.data.installed_commit == "a" * 40
    assert repo.data.installed
    assert repo.ref == f"tags/{release}"
    assert not repo.pending_update
    repo.data.last_version = "v3.0"
    assert repo.pending_update


@pytest.mark.asyncio
async def test_failed_git_transaction_leaves_hacs_installed_metadata_unchanged(tmp_path):
    repo = repository(
        tmp_path, 'import sys\nprint("local patch conflict",file=sys.stderr)\nsys.exit(1)\n'
    )
    with pytest.raises(HacsException, match="local patch conflict"):
        await repo.async_install_repository(version="v2.0")
    assert repo.data.installed_version == "1.0"
    assert repo.data.installed_commit == "b" * 40


@pytest.mark.asyncio
async def test_default_branch_install_uses_commit_metadata(tmp_path):
    repo = repository(
        tmp_path,
        'import json\nprint(json.dumps({"managed":True,"commit":"a"*40,"version":"2.0","selected_ref":"main"}))\n',
    )
    repo.data.default_branch = "main"
    repo.data.releases = False
    repo.data.last_version = None
    repo.data.last_commit = "a" * 40
    await repo.async_install_repository(version="main")
    assert repo.data.installed_version is None
    assert not repo.pending_update


@pytest.mark.asyncio
async def test_cancellation_finishes_metadata_and_post_installation(tmp_path):
    import asyncio
    from unittest.mock import Mock

    marker = tmp_path / "installer-started"
    script = f'from pathlib import Path\nimport time,json\nPath({str(marker)!r}).touch()\ntime.sleep(0.15)\nprint(json.dumps({{"managed":True,"commit":"a"*40,"version":"2.0","selected_ref":"v2.0"}}))\n'
    repo = repository(tmp_path, script)
    repo.hacs.async_dispatch = Mock()

    async def post_install():
        repo.pending_restart = True

    repo._async_post_install = AsyncMock(side_effect=post_install)
    task = asyncio.create_task(repo.async_install(version="v2.0"))
    async with asyncio.timeout(3):
        while not marker.exists():
            await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert repo.data.installed_version == "v2.0"
    assert repo.data.installed_commit == "a" * 40
    assert repo.pending_restart
    repo._async_post_install.assert_awaited_once()


@pytest.mark.asyncio
async def test_branch_update_compares_abbreviated_upstream_sha(tmp_path):
    repo = repository(
        tmp_path,
        'import json\nprint(json.dumps({"managed":True,"commit":"a"*40,"version":"2.0","selected_ref":"main","requested_is_branch":True}))\n',
    )
    repo.data.default_branch = "main"
    repo.data.releases = False
    repo.data.last_version = None
    repo.data.last_commit = "a" * 7
    await repo.async_install_repository(version="main")
    assert repo.data.installed_commit == "a" * 7
    assert not repo.pending_update
    repo.data.last_commit = "b" * 7
    assert repo.pending_update


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", ["prod", "main"])
async def test_nondefault_branch_is_rejected_before_running_installer(tmp_path, selected):
    repo = repository(
        tmp_path,
        'import json\nprint(json.dumps({"managed":True,"commit":"a"*40,"version":"2.0","selected_ref":"prod","requested_is_branch":True}))\n',
    )
    repo.data.default_branch = "main"
    (tmp_path / "integration-sources.json").write_text(
        json.dumps(
            {"demo": {"repository": "owner/demo", "branch": "prod", "protected_branch": "prod"}}
        )
    )
    marker = tmp_path / "installer-started"
    script = tmp_path / "scripts/ha_submodule_update.py"
    script.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\n" + script.read_text()
    )
    with pytest.raises(HacsException, match="Non-default branch"):
        await repo.async_install_repository(version=selected)
    assert not marker.exists()
    assert repo.data.installed_version == "1.0"
    assert repo.data.installed_commit == "b" * 40
