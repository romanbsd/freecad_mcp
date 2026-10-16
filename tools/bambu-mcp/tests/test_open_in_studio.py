"""Opening a project should reuse an existing macOS Studio application."""

import subprocess

from bambu_mcp import server


def test_open_in_studio_uses_launch_services_on_macos(tmp_path, monkeypatch):
    project = tmp_path / "clamp.3mf"
    project.touch()
    monkeypatch.setattr(server.sys, "platform", "darwin")
    monkeypatch.setattr(server, "runner", lambda: type("Runner", (), {
        "binary": "/Applications/BambuStudio.app/Contents/MacOS/BambuStudio"
    })())
    calls = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert server.open_in_studio(str(project)).launched
    assert len(calls) == 1
    assert calls[0][0][0] == ["open", "-a", "/Applications/BambuStudio.app", str(project)]
    assert calls[0][1] == {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "start_new_session": True,
    }


def test_open_in_studio_keeps_direct_executable_on_other_platforms(tmp_path, monkeypatch):
    project = tmp_path / "clamp.3mf"
    project.touch()
    monkeypatch.setattr(server.sys, "platform", "linux")
    monkeypatch.setattr(server, "runner", lambda: type("Runner", (), {
        "binary": "/usr/bin/bambu-studio"
    })())
    calls = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert server.open_in_studio(str(project)).launched
    assert calls[0][0][0] == ["/usr/bin/bambu-studio", str(project)]
