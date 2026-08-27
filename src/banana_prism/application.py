"""BananaPrism desktop application bootstrap.

This module is deliberately small and injectable: creating the production
services is separate from running Qt so startup, migration, and frozen-build
smoke tests can exercise the real wiring without contacting an API.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QCoreApplication, QLockFile, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from banana_prism import __version__
from banana_prism.constants import APP_ID
from banana_prism.i18n import tr, user_error_text
from banana_prism.services.auth_service import AuthService
from banana_prism.services.file_service import FileService
from banana_prism.services.image_service import ImageService
from banana_prism.services.log_service import LogService
from banana_prism.services.migration_service import MigrationError
from banana_prism.services.secret_store import SecretStore, SecretStoreError
from banana_prism.services.settings_service import SettingsError, SettingsService
from banana_prism.ui.main_window import MainWindow
from banana_prism.utils.paths import DATA_DIR_ENV, get_app_data_dir, get_resource_path


INSTANCE_LOCK_FILENAME = ".BananaPrism.instance.lock"
SINGLE_INSTANCE_EXIT_CODE = 3


class SingleInstanceError(RuntimeError):
    """Raised when the selected BananaPrism profile is already in use."""

    def __init__(self, title: str, message: str, *, lock_path: Path) -> None:
        super().__init__(message)
        self.title = title
        self.lock_path = lock_path


@dataclass(slots=True, weakref_slot=True)
class RuntimeServices:
    """The concrete production service graph owned by the application."""

    secret_store: SecretStore
    settings: SettingsService
    auth: AuthService
    files: FileService
    logs: LogService
    images: ImageService
    instance_lock: QLockFile
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        """Close runtime resources and release the profile lock exactly once."""

        if self._closed:
            return
        self._closed = True
        try:
            self.images.close()
        finally:
            self.instance_lock.unlock()


def _acquire_instance_lock(data_dir: Path) -> QLockFile:
    """Acquire the non-blocking lock protecting one resolved data directory."""

    lock_path = data_dir / INSTANCE_LOCK_FILENAME
    lock = QLockFile(str(lock_path))
    if lock.tryLock(0):
        return lock

    if lock.error() == QLockFile.LockError.LockFailedError:
        try:
            pid, hostname, application = lock.getLockInfo()
        except (OSError, RuntimeError, TypeError, ValueError):
            owner = tr("application.single_instance.owner_unknown")
        else:
            owner = tr(
                "application.single_instance.owner",
                pid=pid,
                hostname=hostname or "?",
                application=application or "?",
            )
        title = tr("application.single_instance.title")
        message = tr(
            "application.single_instance.body",
            data_dir=data_dir,
            owner=owner,
        )
    else:
        title = tr("application.instance_lock_failed.title")
        message = tr(
            "application.instance_lock_failed.body",
            data_dir=data_dir,
            error=lock.error().name,
        )
    raise SingleInstanceError(title, message, lock_path=lock_path)


def build_services() -> RuntimeServices:
    """Build production services while preserving migration safety semantics.

    No explicit data directory is passed here.  In production that permits the
    first-run legacy probe; under tests or portable diagnostics the
    ``BANANAPRISM_DATA_DIR`` override is detected by ``SettingsService`` and
    disables any implicit probe of the real roaming profile.
    """

    # Acquire before SettingsService starts migration so two processes can never
    # read or rewrite the same profile concurrently.  Resolving the path here
    # does not alter migration's legacy-probe rules.
    instance_lock = _acquire_instance_lock(get_app_data_dir())
    images: ImageService | None = None
    try:
        # SettingsService owns construction of the production store.  Supplying
        # an externally-created store intentionally disables implicit legacy
        # probing, which is the safe behavior for tests and embeddings.
        settings = SettingsService(auto_migrate=True)
        secret_store = settings.secret_store
        auth = AuthService(secret_store, settings)
        files = FileService(settings)
        logs = LogService(settings.data_dir)
        images = ImageService()
        return RuntimeServices(
            secret_store,
            settings,
            auth,
            files,
            logs,
            images,
            instance_lock,
        )
    except BaseException:
        try:
            if images is not None:
                images.close()
        except Exception:
            # Preserve the construction error while still releasing the lock.
            pass
        finally:
            instance_lock.unlock()
        raise


def create_main_window(services: RuntimeServices) -> MainWindow:
    return MainWindow(
        settings_service=services.settings,
        image_service=services.images,
        storage_service=services.files,
        log_service=services.logs,
        auth_service=services.auth,
    )


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="BananaPrism")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="construct the real application, process events, then exit",
    )
    parser.add_argument(
        "--smoke-hold-ms",
        type=int,
        default=0,
        help=argparse.SUPPRESS,
    )
    return parser


def _configure_qt_application(app: QApplication) -> None:
    QCoreApplication.setOrganizationName(APP_ID)
    QCoreApplication.setOrganizationDomain("banana-prism.local")
    QCoreApplication.setApplicationName(APP_ID)
    QCoreApplication.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(True)
    icon_path = get_resource_path("banana_prism.ico")
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))


def _show_startup_window(window: MainWindow) -> None:
    """Show the workspace maximized with a usable centered restore geometry.

    ``MainWindow`` intentionally has a roomy desktop default size.  Fit that
    size to the current screen's work area before maximizing so platforms that
    ignore the maximize hint still show an accessible window, and restoring a
    maximized window never brings it back partially off-screen.
    """

    # A real show (without yielding to the event loop) is required here rather
    # than only forcing ``winId()``.  Qt's offscreen/minimal backends otherwise
    # discard a move made before ``showMaximized()`` when the window is later
    # restored.  Painting is still deferred, so production does not flash a
    # normal window before the immediately following maximize request.
    window.show()
    screen = window.screen() or QApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        if available.isValid() and not available.isEmpty():
            # Showing once creates the native handle before measuring the
            # frame.  On Windows the title bar is otherwise absent from
            # frameGeometry, leaving the restored window below true center.
            initial_frame = window.frameGeometry()
            frame_extra_width = max(0, initial_frame.width() - window.width())
            frame_extra_height = max(0, initial_frame.height() - window.height())
            window.resize(
                min(window.width(), max(1, available.width() - frame_extra_width)),
                min(window.height(), max(1, available.height() - frame_extra_height)),
            )
            frame = window.frameGeometry()
            frame.moveCenter(available.center())

            # If a minimum size exceeds a very small work area, keep the title
            # bar at the visible top-left instead of centering it off-screen.
            max_x = max(available.left(), available.right() - frame.width() + 1)
            max_y = max(available.top(), available.bottom() - frame.height() + 1)
            window.move(
                min(max(frame.left(), available.left()), max_x),
                min(max(frame.top(), available.top()), max_y),
            )

    window.showMaximized()


def _report_startup_error(app: QApplication, message: str, *, headless: bool) -> None:
    safe_message = tr(
        "application.startup_failed.body",
        reason=message,
    )
    if headless:
        if sys.stderr is not None:
            print(safe_message, file=sys.stderr)
        return
    QMessageBox.critical(None, tr("application.startup_failed.title"), safe_message)


def _report_single_instance_error(
    app: QApplication,
    error: SingleInstanceError,
    *,
    headless: bool,
) -> None:
    del app
    if headless:
        if sys.stderr is not None:
            print(str(error), file=sys.stderr)
        return
    QMessageBox.critical(None, error.title, str(error))


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    options = _make_parser().parse_args(arguments)

    # A diagnostic launch must never become an implicit migration launch.  If
    # the caller did not inject a profile, create an ephemeral one before any
    # service can inspect AppData.
    smoke_profile: tempfile.TemporaryDirectory[str] | None = None
    had_data_override = DATA_DIR_ENV in os.environ
    previous_data_override = os.environ.get(DATA_DIR_ENV)
    had_platform_override = "QT_QPA_PLATFORM" in os.environ
    previous_platform_override = os.environ.get("QT_QPA_PLATFORM")
    if options.smoke_test:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        if not os.environ.get(DATA_DIR_ENV):
            smoke_profile = tempfile.TemporaryDirectory(prefix="BananaPrism-smoke-")
            os.environ[DATA_DIR_ENV] = smoke_profile.name

    def restore_smoke_environment() -> None:
        if not options.smoke_test:
            return
        if had_data_override:
            os.environ[DATA_DIR_ENV] = previous_data_override or ""
        else:
            os.environ.pop(DATA_DIR_ENV, None)
        if had_platform_override:
            os.environ["QT_QPA_PLATFORM"] = previous_platform_override or ""
        else:
            os.environ.pop("QT_QPA_PLATFORM", None)
        if smoke_profile is not None:
            smoke_profile.cleanup()

    existing = QApplication.instance()
    owns_app = existing is None
    app = existing if isinstance(existing, QApplication) else QApplication([sys.argv[0], *arguments])
    _configure_qt_application(app)

    services: RuntimeServices | None = None
    try:
        services = build_services()
        window = create_main_window(services)
    except SingleInstanceError as exc:
        if services is not None:
            services.close()
        _report_single_instance_error(app, exc, headless=options.smoke_test)
        restore_smoke_environment()
        return SINGLE_INSTANCE_EXIT_CODE
    except (MigrationError, SecretStoreError, SettingsError, OSError, ValueError) as exc:
        if services is not None:
            services.close()
        _report_startup_error(app, user_error_text(exc), headless=options.smoke_test)
        restore_smoke_environment()
        return 2
    except Exception as exc:
        if services is not None:
            services.close()
        _report_startup_error(app, user_error_text(exc), headless=options.smoke_test)
        restore_smoke_environment()
        return 2

    app.aboutToQuit.connect(services.close)
    _show_startup_window(window)
    if options.smoke_test:
        # Let queued signal connections, layout, icon loading, and service
        # construction all run at least once before a clean shutdown.
        QTimer.singleShot(max(100, int(options.smoke_hold_ms)), app.quit)

    try:
        exit_code = app.exec() if owns_app else 0
        if not owns_app:
            app.processEvents()
            if options.smoke_test:
                window.close()
        return int(exit_code)
    finally:
        services.close()
        restore_smoke_environment()


__all__ = [
    "INSTANCE_LOCK_FILENAME",
    "SINGLE_INSTANCE_EXIT_CODE",
    "RuntimeServices",
    "SingleInstanceError",
    "build_services",
    "create_main_window",
    "main",
]
