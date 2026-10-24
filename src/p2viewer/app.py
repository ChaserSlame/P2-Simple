from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from p2viewer.clips import ClipAssembly, build_clip_assemblies
from p2viewer.library import MEDIA_EXTENSIONS, MediaFile, iter_media
from p2viewer.p2_metadata import P2ClipMetadata, P2ScanResult, scan_p2_metadata
from p2viewer.playback import (
    PreviewRenderCancelled,
    PreviewRenderError,
    render_playback_proxy,
)
from p2viewer.synchronization import (
    AudioSync,
    SYNC_ANALYSIS_MAX_WORKERS,
    SyncAnalysis,
    analyze_clips_sync,
)

logger = logging.getLogger(__name__)
PREVIEW_CACHE_MAX_ENTRIES = 4
PREVIEW_CACHE_MAX_BYTES = 512 * 1024 * 1024

PreviewCacheKey = tuple[
    tuple[str, int, int],
    tuple[tuple[str, int, int, str], ...],
]


class _PlaybackProxyThread(QThread):
    completed = Signal(str, str, str)

    def __init__(
        self,
        token: str,
        video_path: Path,
        audio_tracks: tuple[AudioSync, ...],
        output_path: Path,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._token = token
        self._video_path = video_path
        self._audio_tracks = audio_tracks
        self._output_path = output_path
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        error_message = ""
        rendered_path = self._output_path
        try:
            rendered_path = render_playback_proxy(
                self._video_path,
                self._audio_tracks,
                self._output_path,
                cancel_event=self._cancel_event,
            )
        except PreviewRenderCancelled:
            pass
        except (OSError, ValueError, PreviewRenderError) as error:
            error_message = str(error)
        self.completed.emit(self._token, str(rendered_path), error_message)


class _ScanThread(QThread):
    batch_ready = Signal(object)
    progress = Signal(int, int, str)
    p2_metadata_ready = Signal(object)
    sync_analysis_ready = Signal(object)
    scan_finished = Signal(int)
    failed = Signal(str)

    def __init__(self, folder: Path, parent: QWidget) -> None:
        super().__init__(parent)
        self._folder = folder

    def run(self) -> None:
        batch: list[MediaFile] = []
        scanned_paths: list[Path] = []
        files_found = 0

        def report_progress(directories: int, found: int, current: Path) -> None:
            self.progress.emit(directories, found, str(current))

        try:
            stage_started = time.perf_counter()
            for media in iter_media(self._folder, report_progress):
                batch.append(media)
                scanned_paths.append(media.path)
                files_found += 1
                if len(batch) >= 50:
                    self.batch_ready.emit(batch)
                    batch = []
            if batch:
                self.batch_ready.emit(batch)
            logger.info(
                "Directory scan completed: files=%d elapsed=%.3fs",
                files_found,
                time.perf_counter() - stage_started,
            )

            stage_started = time.perf_counter()
            p2_metadata = scan_p2_metadata(
                self._folder,
                (path for path in scanned_paths if path.suffix.casefold() == ".mxf"),
            )
            self.p2_metadata_ready.emit(p2_metadata)
            logger.info(
                "P2 metadata scan completed: clips=%d elapsed=%.3fs",
                len(p2_metadata.clips),
                time.perf_counter() - stage_started,
            )

            stage_started = time.perf_counter()

            def report_sync_progress(
                completed: int,
                total: int,
                analysis: SyncAnalysis,
            ) -> None:
                self.progress.emit(
                    completed,
                    total,
                    f"Midiendo sincronía P2: {analysis.clip.clip_id}",
                )

            sync_started = time.perf_counter()
            sync_analyses = analyze_clips_sync(
                p2_metadata.clips,
                report_sync_progress,
            )
            self.sync_analysis_ready.emit(sync_analyses)
            logger.info(
                "P2 synchronization analysis completed: clips=%d workers=%d "
                "elapsed=%.3fs",
                len(sync_analyses),
                min(SYNC_ANALYSIS_MAX_WORKERS, len(sync_analyses)),
                time.perf_counter() - sync_started,
            )
            self.scan_finished.emit(files_found)
        except (OSError, OverflowError, ValueError) as error:
            if batch:
                self.batch_ready.emit(batch)
            self.failed.emit(str(error))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("P2Viewer Plus - Python reconstruction")
        self.resize(1600, 800)
        self._thread: QThread | None = None
        self._media: list[MediaFile] = []
        self._p2_warnings: tuple[str, ...] = ()
        self._p2_result = P2ScanResult({}, (), ())
        self._clip_assemblies: list[ClipAssembly] = []
        self._sync_by_video: dict[Path, SyncAnalysis] = {}
        self._preview_render_thread: _PlaybackProxyThread | None = None
        self._preview_render_result: tuple[str, str, str] | None = None
        self._wanted_playback: tuple[str, Path, tuple[AudioSync, ...]] | None = None
        self._active_preview_path: Path | None = None
        self._preview_requested_at: float | None = None
        self._preview_render_finished_at: float | None = None
        self._active_preview_token: str | None = None
        self._preview_ready_logged_token: str | None = None
        self._preview_cache: OrderedDict[PreviewCacheKey, Path] = OrderedDict()
        self._preview_cache_keys_by_token: dict[str, PreviewCacheKey] = {}
        self._preview_temp_dir = tempfile.TemporaryDirectory(prefix="p2viewer-preview-")
        self._closing_after_preview = False

        self._folder_label = QLabel("Ninguna carpeta seleccionada")
        self._scan_button = QPushButton("Elegir carpeta...")
        self._scan_button.clicked.connect(self._choose_folder)

        self._clip_table = QTableWidget(0, 5)
        self._clip_table.setHorizontalHeaderLabels(
            [
                "Video",
                "Pistas de audio",
                "Timecode inicial",
                "Desfase máx.",
                "Estado",
            ]
        )
        self._clip_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._clip_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._clip_table.itemSelectionChanged.connect(self._show_clip_selection)
        self._clip_table.horizontalHeader().setStretchLastSection(True)
        self._clip_table.horizontalHeader().setMinimumSectionSize(90)

        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(0.7)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._preview_video = QVideoWidget()
        self._preview_video.setFixedSize(600, 400)
        self._preview_video.setStyleSheet("QVideoWidget { background-color: #171717; }")
        self._player.setVideoOutput(self._preview_video)
        self._player.positionChanged.connect(self._playback_position_changed)
        self._player.durationChanged.connect(self._playback_duration_changed)
        self._player.mediaStatusChanged.connect(self._playback_media_status_changed)
        self._player.playbackStateChanged.connect(self._playback_state_changed)
        self._player.errorOccurred.connect(self._playback_error)

        self._preview_position = QSlider(Qt.Orientation.Horizontal)
        self._preview_position.setRange(0, 1000)
        self._preview_position.setEnabled(False)
        self._preview_position.valueChanged.connect(self._seek_playback)
        self._preview_time_label = QLabel("—")
        self._preview_status = QLabel("Selecciona un clip para preparar la preview A/V.")
        self._preview_status.setWordWrap(True)
        self._play_button = QPushButton()
        self._play_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        )
        self._play_button.setToolTip("Reproducir")
        self._play_button.setAccessibleName("Reproducir")
        self._play_button.setEnabled(False)
        self._play_button.clicked.connect(self._toggle_playback)
        self._stop_button = QPushButton()
        self._stop_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaStop)
        )
        self._stop_button.setToolTip("Detener")
        self._stop_button.setAccessibleName("Detener")
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._player.stop)
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(70)
        self._volume_slider.setMaximumWidth(120)
        self._volume_slider.valueChanged.connect(
            lambda volume: self._audio_output.setVolume(volume / 100)
        )
        preview_controls = QHBoxLayout()
        preview_controls.addWidget(self._play_button)
        preview_controls.addWidget(self._stop_button)
        preview_controls.addWidget(self._preview_position, 1)
        preview_controls.addWidget(self._preview_time_label)
        preview_controls.addWidget(QLabel("Volumen"))
        preview_controls.addWidget(self._volume_slider)

        preview_panel = QWidget()
        preview_layout = QVBoxLayout(preview_panel)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.addWidget(QLabel("Video Preview"))
        preview_layout.addWidget(self._preview_video, 1)
        preview_layout.addLayout(preview_controls)
        preview_layout.addWidget(self._preview_status)

        self._preview_render_timer = QTimer(self)
        self._preview_render_timer.setSingleShot(True)
        self._preview_render_timer.timeout.connect(self._start_playback_render)

        self._details = QTextEdit()
        self._details.setReadOnly(True)
        self._details.setPlaceholderText(
            "Selecciona un clip para ver su metadata P2 y sincronía."
        )

        controls = QHBoxLayout()
        controls.addWidget(self._scan_button)
        controls.addWidget(self._folder_label, 1)

        clips_panel = QWidget()
        clips_layout = QVBoxLayout(clips_panel)
        clips_layout.setContentsMargins(0, 0, 0, 0)
        clips_layout.addWidget(QLabel("Clips"))
        clips_layout.addWidget(self._clip_table, 1)

        details_panel = QWidget()
        details_layout = QVBoxLayout(details_panel)
        details_layout.setContentsMargins(0, 0, 0, 0)
        details_layout.addWidget(QLabel("Información y sincronía"))
        details_layout.addWidget(self._details, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(clips_panel)
        splitter.addWidget(preview_panel)
        splitter.addWidget(details_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([450, 640, 450])

        status_note = QLabel(
            "La preview reproduce video y mezcla el audio P2 solo si su sincronía "
            "fue verificada."
        )
        status_note.setWordWrap(True)

        layout = QVBoxLayout()
        layout.addLayout(controls)
        layout.addWidget(splitter, 1)
        layout.addWidget(status_note)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.statusBar().showMessage("Listo")

    @Slot()
    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta")
        if folder:
            self._start_scan(Path(folder))

    def _start_scan(self, folder: Path) -> None:
        if self._thread is not None:
            return

        self._folder_label.setText(str(folder))
        self._scan_button.setEnabled(False)
        self._scan_button.setText("Escaneando...")
        self._details.clear()
        self._media.clear()
        self._p2_warnings = ()
        self._p2_result = P2ScanResult({}, (), ())
        self._clip_assemblies.clear()
        self._sync_by_video.clear()
        self._clip_table.setRowCount(0)
        self._clear_playback_preview("Selecciona un clip para preparar la preview A/V.")
        self.statusBar().showMessage("Buscando archivos de medios...")

        thread = _ScanThread(folder, self)
        thread.batch_ready.connect(self._append_scan_batch)
        thread.progress.connect(self._update_scan_progress)
        thread.p2_metadata_ready.connect(self._set_p2_metadata)
        thread.sync_analysis_ready.connect(self._set_sync_analyses)
        thread.scan_finished.connect(self._scan_finished)
        thread.failed.connect(self._scan_failed)
        thread.finished.connect(self._scan_thread_finished)
        self._thread = thread
        thread.start()

    @Slot(object)
    def _append_scan_batch(self, files: list[MediaFile]) -> None:
        self._media.extend(files)

    @Slot(int, int, str)
    def _update_scan_progress(
        self,
        directories_scanned: int,
        files_found: int,
        current_directory: str,
    ) -> None:
        if current_directory.startswith("Midiendo sincronía P2:"):
            self.statusBar().showMessage(
                f"{current_directory} — clip {directories_scanned} de {files_found}"
            )
        else:
            self.statusBar().showMessage(
                f"Explorando {current_directory} — "
                f"{directories_scanned} carpetas, {files_found} medios encontrados"
            )

    @Slot(object)
    def _set_p2_metadata(self, result: P2ScanResult) -> None:
        self._p2_result = result
        self._p2_warnings = result.warnings

    @Slot(object)
    def _set_sync_analyses(self, analyses: list[SyncAnalysis]) -> None:
        self._sync_by_video = {
            analysis.clip.video_file: analysis
            for analysis in analyses
            if analysis.clip.video_file is not None
        }

    def _prepare_playback_preview(
        self,
        video_path: Path,
        audio_tracks: tuple[AudioSync, ...],
        audio_verified: bool,
        declared_audio_count: int,
        duration_seconds: float | None,
    ) -> None:
        if self._preview_render_thread is not None:
            self._preview_render_thread.cancel()

        self._preview_requested_at = time.perf_counter()
        self._preview_render_finished_at = None
        self._active_preview_token = None
        self._preview_ready_logged_token = None
        self._player.stop()
        self._player.setSource(QUrl())
        self._remove_active_preview_file()
        self._preview_position.blockSignals(True)
        self._preview_position.setValue(0)
        self._preview_position.setEnabled(False)
        self._preview_position.blockSignals(False)
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._preview_time_label.setText(
            f"00:00.000 / {self._format_seconds(duration_seconds)}"
        )

        token = uuid.uuid4().hex
        self._wanted_playback = (token, video_path, audio_tracks)
        if audio_verified and audio_tracks:
            self._preview_status.setText(
                f"Preparando video y mezcla sincronizada de "
                f"{len(audio_tracks)} pista(s) de audio…"
            )
        elif declared_audio_count:
            self._preview_status.setText(
                "Audio desactivado: no se pudo verificar la sincronía de todas "
                "las pistas; preparando video solamente."
            )
        else:
            self._preview_status.setText(
                "El clip no tiene pistas de audio asociadas; preparando video."
            )
        self._preview_render_timer.start(0)

    def _clear_playback_preview(self, status: str) -> None:
        self._wanted_playback = None
        self._preview_requested_at = None
        self._preview_render_finished_at = None
        self._active_preview_token = None
        self._preview_ready_logged_token = None
        self._preview_render_timer.stop()
        if self._preview_render_thread is not None:
            self._preview_render_thread.cancel()
        self._player.stop()
        self._player.setSource(QUrl())
        self._remove_active_preview_file()
        self._preview_position.blockSignals(True)
        self._preview_position.setValue(0)
        self._preview_position.setEnabled(False)
        self._preview_position.blockSignals(False)
        self._preview_time_label.setText("—")
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._preview_status.setText(status)

    def _remove_active_preview_file(self) -> None:
        path = self._active_preview_path
        self._active_preview_path = None
        is_cached = path is not None and path in self._preview_cache.values()
        if path is not None and not is_cached and path.exists():
            try:
                path.unlink()
            except OSError as error:
                self.statusBar().showMessage(
                    f"No se pudo limpiar la preview temporal {path.name}: {error}"
                )

    @Slot()
    def _start_playback_render(self) -> None:
        if (
            self._preview_render_thread is not None
            or self._wanted_playback is None
        ):
            return
        token, video_path, audio_tracks = self._wanted_playback
        try:
            cache_key = self._preview_cache_key(video_path, audio_tracks)
        except OSError as error:
            self._preview_status.setText(
                f"No se pudo comprobar el archivo para la preview: {error}"
            )
            return

        cached_path = self._preview_cache.get(cache_key)
        if cached_path is not None:
            try:
                is_valid = cached_path.is_file() and cached_path.stat().st_size > 0
            except OSError:
                is_valid = False
            if is_valid:
                self._preview_cache.move_to_end(cache_key)
                logger.info("Preview cache hit: video=%s", video_path.name)
                self._activate_playback_preview(token, cached_path, cache_hit=True)
                return
            self._preview_cache.pop(cache_key)

        output_path = Path(self._preview_temp_dir.name) / f"{token}.mp4"
        self._preview_cache_keys_by_token[token] = cache_key
        thread = _PlaybackProxyThread(
            token,
            video_path,
            audio_tracks,
            output_path,
            self,
        )
        thread.completed.connect(self._capture_playback_render_result)
        thread.finished.connect(self._playback_render_finished)
        thread.finished.connect(thread.deleteLater)
        self._preview_render_result = None
        self._preview_render_thread = thread
        thread.start()

    @staticmethod
    def _preview_cache_key(
        video_path: Path,
        audio_tracks: tuple[AudioSync, ...],
    ) -> PreviewCacheKey:
        video_stat = video_path.stat()
        video_signature = (
            str(video_path.resolve()),
            video_stat.st_size,
            video_stat.st_mtime_ns,
        )
        audio_signatures = []
        for track in audio_tracks:
            audio_stat = track.path.stat()
            audio_signatures.append(
                (
                    str(track.path.resolve()),
                    audio_stat.st_size,
                    audio_stat.st_mtime_ns,
                    str(track.timecode_offset_seconds),
                )
            )
        return video_signature, tuple(audio_signatures)

    def _cache_preview(self, key: PreviewCacheKey, path: Path) -> None:
        preview_size = path.stat().st_size
        if preview_size > PREVIEW_CACHE_MAX_BYTES:
            logger.info(
                "Preview not cached: video proxy is larger than cache limit "
                "(size_mb=%.2f limit_mb=%.2f)",
                preview_size / (1024 * 1024),
                PREVIEW_CACHE_MAX_BYTES / (1024 * 1024),
            )
            return
        self._preview_cache[key] = path
        self._preview_cache.move_to_end(key)
        while (
            len(self._preview_cache) > PREVIEW_CACHE_MAX_ENTRIES
            or sum(
                cached_path.stat().st_size
                for cached_path in self._preview_cache.values()
            )
            > PREVIEW_CACHE_MAX_BYTES
        ):
            _, evicted_path = self._preview_cache.popitem(last=False)
            if evicted_path != self._active_preview_path:
                self._delete_preview_file(evicted_path)

    @Slot(str, str, str)
    def _capture_playback_render_result(
        self,
        token: str,
        output_path: str,
        error: str,
    ) -> None:
        self._preview_render_result = (token, output_path, error)

    @Slot()
    def _playback_render_finished(self) -> None:
        self._preview_render_thread = None
        result = self._preview_render_result
        self._preview_render_result = None
        if result is not None:
            token, output_name, error = result
            output_path = Path(output_name)
            wanted_token = (
                self._wanted_playback[0]
                if self._wanted_playback is not None
                else None
            )
            if self._closing_after_preview:
                self._preview_cache_keys_by_token.pop(token, None)
                self._delete_preview_file(output_path)
            elif token == wanted_token and not error:
                cache_key = self._preview_cache_keys_by_token.pop(token, None)
                if cache_key is not None:
                    self._cache_preview(cache_key, output_path)
                self._activate_playback_preview(token, output_path, cache_hit=False)
            else:
                self._preview_cache_keys_by_token.pop(token, None)
                self._delete_preview_file(output_path)
                if token == wanted_token and error:
                    self._preview_status.setText(error)

            if wanted_token is not None and token != wanted_token:
                self._preview_render_timer.start(0)

        if self._closing_after_preview:
            QTimer.singleShot(0, self.close)

    def _activate_playback_preview(
        self,
        token: str,
        output_path: Path,
        *,
        cache_hit: bool,
    ) -> None:
        self._active_preview_path = output_path
        self._preview_render_finished_at = time.perf_counter()
        wanted_playback = self._wanted_playback
        if wanted_playback is None:
            return
        if self._preview_requested_at is not None:
            logger.info(
                "Preview proxy ready: video=%s elapsed_since_selection=%.3fs "
                "cache_hit=%s",
                wanted_playback[1].name,
                self._preview_render_finished_at - self._preview_requested_at,
                cache_hit,
            )
        self._active_preview_token = token
        self._player.setSource(QUrl.fromLocalFile(str(output_path)))
        audio_count = len(wanted_playback[2])
        self._preview_status.setText(
            "Preview lista: video con mezcla de "
            f"{audio_count} pista(s) sincronizada(s)."
            if audio_count
            else "Preview lista: reproducción de video sin audio verificado."
        )

    def _delete_preview_file(self, path: Path) -> None:
        if not path.exists():
            return
        try:
            path.unlink()
        except OSError as error:
            self.statusBar().showMessage(
                f"No se pudo limpiar el archivo temporal {path.name}: {error}"
            )

    @Slot()
    def _toggle_playback(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            self._player.play()

    @Slot()
    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        label = "Pausar" if is_playing else "Reproducir"
        icon = (
            QStyle.StandardPixmap.SP_MediaPause
            if is_playing
            else QStyle.StandardPixmap.SP_MediaPlay
        )
        self._play_button.setIcon(self.style().standardIcon(icon))
        self._play_button.setToolTip(label)
        self._play_button.setAccessibleName(label)

    @Slot(int)
    def _playback_position_changed(self, position: int) -> None:
        duration = self._player.duration()
        self._preview_position.blockSignals(True)
        if duration > 0:
            self._preview_position.setValue(round(position * 1000 / duration))
            self._preview_time_label.setText(
                f"{self._format_milliseconds(position)} / "
                f"{self._format_milliseconds(duration)}"
            )
        else:
            self._preview_position.setValue(0)
            self._preview_time_label.setText("00:00.000 / —")
        self._preview_position.blockSignals(False)

    @Slot(int)
    def _playback_duration_changed(self, duration: int) -> None:
        self._preview_position.setEnabled(duration > 0)
        self._playback_position_changed(self._player.position())

    @Slot(int)
    def _seek_playback(self, value: int) -> None:
        duration = self._player.duration()
        if duration > 0:
            self._player.setPosition(round(duration * value / 1000))

    @Slot(QMediaPlayer.MediaStatus)
    def _playback_media_status_changed(
        self,
        status: QMediaPlayer.MediaStatus,
    ) -> None:
        ready = status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.EndOfMedia,
        )
        if (
            status in (
                QMediaPlayer.MediaStatus.LoadedMedia,
                QMediaPlayer.MediaStatus.BufferedMedia,
            )
            and self._active_preview_token is not None
            and self._active_preview_token != self._preview_ready_logged_token
        ):
            self._preview_ready_logged_token = self._active_preview_token
            now = time.perf_counter()
            requested_at = self._preview_requested_at
            render_finished_at = self._preview_render_finished_at
            if requested_at is not None and render_finished_at is not None:
                logger.info(
                    "Preview playable: video=%s status=%s proxy_elapsed=%.3fs "
                    "player_load_elapsed=%.3fs total_elapsed=%.3fs",
                    self._wanted_playback[1].name if self._wanted_playback else "unknown",
                    status.name,
                    render_finished_at - requested_at,
                    now - render_finished_at,
                    now - requested_at,
                )
        self._play_button.setEnabled(ready)
        self._stop_button.setEnabled(ready)
        if status == QMediaPlayer.MediaStatus.InvalidMedia:
            if (
                self._preview_requested_at is not None
                and self._wanted_playback is not None
            ):
                logger.warning(
                    "Preview player failed: video=%s elapsed_since_selection=%.3fs "
                    "error=%s",
                    self._wanted_playback[1].name,
                    time.perf_counter() - self._preview_requested_at,
                    self._player.errorString(),
                )
            self._preview_status.setText(
                "El reproductor no pudo abrir la preview: "
                f"{self._player.errorString()}"
            )

    @Slot(QMediaPlayer.Error, str)
    def _playback_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError:
            return
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._preview_status.setText(
            f"No se pudo reproducir la preview: {message or self._player.errorString()}"
        )

    @Slot(int)
    def _scan_finished(self, files_found: int) -> None:
        self._populate_clip_table()
        if files_found:
            p2_matches = len(self._p2_result.by_media_path)
            verified = sum(
                1 for analysis in self._sync_by_video.values() if analysis.eligible
            )
            warning_text = (
                f"; {len(self._p2_warnings)} advertencias de metadata"
                if self._p2_warnings
                else ""
            )
            self.statusBar().showMessage(
                f"Escaneo completo: {files_found} archivos de medios, "
                f"{p2_matches} esencias P2, sincronía verificada en "
                f"{verified} clips{warning_text}"
            )
            return

        extensions = ", ".join(sorted(MEDIA_EXTENSIONS))
        self.statusBar().showMessage("Escaneo completo: no se encontraron medios")
        QMessageBox.information(
            self,
            "No se encontraron archivos de medios",
            f"No se encontraron archivos con estas extensiones: {extensions}",
        )

    @Slot(str)
    def _scan_failed(self, message: str) -> None:
        self._populate_clip_table()
        self.statusBar().showMessage("No se pudo completar el escaneo")
        QMessageBox.critical(self, "Error al explorar la carpeta", message)

    @Slot()
    def _scan_thread_finished(self) -> None:
        self._scan_button.setEnabled(True)
        self._scan_button.setText("Elegir carpeta...")
        self._thread = None

    def _populate_clip_table(self) -> None:
        self._clip_assemblies = build_clip_assemblies(self._media, self._p2_result)
        self._clip_table.setRowCount(len(self._clip_assemblies))
        for row, assembly in enumerate(self._clip_assemblies):
            metadata = assembly.metadata
            sync = self._sync_by_video.get(assembly.video.path)
            if metadata is None:
                timecode = "—"
                offset = "—"
                status = "Sin asociación P2"
            else:
                timecode = metadata.start_timecode or "No disponible"
                if sync is not None and sync.max_abs_offset_seconds is not None:
                    offset = f"{float(sync.max_abs_offset_seconds):.6f} s"
                else:
                    offset = "No medido"
                if sync is not None and sync.eligible:
                    status = "Sincronía verificada"
                elif sync is not None and sync.issues:
                    status = f"Sin verificar: {sync.issues[0]}"
                elif metadata.audio_track_count > len(assembly.audio_files):
                    status = "Faltan esencias de audio"
                elif not assembly.audio_files:
                    status = "No hay pistas de audio P2"
                else:
                    status = "Sin análisis de sincronía"

            values = (
                assembly.video.path.name,
                f"{len(assembly.audio_files)} pista(s)",
                timecode,
                offset,
                status,
            )
            for column, value in enumerate(values):
                self._clip_table.setItem(row, column, QTableWidgetItem(value))
        self._clip_table.resizeColumnsToContents()

    @Slot()
    def _show_clip_selection(self) -> None:
        row = self._clip_table.currentRow()
        if row < 0 or row >= len(self._clip_assemblies):
            self._details.clear()
            self._clear_playback_preview(
                "Selecciona un clip para preparar la preview A/V."
            )
            return

        assembly = self._clip_assemblies[row]
        metadata = assembly.metadata
        duration = (
            float(metadata.duration_seconds)
            if metadata is not None and metadata.duration_seconds is not None
            else (
                float(self._sync_by_video[assembly.video.path].video_duration_seconds)
                if (
                    assembly.video.path in self._sync_by_video
                    and self._sync_by_video[assembly.video.path].video_duration_seconds
                    is not None
                )
                else None
            )
        )
        sync = self._sync_by_video.get(assembly.video.path)
        audio_tracks = sync.audio if sync is not None and sync.eligible else ()
        audio_verified = bool(audio_tracks)
        self._prepare_playback_preview(
            assembly.video.path,
            audio_tracks,
            audio_verified,
            len(assembly.audio_files),
            duration,
        )
        details = [
            "Información y sincronía del clip.",
            f"Video: {assembly.video.path}",
        ]
        if metadata is None:
            details.extend(
                (
                    "",
                    "No se encontró metadata P2 que asocie este video "
                    "con pistas de audio.",
                )
            )
        else:
            details.extend(
                (
                    "",
                    f"ID del clip: {metadata.clip_id}",
                    f"GlobalClipID: {metadata.global_clip_id or 'no disponible'}",
                    f"Nombre: {metadata.clip_name or 'no disponible'}",
                    f"Timecode inicial: {metadata.start_timecode or 'no disponible'}",
                    f"Frecuencia de cuadro: {metadata.frame_rate or 'no disponible'}",
                    f"Duración: {self._format_p2_duration(metadata)}",
                    f"XML P2: {metadata.clip_xml}",
                    f"Pistas de audio declaradas: {metadata.audio_track_count}",
                    "Esencias de audio encontradas:",
                )
            )
            if assembly.audio_files:
                details.extend(f"- {path}" for path in assembly.audio_files)
            else:
                details.append("- Ninguna")

        if sync is not None:
            details.extend(
                (
                    "",
                    "Medición de sincronía con ffprobe:",
                )
            )
            for track in sync.audio:
                if track.offset_seconds is None:
                    offset_text = "no disponible"
                else:
                    offset_text = f"{float(track.offset_seconds):+.6f} s"
                details.append(
                    f"- {track.path.name}: desfase {offset_text}; "
                    f"timecode {track.timecode or 'no disponible'}"
                )
            details.append(
                "Estado: sincronía verificada."
                if sync.eligible
                else "Estado: sincronía no verificada."
            )
            details.extend(f"- {issue}" for issue in sync.issues)
        else:
            details.extend(("", "La sincronía todavía no fue medida."))

        if self._p2_warnings:
            details.extend(("", "Advertencias de lectura P2:"))
            details.extend(f"- {warning}" for warning in self._p2_warnings[:10])
        self._details.setPlainText("\n".join(details))

    @staticmethod
    def _format_p2_duration(metadata: P2ClipMetadata) -> str:
        if metadata.duration_frames is None:
            return "no disponible"
        if metadata.duration_seconds is None:
            return f"{metadata.duration_frames} cuadros"
        return (
            f"{metadata.duration_frames} cuadros "
            f"({float(metadata.duration_seconds):.3f} s; "
            f"edit unit {metadata.edit_unit})"
        )

    @staticmethod
    def _format_seconds(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        total_milliseconds = max(0, round(seconds * 1000))
        minutes, milliseconds = divmod(total_milliseconds, 60_000)
        whole_seconds, milliseconds = divmod(milliseconds, 1000)
        return f"{minutes:02d}:{whole_seconds:02d}.{milliseconds:03d}"

    @staticmethod
    def _format_milliseconds(milliseconds: int) -> str:
        return MainWindow._format_seconds(milliseconds / 1000)

    def closeEvent(self, event: QCloseEvent) -> None:
        if (
            self._preview_render_thread is not None
            and self._preview_render_thread.isRunning()
        ):
            self._closing_after_preview = True
            self._wanted_playback = None
            self._preview_render_timer.stop()
            self._preview_render_thread.cancel()
            event.ignore()
            return

        self._player.stop()
        self._player.setSource(QUrl())
        self._preview_temp_dir.cleanup()
        self._preview_cache.clear()
        self._preview_cache_keys_by_token.clear()
        event.accept()
        super().closeEvent(event)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()
