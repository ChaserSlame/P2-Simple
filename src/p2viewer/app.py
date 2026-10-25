from __future__ import annotations

import csv
import html
import logging
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from fractions import Fraction
from pathlib import Path

import qdarktheme
from PySide6.QtCore import QThread, QTimer, QUrl, Qt, Signal, Slot
from PySide6.QtGui import QCloseEvent, QKeySequence, QMouseEvent, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QAbstractSpinBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSlider,
    QSplitter,
    QStackedWidget,
    QStyle,
    QTabWidget,
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
    render_preview_audio,
    render_preview_thumbnail,
)
from p2viewer.synchronization import (
    AudioSync,
    MediaStreamProperties,
    SYNC_ANALYSIS_MAX_WORKERS,
    SyncAnalysis,
    analyze_clips_sync,
)

logger = logging.getLogger(__name__)
PREVIEW_CACHE_MAX_ENTRIES = 4
PREVIEW_CACHE_MAX_BYTES = 512 * 1024 * 1024
PREVIEW_THUMBNAIL_CACHE_MAX_ENTRIES = 4

PreviewCacheKey = tuple[
    tuple[str, int, int],
    tuple[tuple[str, int, int, str], ...],
]
PreviewThumbnailKey = tuple[str, int, int]


class _PreviewAssetsThread(QThread):
    completed = Signal(str, str, str)
    thumbnail_completed = Signal(str, str, str)

    def __init__(
        self,
        token: str,
        video_path: Path,
        audio_tracks: tuple[AudioSync, ...],
        audio_path: Path,
        thumbnail_path: Path,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._token = token
        self._video_path = video_path
        self._audio_tracks = audio_tracks
        self._audio_path = audio_path
        self._thumbnail_path = thumbnail_path
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        error_message = ""
        audio_path = ""
        if self._audio_tracks:
            try:
                audio_path = str(
                    render_preview_audio(
                        self._audio_tracks,
                        self._audio_path,
                        cancel_event=self._cancel_event,
                    )
                )
            except PreviewRenderCancelled:
                pass
            except (OSError, ValueError, PreviewRenderError) as error:
                error_message = str(error)
        self.completed.emit(self._token, audio_path, error_message)

        try:
            thumbnail_path = render_preview_thumbnail(
                self._video_path,
                self._thumbnail_path,
                cancel_event=self._cancel_event,
            )
            self.thumbnail_completed.emit(self._token, str(thumbnail_path), "")
        except PreviewRenderCancelled:
            pass
        except (OSError, ValueError, PreviewRenderError) as error:
            logger.warning(
                "Preview thumbnail failed: video=%s reason=%s",
                self._video_path.name,
                error,
            )
            self.thumbnail_completed.emit(self._token, "", str(error))


class _ClipThumbnailsThread(QThread):
    thumbnail_ready = Signal(str, str, str, str)

    def __init__(
        self,
        token: str,
        video_paths: tuple[Path, ...],
        output_directory: Path,
        parent: QWidget,
    ) -> None:
        super().__init__(parent)
        self._token = token
        self._video_paths = video_paths
        self._output_directory = output_directory
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        for index, video_path in enumerate(self._video_paths):
            if self._cancel_event.is_set():
                return
            thumbnail_path = self._output_directory / f"clip-{self._token}-{index}.jpg"
            try:
                render_preview_thumbnail(
                    video_path,
                    thumbnail_path,
                    cancel_event=self._cancel_event,
                )
            except PreviewRenderCancelled:
                return
            except (OSError, ValueError, PreviewRenderError) as error:
                self.thumbnail_ready.emit(
                    self._token,
                    str(video_path),
                    "",
                    str(error),
                )
            else:
                self.thumbnail_ready.emit(
                    self._token,
                    str(video_path),
                    str(thumbnail_path),
                    "",
                )


class _ClipListRow(QWidget):
    clicked = Signal(int)

    def __init__(
        self,
        row: int,
        filename: str,
        description: str,
        summary: str,
        has_audio: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._row = row
        self.setObjectName("clipListRow")
        self.thumbnail = QLabel("Cargando…")
        self.thumbnail.setObjectName("clipRowThumbnail")
        self.thumbnail.setFixedSize(124, 70)
        self.thumbnail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumbnail.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        self.filename_label = QLabel(filename)
        self.filename_label.setObjectName("clipRowFilename")
        self.filename_label.setToolTip(filename)
        self.filename_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        filename_layout = QHBoxLayout()
        filename_layout.setContentsMargins(0, 0, 0, 0)
        filename_layout.setSpacing(6)
        filename_layout.addWidget(self.filename_label, 1)
        if has_audio:
            audio_icon = QLabel()
            audio_icon.setObjectName("clipRowAudioIcon")
            audio_icon.setPixmap(
                self.style()
                .standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
                .pixmap(16, 16)
            )
            audio_icon.setToolTip("Pistas de audio asociadas")
            audio_icon.setAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
            filename_layout.addWidget(audio_icon)
        self.description_label = QLabel(description)
        self.description_label.setObjectName("clipRowDescription")
        self.description_label.setToolTip(description)
        self.description_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents
        )
        self.summary_label = QLabel(summary)
        self.summary_label.setObjectName("clipRowSummary")
        self.summary_label.setToolTip(summary)
        self.summary_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(3)
        text_layout.addLayout(filename_layout)
        text_layout.addWidget(self.description_label)
        text_layout.addWidget(self.summary_label)

        row_layout = QHBoxLayout(self)
        row_layout.setContentsMargins(8, 7, 8, 7)
        row_layout.setSpacing(12)
        row_layout.addWidget(self.thumbnail)
        row_layout.addLayout(text_layout, 1)

    def set_thumbnail(self, thumbnail: QPixmap) -> None:
        scaled = thumbnail.scaled(
            self.thumbnail.size(),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        left = (scaled.width() - self.thumbnail.width()) // 2
        top = (scaled.height() - self.thumbnail.height()) // 2
        self.thumbnail.setPixmap(
            scaled.copy(
                left,
                top,
                self.thumbnail.width(),
                self.thumbnail.height(),
            )
        )
        self.thumbnail.setText("")

    def set_selected(self, selected: bool) -> None:
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._row)
        super().mousePressEvent(event)


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
        self.resize(1600, 760)
        self.setMinimumSize(1400, 760)
        self._thread: QThread | None = None
        self._selected_folder: Path | None = None
        self._media: list[MediaFile] = []
        self._p2_warnings: tuple[str, ...] = ()
        self._p2_result = P2ScanResult({}, (), ())
        self._clip_assemblies: list[ClipAssembly] = []
        self._sync_by_video: dict[Path, SyncAnalysis] = {}
        self._preview_render_thread: _PreviewAssetsThread | None = None
        self._preview_render_token: str | None = None
        self._wanted_playback: tuple[str, Path, tuple[AudioSync, ...]] | None = None
        self._active_preview_path: Path | None = None
        self._preview_requested_at: float | None = None
        self._preview_render_finished_at: float | None = None
        self._active_preview_token: str | None = None
        self._preview_ready_logged_token: str | None = None
        self._preview_cache: OrderedDict[PreviewCacheKey, Path] = OrderedDict()
        self._preview_cache_keys_by_token: dict[str, PreviewCacheKey] = {}
        self._preview_thumbnail_cache: OrderedDict[
            PreviewThumbnailKey, QPixmap
        ] = OrderedDict()
        self._preview_temp_dir = tempfile.TemporaryDirectory(prefix="p2viewer-preview-")
        self._clip_thumbnail_thread: _ClipThumbnailsThread | None = None
        self._clip_thumbnail_token: str | None = None
        self._clip_thumbnail_rows: dict[Path, int] = {}
        self._playback_muted = False
        self._closing_after_preview = False

        self._folder_label = QLabel("Ninguna carpeta seleccionada")
        self._folder_label.setObjectName("folderPath")
        self._folder_label.setMinimumHeight(48)
        self._folder_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self._scan_button = QPushButton("Elegir carpeta...")
        self._scan_button.setMinimumHeight(48)
        self._scan_button.setObjectName("primaryButton")
        self._scan_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self._scan_button.clicked.connect(self._choose_folder)
        self._export_button = QPushButton("Exportar CSV...")
        self._export_button.setMinimumHeight(48)
        self._export_button.setObjectName("exportButton")
        self._export_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        )
        self._export_button.setEnabled(False)
        self._export_button.clicked.connect(self._export_clips_csv)

        self._clip_table = QTableWidget(0, 1)
        self._clip_table.setObjectName("clipsTable")
        self._clip_table.horizontalHeader().hide()
        self._clip_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows
        )
        self._clip_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._clip_table.setSortingEnabled(False)
        self._clip_table.verticalHeader().hide()
        self._clip_table.itemSelectionChanged.connect(self._show_clip_selection)
        clip_header = self._clip_table.horizontalHeader()
        clip_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)

        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(0.7)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._audio_mix_output = QAudioOutput(self)
        self._audio_mix_output.setVolume(0)
        self._audio_player = QMediaPlayer(self)
        self._audio_player.setAudioOutput(self._audio_mix_output)
        self._audio_player.mediaStatusChanged.connect(
            self._preview_audio_media_status_changed
        )
        self._audio_player.errorOccurred.connect(self._preview_audio_error)
        self._audio_ready_token: str | None = None
        self._audio_failed_token: str | None = None
        self._video_ready_token: str | None = None
        self._has_external_audio = False
        self._preview_video = QVideoWidget()
        self._preview_video.setObjectName("previewVideo")
        self._preview_video.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._preview_video.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._preview_video.setStyleSheet(
            "QVideoWidget#previewVideo { background-color: #050609; "
            "border-radius: 10px; }"
        )
        self._player.setVideoOutput(self._preview_video)
        self._preview_stack = QStackedWidget()
        self._preview_stack.setFixedSize(600, 338)
        self._preview_placeholder = QLabel("Selecciona un clip para ver su miniatura")
        self._preview_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_placeholder.setObjectName("previewPlaceholder")
        self._preview_placeholder.setFixedSize(600, 338)
        self._preview_stack.addWidget(self._preview_placeholder)
        self._preview_stack.addWidget(self._preview_video)
        self._preview_stack.setCurrentWidget(self._preview_placeholder)
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
        self._preview_time_label.setFixedWidth(170)
        self._preview_time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_timecode_value = QLabel("—")
        self._preview_duration_value = QLabel("—")
        for label in (
            self._preview_timecode_value,
            self._preview_duration_value,
        ):
            label.setObjectName("clipTimingValue")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        timing_panel = QFrame()
        timing_panel.setObjectName("clipTimingPanel")
        timing_layout = QHBoxLayout(timing_panel)
        timing_layout.setContentsMargins(12, 8, 12, 8)
        timing_layout.setSpacing(0)
        timecode_group = QVBoxLayout()
        timecode_group.setSpacing(3)
        timecode_title = QLabel("TIMECODE INICIAL")
        timecode_title.setObjectName("clipTimingTitle")
        timecode_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        timecode_group.addWidget(timecode_title)
        timecode_group.addWidget(self._preview_timecode_value)
        duration_group = QVBoxLayout()
        duration_group.setSpacing(3)
        duration_title = QLabel("DURACIÓN")
        duration_title.setObjectName("clipTimingTitle")
        duration_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        duration_group.addWidget(duration_title)
        duration_group.addWidget(self._preview_duration_value)
        timing_layout.addLayout(timecode_group, 1)
        timing_divider = QFrame()
        timing_divider.setFrameShape(QFrame.Shape.VLine)
        timing_divider.setObjectName("clipTimingDivider")
        timing_layout.addWidget(timing_divider)
        timing_layout.addLayout(duration_group, 1)
        timing_panel.setFixedWidth(self._preview_stack.width())

        self._preview_status = QLabel("Selecciona un clip para preparar la preview A/V.")
        self._preview_status.setWordWrap(True)
        self._preview_status.setObjectName("previewStatus")
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
        self._stop_button.clicked.connect(self._stop_playback)
        self._volume_slider = QSlider(Qt.Orientation.Horizontal)
        self._volume_slider.setRange(0, 100)
        self._volume_slider.setValue(70)
        self._volume_slider.setFixedWidth(100)
        self._volume_slider.valueChanged.connect(self._set_playback_volume)
        self._volume_button = QPushButton()
        self._volume_button.setIcon(
            self.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        )
        self._volume_button.setFixedSize(32, 32)
        self._volume_button.setToolTip("Volumen")
        self._volume_button.setAccessibleName("Volumen")
        self._volume_button.clicked.connect(self._toggle_playback_mute)
        self._fullscreen_button = QPushButton()
        self._fullscreen_button.setFixedSize(32, 32)
        self._fullscreen_button.setEnabled(False)
        self._fullscreen_button.clicked.connect(self._toggle_preview_fullscreen)
        self._set_fullscreen_button_state(False)
        self._preview_controls_container = QWidget()
        self._preview_controls_container.setFixedWidth(self._preview_stack.width())
        self._preview_controls_container.setFixedHeight(40)
        preview_controls = QHBoxLayout(self._preview_controls_container)
        preview_controls.setContentsMargins(0, 0, 0, 0)
        preview_controls.addWidget(self._play_button)
        preview_controls.addWidget(self._stop_button)
        preview_controls.addWidget(self._preview_position, 1)
        preview_controls.addWidget(self._preview_time_label)
        preview_controls.addWidget(self._volume_button)
        preview_controls.addWidget(self._volume_slider)
        preview_controls.addWidget(self._fullscreen_button)

        self._preview_fullscreen_dialog = QDialog(self)
        self._preview_fullscreen_dialog.setWindowTitle("Vista previa")
        self._preview_fullscreen_dialog.setStyleSheet(
            "QDialog { background-color: #050609; }"
        )
        self._preview_fullscreen_layout = QVBoxLayout(
            self._preview_fullscreen_dialog
        )
        self._preview_fullscreen_layout.setContentsMargins(12, 12, 12, 12)
        self._preview_fullscreen_layout.setSpacing(8)
        self._preview_fullscreen_video = QVideoWidget(
            self._preview_fullscreen_dialog
        )
        self._preview_fullscreen_video.setObjectName("previewFullscreenVideo")
        self._preview_fullscreen_video.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self._preview_fullscreen_video.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground,
            True,
        )
        self._preview_fullscreen_video.setStyleSheet(
            "QVideoWidget#previewFullscreenVideo { background-color: #050609; }"
        )
        self._preview_fullscreen_layout.addWidget(
            self._preview_fullscreen_video,
            1,
        )
        self._preview_fullscreen_dialog.finished.connect(
            self._exit_preview_fullscreen
        )
        self._preview_is_fullscreen = False

        preview_panel = QFrame()
        preview_panel.setObjectName("panelCard")
        preview_layout = QVBoxLayout(preview_panel)
        self._preview_layout = preview_layout
        preview_layout.setContentsMargins(16, 16, 16, 16)
        preview_layout.setSpacing(12)
        preview_heading = QHBoxLayout()
        preview_title = QLabel("Vista previa")
        preview_title.setObjectName("panelTitle")
        self._preview_file_label = QLabel("Sin clip")
        self._preview_file_label.setObjectName("mutedLabel")
        self._preview_file_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        preview_heading.addWidget(preview_title)
        preview_heading.addWidget(self._preview_file_label, 1)
        preview_layout.addLayout(preview_heading)
        preview_layout.addWidget(
            self._preview_stack, 0, Qt.AlignmentFlag.AlignHCenter
        )
        preview_layout.addWidget(
            self._preview_controls_container, 0, Qt.AlignmentFlag.AlignHCenter
        )
        self._preview_controls_layout_index = preview_layout.indexOf(
            self._preview_controls_container
        )
        preview_layout.addWidget(timing_panel, 0, Qt.AlignmentFlag.AlignHCenter)
        preview_layout.addWidget(self._preview_status)
        preview_layout.addStretch(1)
        self._install_playback_shortcuts()

        self._preview_render_timer = QTimer(self)
        self._preview_render_timer.setSingleShot(True)
        self._preview_render_timer.timeout.connect(self._start_playback_render)

        self._properties = QTextEdit()
        self._properties.setObjectName("clipDetails")
        self._properties.setReadOnly(True)
        self._properties.setPlaceholderText("Selecciona un clip para ver sus propiedades.")
        self._metadata = QTextEdit()
        self._metadata.setObjectName("clipDetails")
        self._metadata.setReadOnly(True)
        self._metadata.setPlaceholderText("Selecciona un clip para ver sus metadatos.")
        self._details_tabs = QTabWidget()
        self._details_tabs.setObjectName("clipDetailsTabs")
        self._details_tabs.addTab(self._properties, "Propiedades")
        self._details_tabs.addTab(self._metadata, "Metadata")
        controls = QHBoxLayout()
        controls.addWidget(self._scan_button)
        controls.addWidget(self._folder_label, 1)
        controls.addWidget(self._export_button)

        clips_panel = QFrame()
        clips_panel.setObjectName("panelCard")
        clips_layout = QVBoxLayout(clips_panel)
        clips_layout.setContentsMargins(16, 16, 16, 16)
        clips_layout.setSpacing(12)
        clips_heading = QHBoxLayout()
        clips_title = QLabel("Clips")
        clips_title.setObjectName("panelTitle")
        self._clip_count_label = QLabel("0 clips")
        self._clip_count_label.setObjectName("countBadge")
        self._clip_count_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        clips_heading.addWidget(clips_title)
        clips_heading.addStretch(1)
        clips_heading.addWidget(self._clip_count_label)
        clips_layout.addLayout(clips_heading)
        clips_layout.addWidget(self._clip_table, 1)

        details_panel = QFrame()
        details_panel.setObjectName("panelCard")
        details_layout = QVBoxLayout(details_panel)
        details_layout.setContentsMargins(16, 16, 16, 16)
        details_layout.setSpacing(12)
        details_title = QLabel("Clip properties")
        details_title.setObjectName("panelTitle")
        details_layout.addWidget(details_title)
        details_layout.addWidget(self._details_tabs, 1)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("mainSplitter")
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(clips_panel)
        splitter.addWidget(preview_panel)
        splitter.addWidget(details_panel)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 5)
        splitter.setStretchFactor(2, 3)
        splitter.setSizes([500, 680, 360])

        status_footer = QWidget()
        status_layout = QHBoxLayout(status_footer)
        status_layout.setContentsMargins(4, 0, 4, 0)
        status_dot = QLabel("●")
        status_dot.setObjectName("statusDot")
        status_note = QLabel(
            "La preview incluye audio P2 cuando la sincronía está verificada."
        )
        status_note.setObjectName("mutedLabel")
        status_layout.addWidget(status_dot)
        status_layout.addWidget(status_note)
        status_layout.addStretch(1)

        layout = QVBoxLayout()
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(14)
        app_title = QLabel("P2Viewer Plus")
        app_title.setObjectName("appTitle")
        app_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(app_title)
        layout.addLayout(controls)
        layout.addWidget(splitter, 1)
        layout.addWidget(status_footer)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)
        self.setStyleSheet(
            self.styleSheet()
            + """
            QFrame#panelCard {
                background-color: #171a22;
                border: 1px solid #282e3a;
                border-radius: 16px;
            }
            QLabel#appTitle {
                color: #dce2ed;
                font-size: 16px;
                font-weight: 600;
                padding: 4px;
            }
            QLabel#panelTitle {
                color: #e0e5ee;
                font-size: 16px;
                font-weight: 600;
                background-color: transparent;
            }
            QLabel#mutedLabel {
                color: #9aa5b7;
                background-color: transparent;
            }
            QLabel#countBadge {
                color: #b9c3d4;
                background-color: #202530;
                border-radius: 12px;
                padding: 4px 12px;
            }
            QLabel#statusDot { color: #48d597; }
            QLabel#previewStatus { color: #9aa5b7; }
            QFrame#clipTimingPanel {
                background-color: #1d2530;
                border: 1px solid #2e3c4d;
                border-radius: 8px;
            }
            QLabel#clipTimingTitle {
                color: #8f9cad;
                font-size: 9px;
                font-weight: 600;
                background-color: transparent;
            }
            QLabel#clipTimingValue {
                color: #e6edf8;
                font-size: 16px;
                font-weight: 600;
                background-color: transparent;
            }
            QLabel#folderPath {
                color: #aeb8c8;
                background-color: #171a22;
                border: 1px solid #282e3a;
                border-radius: 12px;
                padding: 0 16px;
            }
            QPushButton#primaryButton,
            QPushButton#exportButton {
                color: #111827;
                background-color: #ffffff;
                border: none;
                border-radius: 12px;
                padding: 0 18px;
                font-weight: 600;
            }
            QPushButton#primaryButton:hover,
            QPushButton#exportButton:hover { background-color: #e8edf5; }
            QTableWidget#clipsTable {
                background-color: transparent;
                border: none;
                gridline-color: transparent;
            }
            QTableWidget#clipsTable::item {
                border: none;
                padding: 0;
            }
            QWidget#clipListRow {
                background-color: transparent;
                border-bottom: 1px solid #282e3a;
            }
            QWidget#clipListRow[selected="true"] {
                background-color: #1d3851;
            }
            QLabel#clipRowThumbnail {
                color: #9aa5b7;
                background-color: #11151c;
                border-radius: 4px;
                font-size: 10px;
            }
            QLabel#clipRowFilename {
                color: #e5ebf5;
                font-size: 12px;
                font-weight: 600;
            }
            QLabel#clipRowDescription {
                color: #9aa5b7;
                font-size: 11px;
            }
            QLabel#clipRowSummary {
                color: #8793a4;
                font-size: 10px;
            }
            QLabel#clipRowAudioIcon { padding: 2px; }
            QHeaderView::section {
                color: #9aa5b7;
                background-color: #171a22;
                border: none;
                padding: 6px;
                font-size: 11px;
                font-weight: 600;
            }
            QTextEdit#clipDetails {
                color: #dce2ed;
                font-size: 11px;
                background-color: transparent;
                border: none;
                selection-background-color: #344b77;
            }
            QTabWidget#clipDetailsTabs::pane {
                border: 1px solid #282e3a;
                border-radius: 5px;
                top: -1px;
            }
            QTabWidget#clipDetailsTabs QTabBar::tab {
                color: #9aa5b7;
                background-color: transparent;
                border: none;
                border-bottom: 2px solid transparent;
                padding: 8px 12px;
            }
            QTabWidget#clipDetailsTabs QTabBar::tab:selected {
                color: #79a9ff;
                border-bottom-color: #79a9ff;
            }
            QLabel#previewPlaceholder {
                color: #9aa5b7;
                background-color: #050609;
                border-radius: 10px;
            }
            QVideoWidget#previewVideo { background-color: #050609; }
            QSplitter::handle { background-color: transparent; width: 12px; }
            """
        )
        self.statusBar().showMessage("Listo")

    @Slot()
    def _choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta")
        if folder:
            self._start_scan(Path(folder))

    def _start_scan(self, folder: Path) -> None:
        if self._thread is not None:
            return
        self._cancel_clip_thumbnail_render()

        self._selected_folder = folder
        self._folder_label.setText(str(folder))
        self._scan_button.setEnabled(False)
        self._scan_button.setText("Escaneando...")
        self._export_button.setEnabled(False)
        self._properties.clear()
        self._metadata.clear()
        self._preview_timecode_value.setText("—")
        self._preview_duration_value.setText("—")
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
        started_at = time.perf_counter()
        self._p2_result = result
        self._p2_warnings = result.warnings
        self._populate_clip_table()
        self.statusBar().showMessage(
            f"{len(self._clip_assemblies)} clips listos; midiendo sincronía P2…"
        )
        logger.info(
            "Clip list displayed before sync analysis: clips=%d elapsed=%.3fs",
            len(self._clip_assemblies),
            time.perf_counter() - started_at,
        )

    @Slot(object)
    def _set_sync_analyses(self, analyses: list[SyncAnalysis]) -> None:
        self._sync_by_video = {
            analysis.clip.video_file: analysis
            for analysis in analyses
            if analysis.clip.video_file is not None
        }
        for video_path, row in self._clip_thumbnail_rows.items():
            sync = self._sync_by_video.get(video_path)
            if sync is None:
                continue
            assembly = self._clip_assemblies[row]
            clip_row = self._clip_table.cellWidget(row, 0)
            if isinstance(clip_row, _ClipListRow):
                clip_row.summary_label.setText(
                    self._format_clip_row_summary(assembly.metadata, sync)
                )
                clip_row.summary_label.setToolTip(
                    clip_row.summary_label.text()
                )
        self._update_clip_information(self._clip_table.currentRow())

    def _prepare_playback_preview(
        self,
        video_path: Path,
        audio_tracks: tuple[AudioSync, ...],
        audio_verified: bool,
        declared_audio_count: int,
        duration_seconds: float | None,
    ) -> None:
        self._exit_preview_fullscreen()
        if self._preview_render_thread is not None:
            self._preview_render_thread.cancel()

        self._preview_requested_at = time.perf_counter()
        self._preview_render_finished_at = None
        self._active_preview_token = None
        self._preview_ready_logged_token = None
        self._video_ready_token = None
        self._audio_ready_token = None
        self._audio_failed_token = None
        self._has_external_audio = audio_verified and bool(audio_tracks)
        self._player.stop()
        self._player.setSource(QUrl())
        self._audio_player.stop()
        self._audio_player.setSource(QUrl())
        self._show_preview_thumbnail(video_path)
        self._remove_active_preview_file()
        self._preview_position.blockSignals(True)
        self._preview_position.setValue(0)
        self._preview_position.setEnabled(False)
        self._preview_position.blockSignals(False)
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._fullscreen_button.setEnabled(False)
        self._preview_time_label.setText(
            f"00:00.000 / {self._format_seconds(duration_seconds)}"
        )

        token = uuid.uuid4().hex
        self._wanted_playback = (token, video_path, audio_tracks)
        self._active_preview_token = token
        if not self._has_external_audio:
            self._audio_ready_token = token
        self._set_playback_volume(self._volume_slider.value())
        self._player.setSource(QUrl.fromLocalFile(str(video_path)))
        if audio_verified and audio_tracks:
            self._preview_status.setText(
                f"Abriendo MXF original y preparando mezcla sincronizada de "
                f"{len(audio_tracks)} pista(s) de audio…"
            )
        elif declared_audio_count:
            self._preview_status.setText(
                "Abriendo MXF original. Audio desactivado: no se pudo verificar "
                "la sincronía de todas las pistas."
            )
        else:
            self._preview_status.setText(
                "Abriendo MXF original sin pistas de audio asociadas."
            )
        self._preview_render_timer.start(0)

    def _clear_playback_preview(self, status: str) -> None:
        self._exit_preview_fullscreen()
        self._wanted_playback = None
        self._preview_file_label.setText("Sin clip")
        self._preview_placeholder.setPixmap(QPixmap())
        self._preview_placeholder.setText("Selecciona un clip para ver su miniatura")
        self._preview_stack.setCurrentWidget(self._preview_placeholder)
        self._preview_requested_at = None
        self._preview_render_finished_at = None
        self._active_preview_token = None
        self._preview_ready_logged_token = None
        self._video_ready_token = None
        self._audio_ready_token = None
        self._audio_failed_token = None
        self._has_external_audio = False
        self._preview_render_timer.stop()
        if self._preview_render_thread is not None:
            self._preview_render_thread.cancel()
        self._player.stop()
        self._player.setSource(QUrl())
        self._audio_player.stop()
        self._audio_player.setSource(QUrl())
        self._set_playback_volume(self._volume_slider.value())
        self._remove_active_preview_file()
        self._preview_position.blockSignals(True)
        self._preview_position.setValue(0)
        self._preview_position.setEnabled(False)
        self._preview_position.blockSignals(False)
        self._preview_time_label.setText("—")
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._fullscreen_button.setEnabled(False)
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
        cache_key: PreviewCacheKey | None = None
        if audio_tracks:
            try:
                cache_key = self._preview_cache_key(video_path, audio_tracks)
            except OSError as error:
                self._preview_status.setText(
                    f"No se pudo comprobar el audio para la preview: {error}"
                )
                self._audio_failed_token = token
                self._has_external_audio = False
                self._set_playback_volume(self._volume_slider.value())
                self._update_playback_controls()
                return
            cached_path = self._preview_cache.get(cache_key)
            if cached_path is not None:
                try:
                    is_valid = cached_path.is_file() and cached_path.stat().st_size > 0
                except OSError:
                    is_valid = False
                if is_valid:
                    self._preview_cache.move_to_end(cache_key)
                    logger.info("Preview audio cache hit: video=%s", video_path.name)
                    self._activate_playback_preview(
                        cached_path,
                        cache_hit=True,
                    )
                    return
                self._preview_cache.pop(cache_key)

        audio_path = Path(self._preview_temp_dir.name) / f"{token}.wav"
        thumbnail_path = Path(self._preview_temp_dir.name) / f"{token}.jpg"
        if cache_key is not None:
            self._preview_cache_keys_by_token[token] = cache_key
        thread = _PreviewAssetsThread(
            token,
            video_path,
            audio_tracks,
            audio_path,
            thumbnail_path,
            self,
        )
        thread.completed.connect(self._playback_audio_rendered)
        thread.thumbnail_completed.connect(self._preview_thumbnail_rendered)
        thread.finished.connect(self._playback_render_finished)
        thread.finished.connect(thread.deleteLater)
        self._preview_render_thread = thread
        self._preview_render_token = token
        thread.start()

    @staticmethod
    def _preview_thumbnail_key(video_path: Path) -> PreviewThumbnailKey:
        video_stat = video_path.stat()
        return (
            str(video_path.resolve()),
            video_stat.st_size,
            video_stat.st_mtime_ns,
        )

    def _show_preview_thumbnail(self, video_path: Path) -> None:
        self._preview_placeholder.setPixmap(QPixmap())
        self._preview_placeholder.setText("Generando miniatura…")
        self._preview_stack.setCurrentWidget(self._preview_placeholder)
        try:
            key = self._preview_thumbnail_key(video_path)
        except OSError as error:
            self._preview_placeholder.setText("Miniatura no disponible")
            logger.warning(
                "Preview thumbnail cache lookup failed: video=%s reason=%s",
                video_path.name,
                error,
            )
            return
        cached_thumbnail = self._preview_thumbnail_cache.get(key)
        if cached_thumbnail is not None:
            self._preview_thumbnail_cache.move_to_end(key)
            self._set_preview_thumbnail(cached_thumbnail)

    def _set_preview_thumbnail(self, thumbnail: QPixmap) -> None:
        scaled_thumbnail = thumbnail.scaled(
            self._preview_placeholder.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._preview_placeholder.setPixmap(scaled_thumbnail)
        self._preview_placeholder.setText("")
        self._preview_stack.setCurrentWidget(self._preview_placeholder)

    @Slot(str, str, str)
    def _preview_thumbnail_rendered(
        self,
        token: str,
        thumbnail_name: str,
        error: str,
    ) -> None:
        thumbnail_path = Path(thumbnail_name) if thumbnail_name else None
        wanted_token = (
            self._wanted_playback[0]
            if self._wanted_playback is not None
            else None
        )
        if token != wanted_token:
            if thumbnail_path is not None:
                self._delete_preview_file(thumbnail_path)
            return
        if error:
            self._preview_placeholder.setText("Miniatura no disponible")
            logger.warning(
                "Preview thumbnail unavailable: video=%s reason=%s",
                self._wanted_playback[1].name if self._wanted_playback else "unknown",
                error,
            )
            return
        if thumbnail_path is None:
            return
        thumbnail = QPixmap(str(thumbnail_path))
        self._delete_preview_file(thumbnail_path)
        if thumbnail.isNull():
            self._preview_placeholder.setText("Miniatura no disponible")
            logger.warning(
                "Preview thumbnail could not be loaded: video=%s",
                self._wanted_playback[1].name if self._wanted_playback else "unknown",
            )
            return
        if self._wanted_playback is not None:
            try:
                key = self._preview_thumbnail_key(self._wanted_playback[1])
            except OSError as error:
                logger.warning(
                    "Preview thumbnail could not be cached: reason=%s",
                    error,
                )
            else:
                self._preview_thumbnail_cache[key] = thumbnail
                self._preview_thumbnail_cache.move_to_end(key)
                while (
                    len(self._preview_thumbnail_cache)
                    > PREVIEW_THUMBNAIL_CACHE_MAX_ENTRIES
                ):
                    self._preview_thumbnail_cache.popitem(last=False)
        self._set_preview_thumbnail(thumbnail)

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
                "Preview audio not cached: WAV mix is larger than cache limit "
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

    @Slot()
    def _playback_render_finished(self) -> None:
        self._preview_render_thread = None
        finished_token = self._preview_render_token
        self._preview_render_token = None
        wanted_token = (
            self._wanted_playback[0]
            if self._wanted_playback is not None
            else None
        )
        if wanted_token is not None and finished_token != wanted_token:
            self._preview_render_timer.start(0)

        if self._closing_after_preview:
            QTimer.singleShot(0, self.close)

    @Slot(str, str, str)
    def _playback_audio_rendered(
        self,
        token: str,
        audio_name: str,
        error: str,
    ) -> None:
        audio_path = Path(audio_name) if audio_name else None
        if self._closing_after_preview:
            self._preview_cache_keys_by_token.pop(token, None)
            if audio_path is not None:
                self._delete_preview_file(audio_path)
            return
        if token != self._active_preview_token:
            self._preview_cache_keys_by_token.pop(token, None)
            if audio_path is not None:
                self._delete_preview_file(audio_path)
            return
        if error:
            self._preview_cache_keys_by_token.pop(token, None)
            self._audio_failed_token = token
            self._has_external_audio = False
            self._set_playback_volume(self._volume_slider.value())
            self._preview_status.setText(
                "MXF original abierto; no se pudo preparar el audio "
                f"sincronizado: {error}"
            )
            self._update_playback_controls()
            return
        if audio_path is None:
            return

        cache_key = self._preview_cache_keys_by_token.pop(token, None)
        if cache_key is not None:
            self._cache_preview(cache_key, audio_path)
        self._activate_playback_preview(audio_path, cache_hit=False)

    def _activate_playback_preview(
        self,
        output_path: Path,
        *,
        cache_hit: bool,
    ) -> None:
        self._active_preview_path = output_path
        wanted_playback = self._wanted_playback
        if wanted_playback is None:
            return
        logger.info(
            "Preview audio ready: video=%s audio_tracks=%d cache_hit=%s",
            wanted_playback[1].name,
            len(wanted_playback[2]),
            cache_hit,
        )
        self._audio_ready_token = None
        self._audio_failed_token = None
        self._audio_player.setSource(QUrl.fromLocalFile(str(output_path)))
        self._preview_status.setText(
            "MXF original abierto; cargando mezcla de audio sincronizada…"
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

    def _install_playback_shortcuts(self) -> None:
        shortcuts = (
            ("Space", self._shortcut_play_pause),
            ("M", self._shortcut_toggle_mute),
            ("Left", self._shortcut_rewind),
            ("Right", self._shortcut_forward),
            ("F", self._shortcut_toggle_fullscreen),
        )
        self._playback_shortcuts: list[QShortcut] = []
        for sequence, callback in shortcuts:
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self._playback_shortcuts.append(shortcut)
        for sequence, callback in (
            ("F", self._shortcut_toggle_fullscreen),
            ("Escape", self._shortcut_exit_fullscreen),
        ):
            shortcut = QShortcut(
                QKeySequence(sequence),
                self._preview_fullscreen_dialog,
            )
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(callback)
            self._playback_shortcuts.append(shortcut)

    def _shortcut_is_blocked_by_text_input(self) -> bool:
        widget = QApplication.focusWidget()
        while widget is not None:
            if isinstance(
                widget,
                (QLineEdit, QPlainTextEdit, QTextEdit, QAbstractSpinBox),
            ):
                return True
            widget = widget.parentWidget()
        return False

    @Slot()
    def _shortcut_play_pause(self) -> None:
        if (
            not self._shortcut_is_blocked_by_text_input()
            and self._play_button.isEnabled()
        ):
            self._toggle_playback()

    @Slot()
    def _shortcut_toggle_mute(self) -> None:
        if (
            not self._shortcut_is_blocked_by_text_input()
            and self._active_preview_token is not None
        ):
            self._toggle_playback_mute()

    @Slot()
    def _shortcut_rewind(self) -> None:
        if not self._shortcut_is_blocked_by_text_input():
            self._seek_playback_by(-1000)

    @Slot()
    def _shortcut_forward(self) -> None:
        if not self._shortcut_is_blocked_by_text_input():
            self._seek_playback_by(1000)

    @Slot()
    def _shortcut_toggle_fullscreen(self) -> None:
        if self._preview_is_fullscreen or (
            not self._shortcut_is_blocked_by_text_input()
            and self._fullscreen_button.isEnabled()
        ):
            self._toggle_preview_fullscreen()

    @Slot()
    def _shortcut_exit_fullscreen(self) -> None:
        self._exit_preview_fullscreen()

    @Slot()
    def _toggle_preview_fullscreen(self) -> None:
        if self._preview_is_fullscreen:
            self._exit_preview_fullscreen()
            return
        if not self._fullscreen_button.isEnabled():
            return

        self._preview_is_fullscreen = True
        self._preview_stack.setCurrentWidget(self._preview_video)
        self._player.setVideoOutput(self._preview_fullscreen_video)
        self._preview_fullscreen_layout.addWidget(
            self._preview_controls_container
        )
        self._preview_controls_container.setMinimumWidth(
            self._preview_stack.width()
        )
        self._preview_controls_container.setMaximumWidth(16_777_215)
        self._preview_controls_container.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )
        self._set_fullscreen_button_state(True)
        self._preview_fullscreen_dialog.showFullScreen()

    @Slot()
    def _exit_preview_fullscreen(self) -> None:
        if not self._preview_is_fullscreen:
            return
        self._preview_is_fullscreen = False
        self._preview_fullscreen_dialog.hide()
        self._player.setVideoOutput(self._preview_video)
        self._preview_stack.setCurrentWidget(self._preview_video)
        self._preview_layout.insertWidget(
            self._preview_controls_layout_index,
            self._preview_controls_container,
            0,
            Qt.AlignmentFlag.AlignHCenter,
        )
        self._preview_controls_container.setFixedWidth(
            self._preview_stack.width()
        )
        self._set_fullscreen_button_state(False)

    def _set_fullscreen_button_state(self, fullscreen: bool) -> None:
        icon = (
            QStyle.StandardPixmap.SP_TitleBarNormalButton
            if fullscreen
            else QStyle.StandardPixmap.SP_TitleBarMaxButton
        )
        label = (
            "Salir de pantalla completa (F)"
            if fullscreen
            else "Pantalla completa (F)"
        )
        self._fullscreen_button.setIcon(self.style().standardIcon(icon))
        self._fullscreen_button.setToolTip(label)
        self._fullscreen_button.setAccessibleName(label)

    @Slot()
    def _toggle_playback(self) -> None:
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
            self._audio_player.pause()
        else:
            self._preview_stack.setCurrentWidget(self._preview_video)
            if self._audio_ready_token == self._active_preview_token:
                self._audio_player.setPosition(self._player.position())
                self._audio_player.play()
            self._player.play()

    @Slot()
    def _stop_playback(self) -> None:
        self._player.stop()
        self._audio_player.stop()
        if self._audio_ready_token == self._active_preview_token:
            self._audio_player.setPosition(0)

    @Slot(int)
    def _set_playback_volume(self, volume: int) -> None:
        level = 0 if self._playback_muted else volume / 100
        self._audio_output.setVolume(0 if self._has_external_audio else level)
        self._audio_mix_output.setVolume(level if self._has_external_audio else 0)

    @Slot()
    def _toggle_playback_mute(self) -> None:
        self._playback_muted = not self._playback_muted
        icon = (
            QStyle.StandardPixmap.SP_MediaVolumeMuted
            if self._playback_muted
            else QStyle.StandardPixmap.SP_MediaVolume
        )
        label = "Activar audio" if self._playback_muted else "Silenciar audio"
        self._volume_button.setIcon(self.style().standardIcon(icon))
        self._volume_button.setToolTip(label)
        self._volume_button.setAccessibleName(label)
        self._set_playback_volume(self._volume_slider.value())

    def _seek_playback_by(self, offset_ms: int) -> None:
        duration = self._player.duration()
        if duration <= 0:
            return
        position = min(duration, max(0, self._player.position() + offset_ms))
        self._player.setPosition(position)
        if self._audio_ready_token == self._active_preview_token:
            self._audio_player.setPosition(position)

    def _update_playback_controls(self) -> None:
        token = self._active_preview_token
        video_ready = token is not None and self._video_ready_token == token
        audio_ready = (
            not self._has_external_audio
            or self._audio_ready_token == token
            or self._audio_failed_token == token
        )
        ready = video_ready and audio_ready
        self._play_button.setEnabled(ready)
        self._stop_button.setEnabled(ready)
        self._fullscreen_button.setEnabled(ready or self._preview_is_fullscreen)
        if ready and token != self._preview_ready_logged_token:
            self._preview_ready_logged_token = token
            now = time.perf_counter()
            requested_at = self._preview_requested_at
            if requested_at is not None:
                logger.info(
                    "Preview playable: video=%s source=original-mxf "
                    "elapsed_since_selection=%.3fs audio_tracks=%d",
                    self._wanted_playback[1].name
                    if self._wanted_playback is not None
                    else "unknown",
                    now - requested_at,
                    len(self._wanted_playback[2])
                    if self._wanted_playback is not None
                    and self._audio_ready_token == token
                    else 0,
                )

    @Slot(QMediaPlayer.MediaStatus)
    def _preview_audio_media_status_changed(
        self,
        status: QMediaPlayer.MediaStatus,
    ) -> None:
        if self._audio_player.source().isEmpty():
            return
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._audio_ready_token = self._active_preview_token
            self._preview_render_finished_at = time.perf_counter()
            self._set_playback_volume(self._volume_slider.value())
            self._preview_status.clear()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._audio_failed_token = self._active_preview_token
            self._has_external_audio = False
            self._set_playback_volume(self._volume_slider.value())
            self._preview_status.setText(
                "MXF original abierto; no se pudo cargar el audio sincronizado: "
                f"{self._audio_player.errorString()}"
            )
        self._update_playback_controls()

    @Slot(QMediaPlayer.Error, str)
    def _preview_audio_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError or self._active_preview_token is None:
            return
        self._audio_failed_token = self._active_preview_token
        self._has_external_audio = False
        self._set_playback_volume(self._volume_slider.value())
        self._preview_status.setText(
            "MXF original abierto; falló el audio sincronizado: "
            f"{message or self._audio_player.errorString()}"
        )
        self._update_playback_controls()

    @Slot()
    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        is_playing = state == QMediaPlayer.PlaybackState.PlayingState
        if is_playing:
            self._preview_stack.setCurrentWidget(self._preview_video)
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
        if (
            self._audio_ready_token == self._active_preview_token
            and self._audio_player.playbackState()
            == QMediaPlayer.PlaybackState.PlayingState
            and abs(self._audio_player.position() - position) > 200
        ):
            self._audio_player.setPosition(position)
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
            position = round(duration * value / 1000)
            self._player.setPosition(position)
            if self._audio_ready_token == self._active_preview_token:
                self._audio_player.setPosition(position)

    @Slot(QMediaPlayer.MediaStatus)
    def _playback_media_status_changed(
        self,
        status: QMediaPlayer.MediaStatus,
    ) -> None:
        if status in (
            QMediaPlayer.MediaStatus.LoadedMedia,
            QMediaPlayer.MediaStatus.BufferedMedia,
        ):
            self._video_ready_token = self._active_preview_token
            if not self._has_external_audio:
                self._preview_status.clear()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia:
            self._video_ready_token = None
            self._audio_player.stop()
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
                "El reproductor no pudo abrir el MXF original: "
                f"{self._player.errorString()}"
            )
        elif status == QMediaPlayer.MediaStatus.EndOfMedia:
            self._audio_player.stop()
        self._update_playback_controls()

    @Slot(QMediaPlayer.Error, str)
    def _playback_error(self, error: QMediaPlayer.Error, message: str) -> None:
        if error == QMediaPlayer.Error.NoError or self._active_preview_token is None:
            return
        self._play_button.setEnabled(False)
        self._stop_button.setEnabled(False)
        self._fullscreen_button.setEnabled(self._preview_is_fullscreen)
        self._preview_status.setText(
            f"No se pudo reproducir la preview: {message or self._player.errorString()}"
        )

    @Slot(int)
    def _scan_finished(self, files_found: int) -> None:
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
        started_at = time.perf_counter()
        self._cancel_clip_thumbnail_render()
        self._clip_assemblies = build_clip_assemblies(self._media, self._p2_result)
        self._clip_table.setUpdatesEnabled(False)
        try:
            self._clip_table.setRowCount(len(self._clip_assemblies))
            self._clip_count_label.setText(f"{len(self._clip_assemblies)} clips")
            self._export_button.setEnabled(bool(self._clip_assemblies))
            video_paths: list[Path] = []
            self._clip_thumbnail_rows = {
                assembly.video.path: row
                for row, assembly in enumerate(self._clip_assemblies)
            }
            for row, assembly in enumerate(self._clip_assemblies):
                metadata = assembly.metadata
                description = (
                    metadata.clip_name or "Sin descripción"
                    if metadata is not None
                    else "Sin metadata P2"
                )
                summary = self._format_clip_row_summary(metadata, None)

                item = QTableWidgetItem()
                item.setFlags(
                    Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
                )
                self._clip_table.setItem(row, 0, item)
                clip_row = _ClipListRow(
                    row,
                    assembly.video.path.name,
                    description,
                    summary,
                    bool(assembly.audio_files),
                    self._clip_table,
                )
                clip_row.clicked.connect(self._select_clip_row)
                self._clip_table.setCellWidget(row, 0, clip_row)
                self._clip_table.setRowHeight(row, 86)
                video_paths.append(assembly.video.path)
        finally:
            self._clip_table.setUpdatesEnabled(True)
        self._start_clip_thumbnail_render(tuple(video_paths))
        logger.info(
            "Clip table populated: clips=%d elapsed=%.3fs",
            len(self._clip_assemblies),
            time.perf_counter() - started_at,
        )

    @classmethod
    def _clip_export_rows(
        cls,
        folder_name: str,
        assemblies: list[ClipAssembly],
        sync_by_video: dict[Path, SyncAnalysis],
    ) -> list[tuple[str, str, str]]:
        rows: list[tuple[str, str, str]] = []
        for assembly in assemblies:
            metadata = assembly.metadata
            sync = sync_by_video.get(assembly.video.path)
            clip_name = (
                metadata.clip_name
                if metadata is not None and metadata.clip_name
                else assembly.video.path.stem
            )
            rows.append(
                (
                    f"{folder_name}_{clip_name}",
                    cls._clip_timecode(metadata, sync) or "—",
                    cls._format_duration_clock(
                        cls._clip_duration_seconds(metadata, sync)
                    ),
                )
            )
        return rows

    @Slot()
    def _export_clips_csv(self) -> None:
        if not self._clip_assemblies or self._selected_folder is None:
            self.statusBar().showMessage("No hay clips para exportar.")
            return
        default_path = self._selected_folder / f"{self._selected_folder.name}_clips.csv"
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar clips a CSV",
            str(default_path),
            "CSV (*.csv)",
        )
        if not file_name:
            return
        output_path = Path(file_name)
        if output_path.suffix.casefold() != ".csv":
            output_path = output_path.with_suffix(".csv")
        rows = self._clip_export_rows(
            self._selected_folder.name,
            self._clip_assemblies,
            self._sync_by_video,
        )
        try:
            with output_path.open("w", encoding="utf-8-sig", newline="") as csv_file:
                writer = csv.writer(csv_file)
                writer.writerow(("Clip", "Timecode", "Duración"))
                writer.writerows(rows)
        except OSError as error:
            QMessageBox.critical(
                self,
                "No se pudo exportar el CSV",
                f"No se pudo guardar {output_path}: {error}",
            )
            return
        self.statusBar().showMessage(
            f"CSV exportado: {output_path} ({len(rows)} clips)"
        )

    def _start_clip_thumbnail_render(self, video_paths: tuple[Path, ...]) -> None:
        if not video_paths:
            return
        token = uuid.uuid4().hex
        thread = _ClipThumbnailsThread(
            token,
            video_paths,
            Path(self._preview_temp_dir.name),
            self,
        )
        thread.thumbnail_ready.connect(self._clip_thumbnail_ready)
        thread.finished.connect(self._clip_thumbnail_render_finished)
        thread.finished.connect(thread.deleteLater)
        self._clip_thumbnail_token = token
        self._clip_thumbnail_thread = thread
        thread.start()

    def _cancel_clip_thumbnail_render(self) -> None:
        self._clip_thumbnail_token = None
        self._clip_thumbnail_rows.clear()
        thread = self._clip_thumbnail_thread
        if thread is None:
            return
        thread.cancel()
        thread.wait()
        self._clip_thumbnail_thread = None

    @Slot(str, str, str, str)
    def _clip_thumbnail_ready(
        self,
        token: str,
        video_name: str,
        thumbnail_name: str,
        error: str,
    ) -> None:
        thumbnail_path = Path(thumbnail_name) if thumbnail_name else None
        if token != self._clip_thumbnail_token:
            if thumbnail_path is not None:
                self._delete_preview_file(thumbnail_path)
            return
        try:
            video_path = Path(video_name)
            row = self._clip_thumbnail_rows.get(video_path, -1)
            if row < 0:
                return
            clip_row = self._clip_table.cellWidget(row, 0)
            if not isinstance(clip_row, _ClipListRow):
                return
            if error:
                clip_row.thumbnail.setText("No disponible")
                logger.warning(
                    "Clip thumbnail unavailable: video=%s reason=%s",
                    video_path.name,
                    error,
                )
                return
            if thumbnail_path is None:
                return
            thumbnail = QPixmap(str(thumbnail_path))
            if thumbnail.isNull():
                clip_row.thumbnail.setText("No disponible")
                logger.warning(
                    "Clip thumbnail could not be loaded: video=%s",
                    video_path.name,
                )
                return
            clip_row.set_thumbnail(thumbnail)
            try:
                key = self._preview_thumbnail_key(video_path)
            except OSError as cache_error:
                logger.warning(
                    "Clip thumbnail could not be cached for preview: video=%s "
                    "reason=%s",
                    video_path.name,
                    cache_error,
                )
            else:
                self._preview_thumbnail_cache[key] = thumbnail
                self._preview_thumbnail_cache.move_to_end(key)
                while (
                    len(self._preview_thumbnail_cache)
                    > PREVIEW_THUMBNAIL_CACHE_MAX_ENTRIES
                ):
                    self._preview_thumbnail_cache.popitem(last=False)
        finally:
            if thumbnail_path is not None:
                self._delete_preview_file(thumbnail_path)

    @Slot()
    def _clip_thumbnail_render_finished(self) -> None:
        if self.sender() is self._clip_thumbnail_thread:
            self._clip_thumbnail_thread = None

    @Slot(int)
    def _select_clip_row(self, row: int) -> None:
        self._clip_table.setCurrentCell(row, 0)
        self._clip_table.selectRow(row)

    @Slot()
    def _show_clip_selection(self) -> None:
        current_row = self._clip_table.currentRow()
        for row in range(self._clip_table.rowCount()):
            clip_row = self._clip_table.cellWidget(row, 0)
            if isinstance(clip_row, _ClipListRow):
                clip_row.set_selected(row == current_row)
        if current_row < 0 or current_row >= len(self._clip_assemblies):
            self._properties.clear()
            self._metadata.clear()
            self._preview_timecode_value.setText("—")
            self._preview_duration_value.setText("—")
            self._clear_playback_preview(
                "Selecciona un clip para preparar la preview A/V."
            )
            return

        assembly = self._clip_assemblies[current_row]
        self._preview_file_label.setText(assembly.video.path.name)
        metadata = assembly.metadata
        sync = self._sync_by_video.get(assembly.video.path)
        duration_seconds = self._clip_duration_seconds(metadata, sync)
        duration = (
            float(duration_seconds) if duration_seconds is not None else None
        )
        audio_tracks = sync.audio if sync is not None and sync.eligible else ()
        audio_verified = bool(audio_tracks)
        self._prepare_playback_preview(
            assembly.video.path,
            audio_tracks,
            audio_verified,
            len(assembly.audio_files),
            duration,
        )
        self._update_clip_information(current_row)

    @staticmethod
    def _clip_timecode(
        metadata: P2ClipMetadata | None,
        sync: SyncAnalysis | None,
    ) -> str | None:
        if metadata is not None and metadata.start_timecode:
            return metadata.start_timecode
        return sync.video_timecode if sync is not None else None

    @staticmethod
    def _clip_duration_seconds(
        metadata: P2ClipMetadata | None,
        sync: SyncAnalysis | None,
    ) -> Fraction | None:
        if metadata is not None and metadata.duration_seconds is not None:
            return metadata.duration_seconds
        if (
            metadata is not None
            and metadata.duration_frames is not None
            and metadata.edit_unit is not None
        ):
            try:
                return metadata.duration_frames * Fraction(metadata.edit_unit)
            except (ValueError, ZeroDivisionError):
                return None
        return sync.video_duration_seconds if sync is not None else None

    @staticmethod
    def _format_timecode_clock(timecode: str | None) -> str:
        if timecode is None or len(timecode) != 11:
            return "—"
        if timecode[2] != ":" or timecode[5] != ":" or timecode[8] not in ":;":
            return "—"
        if not all(
            timecode[start : start + 2].isdigit()
            for start in (0, 3, 6, 9)
        ):
            return "—"
        return timecode[:8]

    @staticmethod
    def _format_duration_clock(seconds: Fraction | None) -> str:
        if seconds is None:
            return "—"
        total_seconds = max(0, int(seconds + Fraction(1, 2)))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, whole_seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}"

    @classmethod
    def _format_clip_row_summary(
        cls,
        metadata: P2ClipMetadata | None,
        sync: SyncAnalysis | None,
    ) -> str:
        timecode = cls._format_timecode_clock(cls._clip_timecode(metadata, sync))
        duration = cls._format_duration_clock(
            cls._clip_duration_seconds(metadata, sync)
        )
        frame_rate = metadata.frame_rate if metadata is not None else None
        summary = f"TC {timecode}  ·  Dur. {duration}"
        if frame_rate:
            summary += f"  ·  {frame_rate}p"
        return summary

    @staticmethod
    def _property_rows(rows: tuple[tuple[str, str], ...]) -> str:
        return "".join(
            "<tr>"
            f'<td style="color:#8290a1;padding:4px 8px 4px 0">'
            f"{html.escape(label)}</td>"
            f'<td style="color:#dce2ed;padding:4px 0">'
            f"{html.escape(value)}</td>"
            "</tr>"
            for label, value in rows
        )

    @staticmethod
    def _format_stream_bitrate(bit_rate: str | None) -> str:
        if bit_rate is None:
            return "—"
        try:
            megabits = int(bit_rate) / 1_000_000
        except ValueError:
            return "—"
        return f"{megabits:.1f} Mb/s"

    @staticmethod
    def _format_scan_type(field_order: str | None) -> str:
        return {
            "progressive": "Progressive",
            "tt": "Interlaced (top first)",
            "tb": "Interlaced (top first)",
            "bb": "Interlaced (bottom first)",
            "bt": "Interlaced (bottom first)",
        }.get(field_order or "", "—")

    @staticmethod
    def _format_pixel_format(pixel_format: str | None) -> str:
        if pixel_format is None:
            return "—"
        sampling = next(
            (
                label
                for prefix, label in (
                    ("yuv420", "4:2:0"),
                    ("yuv422", "4:2:2"),
                    ("yuv444", "4:4:4"),
                    ("yuv411", "4:1:1"),
                    ("yuv440", "4:4:0"),
                )
                if pixel_format.startswith(prefix)
            ),
            None,
        )
        bit_depth = next(
            (
                f"{depth}-bit"
                for depth in (16, 14, 12, 10, 9)
                if f"{depth}le" in pixel_format or f"{depth}be" in pixel_format
            ),
            "8-bit",
        )
        return f"{sampling} / {bit_depth}" if sampling else pixel_format

    def _update_clip_information(self, row: int) -> None:
        if row < 0 or row >= len(self._clip_assemblies):
            self._properties.clear()
            self._metadata.clear()
            return
        assembly = self._clip_assemblies[row]
        metadata = assembly.metadata
        sync = self._sync_by_video.get(assembly.video.path)
        duration = self._clip_duration_seconds(metadata, sync)
        timecode = self._clip_timecode(metadata, sync)
        self._preview_timecode_value.setText(
            self._format_timecode_clock(timecode)
        )
        self._preview_duration_value.setText(
            self._format_duration_clock(duration)
        )
        file_size = (
            f"{assembly.video.size_bytes / (1024 * 1024):.2f} MiB"
            if assembly.video.size_bytes >= 1024 * 1024
            else f"{assembly.video.size_bytes / 1024:.1f} KiB"
        )
        frame_rate = (
            metadata.frame_rate if metadata is not None and metadata.frame_rate else "—"
        )
        video_properties = sync.video_properties if sync is not None else None
        audio_properties = (
            next(
                (
                    stream
                    for stream in sync.audio_properties
                    if stream is not None
                ),
                None,
            )
            if sync is not None
            else None
        )
        video_rows_list = [
            ("Codec", self._stream_codec(video_properties)),
            (
                "Frame size",
                (
                    f"{video_properties.width} × {video_properties.height}"
                    if video_properties is not None
                    and video_properties.width
                    and video_properties.height
                    else "—"
                ),
            ),
            (
                "Frame rate",
                (
                    f"{video_properties.frame_rate} fps"
                    if video_properties is not None
                    and video_properties.frame_rate
                    else f"{frame_rate} fps"
                    if frame_rate != "—"
                    else "—"
                ),
            ),
            (
                "Scan type",
                self._format_scan_type(
                    video_properties.field_order
                    if video_properties is not None
                    else None
                ),
            ),
            (
                "Color sampling",
                self._format_pixel_format(
                    video_properties.pixel_format
                    if video_properties is not None
                    else None
                ),
            ),
            (
                "Bit rate",
                self._format_stream_bitrate(
                    video_properties.bit_rate
                    if video_properties is not None
                    else None
                ),
            ),
            ("Container", assembly.video.path.suffix.lstrip(".").upper() or "—"),
            ("File size", file_size),
            ("Initial timecode", timecode or "—"),
            ("Duration", self._format_duration_clock(duration)),
        ]
        video_rows = self._property_rows(tuple(video_rows_list))
        sync_status = (
            "Pendiente de medición"
            if sync is None
            else "Verificada"
            if sync.eligible
            else "No verificada"
        )
        audio_rows_list = [
            ("Format", self._stream_codec(audio_properties)),
            (
                "Sample rate",
                (
                    f"{audio_properties.sample_rate} Hz"
                    if audio_properties is not None and audio_properties.sample_rate
                    else "—"
                ),
            ),
            (
                "Channels",
                (
                    audio_properties.channel_layout or audio_properties.channels
                    if audio_properties is not None
                    else "—"
                ),
            ),
            (
                "Bit rate",
                self._format_stream_bitrate(
                    audio_properties.bit_rate
                    if audio_properties is not None
                    else None
                ),
            ),
            (
                "P2 tracks",
                str(metadata.audio_track_count) if metadata is not None else "—",
            ),
            ("Associated files", str(len(assembly.audio_files))),
            ("Synchronization", sync_status),
        ]
        audio_rows = self._property_rows(tuple(audio_rows_list))
        description = (
            metadata.clip_name or "Sin descripción"
            if metadata is not None
            else "Sin metadata P2"
        )
        properties_html = (
            f'<h2 style="font-size:15px;color:#e5ebf5">'
            f"{html.escape(assembly.video.path.name)}</h2>"
            f'<p style="color:#9aa5b7">{html.escape(description)}</p>'
            '<h3 style="font-size:12px;color:#cbd5e1">Video</h3>'
            f'<table width="100%">{video_rows}</table>'
            '<h3 style="font-size:12px;color:#cbd5e1">Audio</h3>'
            f'<table width="100%">{audio_rows}</table>'
        )
        self._properties.setHtml(properties_html)

        metadata_sections = [
            f'<h2 style="font-size:15px;color:#e5ebf5">'
            f"{html.escape(assembly.video.path.name)}</h2>",
            f'<p style="color:#9aa5b7">{html.escape(str(assembly.video.path))}</p>',
        ]
        if metadata is None:
            metadata_sections.append("<p>No se encontró metadata P2 para este archivo.</p>")
        else:
            p2_rows = self._property_rows(
                (
                    ("Nombre", metadata.clip_name or "—"),
                    ("Global Clip ID", metadata.global_clip_id or "—"),
                    ("Timecode declarado", metadata.start_timecode or "—"),
                    ("Edit unit", metadata.edit_unit or "—"),
                    (
                        "Duración en cuadros",
                        (
                            str(metadata.duration_frames)
                            if metadata.duration_frames is not None
                            else "—"
                        ),
                    ),
                    ("Pistas de audio", str(metadata.audio_track_count)),
                    ("Archivo XML", str(metadata.clip_xml)),
                )
            )
            metadata_sections.extend(
                (
                    '<h3 style="font-size:12px;color:#cbd5e1">Metadata P2</h3>',
                    f'<table width="100%">{p2_rows}</table>',
                    '<h3 style="font-size:12px;color:#cbd5e1">Esencias de audio</h3>',
                )
            )
            if assembly.audio_files:
                metadata_sections.extend(
                    f"<p>{html.escape(path.name)}</p>"
                    for path in assembly.audio_files
                )
            else:
                metadata_sections.append("<p>Sin pistas asociadas</p>")

        if sync is not None:
            metadata_sections.extend(
                (
                    '<h3 style="font-size:12px;color:#cbd5e1">Sincronización</h3>',
                    "<p>Medición con ffprobe</p>",
                )
            )
            for track in sync.audio:
                if track.offset_seconds is None:
                    offset_text = "no disponible"
                else:
                    offset_text = f"{float(track.offset_seconds):+.6f} s"
                metadata_sections.append(
                    f"<p><b>{html.escape(track.path.name)}</b><br>"
                    f"Desfase {html.escape(offset_text)} · "
                    f"TC {html.escape(track.timecode or '—')}</p>"
                )
            metadata_sections.append(
                "<p><b>Sincronía verificada</b></p>"
                if sync.eligible
                else "<p><b>Sincronía no verificada</b></p>"
            )
            if sync.max_abs_offset_seconds is not None:
                metadata_sections.append(
                    '<p><span style="color:#9aa5b7">DESFASE MÁXIMO</span><br>'
                    f"{float(sync.max_abs_offset_seconds):.6f} s</p>"
                )
            metadata_sections.extend(
                f"<p>{html.escape(issue)}</p>" for issue in sync.issues
            )
        else:
            metadata_sections.extend(
                (
                    '<h3 style="font-size:12px;color:#cbd5e1">Sincronización</h3>',
                    "<p>Pendiente de medición.</p>",
                )
            )

        if self._p2_warnings:
            metadata_sections.append(
                '<h3 style="font-size:12px;color:#cbd5e1">Avisos</h3>'
            )
            metadata_sections.extend(
                f"<p>{html.escape(warning)}</p>"
                for warning in self._p2_warnings[:10]
            )
        self._metadata.setHtml("".join(metadata_sections))

    @staticmethod
    def _stream_codec(properties: MediaStreamProperties | None) -> str:
        if properties is None:
            return "—"
        return properties.codec_long_name or properties.codec_name or "—"

    @staticmethod
    def _format_clip_table_duration(metadata: P2ClipMetadata) -> str:
        if metadata.duration_seconds is not None:
            return f"{float(metadata.duration_seconds):.3f} s"
        if metadata.duration_frames is not None:
            return f"{metadata.duration_frames} cuadros"
        return "—"

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
        self._exit_preview_fullscreen()
        if (
            self._clip_thumbnail_thread is not None
            and self._clip_thumbnail_thread.isRunning()
        ):
            self._clip_thumbnail_thread.cancel()
            self._clip_thumbnail_thread.wait()

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
        self._audio_player.stop()
        self._audio_player.setSource(QUrl())
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
    app.setStyleSheet(qdarktheme.load_stylesheet("dark"))
    app.setPalette(qdarktheme.load_palette("dark"))
    window = MainWindow()
    window.show()
    return app.exec()
