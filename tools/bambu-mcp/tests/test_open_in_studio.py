"""Opening a project must not silently create extra macOS Studio instances."""

import json
import subprocess

import pytest

from bambu_mcp import server


@pytest.fixture
def mac_launch(tmp_path, monkeypatch):
    project = tmp_path / "clamp test.3mf"
    project.touch()
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(server, "runner", lambda: type("Runner", (), {
        "binary": "/Applications/Bambu Studio.app/Contents/MacOS/BambuStudio"
    })())
    config = tmp_path / "Library/Application Support/BambuStudio/BambuStudio.conf"
    config.parent.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    return project, config, calls


@pytest.mark.parametrize("enabled", [True, "true"])
def test_open_in_studio_reuses_macos_app_when_preference_enabled(mac_launch, enabled):
    project, config, calls = mac_launch
    config.write_text(json.dumps({"app": {"single_instance": enabled}}))
    assert server.open_in_studio(str(project)).launched
    assert len(calls) == 1
    assert calls[0][0][0] == ["open", "-a", "/Applications/Bambu Studio.app", str(project)]
    assert calls[0][1] == {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }


@pytest.mark.parametrize("contents", [
    None, "{invalid", "[]", '{"app": null}', '{"app": {}}',
    '{"app": {"single_instance": false}}', '{"app": {"single_instance": "false"}}',
    '{"app": {"single_instance": 1}}',
])
def test_unsafe_or_unreadable_preference_does_not_launch(mac_launch, contents):
    project, config, calls = mac_launch
    if contents is not None:
        config.write_text(contents)
    with pytest.raises(ValueError, match="Keep only one Bambu Studio instance"):
        server.open_in_studio(str(project))
    assert calls == []


def test_macos_non_bundle_does_not_launch(mac_launch, monkeypatch):
    project, config, calls = mac_launch
    config.write_text('{"app": {"single_instance": true}}')
    monkeypatch.setattr(server, "runner", lambda: type("Runner", (), {"binary": "/usr/bin/bambu-studio"})())
    with pytest.raises(ValueError, match=".app bundle"):
        server.open_in_studio(str(project))
    assert calls == []


def test_open_in_studio_keeps_direct_executable_on_other_platforms(tmp_path, monkeypatch):
    project = tmp_path / "clamp.3mf"
    project.touch()
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server, "runner", lambda: type("Runner", (), {"binary": "/usr/bin/bambu-studio"})())
    calls = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert server.open_in_studio(str(project)).launched
    assert calls[0][0][0] == ["/usr/bin/bambu-studio", str(project)]


def test_missing_project_does_not_launch_studio(tmp_path, monkeypatch):
    def unexpected_launch(*args, **kwargs):
        pytest.fail("missing input must not launch a GUI")

    monkeypatch.setattr(server.subprocess, "Popen", unexpected_launch)
    with pytest.raises(ValueError, match="not found"):
        server.open_in_studio(str(tmp_path / "missing.3mf"))
