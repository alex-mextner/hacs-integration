"""Exercise the real subprocess bridge without HACS's network test fixtures."""

import importlib.util
import json
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "custom_components/hacs/utils/submodule_update.py"


def adapter():
    assert MODULE.exists(), "HACS must route managed updates through the Git installer"
    spec = importlib.util.spec_from_file_location("hacs_submodule_adapter", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "integration-sources.json").write_text(
        json.dumps({"demo": {"repository": "owner/demo"}})
    )
    return tmp_path


def test_managed_policy_and_unmanaged_fallback(config):
    assert adapter().is_managed(config, "demo")
    assert not adapter().is_managed(config, "ordinary")


def test_missing_policy_for_git_symlink_fails_closed(config):
    (config / "submodules/unknown/custom_components/unknown").mkdir(parents=True)
    (config / "custom_components").mkdir()
    (config / "custom_components/unknown").symlink_to(
        "../submodules/unknown/custom_components/unknown"
    )
    with pytest.raises(RuntimeError, match="policy"):
        adapter().is_managed(config, "unknown")


@pytest.mark.asyncio
async def test_adapter_returns_actual_installer_result(config):
    (config / "scripts/ha_submodule_update.py").write_text(
        'import json,sys\nassert sys.argv[1:] == ["--root",sys.argv[2],"--domain","demo","--repository","owner/demo","--ref","v2.0","--ha-version","2026.9.3"]\nprint(json.dumps({"managed":True,"commit":"a"*40,"version":"2.0"}))\n'
    )
    result = await adapter().async_install_managed(config, "demo", "owner/demo", "v2.0", "2026.9.3")
    assert result["commit"] == "a" * 40
    assert result["version"] == "2.0"


@pytest.mark.asyncio
async def test_failed_installer_never_falls_back_to_zip(config):
    (config / "scripts/ha_submodule_update.py").write_text(
        'import sys\nprint("protected fork is dirty",file=sys.stderr)\nsys.exit(1)\n'
    )
    with pytest.raises(RuntimeError, match="protected fork is dirty"):
        await adapter().async_install_managed(config, "demo", "owner/demo", "v2.0", "2026.9.3")


@pytest.mark.asyncio
async def test_missing_installer_fails_closed(config):
    with pytest.raises(RuntimeError, match="installer"):
        await adapter().async_install_managed(config, "demo", "owner/demo", "v2.0", "2026.9.3")


@pytest.mark.asyncio
async def test_invalid_installer_response_fails_closed(config):
    (config / "scripts/ha_submodule_update.py").write_text('print("not json")\n')
    with pytest.raises(RuntimeError, match="response"):
        await adapter().async_install_managed(config, "demo", "owner/demo", "v2.0", "2026.9.3")


def test_symlinked_config_root_still_protects_unregistered_git_links(config):
    (config / "submodules/unknown/custom_components/unknown").mkdir(parents=True)
    (config / "custom_components").mkdir()
    (config / "custom_components/unknown").symlink_to(
        "../submodules/unknown/custom_components/unknown"
    )
    alias = config.parent / (config.name + "-alias")
    alias.symlink_to(config, target_is_directory=True)
    with pytest.raises(RuntimeError, match="policy"):
        adapter().is_managed(alias, "unknown")


@pytest.mark.parametrize("content", ["{broken", "5", "null", "[]"])
def test_invalid_policy_is_a_clean_fail_closed_error(config, content):
    (config / "integration-sources.json").write_text(content)
    with pytest.raises(RuntimeError, match="policy"):
        adapter().is_managed(config, "demo")


@pytest.mark.asyncio
async def test_stalled_installer_has_a_bounded_wait(config):
    (config / "scripts/ha_submodule_update.py").write_text("import time\ntime.sleep(10)\n")
    module = adapter()
    module.INSTALL_TIMEOUT = 0.1
    with pytest.raises(RuntimeError, match="timed out"):
        await module.async_install_managed(config, "demo", "owner/demo", "v2.0", "2026.9.3")
