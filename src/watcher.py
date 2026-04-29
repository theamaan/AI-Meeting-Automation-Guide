"""
watcher.py — File System Watcher
Monitors OneDrive/SharePoint synced folder for new recordings.

Design decisions:
  - Uses a per-file timer (delay_seconds) so we don't start processing
    while OneDrive is still mid-sync. File must be stable for `delay_seconds`.
  - On_moved handles files that appear via OneDrive's rename-on-complete sync pattern.
  - Timer resets if file changes again during the delay window.
"""

import logging
import os
from pathlib import Path
from threading import Timer
from typing import Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from database import Database

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".mp4", ".vtt", ".docx"}


# ──────────────────────────────────────────────────────────────
# Event Handler
# ──────────────────────────────────────────────────────────────

class RecordingEventHandler(FileSystemEventHandler):
    """
    Watches for new or moved-in files.
    Each qualifying file gets a delayed callback so sync can complete.
    """

    def __init__(self, callback: Callable[[str], None], delay_seconds: int, db: Database):
        self.callback = callback
        self.delay_seconds = delay_seconds
        self.db = db
        self._pending: dict[str, Timer] = {}

    # ── Watchdog callbacks ────────────────────────────────────

    def on_created(self, event):
        if not event.is_directory and self._is_supported(event.src_path):
            self._schedule(event.src_path)

    def on_moved(self, event):
        # OneDrive often writes to a temp name then renames to the real file.
        if not event.is_directory and self._is_supported(event.dest_path):
            self._schedule(event.dest_path)

    def on_modified(self, event):
        # Re-schedule on modification so we always process the final version.
        if not event.is_directory and self._is_supported(event.src_path):
            self._schedule(event.src_path)

    # ── Internal ──────────────────────────────────────────────

    def _is_supported(self, path: str) -> bool:
        return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS

    def _schedule(self, file_path: str):
        # Cancel any existing pending timer for this file
        if file_path in self._pending:
            self._pending[file_path].cancel()
            logger.debug(f"Timer reset for: {Path(file_path).name}")

        # Skip already-processed files
        if self.db.is_processed(file_path):
            logger.info(f"Already processed, skipping: {Path(file_path).name}")
            return

        logger.info(
            f"New file detected: {Path(file_path).name} — "
            f"processing in {self.delay_seconds}s"
        )
        timer = Timer(self.delay_seconds, self._fire, args=[file_path])
        timer.daemon = True
        self._pending[file_path] = timer
        timer.start()

    def _fire(self, file_path: str):
        """Called after delay — validate file still exists then dispatch."""
        self._pending.pop(file_path, None)
        if not os.path.exists(file_path):
            logger.warning(f"File disappeared before processing: {file_path}")
            return
        try:
            logger.info(f"Dispatching processing: {Path(file_path).name}")
            self.callback(file_path)
        except Exception as exc:
            logger.error(f"Error in callback for {file_path}: {exc}", exc_info=True)

    def cancel_all(self):
        """Clean shutdown — cancel all pending timers."""
        for timer in self._pending.values():
            timer.cancel()
        self._pending.clear()


# ──────────────────────────────────────────────────────────────
# Watcher — public API
# ──────────────────────────────────────────────────────────────

class FileWatcher:
    """
    Thin wrapper around watchdog.Observer.
    Usage:
        watcher = FileWatcher(path, callback, delay_seconds=300, db=db)
        watcher.start()
        ...
        watcher.stop()
    """

    def __init__(
        self,
        watch_path: str,
        callback: Callable[[str], None],
        delay_seconds: int,
        db: Database,
    ):
        self.watch_path = watch_path
        self._handler = RecordingEventHandler(callback, delay_seconds, db)
        self._observer = Observer()

    def start(self):
        self._observer.schedule(self._handler, self.watch_path, recursive=False)
        self._observer.start()
        logger.info(f"Watcher active on: {self.watch_path}")

    def stop(self):
        self._handler.cancel_all()
        self._observer.stop()
        self._observer.join()
        logger.info("Watcher stopped.")
