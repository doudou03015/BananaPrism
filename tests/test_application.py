from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication

from banana_prism import __version__
from banana_prism.application import (
    INSTANCE_LOCK_FILENAME,
    SINGLE_INSTANCE_EXIT_CODE,
    SingleInstanceError,
    build_services,
    create_main_window,
)
from banana_prism.i18n import tr


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def test_real_service_graph_uses_isolated_profile(
    app: QApplication,
    isolated_data_dir: Path,
) -> None:
    services = build_services()
    try:
        assert services.settings.data_dir == isolated_data_dir
        assert services.settings.path == isolated_data_dir / "settings.json"
        assert services.secret_store.path == isolated_data_dir / "secrets.v1.dpapi"
        assert not services.secret_store.exists

        window = create_main_window(services)
        assert window.windowTitle() == f"{tr('app.display_name')} · v{__version__}"
        assert window._settings_service is services.settings
        assert window._image_service is services.images
        assert window._storage_service is services.files
        assert window._auth_service is services.auth
        window.close()
    finally:
        services.close()


def test_injected_profile_never_probes_legacy_appdata(
    app: QApplication,
    isolated_data_dir: Path,
    monkeypatch,
) -> None:
    import banana_prism.utils.paths as paths

    def forbidden_legacy_probe(*args, **kwargs):
        raise AssertionError("real legacy profile must not be probed")

    monkeypatch.setattr(paths, "get_legacy_app_data_dir", forbidden_legacy_probe)
    services = build_services()
    try:
        assert services.settings.data_dir == isolated_data_dir
    finally:
        services.close()


def test_direct_smoke_mode_uses_ephemeral_profile_and_never_migrates_real_appdata(
    app: QApplication,
    tmp_path: Path,
    monkeypatch,
) -> None:
    from banana_prism.application import main

    fake_appdata = tmp_path / "real-appdata-trap"
    legacy_dir = fake_appdata / "NanaBananaStudio"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "settings.json"
    legacy_bytes = b'{"api_key":"must-stay-untouched"}'
    legacy_path.write_bytes(legacy_bytes)
    monkeypatch.delenv("BANANAPRISM_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(fake_appdata))

    assert main(["--smoke-test"]) == 0
    assert legacy_path.read_bytes() == legacy_bytes
    assert not (fake_appdata / "BananaPrism").exists()


def test_second_service_graph_is_rejected_until_owner_closes(
    app: QApplication,
    isolated_data_dir: Path,
    monkeypatch,
) -> None:
    services = build_services()
    close_calls: list[None] = []
    monkeypatch.setattr(services.images, "close", lambda: close_calls.append(None))
    try:
        assert services.instance_lock.isLocked()
        assert services.instance_lock.fileName() == str(
            isolated_data_dir / INSTANCE_LOCK_FILENAME
        )
        with pytest.raises(SingleInstanceError, match="已在此用户数据目录运行"):
            build_services()
    finally:
        services.close()
        services.close()

    assert close_calls == [None]
    replacement = build_services()
    replacement.close()


def test_service_construction_failure_releases_profile_lock(
    app: QApplication,
    isolated_data_dir: Path,
    monkeypatch,
) -> None:
    import banana_prism.application as application

    def fail_settings(*args, **kwargs):
        raise ValueError("synthetic settings failure")

    monkeypatch.setattr(application, "SettingsService", fail_settings)
    with pytest.raises(ValueError, match="synthetic settings failure"):
        application.build_services()

    probe = QLockFile(str(isolated_data_dir / INSTANCE_LOCK_FILENAME))
    assert probe.tryLock(0), probe.error()
    probe.unlock()


def test_smoke_mode_reports_single_instance_with_distinct_exit_code(
    app: QApplication,
    isolated_data_dir: Path,
    capsys,
) -> None:
    from banana_prism.application import main

    services = build_services()
    try:
        assert main(["--smoke-test"]) == SINGLE_INSTANCE_EXIT_CODE
        captured = capsys.readouterr()
        assert "BananaPrism 已在此用户数据目录运行" in captured.err
        assert str(isolated_data_dir) in captured.err
    finally:
        services.close()


def test_window_construction_failure_closes_services_and_releases_lock(
    app: QApplication,
    isolated_data_dir: Path,
    monkeypatch,
) -> None:
    import banana_prism.application as application

    def fail_window(_services):
        raise ValueError("synthetic window failure")

    monkeypatch.setattr(application, "create_main_window", fail_window)
    assert application.main(["--smoke-test"]) == 2

    probe = QLockFile(str(isolated_data_dir / INSTANCE_LOCK_FILENAME))
    assert probe.tryLock(0), probe.error()
    probe.unlock()


def test_two_smoke_processes_enforce_the_same_profile_lock(
    isolated_data_dir: Path,
) -> None:
    environment = os.environ.copy()
    environment["BANANAPRISM_DATA_DIR"] = str(isolated_data_dir)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    source_root = Path(__file__).parents[1] / "src"
    inherited_pythonpath = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(source_root) + (
        os.pathsep + inherited_pythonpath if inherited_pythonpath else ""
    )
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    owner = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "banana_prism",
            "--smoke-test",
            "--smoke-hold-ms",
            "3000",
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creation_flags,
    )
    try:
        lock_path = isolated_data_dir / INSTANCE_LOCK_FILENAME
        deadline = time.monotonic() + 10
        while not lock_path.is_file() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert lock_path.is_file(), owner.communicate(timeout=5)

        contender = subprocess.run(
            [sys.executable, "-m", "banana_prism", "--smoke-test"],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=creation_flags,
            check=False,
        )
        assert contender.returncode == SINGLE_INSTANCE_EXIT_CODE
        assert "BananaPrism 已在此用户数据目录运行" in contender.stderr

        owner_stdout, owner_stderr = owner.communicate(timeout=15)
        assert owner.returncode == 0, (owner_stdout, owner_stderr)
    finally:
        if owner.poll() is None:
            owner.kill()
        try:
            owner.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            owner.kill()
            owner.communicate(timeout=10)
        # On Windows the process can be reaped just before the final lock-file
        # handle is released.  Do not let pytest remove its base directory until
        # that release is observable.
        lock_path = isolated_data_dir / INSTANCE_LOCK_FILENAME
        release_deadline = time.monotonic() + 5
        while lock_path.exists() and time.monotonic() < release_deadline:
            time.sleep(0.05)
        assert not lock_path.exists()
