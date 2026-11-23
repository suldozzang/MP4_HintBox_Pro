# -*- coding: utf-8 -*-
"""
==========================================================================
 MP4 HintBox Pro Tiny v2.0.0
--------------------------------------------------------------------------
  Author : EV7lab
  Site   : https://www.mp4hintbox.co.kr
  Engine : NativeFastStart 단독 (FFmpeg / MP4Box / psutil 모두 불필요)
==========================================================================
  • 의존성 : PyQt5 만
  • 기능   : MP4 faststart 처리 (qtfaststart 알고리즘 - 순수 Python)
  • 특징   : 단일 파일, 외부 실행파일 동봉 불필요, 가벼움
==========================================================================
"""
from __future__ import annotations

import sys
import os
import re
import json
import time
import uuid
import shutil
import struct
import logging
import threading
import mmap
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple, Dict, Any, Callable

from PyQt5.QtCore import (
    Qt, pyqtSignal, QThreadPool, QRunnable, QObject, QMutex,
    QMutexLocker, QSettings, QSize, QRect, pyqtSlot, QTimer, QPropertyAnimation
)
from PyQt5.QtGui import (
    QFont, QPainter, QPen, QColor, QPalette, QIcon, QBrush,
    QKeySequence, QLinearGradient
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel, QProgressBar,
    QFileDialog, QVBoxLayout, QHBoxLayout, QWidget, QComboBox, QMessageBox,
    QTextEdit, QSplitter, QCheckBox, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QLineEdit, QDialog, QDialogButtonBox, QFormLayout, QSpinBox, QGroupBox,
    QStyledItemDelegate, QStyle, QMenu, QAction, QStatusBar,
    QGridLayout, QRadioButton, QButtonGroup, QAbstractItemView, QShortcut,
    QDoubleSpinBox, QFrame, QScrollArea
)


# =====================================================================
APP_NAME       = "MP4 HintBox Pro Tiny"
APP_VERSION    = "2.1.0"
APP_ORG        = "EV7lab"
APP_WEBSITE    = "https://www.mp4hintbox.co.kr"
APP_AUTHOR     = "EV7lab"
APP_BUILD_YEAR = "2026"
APP_USER_MODEL_ID = "EV7lab.MP4HintBoxProTiny"

(COL_NAME, COL_RES, COL_CODEC, COL_BITRATE,
 COL_DURATION, COL_SIZE, COL_STATUS, COL_PROGRESS, COL_PATH) = range(9)


# =====================================================================
def app_base_dir() -> str:
    if getattr(sys, "frozen", False):
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def user_data_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.path.expanduser("~/.local/share")
    p = os.path.join(base, "MP4HintBoxProTiny")
    os.makedirs(p, exist_ok=True)
    return p


def setup_logging() -> logging.Logger:
    log_file = os.path.join(user_data_dir(), "mp4hintbox_tiny.log")
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return logging.getLogger("MP4HintBoxTiny")


logger = setup_logging()


# =====================================================================
class OutputMode(Enum):
    OVERWRITE    = "overwrite"
    SEPARATE_DIR = "separate_dir"
    SUFFIX       = "suffix"


class TaskState(Enum):
    PENDING    = "대기 중"
    CHECKING   = "확인 중..."
    QUEUED     = "큐 대기"
    PROCESSING = "처리 중"
    DONE       = "완료 ✓"
    SKIPPED    = "이미 faststart ⏭"
    FAILED     = "실패 ✗"
    CANCELLED  = "취소됨"


@dataclass
class MediaInfo:
    duration: float = 0.0
    size: int = 0
    has_faststart: bool = False


@dataclass
class AppConfig:
    process_mode: str = "serial"
    max_parallel: int = 4
    output_mode: str = OutputMode.OVERWRITE.value
    output_dir: str = ""
    output_suffix: str = "_fs"
    audio_sync_offset_sec: float = 0.00
    flatten_video_stts: bool = False    # 비디오 stts 평탄화 (VFR → 의사 CFR, 재인코딩 X)
    show_log: bool = False
    preserve_mtime: bool = True         # 출력 파일에 원본 수정 날짜 유지
    theme: str = "dark"
    recent_dirs: List[str] = field(default_factory=list)


# =====================================================================
class SettingsManager:
    def __init__(self):
        self.q = QSettings(APP_ORG, APP_NAME)
        self.cfg = AppConfig()
        self.load()

    def load(self):
        c = self.cfg
        c.process_mode  = self.q.value("process_mode", c.process_mode, str)
        c.max_parallel  = int(self.q.value("max_parallel", c.max_parallel))
        c.output_mode   = self.q.value("output_mode", c.output_mode, str)
        c.output_dir    = self.q.value("output_dir", c.output_dir, str)
        c.output_suffix = self.q.value("output_suffix", c.output_suffix, str)
        try:
            c.audio_sync_offset_sec = float(self.q.value("audio_sync_offset_sec",
                                                          c.audio_sync_offset_sec))
        except (TypeError, ValueError):
            pass
        c.flatten_video_stts = self.q.value("flatten_video_stts", c.flatten_video_stts, bool)
        c.show_log = self.q.value("show_log", c.show_log, bool)
        c.preserve_mtime = self.q.value("preserve_mtime", c.preserve_mtime, bool)
        c.theme    = self.q.value("theme", c.theme, str)
        recent     = self.q.value("recent_dirs", "", str)
        c.recent_dirs = [p for p in (recent or "").split("|") if p]

        # ── 버전 업그레이드 마이그레이션 ──
        # v2.0.3 ~ v2.0.5 는 audio_sync_offset_sec 의 기본값이 0.15 였음 (정상 파일 싱크 어긋남 원인).
        # v2.0.6 부터 기본 0.0. 옛 기본값(0.15) 으로 저장된 경우 자동 리셋.
        seen_version = self.q.value("seen_version", "", str)
        if seen_version != APP_VERSION:
            if seen_version in ("", "2.0.3", "2.0.4", "2.0.5"):
                if abs(c.audio_sync_offset_sec - 0.15) < 0.001:
                    c.audio_sync_offset_sec = 0.0
                    self.q.setValue("audio_sync_offset_sec", 0.0)
            self.q.setValue("seen_version", APP_VERSION)

    def save(self):
        c = self.cfg
        self.q.setValue("process_mode", c.process_mode)
        self.q.setValue("max_parallel", c.max_parallel)
        self.q.setValue("output_mode", c.output_mode)
        self.q.setValue("output_dir", c.output_dir)
        self.q.setValue("output_suffix", c.output_suffix)
        self.q.setValue("audio_sync_offset_sec", c.audio_sync_offset_sec)
        self.q.setValue("flatten_video_stts", c.flatten_video_stts)
        self.q.setValue("show_log", c.show_log)
        self.q.setValue("preserve_mtime", c.preserve_mtime)
        self.q.setValue("theme", c.theme)
        self.q.setValue("recent_dirs", "|".join(c.recent_dirs[:10]))
        self.q.sync()


# =====================================================================
class Theme:
    @staticmethod
    def apply_dark(app: QApplication):
        app.setStyle("Fusion")
        p = QPalette()
        p.setColor(QPalette.Window,           QColor(32, 33, 36))
        p.setColor(QPalette.WindowText,       QColor(232, 234, 237))
        p.setColor(QPalette.Base,             QColor(40, 42, 46))
        p.setColor(QPalette.AlternateBase,    QColor(48, 50, 54))
        p.setColor(QPalette.ToolTipBase,      QColor(45, 47, 51))
        p.setColor(QPalette.ToolTipText,      QColor(232, 234, 237))
        p.setColor(QPalette.Text,             QColor(232, 234, 237))
        p.setColor(QPalette.Button,           QColor(48, 50, 54))
        p.setColor(QPalette.ButtonText,       QColor(232, 234, 237))
        p.setColor(QPalette.BrightText,       QColor(255, 80, 80))
        p.setColor(QPalette.Link,             QColor(102, 178, 255))
        p.setColor(QPalette.Highlight,        QColor(0, 122, 204))
        p.setColor(QPalette.HighlightedText,  QColor(255, 255, 255))
        p.setColor(QPalette.Disabled, QPalette.Text,       QColor(120, 124, 130))
        p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 124, 130))
        app.setPalette(p)
        app.setStyleSheet("""
            QToolTip { color:#e8eaed; background:#2d2f33; border:1px solid #444; }
            QPushButton { padding:6px 14px; border-radius:4px;
                          background:#3a3d42; border:1px solid #4a4d52; }
            QPushButton:hover { background:#4a5057; }
            QPushButton:pressed { background:#2c2e32; }
            QPushButton:disabled { color:#777; background:#2c2e32; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                padding:5px; border:1px solid #4a4d52;
                border-radius:4px; background:#2a2c30; }
            QHeaderView::section { background:#2c2e32; color:#cfd2d6;
                                    padding:6px; border:0; border-right:1px solid #1a1b1d; }
            QTreeWidget { background:#1f2125; alternate-background-color:#26282c;
                          gridline-color:#2c2e32; border:1px solid #2c2e32; }
            QTreeWidget::item { padding:3px; }
            QTreeWidget::item:selected { background:#0a4d8c; }
            QProgressBar { background:#1f2125; border:1px solid #2c2e32;
                            border-radius:3px; text-align:center; color:#e8eaed; }
            QProgressBar::chunk { background:#FB8C00; border-radius:2px; }
            QStatusBar { background:#26282c; }
            QMenu { background:#2a2c30; border:1px solid #444; }
            QMenu::item:selected { background:#0a4d8c; }
            QPushButton#primary { background:#A0522D; border:1px solid #C05800;
                                  color:#ffffff; font-weight:600; }
            QPushButton#primary:hover { background:#B35A2F; }
            QPushButton#primary:pressed { background:#8A4626; }
            QPushButton#primary:disabled { background:#3a2e28; color:#9a8f88; border-color:#4a3a30; }
            QPushButton#danger:enabled { border:1px solid #9B1313; }
            QPushButton#danger:enabled:hover { background:#5a2a2a; }
        """)

    @staticmethod
    def apply_light(app: QApplication):
        app.setStyle("Fusion")
        app.setPalette(app.style().standardPalette())
        app.setStyleSheet("""
            QPushButton { padding:6px 14px; }
            QPushButton#primary { background:#A0522D; border:1px solid #8A4626; border-radius:4px;
                                  color:#ffffff; font-weight:600; }
            QPushButton#primary:hover { background:#B35A2F; }
            QPushButton#primary:disabled { background:#d9c9c0; color:#5c4f48; }
        """)


# =====================================================================
#  MP4 atom 파서 + 미디어 정보 (모두 순수 Python)
# =====================================================================
class MP4Reader:
    @staticmethod
    def iter_top_atoms(mm_or_data, end_pos: int):
        size_total = end_pos
        pos = 0
        while pos + 8 <= size_total:
            try:
                size = struct.unpack(">I", mm_or_data[pos:pos+4])[0]
                atype = mm_or_data[pos+4:pos+8]
                if isinstance(atype, (bytes, bytearray)):
                    atype = bytes(atype).decode("ascii", errors="ignore")
            except Exception:
                return
            header_size = 8
            if size == 1:
                if pos + 16 > size_total:
                    return
                size = struct.unpack(">Q", mm_or_data[pos+8:pos+16])[0]
                header_size = 16
            elif size == 0:
                size = size_total - pos
            if size < 8 or pos + size > size_total:
                return
            yield (pos, atype, size, header_size)
            pos += size

    @staticmethod
    def find_box(data, start: int, end: int, target: str,
                 recurse_into: tuple = ()) -> Optional[Tuple[int, int, int]]:
        pos = start
        while pos + 8 <= end:
            try:
                size = struct.unpack(">I", data[pos:pos+4])[0]
                atype = bytes(data[pos+4:pos+8]).decode("ascii", errors="ignore")
            except Exception:
                return None
            header_size = 8
            if size == 1:
                if pos + 16 > end:
                    return None
                size = struct.unpack(">Q", data[pos+8:pos+16])[0]
                header_size = 16
            if size < 8 or pos + size > end:
                return None
            if atype == target:
                return (pos, size, header_size)
            if atype in recurse_into:
                found = MP4Reader.find_box(data, pos + header_size,
                                            pos + size, target, recurse_into)
                if found:
                    return found
            pos += size
        return None

    @staticmethod
    def has_faststart(filepath: str) -> bool:
        try:
            sz = os.path.getsize(filepath)
            if sz < 16:
                return False
            with open(filepath, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    moov_pos = -1
                    mdat_pos = -1
                    for offset, atype, _size, _hs in MP4Reader.iter_top_atoms(mm, sz):
                        if atype == "moov" and moov_pos == -1:
                            moov_pos = offset
                        elif atype == "mdat" and mdat_pos == -1:
                            mdat_pos = offset
                        if moov_pos != -1 and mdat_pos != -1:
                            break
                    if moov_pos == -1:
                        return False
                    if mdat_pos == -1:
                        return True
                    return moov_pos < mdat_pos
        except Exception as e:
            logger.warning(f"faststart 검사 실패 ({filepath}): {e}")
            return False

    @staticmethod
    def get_quick_meta(filepath: str) -> Dict[str, Any]:
        """파일 목록 표시용 빠른 메타 추출 (size/duration/faststart/codec/resolution)"""
        meta = {
            'size': 0, 'duration': 0.0, 'has_faststart': False,
            'video_codec': '', 'audio_codec': '', 'width': 0, 'height': 0,
        }
        try:
            meta['size'] = os.path.getsize(filepath)
        except Exception:
            pass
        try:
            with open(filepath, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    sz = len(mm)
                    moov_pos = -1; mdat_pos = -1; moov_size = 0
                    for offset, atype, asize, hs in MP4Reader.iter_top_atoms(mm, sz):
                        if atype == "moov" and moov_pos == -1:
                            moov_pos = offset; moov_size = asize
                        elif atype == "mdat" and mdat_pos == -1:
                            mdat_pos = offset
                    if moov_pos != -1 and mdat_pos != -1:
                        meta['has_faststart'] = moov_pos < mdat_pos
                    elif moov_pos != -1 and mdat_pos == -1:
                        meta['has_faststart'] = True
                    if moov_pos == -1:
                        return meta
                    # mvhd duration
                    mvhd = MP4Reader.find_box(mm, moov_pos + 8, moov_pos + moov_size, "mvhd")
                    if mvhd:
                        v = mm[mvhd[0] + mvhd[2]]
                        if v == 1:
                            tp = mvhd[0] + mvhd[2] + 4 + 8 + 8
                            timescale = struct.unpack(">I", mm[tp:tp+4])[0]
                            duration  = struct.unpack(">Q", mm[tp+4:tp+12])[0]
                        else:
                            tp = mvhd[0] + mvhd[2] + 4 + 4 + 4
                            timescale = struct.unpack(">I", mm[tp:tp+4])[0]
                            duration  = struct.unpack(">I", mm[tp+4:tp+8])[0]
                        if timescale > 0:
                            meta['duration'] = duration / timescale
                    # 트랙 순회: 각 trak 의 hdlr/stsd/tkhd 탐색
                    pos = moov_pos + 8
                    end = moov_pos + moov_size
                    while pos + 8 <= end:
                        try:
                            size = struct.unpack(">I", mm[pos:pos+4])[0]
                            atype = mm[pos+4:pos+8].decode("ascii", errors="ignore")
                        except Exception:
                            break
                        hs = 8
                        if size == 1:
                            size = struct.unpack(">Q", mm[pos+8:pos+16])[0]
                            hs = 16
                        if size < 8 or pos + size > end: break
                        if atype == "trak":
                            mdia = MP4Reader.find_box(mm, pos + hs, pos + size, "mdia")
                            handler = ""
                            codec = ""
                            if mdia:
                                hdlr = MP4Reader.find_box(mm, mdia[0] + mdia[2],
                                                           mdia[0] + mdia[1], "hdlr")
                                if hdlr:
                                    ht_pos = hdlr[0] + hdlr[2] + 4 + 4
                                    if ht_pos + 4 <= hdlr[0] + hdlr[1]:
                                        handler = mm[ht_pos:ht_pos+4].decode("ascii", errors="ignore")
                                minf = MP4Reader.find_box(mm, mdia[0] + mdia[2],
                                                           mdia[0] + mdia[1], "minf")
                                if minf:
                                    stbl = MP4Reader.find_box(mm, minf[0] + minf[2],
                                                               minf[0] + minf[1], "stbl")
                                    if stbl:
                                        stsd = MP4Reader.find_box(mm, stbl[0] + stbl[2],
                                                                   stbl[0] + stbl[1], "stsd")
                                        if stsd:
                                            ep = stsd[0] + stsd[2] + 4 + 4
                                            if ep + 8 <= stsd[0] + stsd[1]:
                                                codec = mm[ep+4:ep+8].decode("ascii", errors="ignore").strip("\0")
                            if handler == "vide" and not meta['video_codec']:
                                meta['video_codec'] = codec
                                tkhd = MP4Reader.find_box(mm, pos + hs, pos + size, "tkhd")
                                if tkhd:
                                    try:
                                        wp = tkhd[0] + tkhd[1] - 8
                                        w = struct.unpack(">I", mm[wp:wp+4])[0] >> 16
                                        h = struct.unpack(">I", mm[wp+4:wp+8])[0] >> 16
                                        meta['width']  = w
                                        meta['height'] = h
                                    except Exception:
                                        pass
                            elif handler == "soun" and not meta['audio_codec']:
                                meta['audio_codec'] = codec
                        pos += size
        except Exception as e:
            logger.warning(f"get_quick_meta 실패 ({filepath}): {e}")
        return meta

    @staticmethod
    def get_info(filepath: str) -> MediaInfo:
        info = MediaInfo()
        try:
            info.size = os.path.getsize(filepath)
        except Exception:
            pass
        info.has_faststart = MP4Reader.has_faststart(filepath)
        # mvhd 에서 duration 추출
        try:
            with open(filepath, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    sz = len(mm)
                    moov = None
                    for offset, atype, asize, hs in MP4Reader.iter_top_atoms(mm, sz):
                        if atype == "moov":
                            moov = (offset, asize, hs)
                            break
                    if moov:
                        mvhd = MP4Reader.find_box(mm, moov[0] + moov[2],
                                                   moov[0] + moov[1], "mvhd")
                        if mvhd:
                            ver = mm[mvhd[0] + mvhd[2]]
                            if ver == 1:
                                ts_pos = mvhd[0] + mvhd[2] + 4 + 8 + 8
                                dur_pos = ts_pos + 4
                                if dur_pos + 8 <= mvhd[0] + mvhd[1]:
                                    timescale = struct.unpack(">I", mm[ts_pos:ts_pos+4])[0]
                                    duration  = struct.unpack(">Q", mm[dur_pos:dur_pos+8])[0]
                                    if timescale > 0:
                                        info.duration = duration / timescale
                            else:
                                ts_pos = mvhd[0] + mvhd[2] + 4 + 4 + 4
                                dur_pos = ts_pos + 4
                                if dur_pos + 4 <= mvhd[0] + mvhd[1]:
                                    timescale = struct.unpack(">I", mm[ts_pos:ts_pos+4])[0]
                                    duration  = struct.unpack(">I", mm[dur_pos:dur_pos+4])[0]
                                    if timescale > 0:
                                        info.duration = duration / timescale
        except Exception as e:
            logger.warning(f"mvhd 파싱 실패 ({filepath}): {e}")
        return info


# =====================================================================
#  처리 컨트롤러 (Python 레벨 pause/cancel)
# =====================================================================
class ProcessController(QObject):
    state_changed = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._paused = threading.Event()
        self._paused.set()
        self._cancelled = False

    def pause(self):
        with self._lock:
            self._paused.clear()
        self.state_changed.emit("paused")
        logger.info("일시정지")

    def resume(self):
        with self._lock:
            self._paused.set()
        self.state_changed.emit("running")
        logger.info("재개")

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._paused.set()
        self.state_changed.emit("cancelled")
        logger.info("취소")

    def reset(self):
        with self._lock:
            self._cancelled = False
            self._paused.set()
        self.state_changed.emit("idle")

    def is_cancelled(self) -> bool:
        return self._cancelled

    def wait_if_paused(self, timeout: float = 0.5) -> None:
        while not self._paused.wait(timeout):
            if self._cancelled:
                return


# =====================================================================
#  NativeFastStart  (qtfaststart + 오디오 싱크 보정)
# =====================================================================
class NativeFastStart:
    CHUNK_SIZE = 8 * 1024 * 1024

    # ── 안전성 검사 ──
    @staticmethod
    def _meta_has_iloc(filepath: str, atoms) -> bool:
        try:
            for offset, atype, size, hs in atoms:
                if atype != "meta":
                    continue
                with open(filepath, "rb") as f:
                    f.seek(offset + hs + 4)
                    end = offset + size
                    pos = offset + hs + 4
                    while pos + 8 <= end:
                        f.seek(pos)
                        h = f.read(8)
                        if len(h) < 8:
                            break
                        bsize = struct.unpack(">I", h[:4])[0]
                        btype = h[4:8].decode("ascii", errors="ignore")
                        if btype == "iloc":
                            return True
                        if bsize < 8:
                            break
                        pos += bsize
            return False
        except Exception:
            return False

    # ── stco/co64 보정 ──
    @staticmethod
    def _adjust_offsets(moov_bytes: bytes, delta: int) -> bytes:
        result = bytearray(moov_bytes)

        def recurse(pos: int, end: int):
            while pos + 8 <= end:
                size = struct.unpack(">I", result[pos:pos+4])[0]
                atype = bytes(result[pos+4:pos+8]).decode("ascii", errors="ignore")
                hs = 8
                if size == 1:
                    if pos + 16 > end:
                        return
                    size = struct.unpack(">Q", result[pos+8:pos+16])[0]
                    hs = 16
                if size < 8 or pos + size > end:
                    return
                if atype in ("stco", "co64"):
                    cpos = pos + hs + 4
                    epos = cpos + 4
                    cnt = struct.unpack(">I", result[cpos:cpos+4])[0]
                    if atype == "stco":
                        for i in range(cnt):
                            ep = epos + i * 4
                            old = struct.unpack(">I", result[ep:ep+4])[0]
                            new = old + delta
                            if new > 0xFFFFFFFF:
                                raise ValueError("stco 32bit 오버플로 (co64 필요)")
                            struct.pack_into(">I", result, ep, new)
                    else:
                        for i in range(cnt):
                            ep = epos + i * 8
                            old = struct.unpack(">Q", result[ep:ep+8])[0]
                            struct.pack_into(">Q", result, ep, old + delta)
                elif atype in ("trak", "mdia", "minf", "stbl", "edts", "udta"):
                    recurse(pos + hs, pos + size)
                pos += size

        recurse(8, len(result))
        return bytes(result)

    # ── 오디오 싱크 오프셋 (audio elst.media_time in-place) ──
    @staticmethod
    def _find_audio_trak(moov) -> Optional[Tuple[int, int, int]]:
        pos, end = 8, len(moov)
        while pos + 8 <= end:
            try:
                size = struct.unpack(">I", moov[pos:pos+4])[0]
                atype = bytes(moov[pos+4:pos+8]).decode("ascii", errors="ignore")
            except Exception:
                return None
            hs = 8
            if size == 1:
                if pos + 16 > end:
                    return None
                size = struct.unpack(">Q", moov[pos+8:pos+16])[0]
                hs = 16
            if size < 8 or pos + size > end:
                return None
            if atype == "trak":
                mdia = MP4Reader.find_box(moov, pos + hs, pos + size, "mdia")
                if mdia:
                    hdlr = MP4Reader.find_box(moov, mdia[0] + mdia[2],
                                               mdia[0] + mdia[1], "hdlr")
                    if hdlr:
                        ht_pos = hdlr[0] + hdlr[2] + 4 + 4
                        if ht_pos + 4 <= hdlr[0] + hdlr[1]:
                            ht = bytes(moov[ht_pos:ht_pos+4]).decode("ascii", errors="ignore")
                            if ht == "soun":
                                return (pos, size, hs)
            pos += size
        return None

    @staticmethod
    def _audio_timescale(moov, trak_start, trak_size, trak_hs) -> Optional[int]:
        mdia = MP4Reader.find_box(moov, trak_start + trak_hs,
                                   trak_start + trak_size, "mdia")
        if not mdia:
            return None
        mdhd = MP4Reader.find_box(moov, mdia[0] + mdia[2],
                                   mdia[0] + mdia[1], "mdhd")
        if not mdhd:
            return None
        version = moov[mdhd[0] + mdhd[2]]
        if version == 1:
            ts_pos = mdhd[0] + mdhd[2] + 4 + 8 + 8
        else:
            ts_pos = mdhd[0] + mdhd[2] + 4 + 4 + 4
        if ts_pos + 4 > mdhd[0] + mdhd[1]:
            return None
        return struct.unpack(">I", moov[ts_pos:ts_pos+4])[0]

    @staticmethod
    def _modify_audio_elst(moov, trak_start, trak_size, trak_hs, units: int) -> bool:
        edts = MP4Reader.find_box(moov, trak_start + trak_hs,
                                   trak_start + trak_size, "edts")
        if not edts:
            return False
        elst = MP4Reader.find_box(moov, edts[0] + edts[2],
                                   edts[0] + edts[1], "elst")
        if not elst:
            return False
        version = moov[elst[0] + elst[2]]
        cpos = elst[0] + elst[2] + 4
        if cpos + 4 > elst[0] + elst[1]:
            return False
        cnt = struct.unpack(">I", moov[cpos:cpos+4])[0]
        if cnt == 0:
            return False
        epos = cpos + 4
        if version == 0:
            mt_pos = epos + 4
            if mt_pos + 4 > elst[0] + elst[1]:
                return False
            old = struct.unpack(">i", moov[mt_pos:mt_pos+4])[0]
            if old == -1:
                return False
            struct.pack_into(">i", moov, mt_pos, max(0, old + units))
            return True
        elif version == 1:
            mt_pos = epos + 8
            if mt_pos + 8 > elst[0] + elst[1]:
                return False
            old = struct.unpack(">q", moov[mt_pos:mt_pos+8])[0]
            if old == -1:
                return False
            struct.pack_into(">q", moov, mt_pos, max(0, old + units))
            return True
        return False

    # ── 비디오 stts 평탄화 (VFR → 의사 CFR, 박스 크기 변경 없음) ──
    @staticmethod
    def _find_video_trak(moov) -> Optional[Tuple[int, int, int]]:
        pos, end = 8, len(moov)
        while pos + 8 <= end:
            try:
                size = struct.unpack(">I", moov[pos:pos+4])[0]
                atype = bytes(moov[pos+4:pos+8]).decode("ascii", errors="ignore")
            except Exception:
                return None
            hs = 8
            if size == 1:
                if pos + 16 > end: return None
                size = struct.unpack(">Q", moov[pos+8:pos+16])[0]
                hs = 16
            if size < 8 or pos + size > end:
                return None
            if atype == "trak":
                mdia = MP4Reader.find_box(moov, pos + hs, pos + size, "mdia")
                if mdia:
                    hdlr = MP4Reader.find_box(moov, mdia[0] + mdia[2],
                                               mdia[0] + mdia[1], "hdlr")
                    if hdlr:
                        ht_pos = hdlr[0] + hdlr[2] + 4 + 4
                        if ht_pos + 4 <= hdlr[0] + hdlr[1]:
                            ht = bytes(moov[ht_pos:ht_pos+4]).decode("ascii", errors="ignore")
                            if ht == "vide":
                                return (pos, size, hs)
            pos += size
        return None

    @staticmethod
    def _flatten_video_stts(moov_bytes: bytes,
                             on_log: Callable[[str], None]) -> bytes:
        """비디오 stts 의 모든 sample_delta 를 평균값으로 일괄 변경 (VFR → 의사 CFR).
           박스 크기 변동 없음. mdhd.duration 도 함께 보정."""
        result = bytearray(moov_bytes)
        video = NativeFastStart._find_video_trak(result)
        if not video:
            on_log("⚠ 비디오 trak 미발견 - stts 평탄화 스킵")
            return moov_bytes
        ts_start, ts_size, ts_hs = video

        mdia = MP4Reader.find_box(result, ts_start + ts_hs, ts_start + ts_size, "mdia")
        if not mdia: return moov_bytes
        minf = MP4Reader.find_box(result, mdia[0] + mdia[2], mdia[0] + mdia[1], "minf")
        if not minf: return moov_bytes
        stbl = MP4Reader.find_box(result, minf[0] + minf[2], minf[0] + minf[1], "stbl")
        if not stbl: return moov_bytes
        stts = MP4Reader.find_box(result, stbl[0] + stbl[2], stbl[0] + stbl[1], "stts")
        if not stts:
            on_log("⚠ stts 미발견 - 평탄화 스킵")
            return moov_bytes

        cpos = stts[0] + stts[2] + 4
        if cpos + 4 > stts[0] + stts[1]: return moov_bytes
        count = struct.unpack(">I", result[cpos:cpos+4])[0]
        if count == 0: return moov_bytes
        espos = cpos + 4

        # 모든 entry 합산
        total_samples = 0
        total_duration = 0
        deltas_seen = set()
        for i in range(count):
            ep = espos + i * 8
            if ep + 8 > stts[0] + stts[1]: break
            sc = struct.unpack(">I", result[ep:ep+4])[0]
            sd = struct.unpack(">I", result[ep+4:ep+8])[0]
            total_samples += sc
            total_duration += sc * sd
            deltas_seen.add(sd)
        if total_samples == 0: return moov_bytes

        # 이미 균일 (CFR) 이면 스킵
        if len(deltas_seen) == 1:
            on_log(f"  stts 이미 균일 ({list(deltas_seen)[0]} ticks) - 평탄화 불필요")
            return moov_bytes

        new_delta = max(1, round(total_duration / total_samples))

        # 모든 entry 의 sample_delta 를 new_delta 로 덮어쓰기 (sample_count 유지)
        for i in range(count):
            ep = espos + i * 8
            if ep + 8 > stts[0] + stts[1]: break
            struct.pack_into(">I", result, ep + 4, new_delta)

        new_total = total_samples * new_delta
        drift = new_total - total_duration

        # mdhd.duration 동기화
        mdhd = MP4Reader.find_box(result, mdia[0] + mdia[2], mdia[0] + mdia[1], "mdhd")
        if mdhd:
            v = result[mdhd[0] + mdhd[2]]
            if v == 1:
                dur_pos = mdhd[0] + mdhd[2] + 4 + 8 + 8 + 4
                if dur_pos + 8 <= mdhd[0] + mdhd[1]:
                    struct.pack_into(">Q", result, dur_pos, new_total)
            else:
                dur_pos = mdhd[0] + mdhd[2] + 4 + 4 + 4 + 4
                if dur_pos + 4 <= mdhd[0] + mdhd[1]:
                    struct.pack_into(">I", result, dur_pos, new_total)

        on_log(f"✓ 비디오 stts 평탄화: {len(deltas_seen)}종 → 단일 {new_delta} ticks "
               f"(samples={total_samples:,}, drift={drift:+d} ticks ≈ "
               f"{drift*1000/(new_delta*30 if new_delta else 1):+.1f}ms)")
        return bytes(result)

    @staticmethod
    def _apply_audio_offset(moov_bytes: bytes, offset_sec: float,
                             on_log: Callable[[str], None]) -> bytes:
        if abs(offset_sec) < 0.001:
            return moov_bytes
        result = bytearray(moov_bytes)
        audio = NativeFastStart._find_audio_trak(result)
        if not audio:
            on_log("⚠ audio trak 미발견 - 싱크 보정 스킵")
            return moov_bytes
        ts_start, ts_size, ts_hs = audio
        ts = NativeFastStart._audio_timescale(result, ts_start, ts_size, ts_hs)
        if not ts:
            on_log("⚠ mdhd timescale 획득 실패 - 싱크 보정 스킵")
            return moov_bytes
        units = int(round(offset_sec * ts))
        ok = NativeFastStart._modify_audio_elst(result, ts_start, ts_size, ts_hs, units)
        if not ok:
            on_log("⚠ audio elst 미발견 - 싱크 보정 스킵")
            return moov_bytes
        on_log(f"✓ 오디오 싱크 보정 +{offset_sec:.3f}s ({units} units @ {ts}Hz)")
        return bytes(result)

    # ── 메인 처리 ──
    @staticmethod
    def process(input_path: str, output_path: str, ctrl: ProcessController,
                on_progress: Callable[[float, float, float], None],
                on_log: Callable[[str], None],
                audio_sync_offset_sec: float = 0.0,
                flatten_video_stts: bool = False) -> Tuple[bool, str]:
        try:
            file_size = os.path.getsize(input_path)
            if file_size < 32:
                return False, "파일 너무 작음"

            # mmap 을 with 로 확실히 닫음 (열린 채 남으면 Windows 에서 원본 교체가 막힐 수 있음)
            with open(input_path, "rb") as fin:
                with mmap.mmap(fin.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    atoms = list(MP4Reader.iter_top_atoms(mm, file_size))

            ftyp = next((a for a in atoms if a[1] == "ftyp"), None)
            moov = next((a for a in atoms if a[1] == "moov"), None)
            mdat = next((a for a in atoms if a[1] == "mdat"), None)
            if not (ftyp and moov and mdat):
                return False, "ftyp/moov/mdat 누락 (유효한 MP4 가 아님)"

            # 안전성 검사
            mdat_count = sum(1 for a in atoms if a[1] == "mdat")
            moov_count = sum(1 for a in atoms if a[1] == "moov")
            moof_count = sum(1 for a in atoms if a[1] == "moof")
            if moof_count > 0:
                return False, f"fragmented MP4 (moof × {moof_count}) - 처리 불가"
            if mdat_count > 1:
                return False, f"다중 mdat ({mdat_count}) - 처리 불가"
            if moov_count > 1:
                return False, f"다중 moov ({moov_count}) - 처리 불가"
            if any(a[1] == "meta" for a in atoms):
                if NativeFastStart._meta_has_iloc(input_path, atoms):
                    return False, "최상위 meta/iloc 감지 - 처리 불가"

            # 이미 faststart
            if moov[0] < mdat[0]:
                on_log(f"  ⏭ 이미 faststart - 단순 복사")
                if not NativeFastStart._copy(input_path, output_path, file_size, ctrl, on_progress):
                    return False, "취소됨"
                return True, "이미 faststart (복사 완료)"

            on_log(f"  moov={moov[2]/1024:.1f}KB @ {moov[0]} → 선두로 이동")

            # moov 읽기
            with open(input_path, "rb") as fin:
                fin.seek(moov[0])
                moov_data = fin.read(moov[2])

            # 오디오 싱크 보정 (in-place, 박스 크기 동일)
            if abs(audio_sync_offset_sec) > 0.001:
                moov_data = NativeFastStart._apply_audio_offset(
                    moov_data, audio_sync_offset_sec, on_log)

            # 비디오 stts 평탄화 (VFR → 의사 CFR)
            if flatten_video_stts:
                moov_data = NativeFastStart._flatten_video_stts(moov_data, on_log)

            # stco/co64 보정
            try:
                moov_new = NativeFastStart._adjust_offsets(moov_data, moov[2])
            except ValueError as e:
                return False, f"native 처리 불가 ({e})"

            # 출력
            start = time.monotonic()
            written = 0

            with open(input_path, "rb") as fin, open(output_path, "wb") as fout:
                fin.seek(ftyp[0])
                fout.write(fin.read(ftyp[2]))
                written += ftyp[2]

                fout.write(moov_new)
                written += len(moov_new)

                for atom in atoms:
                    if atom == ftyp or atom == moov:
                        continue
                    offset, atype, size, _hs = atom
                    fin.seek(offset)
                    remaining = size
                    while remaining > 0:
                        if ctrl.is_cancelled():
                            return False, "취소됨"
                        ctrl.wait_if_paused(0.2)
                        n = min(NativeFastStart.CHUNK_SIZE, remaining)
                        buf = fin.read(n)
                        if not buf:
                            break
                        fout.write(buf)
                        written += len(buf)
                        remaining -= len(buf)
                        elapsed = max(0.001, time.monotonic() - start)
                        bps = written / elapsed
                        pct = max(0.0, min(99.5, written / file_size * 100.0))
                        eta = (file_size - written) / bps if bps > 0 else 0
                        on_progress(pct, eta, bps / (1024 * 1024))

                # 마지막 정상 atom 뒤에 붙은 비표준 꼬리 데이터 (예: 배포처가 덧붙인 식별 문자열)
                # 는 MP4 블록으로 해석되지 않아 위 루프에서 빠진다 → 원본 그대로 끝에 보존.
                # 파일 끝에 붙으므로 mdat 오프셋(stco/co64)에는 영향 없음.
                tail_start = max(a[0] + a[2] for a in atoms)
                if tail_start < file_size:
                    on_log(f"  파일 끝 비표준 데이터 {file_size - tail_start:,} bytes 그대로 보존")
                    fin.seek(tail_start)
                    while True:
                        if ctrl.is_cancelled():
                            return False, "취소됨"
                        buf = fin.read(NativeFastStart.CHUNK_SIZE)
                        if not buf:
                            break
                        fout.write(buf)
                        written += len(buf)

            # moov 위치만 옮기므로 출력 크기는 입력과 정확히 같아야 함 (잘린 파일 등 검출)
            out_size = os.path.getsize(output_path)
            if out_size != file_size:
                return False, f"출력 크기 불일치 (입력 {file_size:,} / 출력 {out_size:,} bytes)"
            on_progress(100.0, 0, 0)
            elapsed = max(0.001, time.monotonic() - start)
            on_log(f"  완료 ({elapsed:.2f}s, 평균 {written/elapsed/1024/1024:.1f} MB/s)")
            return True, "OK"
        except Exception as e:
            return False, f"처리 예외: {e}"

    @staticmethod
    def _copy(src: str, dst: str, size: int, ctrl: ProcessController,
              on_progress: Callable[[float, float, float], None]) -> bool:
        """단순 복사. 취소되면 False."""
        copied = 0
        start = time.monotonic()
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            while True:
                if ctrl.is_cancelled():
                    return False
                ctrl.wait_if_paused(0.2)
                buf = fin.read(NativeFastStart.CHUNK_SIZE)
                if not buf:
                    break
                fout.write(buf)
                copied += len(buf)
                elapsed = max(0.001, time.monotonic() - start)
                bps = copied / elapsed
                pct = max(0.0, min(99.5, copied / size * 100.0)) if size else 0
                eta = (size - copied) / bps if bps > 0 else 0
                on_progress(pct, eta, bps / (1024 * 1024))
        return True


# =====================================================================
#  진단 도구 (MP4 timing 메타 분석)
# =====================================================================
class MP4Diagnostics:
    """파일의 stts/ctts/mvhd/mdhd/elst 등을 파싱해 VFR / B-프레임 / 싱크 진단"""

    @staticmethod
    def analyze(filepath: str) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            'filepath': filepath, 'size': 0, 'errors': [], 'top_atoms': [],
            'is_faststart': False, 'is_fragmented': False,
            'multi_mdat': False, 'multi_moov': False,
            'mvhd': None, 'tracks': [],
        }
        try:
            result['size'] = os.path.getsize(filepath)
        except Exception as e:
            result['errors'].append(f"파일 크기 확인 실패: {e}")
            return result

        try:
            with open(filepath, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    MP4Diagnostics._analyze_mmap(mm, len(mm), result)
        except Exception as e:
            result['errors'].append(f"분석 실패: {e}")
        return result

    @staticmethod
    def _analyze_mmap(mm, size, out: Dict[str, Any]):
        moov_pos = -1
        mdat_pos = -1
        mdat_count = 0
        moov_count = 0

        for offset, atype, asize, hs in MP4Reader.iter_top_atoms(mm, size):
            out['top_atoms'].append({'type': atype, 'offset': offset, 'size': asize})
            if atype == 'moov':
                moov_count += 1
                if moov_pos == -1: moov_pos = offset
            elif atype == 'mdat':
                mdat_count += 1
                if mdat_pos == -1: mdat_pos = offset
            elif atype == 'moof':
                out['is_fragmented'] = True

        out['multi_mdat'] = mdat_count > 1
        out['multi_moov'] = moov_count > 1

        if moov_pos != -1 and mdat_pos != -1:
            out['is_faststart'] = moov_pos < mdat_pos
        elif moov_pos != -1 and mdat_pos == -1:
            out['is_faststart'] = True

        if moov_pos != -1:
            moov_size = next(a['size'] for a in out['top_atoms'] if a['type'] == 'moov')
            out['mvhd'] = MP4Diagnostics._parse_mvhd(mm, moov_pos, moov_size)
            out['tracks'] = MP4Diagnostics._parse_tracks(mm, moov_pos, moov_size)

    @staticmethod
    def _parse_mvhd(mm, moov_offset, moov_size):
        mvhd = MP4Reader.find_box(mm, moov_offset + 8, moov_offset + moov_size, "mvhd")
        if not mvhd:
            return None
        try:
            v = mm[mvhd[0] + mvhd[2]]
            if v == 1:
                ts_pos = mvhd[0] + mvhd[2] + 4 + 8 + 8
                timescale = struct.unpack(">I", mm[ts_pos:ts_pos+4])[0]
                duration  = struct.unpack(">Q", mm[ts_pos+4:ts_pos+12])[0]
            else:
                ts_pos = mvhd[0] + mvhd[2] + 4 + 4 + 4
                timescale = struct.unpack(">I", mm[ts_pos:ts_pos+4])[0]
                duration  = struct.unpack(">I", mm[ts_pos+4:ts_pos+8])[0]
            return {'timescale': timescale, 'duration': duration,
                    'duration_sec': duration / timescale if timescale > 0 else 0}
        except Exception:
            return None

    @staticmethod
    def _parse_tracks(mm, moov_offset, moov_size):
        tracks = []
        pos = moov_offset + 8
        end = moov_offset + moov_size
        while pos + 8 <= end:
            try:
                size = struct.unpack(">I", mm[pos:pos+4])[0]
                atype = mm[pos+4:pos+8].decode("ascii", errors="ignore")
            except Exception:
                break
            hs = 8
            if size == 1:
                size = struct.unpack(">Q", mm[pos+8:pos+16])[0]
                hs = 16
            if size < 8 or pos + size > end:
                break
            if atype == "trak":
                tracks.append(MP4Diagnostics._parse_trak(mm, pos, size, hs))
            pos += size
        return tracks

    @staticmethod
    def _parse_trak(mm, pos, size, hs):
        trak_end = pos + size
        info: Dict[str, Any] = {
            'type': 'unknown', 'codec': '', 'timescale': 0, 'duration': 0,
            'sample_count': 0, 'stts_entries': [], 'stts_total': 0,
            'ctts_total': 0, 'has_b_frames': False,
            'avg_fps': 0.0, 'avg_duration_ticks': 0.0,
            'is_cfr': True, 'unique_durations': [], 'duration_distrib': {},
            'elst_entries': [], 'width': 0, 'height': 0,
        }

        # tkhd (width/height for video)
        tkhd = MP4Reader.find_box(mm, pos + hs, trak_end, "tkhd")
        if tkhd:
            try:
                v = mm[tkhd[0] + tkhd[2]]
                # tkhd 끝쪽 8바이트 = width(4 fixed) + height(4 fixed)
                wh_pos = tkhd[0] + tkhd[1] - 8
                w_fixed = struct.unpack(">I", mm[wh_pos:wh_pos+4])[0]
                h_fixed = struct.unpack(">I", mm[wh_pos+4:wh_pos+8])[0]
                info['width']  = w_fixed >> 16  # 16.16 fixed → integer part
                info['height'] = h_fixed >> 16
            except Exception:
                pass

        mdia = MP4Reader.find_box(mm, pos + hs, trak_end, "mdia")
        if not mdia:
            return info

        # mdhd
        mdhd = MP4Reader.find_box(mm, mdia[0] + mdia[2], mdia[0] + mdia[1], "mdhd")
        if mdhd:
            try:
                v = mm[mdhd[0] + mdhd[2]]
                if v == 1:
                    ts_pos = mdhd[0] + mdhd[2] + 4 + 8 + 8
                    info['timescale'] = struct.unpack(">I", mm[ts_pos:ts_pos+4])[0]
                    info['duration']  = struct.unpack(">Q", mm[ts_pos+4:ts_pos+12])[0]
                else:
                    ts_pos = mdhd[0] + mdhd[2] + 4 + 4 + 4
                    info['timescale'] = struct.unpack(">I", mm[ts_pos:ts_pos+4])[0]
                    info['duration']  = struct.unpack(">I", mm[ts_pos+4:ts_pos+8])[0]
            except Exception:
                pass

        # hdlr handler_type
        hdlr = MP4Reader.find_box(mm, mdia[0] + mdia[2], mdia[0] + mdia[1], "hdlr")
        if hdlr:
            try:
                ht_pos = hdlr[0] + hdlr[2] + 4 + 4
                ht = mm[ht_pos:ht_pos+4].decode("ascii", errors="ignore")
                info['type'] = {'vide': 'video', 'soun': 'audio', 'subt': 'subtitle',
                                'text': 'text', 'hint': 'hint'}.get(ht, ht)
            except Exception:
                pass

        # minf > stbl
        minf = MP4Reader.find_box(mm, mdia[0] + mdia[2], mdia[0] + mdia[1], "minf")
        if not minf: return info
        stbl = MP4Reader.find_box(mm, minf[0] + minf[2], minf[0] + minf[1], "stbl")
        if not stbl: return info

        # stsd 코덱
        stsd = MP4Reader.find_box(mm, stbl[0] + stbl[2], stbl[0] + stbl[1], "stsd")
        if stsd:
            try:
                ep = stsd[0] + stsd[2] + 4 + 4
                if ep + 8 <= stsd[0] + stsd[1]:
                    info['codec'] = mm[ep+4:ep+8].decode("ascii", errors="ignore").strip("\0")
            except Exception:
                pass

        # stts
        stts = MP4Reader.find_box(mm, stbl[0] + stbl[2], stbl[0] + stbl[1], "stts")
        if stts:
            try:
                cpos = stts[0] + stts[2] + 4
                count = struct.unpack(">I", mm[cpos:cpos+4])[0]
                info['stts_total'] = count
                espos = cpos + 4
                # 모든 entry 를 분석 (분포 계산용)
                distrib = {}
                total_samples = 0
                total_duration = 0
                for i in range(count):
                    ep = espos + i * 8
                    if ep + 8 > stts[0] + stts[1]:
                        break
                    sc = struct.unpack(">I", mm[ep:ep+4])[0]
                    sd = struct.unpack(">I", mm[ep+4:ep+8])[0]
                    if i < 50:  # 최대 50개만 entry 리스트로 보관
                        info['stts_entries'].append((sc, sd))
                    distrib[sd] = distrib.get(sd, 0) + sc
                    total_samples += sc
                    total_duration += sc * sd
                info['sample_count'] = total_samples
                info['unique_durations'] = sorted(distrib.keys())
                info['is_cfr'] = len(distrib) == 1
                info['duration_distrib'] = distrib
                if total_samples > 0 and info['timescale'] > 0:
                    info['avg_duration_ticks'] = total_duration / total_samples
                    info['avg_fps'] = info['timescale'] / info['avg_duration_ticks']
            except Exception:
                pass

        # ctts (B-프레임 존재 여부)
        ctts = MP4Reader.find_box(mm, stbl[0] + stbl[2], stbl[0] + stbl[1], "ctts")
        if ctts:
            try:
                cpos = ctts[0] + ctts[2] + 4
                count = struct.unpack(">I", mm[cpos:cpos+4])[0]
                info['ctts_total'] = count
                info['has_b_frames'] = count > 0
            except Exception:
                pass

        # elst
        edts = MP4Reader.find_box(mm, pos + hs, trak_end, "edts")
        if edts:
            elst = MP4Reader.find_box(mm, edts[0] + edts[2], edts[0] + edts[1], "elst")
            if elst:
                try:
                    v = mm[elst[0] + elst[2]]
                    cpos = elst[0] + elst[2] + 4
                    cnt = struct.unpack(">I", mm[cpos:cpos+4])[0]
                    ep = cpos + 4
                    for i in range(min(cnt, 10)):
                        if v == 1:
                            if ep + 20 > elst[0] + elst[1]: break
                            sd = struct.unpack(">Q", mm[ep:ep+8])[0]
                            mt = struct.unpack(">q", mm[ep+8:ep+16])[0]
                            ep += 20
                        else:
                            if ep + 12 > elst[0] + elst[1]: break
                            sd = struct.unpack(">I", mm[ep:ep+4])[0]
                            mt = struct.unpack(">i", mm[ep+4:ep+8])[0]
                            ep += 12
                        info['elst_entries'].append((sd, mt))
                except Exception:
                    pass

        return info

    @staticmethod
    def format_report(r: Dict[str, Any]) -> str:
        L = []
        L.append("=" * 76)
        L.append(f"  진단: {os.path.basename(r['filepath'])}")
        L.append("=" * 76)

        if r['errors']:
            L.append("\n[오류]")
            for e in r['errors']:
                L.append(f"  ✗ {e}")
            return "\n".join(L)

        # 기본
        L.append("")
        L.append("[기본 정보]")
        L.append(f"  • 파일 크기   : {r['size']:,} bytes ({r['size']/1024/1024:.2f} MB)")
        L.append(f"  • Top atoms   : {len(r['top_atoms'])}개 ({', '.join(a['type'] for a in r['top_atoms'][:8])}{'...' if len(r['top_atoms']) > 8 else ''})")
        L.append(f"  • FastStart   : {'✓ 적용됨' if r['is_faststart'] else '✗ 미적용'}")
        if r['is_fragmented']:   L.append(f"  • Fragmented  : ⚠ moof 감지 (NativeFastStart 거부 대상)")
        if r['multi_mdat']:      L.append(f"  • multi-mdat  : ⚠ 다중 mdat (NativeFastStart 거부 대상)")
        if r['multi_moov']:      L.append(f"  • multi-moov  : ⚠ 다중 moov (NativeFastStart 거부 대상)")

        # mvhd
        if r['mvhd']:
            m = r['mvhd']
            L.append("")
            L.append("[Movie Header (mvhd)]")
            L.append(f"  • 영화 timescale : {m['timescale']} Hz")
            L.append(f"  • 영화 duration  : {m['duration']} ticks ({m['duration_sec']:.3f} 초)")

        # 트랙
        for i, t in enumerate(r['tracks']):
            L.append("")
            head = f"[Track #{i+1}: {t['type']}"
            if t['codec']: head += f" / {t['codec']}"
            if t['type'] == 'video' and t['width'] and t['height']:
                head += f" / {t['width']}x{t['height']}"
            L.append(head + "]")

            if t['timescale']:
                dur_s = t['duration'] / t['timescale'] if t['timescale'] > 0 else 0
                L.append(f"  • 미디어 timescale : {t['timescale']} Hz")
                L.append(f"  • 미디어 duration  : {t['duration']} ticks ({dur_s:.3f} 초)")
            L.append(f"  • 샘플 수          : {t['sample_count']:,}")
            L.append(f"  • stts 엔트리 수   : {t['stts_total']}")

            if t['type'] == 'video':
                if t['avg_fps']:
                    L.append(f"  • 평균 FPS         : {t['avg_fps']:.4f} (avg duration {t['avg_duration_ticks']:.1f} ticks)")

                if t['is_cfr']:
                    dur = t['unique_durations'][0] if t['unique_durations'] else 0
                    fps = t['timescale'] / dur if dur > 0 and t['timescale'] else 0
                    L.append(f"  • 프레임 일관성    : ✓ CFR (모든 프레임 {dur} ticks / {fps:.4f} fps)")
                else:
                    L.append(f"  • 프레임 일관성    : ⚠ VFR 감지 (고유 duration {len(t['unique_durations'])}종)")
                    # 상위 5종 분포
                    items = sorted(t['duration_distrib'].items(), key=lambda x: -x[1])
                    total = sum(t['duration_distrib'].values())
                    L.append(f"    └ 상위 분포 (max 5):")
                    for dur, cnt in items[:5]:
                        pct = cnt / total * 100 if total else 0
                        ms  = dur / t['timescale'] * 1000 if t['timescale'] else 0
                        fps = t['timescale'] / dur if dur > 0 and t['timescale'] else 0
                        L.append(f"       {dur:>8} ticks  ={ms:>8.3f} ms / {fps:>8.3f} fps   {cnt:>8,}개 ({pct:5.2f}%)")

                if t['has_b_frames']:
                    L.append(f"  • B-프레임         : ✓ 존재 (ctts entries: {t['ctts_total']:,})")
                else:
                    L.append(f"  • B-프레임         : ✗ 없음")

            elif t['type'] == 'audio':
                if t['avg_duration_ticks']:
                    sps = t['timescale'] / t['avg_duration_ticks'] if t['avg_duration_ticks'] else 0
                    L.append(f"  • 평균 sample/sec  : {sps:.2f}")
                if len(t['unique_durations']) == 1:
                    L.append(f"  • 샘플 일관성      : ✓ 균일 ({t['unique_durations'][0]} ticks)")
                else:
                    L.append(f"  • 샘플 일관성      : ⚠ duration 종류 {len(t['unique_durations'])}종")

            if t['elst_entries']:
                L.append(f"  • Edit List (elst) : {len(t['elst_entries'])}개 entry")
                for j, (sd, mt) in enumerate(t['elst_entries']):
                    if mt == -1:
                        L.append(f"    [{j}] segment_dur={sd}, media_time=-1 (empty edit / 빈 구간)")
                    else:
                        sec = mt / t['timescale'] if t['timescale'] else 0
                        L.append(f"    [{j}] segment_dur={sd}, media_time={mt} ({sec:+.4f}초)")

        # 종합
        L.append("")
        L.append("[종합 진단]")
        warnings = []
        for t in r['tracks']:
            if t['type'] == 'video' and not t['is_cfr'] and t['stts_total'] > 0:
                warnings.append(
                    "⚠ 비디오 트랙이 VFR (가변 프레임 레이트) 입니다.\n"
                    "    → frame duration 이 일정하지 않아 미세한 jitter / 떨림으로 인지될 수 있습니다.\n"
                    "    → NativeFastStart 는 원본의 stts 를 그대로 보존하므로 이 특성이 출력에도 유지됩니다.\n"
                    "    → 매끄럽게 만들려면 비디오 재인코딩 (PotEncoder / FFmpeg trans-code) 이 필요합니다."
                )
            if t['type'] == 'audio' and len(t['unique_durations']) > 1:
                warnings.append(
                    "ℹ 오디오 sample duration 이 변동합니다 (드물게 보임). 일반적으로 재생에 영향 없음."
                )
            if t['elst_entries']:
                # media_time != 0 인 entry 가 있으면 시작 offset 안내
                first = t['elst_entries'][0]
                if first[1] not in (0, -1):
                    sec = first[1] / t['timescale'] if t['timescale'] else 0
                    warnings.append(
                        f"ℹ {t['type']} 트랙에 media_time={first[1]} ({sec:+.3f}초) 의 elst 가 존재.\n"
                        f"    → 이 트랙은 시작 시 {sec:+.3f}초 offset 으로 재생됨 (의도된 싱크 보정일 수 있음)."
                    )

        if r['is_fragmented']:
            warnings.append("⚠ Fragmented MP4 - NativeFastStart 가 처리를 거부합니다.")
        if r['multi_mdat']:
            warnings.append("⚠ multi-mdat - NativeFastStart 가 처리를 거부합니다.")
        if r['multi_moov']:
            warnings.append("⚠ multi-moov - NativeFastStart 가 처리를 거부합니다.")

        if not warnings:
            L.append("  ✓ 특이사항 없음. NativeFastStart 결과는 원본과 byte-perfect 한 timing 을 유지합니다.")
        else:
            for w in warnings:
                L.append(f"  {w}")
        L.append("")
        L.append("=" * 76)
        return "\n".join(L)


class DiagnosticDialog(QDialog):
    def __init__(self, filepath: str, parent=None):
        super().__init__(parent)
        self.filepath = filepath
        self.setWindowTitle(f"파일 진단 - {os.path.basename(filepath)}")
        self.resize(900, 650)

        layout = QVBoxLayout(self)
        result = MP4Diagnostics.analyze(filepath)
        report = MP4Diagnostics.format_report(result)

        self.text = QTextEdit()
        self.text.setReadOnly(True)
        f = QFont("Consolas", 9)
        f.setStyleHint(QFont.TypeWriter)
        self.text.setFont(f)
        self.text.setPlainText(report)
        self.text.setLineWrapMode(QTextEdit.NoWrap)
        layout.addWidget(self.text)

        btns = QHBoxLayout()
        btn_copy = QPushButton("📋 클립보드 복사")
        btn_copy.clicked.connect(self._copy)
        btn_save = QPushButton("💾 텍스트로 저장")
        btn_save.clicked.connect(self._save)
        btn_close = QPushButton("닫기")
        btn_close.clicked.connect(self.reject)
        btns.addWidget(btn_copy); btns.addWidget(btn_save); btns.addStretch(); btns.addWidget(btn_close)
        layout.addLayout(btns)

    def _copy(self):
        QApplication.clipboard().setText(self.text.toPlainText())

    def _save(self):
        default = os.path.splitext(self.filepath)[0] + "_diagnostic.txt"
        path, _ = QFileDialog.getSaveFileName(self, "진단 결과 저장", default,
                                                "Text Files (*.txt);;All Files (*)")
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.text.toPlainText())
            except Exception as e:
                QMessageBox.warning(self, "저장 실패", str(e))


# =====================================================================
# 작업(QRunnable)이 끝나며 신호 객체가 먼저 지워지면 아직 전달되지 않은 신호가
# 사라질 수 있음 (PySide6 에서 실제로 발생). 마지막 신호 뒤 2초간 참조를 붙잡아 둔다.
_ALIVE_SIGNALS: set = set()


class WorkerSignals(QObject):
    state    = pyqtSignal(str, str)
    progress = pyqtSignal(str, float, float, float)
    log      = pyqtSignal(str)
    finished = pyqtSignal(str, bool, str)

    def __init__(self, last: str = "finished"):
        super().__init__()
        _ALIVE_SIGNALS.add(self)
        getattr(self, last).connect(self._release)

    def _release(self, *_args):
        QTimer.singleShot(2000, lambda: _ALIVE_SIGNALS.discard(self))


TEMP_PREFIX = ".mp4hb_"
TEMP_SUFFIX = ".tmp.mp4"


def is_temp_artifact(name: str) -> bool:
    """처리 중 생성되는 임시 파일인지 (폴더 스캔 시 목록에서 제외)"""
    base = os.path.basename(name)
    return base.startswith(".") and base.lower().endswith(TEMP_SUFFIX)


def make_temp_path(final_out: str, fallback_dir: str) -> str:
    """최종 출력과 같은 폴더에 임시 파일 생성 → os.replace 가 같은 볼륨에서 원자적으로 동작.
    폴더에 쓸 수 없으면 앱 임시 폴더로 대체."""
    name = f"{TEMP_PREFIX}{uuid.uuid4().hex}{TEMP_SUFFIX}"
    target_dir = os.path.dirname(os.path.abspath(final_out))
    try:
        os.makedirs(target_dir, exist_ok=True)
        if os.access(target_dir, os.W_OK):
            return os.path.join(target_dir, name)
    except Exception:
        pass
    os.makedirs(fallback_dir, exist_ok=True)
    return os.path.join(fallback_dir, name)


def move_into_place(src: str, dst: str) -> None:
    """임시 파일을 최종 위치로 교체 (같은 볼륨이면 원자적, 아니면 복사 후 삭제)"""
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(src, dst)


def restore_times(path: str, st: Optional[os.stat_result]) -> None:
    if st is None:
        return
    try:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
    except Exception:
        pass


class FileProcessor(QRunnable):
    def __init__(self, filepath: str, cfg: AppConfig, ctrl: ProcessController,
                 temp_dir: str, output_path: Optional[str] = None):
        super().__init__()
        self.setAutoDelete(True)
        self.filepath = filepath
        self.cfg = cfg
        self.ctrl = ctrl
        self.temp_dir = temp_dir
        self.output_path = output_path   # 메인창에서 미리 계산한 출력 경로 (이름 충돌 해소 포함)
        self.signals = WorkerSignals()

    @staticmethod
    def resolve_output_path(filepath: str, cfg: AppConfig) -> str:
        src = Path(filepath)
        if cfg.output_mode == OutputMode.SEPARATE_DIR.value and cfg.output_dir:
            return str(Path(cfg.output_dir) / src.name)
        if cfg.output_mode == OutputMode.SUFFIX.value:
            suf = cfg.output_suffix or "_fs"
            return str(src.with_name(src.stem + suf + src.suffix))
        return filepath

    def _fail(self, path: str, msg: str):
        self.signals.state.emit(path, TaskState.FAILED.value)
        self.signals.finished.emit(path, False, msg)

    def run(self):
        path = self.filepath
        temp_out = ""
        try:
            if self.ctrl.is_cancelled():
                self.signals.state.emit(path, TaskState.CANCELLED.value)
                self.signals.finished.emit(path, False, "취소됨")
                return
            if not os.path.isfile(path):
                self._fail(path, "원본 파일을 찾을 수 없음 (이동/삭제됨)")
                return

            self.signals.state.emit(path, TaskState.CHECKING.value)
            info = MP4Reader.get_info(path)
            try:
                src_stat = os.stat(path)
            except OSError:
                src_stat = None
            keep_times = src_stat if self.cfg.preserve_mtime else None

            final_out = self.output_path or self.resolve_output_path(path, self.cfg)
            in_place = os.path.normcase(os.path.abspath(final_out)) == \
                       os.path.normcase(os.path.abspath(path))

            if info.has_faststart and in_place:
                self.signals.state.emit(path, TaskState.SKIPPED.value)
                self.signals.progress.emit(path, 100.0, 0, 0)
                self.signals.finished.emit(path, True, "이미 faststart")
                return
            # (출력이 별도 위치면 이미 faststart 여도 아래에서 복사 진행)

            temp_out = make_temp_path(final_out, self.temp_dir)

            # 디스크 여유 - 실제로 임시 파일이 생기는 드라이브 기준
            need = max(int(info.size * 1.2), 64 * 1024 * 1024)
            try:
                free = shutil.disk_usage(os.path.dirname(temp_out)).free
                if free < need:
                    self._fail(path, f"디스크 여유 부족 (필요 {need//1024//1024}MB / "
                                     f"가용 {free//1024//1024}MB)")
                    return
            except Exception:
                pass

            self.signals.state.emit(path, TaskState.PROCESSING.value)

            def on_progress(pct, eta, speed):
                self.signals.progress.emit(path, pct, eta, speed)

            def on_log(msg):
                self.signals.log.emit(msg)

            ok, msg = NativeFastStart.process(
                path, temp_out, self.ctrl, on_progress, on_log,
                audio_sync_offset_sec=self.cfg.audio_sync_offset_sec,
                flatten_video_stts=self.cfg.flatten_video_stts)

            if self.ctrl.is_cancelled():
                self._cleanup(temp_out)
                self.signals.state.emit(path, TaskState.CANCELLED.value)
                self.signals.finished.emit(path, False, "취소됨")
                return

            if not ok or not os.path.isfile(temp_out):
                self._cleanup(temp_out)
                self._fail(path, msg or "처리 실패")
                return

            # 출력 검증: moov 가 mdat 보다 앞에 있고, 메타(재생 길이)가 정상적으로 읽혀야 함
            out_info = MP4Reader.get_info(temp_out)
            if not out_info.has_faststart or \
               (info.duration > 0 and abs(out_info.duration - info.duration) > max(1.5, info.duration * 0.05)):
                self._cleanup(temp_out)
                self._fail(path, "출력 무결성 검증 실패")
                return

            try:
                move_into_place(temp_out, final_out)
            except Exception as e:
                self._cleanup(temp_out)
                hint = " (다른 프로그램이 파일을 사용 중인지 확인하세요)" \
                    if isinstance(e, PermissionError) else ""
                self._fail(path, f"파일 교체 실패: {e}{hint}")
                return

            restore_times(final_out, keep_times)
            self.signals.progress.emit(path, 100.0, 0, 0)
            self.signals.state.emit(path, TaskState.DONE.value)
            self.signals.finished.emit(path, True, "OK")
        except Exception as e:
            logger.exception(f"FileProcessor 예외: {path}")
            self._cleanup(temp_out)
            self._fail(path, f"예외: {e}")

    @staticmethod
    def _cleanup(path):
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


class CheckWorker(QRunnable):
    def __init__(self, filepath: str):
        super().__init__()
        self.setAutoDelete(True)
        self.filepath = filepath
        self.signals = WorkerSignals(last="state")

    def run(self):
        try:
            m = MP4Reader.get_quick_meta(self.filepath)
            state = TaskState.SKIPPED.value if m['has_faststart'] else TaskState.PENDING.value
            br = (m['size'] * 8 / m['duration']) if m['duration'] > 0 else 0
            payload = json.dumps({
                "path": self.filepath,
                "meta": {"size": m['size'], "duration": m['duration'],
                         "fs": m['has_faststart'], "vc": m['video_codec'],
                         "ac": m['audio_codec'], "w": m['width'], "h": m['height'],
                         "br": br}
            }, ensure_ascii=False)
            self.signals.log.emit(f"__META__{payload}")
            self.signals.state.emit(self.filepath, state)
        except Exception as e:
            self.signals.state.emit(self.filepath, TaskState.FAILED.value)


# =====================================================================
class ProgressDelegate(QStyledItemDelegate):
    PROG_ROLE = Qt.UserRole + 1

    def paint(self, painter, option, index):
        progress = index.data(self.PROG_ROLE)
        if progress is None:
            super().paint(painter, option, index)
            return
        rect = option.rect.adjusted(4, 3, -4, -3)
        if rect.height() < 4 or rect.width() < 8:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(60, 62, 66), 1))
        painter.setBrush(QBrush(QColor(31, 33, 37)))
        painter.drawRoundedRect(rect, 3, 3)
        pct = max(0, min(100, int(progress)))
        if pct > 0:
            fill_w = max(0, int(rect.width() * pct / 100.0))
            fill_rect = QRect(rect.x(), rect.y(), fill_w, rect.height())
            grad = QLinearGradient(fill_rect.topLeft(), fill_rect.bottomLeft())
            if pct < 100:
                grad.setColorAt(0.0, QColor(255, 167, 38))
                grad.setColorAt(1.0, QColor(251, 140, 0))
            else:
                grad.setColorAt(0.0, QColor(251, 140, 0))
                grad.setColorAt(1.0, QColor(230, 81, 0))
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(fill_rect, 3, 3)
        target_px = max(8, min(13, int(rect.height() * 0.72)))
        f = painter.font()
        f.setPixelSize(target_px)
        painter.setFont(f)
        painter.setPen(QColor(232, 234, 237))
        painter.drawText(rect, Qt.AlignCenter, f"{pct}%")
        painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        if s.height() < 22:
            s.setHeight(22)
        return s


class DnDTreeWidget(QTreeWidget):
    files_dropped = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self._drag = False

    def dragEnterEvent(self, e):
        # 로컬 파일/폴더가 하나라도 있으면 받는다. MP4 인지는 놓은 뒤 판단해서 안내
        # (예전에는 MP4 가 없으면 조용히 거부해 "끌어 놓기가 고장났다"고 느껴졌음)
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()
            self._drag_active = True
            self.viewport().update()
            return
        e.ignore()

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        self._drag = False
        self.viewport().update()
        e.accept()

    def dropEvent(self, e):
        self._drag = False
        self.viewport().update()
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            if paths:
                self.files_dropped.emit(paths)
                e.acceptProposedAction()
                return
        e.ignore()

    def paintEvent(self, e):
        super().paintEvent(e)
        if self._drag:
            p = QPainter(self.viewport())
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(251, 140, 0, 200), 3, Qt.DashLine))
            r = self.viewport().rect().adjusted(4, 4, -4, -4)
            p.drawRect(r)
            p.end()


# =====================================================================
#  토스트 알림 (작업 완료 시 화면 우하단, 3초 후 자동으로 사라짐)
# =====================================================================
class ToastNotification(QWidget):
    """Windows 토스트 스타일 알림.
    - 작업표시줄 위 화면 우하단에 표시, 포커스를 빼앗지 않음
    - duration_ms 후 페이드아웃되며 사라짐 (클릭하면 즉시 닫힘)
    - 여러 개가 겹치면 위로 쌓임
    """
    _active: List["ToastNotification"] = []

    ACCENT = {"success": "#2E7D32", "warning": "#C05800", "error": "#CD1C18"}
    ICON   = {"success": "✓", "warning": "!", "error": "✕"}

    def __init__(self, title: str, message: str, level: str = "success",
                 duration_ms: int = 3000, on_click: Optional[Callable[[], None]] = None):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._on_click = on_click
        accent = self.ACCENT.get(level, self.ACCENT["success"])

        frame = QFrame(self)
        frame.setObjectName("toastFrame")
        frame.setStyleSheet(f"""
            QFrame#toastFrame {{ background:#2b2d31; border:1px solid #45484e;
                                 border-left:4px solid {accent}; border-radius:8px; }}
            QLabel {{ background:transparent; border:none; }}
            QLabel#toastIcon {{ color:#ffffff; background:{accent}; border-radius:12px;
                                font-weight:700; font-size:11pt; }}
            QLabel#toastTitle {{ color:#ffffff; font-weight:700; font-size:10.5pt; }}
            QLabel#toastBody  {{ color:#d5d8dc; font-size:9.5pt; }}
            QLabel#toastApp   {{ color:#b4b8be; font-size:8.5pt; }}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)

        row = QHBoxLayout(frame)
        row.setContentsMargins(14, 12, 18, 12)
        row.setSpacing(12)
        icon = QLabel(self.ICON.get(level, "✓"))
        icon.setObjectName("toastIcon")
        icon.setFixedSize(24, 24)
        icon.setAlignment(Qt.AlignCenter)
        row.addWidget(icon, 0, Qt.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(3)
        app_lbl = QLabel(APP_NAME)
        app_lbl.setObjectName("toastApp")
        t = QLabel(title)
        t.setObjectName("toastTitle")
        b = QLabel(message)
        b.setObjectName("toastBody")
        b.setWordWrap(True)
        col.addWidget(app_lbl)
        col.addWidget(t)
        col.addWidget(b)
        row.addLayout(col, 1)

        self.setFixedWidth(360)
        self.adjustSize()

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(250)
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.finished.connect(self.close)
        # duration_ms 시점에 완전히 사라지도록 페이드 시작 시점을 앞당김
        QTimer.singleShot(max(0, duration_ms - self._fade.duration()), self._fade.start)

    @classmethod
    def show_toast(cls, title: str, message: str, level: str = "success",
                   duration_ms: int = 3000, on_click: Optional[Callable[[], None]] = None):
        toast = cls(title, message, level, duration_ms, on_click)
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        margin = 16
        y = area.bottom() - margin - toast.height()
        for other in cls._active:
            y = min(y, other.y() - 8 - toast.height())
        toast.move(area.right() - margin - toast.width(), y)
        cls._active.append(toast)
        toast.show()
        return toast

    def mousePressEvent(self, e):
        if self._on_click:
            try:
                self._on_click()
            except Exception:
                pass
        self.close()

    def closeEvent(self, e):
        if self in ToastNotification._active:
            ToastNotification._active.remove(self)
        super().closeEvent(e)


def vline() -> QFrame:
    """버튼 그룹 구분선"""
    f = QFrame()
    f.setFrameShape(QFrame.VLine)
    f.setFrameShadow(QFrame.Plain)
    f.setStyleSheet("color:#4a4d52;")
    f.setFixedHeight(24)
    return f


# =====================================================================
class SettingsDialog(QDialog):
    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("설정")
        self.setMinimumWidth(500)
        layout = QVBoxLayout(self)

        gb_out = QGroupBox("출력 동작")
        ol = QGridLayout(gb_out)
        self.rb_overwrite = QRadioButton("원본 덮어쓰기 (atomic replace)")
        self.rb_separate  = QRadioButton("별도 폴더 출력")
        self.rb_suffix    = QRadioButton("같은 폴더 + 접미사")
        bg = QButtonGroup(self)
        bg.addButton(self.rb_overwrite); bg.addButton(self.rb_separate); bg.addButton(self.rb_suffix)
        ol.addWidget(self.rb_overwrite, 0, 0, 1, 3)
        ol.addWidget(self.rb_separate,  1, 0, 1, 3)
        self.le_outdir = QLineEdit(cfg.output_dir)
        self.btn_browse = QPushButton("폴더 선택...")
        self.btn_browse.clicked.connect(self._browse)
        ol.addWidget(QLabel("    출력 폴더:"), 2, 0)
        ol.addWidget(self.le_outdir, 2, 1)
        ol.addWidget(self.btn_browse, 2, 2)
        ol.addWidget(self.rb_suffix, 3, 0, 1, 3)
        self.le_suffix = QLineEdit(cfg.output_suffix)
        ol.addWidget(QLabel("    접미사:"), 4, 0)
        ol.addWidget(self.le_suffix, 4, 1, 1, 2)
        layout.addWidget(gb_out)

        gb_proc = QGroupBox("처리 옵션")
        pl = QFormLayout(gb_proc)
        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["직렬 (Serial)", "병렬 (Parallel)"])
        self.cb_mode.setCurrentIndex(0 if cfg.process_mode == "serial" else 1)
        pl.addRow("처리 방식:", self.cb_mode)
        self.sp_parallel = QSpinBox()
        self.sp_parallel.setRange(1, 32)
        self.sp_parallel.setValue(cfg.max_parallel)
        pl.addRow("최대 동시 작업:", self.sp_parallel)

        self.sp_audio = QDoubleSpinBox()
        self.sp_audio.setRange(0.000, 5.000)
        self.sp_audio.setSingleStep(0.05)
        self.sp_audio.setDecimals(3)
        self.sp_audio.setSuffix("  초")
        self.sp_audio.setValue(float(cfg.audio_sync_offset_sec))
        self.sp_audio.setToolTip(
            "【권장: 0.000】\n\n"
            "NativeFastStart 는 stts/ctts/elst/mvhd 등 모든 타이밍 메타를\n"
            "그대로 보존하므로 일반적으로 보정이 필요 없습니다.\n\n"
            "특정 파일에서만 일관된 싱크 드리프트가 보일 때\n"
            "양수 값을 입력하면 audio elst.media_time 을\n"
            "지정 초만큼 앞당겨 보정합니다.\n\n"
            "값을 잘못 설정하면 오히려 정상 파일의 싱크를 깨뜨릴 수 있으니\n"
            "특별한 이유가 없다면 0 으로 두세요.")
        pl.addRow("오디오 싱크 보정:", self.sp_audio)

        self.chk_flatten = QCheckBox("비디오 stts 평탄화 (VFR → CFR, 떨림 제거)")
        self.chk_flatten.setChecked(cfg.flatten_video_stts)
        self.chk_flatten.setToolTip(
            "비디오 stts 의 모든 sample_delta 를 평균값으로 일괄 변경하여\n"
            "VFR (가변 프레임 듀레이션) 을 의사 CFR 로 만듭니다.\n\n"
            "효과: 990↔1020 ticks 식으로 교대하는 미세 jitter 제거\n"
            "방식: 박스 크기 변경 없이 in-place 수정 (재인코딩 X, 매우 빠름)\n"
            "부작용: 총 duration 미세 변경 (~10ms 내외)\n\n"
            "원본이 이미 CFR 이면 자동 스킵됩니다.\n"
            "WEB-DL / 방송 녹화 등 NTSC 29.97 fps VFR 파일에 권장.")
        pl.addRow(self.chk_flatten)

        self.chk_mtime = QCheckBox("원본 파일의 수정 날짜 유지")
        self.chk_mtime.setChecked(cfg.preserve_mtime)
        self.chk_mtime.setToolTip("처리 후에도 탐색기의 '수정한 날짜'가 원본과 같게 유지됩니다.")
        pl.addRow(self.chk_mtime)

        layout.addWidget(gb_proc)

        gb_ui = QGroupBox("외관")
        ul = QFormLayout(gb_ui)
        self.cb_theme = QComboBox()
        self.cb_theme.addItems(["다크", "라이트"])
        self.cb_theme.setCurrentIndex(0 if cfg.theme == "dark" else 1)
        ul.addRow("테마:", self.cb_theme)
        layout.addWidget(gb_ui)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        if cfg.output_mode == OutputMode.SEPARATE_DIR.value:
            self.rb_separate.setChecked(True)
        elif cfg.output_mode == OutputMode.SUFFIX.value:
            self.rb_suffix.setChecked(True)
        else:
            self.rb_overwrite.setChecked(True)

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "출력 폴더 선택", self.le_outdir.text() or "")
        if d:
            self.le_outdir.setText(d)
            self.rb_separate.setChecked(True)

    def apply_to(self, cfg: AppConfig):
        if self.rb_separate.isChecked():
            cfg.output_mode = OutputMode.SEPARATE_DIR.value
        elif self.rb_suffix.isChecked():
            cfg.output_mode = OutputMode.SUFFIX.value
        else:
            cfg.output_mode = OutputMode.OVERWRITE.value
        cfg.output_dir            = self.le_outdir.text().strip()
        cfg.output_suffix         = self.le_suffix.text().strip() or "_fs"
        cfg.process_mode          = "serial" if self.cb_mode.currentIndex() == 0 else "parallel"
        cfg.max_parallel          = self.sp_parallel.value()
        cfg.audio_sync_offset_sec = float(self.sp_audio.value())
        cfg.flatten_video_stts    = self.chk_flatten.isChecked()
        cfg.preserve_mtime        = self.chk_mtime.isChecked()
        cfg.theme                 = "dark" if self.cb_theme.currentIndex() == 0 else "light"


# =====================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = SettingsManager()
        self.cfg = self.settings.cfg
        self.temp_dir = os.path.join(user_data_dir(), "temp")
        os.makedirs(self.temp_dir, exist_ok=True)
        self._cleanup_orphan()

        self.controller = ProcessController()
        self.pool = QThreadPool()
        self._update_pool()
        # 메타 정보 검사는 별도 풀 → 직렬 처리 중에도 목록 정보가 바로 채워짐
        self.check_pool = QThreadPool()
        self.check_pool.setMaxThreadCount(max(2, min(4, (os.cpu_count() or 2))))
        # 경로 → 트리 항목 (O(1) 조회)
        self._items: Dict[str, QTreeWidgetItem] = {}

        self._mutex = QMutex()
        self._total = 0
        self._done = 0
        self._success = 0
        self._fail = 0
        self._processing_active = False
        self._meta: Dict[str, Dict[str, Any]] = {}

        self._build_ui()
        self._build_menu()
        self._build_shortcuts()

    def _build_ui(self):
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1000, 620)
        central = QWidget()
        self.setCentralWidget(central)
        v = QVBoxLayout(central)
        v.setContentsMargins(8, 8, 8, 4)

        # ---- 상단 툴바: [목록 관리] ......... [처리 제어] | [실행] | [설정] ----
        top = QHBoxLayout()
        top.setSpacing(6)
        self.btn_add      = QPushButton("📁 파일 추가")
        self.btn_add_dir  = QPushButton("📂 폴더 추가")
        self.btn_remove   = QPushButton("선택 제거")
        self.btn_clear    = QPushButton("목록 비우기")
        self.btn_pause    = QPushButton("⏸ 일시정지")
        self.btn_cancel   = QPushButton("⏹ 취소")
        self.btn_proc_sel = QPushButton("▶ 선택 처리")
        self.btn_proc_all = QPushButton("▶▶ 전체 처리")
        self.btn_settings = QPushButton("⚙")

        self.btn_proc_all.setObjectName("primary")
        self.btn_cancel.setObjectName("danger")
        self.btn_settings.setFixedWidth(40)

        tips = {
            self.btn_add:      "MP4 파일 추가 (Ctrl+O)",
            self.btn_add_dir:  "폴더의 하위 MP4 모두 추가 (Ctrl+Shift+O)",
            self.btn_remove:   "선택한 항목을 목록에서 제거 (Delete)",
            self.btn_clear:    "목록 전체 비우기",
            self.btn_pause:    "일시정지 / 재개 (Space)",
            self.btn_cancel:   "진행 중인 작업 취소 (Esc)",
            self.btn_proc_sel: "선택한 파일만 처리 (Ctrl+R)",
            self.btn_proc_all: "목록의 모든 파일 처리 (F5)",
            self.btn_settings: "환경설정 (Ctrl+,)",
        }
        for btn, tip in tips.items():
            btn.setMinimumHeight(34)
            btn.setToolTip(tip)
            btn.setCursor(Qt.PointingHandCursor)

        for w in (self.btn_add, self.btn_add_dir, vline(), self.btn_remove, self.btn_clear):
            top.addWidget(w)
        top.addStretch(1)
        for w in (self.btn_pause, self.btn_cancel, vline(),
                  self.btn_proc_sel, self.btn_proc_all, vline(), self.btn_settings):
            top.addWidget(w)

        self.btn_pause.setEnabled(False)
        self.btn_cancel.setEnabled(False)
        self.btn_add.clicked.connect(self._on_add_files)
        self.btn_add_dir.clicked.connect(self._on_add_dir)
        self.btn_remove.clicked.connect(self._remove_selected)
        self.btn_clear.clicked.connect(self._clear_all)
        self.btn_proc_sel.clicked.connect(lambda: self._start(True))
        self.btn_proc_all.clicked.connect(lambda: self._start(False))
        self.btn_pause.clicked.connect(self._on_pause)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_settings.clicked.connect(self._on_settings)
        v.addLayout(top)

        # ---- 옵션 행: 방식 ......... 검색 ----
        opt = QHBoxLayout()
        opt.setSpacing(6)
        opt.addWidget(QLabel("방식"))
        self.cb_mode = QComboBox()
        self.cb_mode.addItems(["직렬 처리", "병렬 처리"])
        self.cb_mode.setCurrentIndex(0 if self.cfg.process_mode == "serial" else 1)
        self.cb_mode.currentIndexChanged.connect(self._on_mode_changed)
        opt.addWidget(self.cb_mode)
        opt.addStretch(1)
        self.le_filter = QLineEdit()
        self.le_filter.setPlaceholderText("🔍 파일명 또는 상태로 검색...")
        self.le_filter.setClearButtonEnabled(True)
        self.le_filter.setMinimumWidth(260)
        self.le_filter.textChanged.connect(self._apply_filter)
        opt.addWidget(self.le_filter)
        v.addLayout(opt)

        self.splitter = QSplitter(Qt.Vertical)
        v.addWidget(self.splitter, 1)

        self.tree = DnDTreeWidget()
        self.tree.setColumnCount(9)
        self.tree.setHeaderLabels(
            ["파일명", "해상도", "코덱", "비트레이트", "길이", "크기", "상태", "진행률", "경로"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setSortingEnabled(True)
        self.tree.setItemDelegateForColumn(COL_PROGRESS, ProgressDelegate(self))
        h = self.tree.header()
        h.setSectionResizeMode(COL_NAME,     QHeaderView.Stretch)
        h.setSectionResizeMode(COL_RES,      QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_CODEC,    QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_BITRATE,  QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_DURATION, QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_SIZE,     QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_STATUS,   QHeaderView.ResizeToContents)
        h.setSectionResizeMode(COL_PROGRESS, QHeaderView.Fixed)
        self.tree.setColumnWidth(COL_PROGRESS, 80)   # 130 → 80
        self.tree.setColumnHidden(COL_PATH, True)
        self.tree.files_dropped.connect(self._on_paths_dropped)
        self.tree.itemDoubleClicked.connect(self._on_dbl)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        self.splitter.addWidget(self.tree)

        self.log_box = QWidget()
        lv = QVBoxLayout(self.log_box)
        lv.setContentsMargins(0, 4, 0, 0)
        lv.addWidget(QLabel("처리 로그:"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.document().setMaximumBlockCount(5000)
        lv.addWidget(self.log)
        self.splitter.addWidget(self.log_box)
        if self.cfg.show_log:
            self.splitter.setSizes([460, 200])
        else:
            self.log_box.setVisible(False)
            self.splitter.setSizes([1, 0])

        bot = QHBoxLayout()
        self.lbl_progress = QLabel("준비됨")
        self.pbar = QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setMaximumHeight(18)
        bot.addWidget(self.lbl_progress, 1)
        bot.addWidget(self.pbar, 2)
        v.addLayout(bot)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self._site = QLabel(f'<a href="{APP_WEBSITE}" style="color:#FFA726;">Official Website</a>')
        self._site.setOpenExternalLinks(True)
        sb.addPermanentWidget(self._site)

    def _build_menu(self):
        mb = self.menuBar()
        m_file = mb.addMenu("&파일")
        a_add = QAction("파일 추가...", self); a_add.setShortcut("Ctrl+O"); a_add.triggered.connect(self._on_add_files)
        a_dir = QAction("폴더 추가 (재귀)...", self); a_dir.setShortcut("Ctrl+Shift+O"); a_dir.triggered.connect(self._on_add_dir)
        a_quit = QAction("종료", self); a_quit.setShortcut("Ctrl+Q"); a_quit.triggered.connect(self.close)
        m_file.addAction(a_add); m_file.addAction(a_dir); m_file.addSeparator(); m_file.addAction(a_quit)

        m_run = mb.addMenu("&처리")
        a_run = QAction("전체 처리", self); a_run.setShortcut("F5"); a_run.triggered.connect(lambda: self._start(False))
        a_sel = QAction("선택 처리", self); a_sel.setShortcut("Ctrl+R"); a_sel.triggered.connect(lambda: self._start(True))
        a_pau = QAction("일시정지/재개", self); a_pau.setShortcut("Space"); a_pau.triggered.connect(self._on_pause)
        a_cc  = QAction("취소", self); a_cc.setShortcut("Esc"); a_cc.triggered.connect(self._on_cancel)
        m_run.addAction(a_run); m_run.addAction(a_sel); m_run.addSeparator(); m_run.addAction(a_pau); m_run.addAction(a_cc)

        m_view = mb.addMenu("&보기")
        self.act_show_log = QAction("처리 로그", self)
        self.act_show_log.setCheckable(True)
        self.act_show_log.setChecked(self.cfg.show_log)
        self.act_show_log.setShortcut("Ctrl+L")
        self.act_show_log.toggled.connect(self._on_toggle_log)
        m_view.addAction(self.act_show_log)

        m_tool = mb.addMenu("&도구")
        a_diag = QAction("📊 파일 진단...", self); a_diag.setShortcut("Ctrl+D")
        a_diag.triggered.connect(self._on_diagnose)
        m_tool.addAction(a_diag)

        m_set = mb.addMenu("&설정")
        a_set = QAction("환경설정...", self); a_set.setShortcut("Ctrl+,"); a_set.triggered.connect(self._on_settings)
        m_set.addAction(a_set)

        m_help = mb.addMenu("&도움말")
        a_about = QAction("정보", self); a_about.triggered.connect(self._on_about)
        m_help.addAction(a_about)

    def _build_shortcuts(self):
        self._sc_del = QShortcut(QKeySequence("Delete"), self)
        self._sc_del.activated.connect(self._remove_selected)
        self._sc_all = QShortcut(QKeySequence("Ctrl+A"), self.tree)
        self._sc_all.activated.connect(self.tree.selectAll)

    def _on_toggle_log(self, checked: bool):
        self.cfg.show_log = checked
        self.settings.save()
        self.log_box.setVisible(checked)
        if checked:
            h = max(400, self.splitter.height())
            self.splitter.setSizes([int(h * 0.7), int(h * 0.3)])
        else:
            self.splitter.setSizes([1, 0])

    def _update_pool(self):
        if self.cfg.process_mode == "parallel":
            self.pool.setMaxThreadCount(max(1, min(self.cfg.max_parallel, 32)))
        else:
            self.pool.setMaxThreadCount(1)

    def _on_add_files(self):
        start = self.cfg.recent_dirs[0] if self.cfg.recent_dirs else ""
        files, _ = QFileDialog.getOpenFileNames(self, "MP4 파일 선택", start,
                                                 "MP4 Files (*.mp4);;All Files (*)")
        if files:
            self._on_paths_dropped(files)

    def _on_add_dir(self):
        start = self.cfg.recent_dirs[0] if self.cfg.recent_dirs else ""
        d = QFileDialog.getExistingDirectory(self, "폴더 선택", start)
        if d:
            self._on_paths_dropped([d])

    @staticmethod
    def _skipped_text(skipped: Dict[str, int], prefix: str = "") -> str:
        """{"MKV": 2, "SRT": 1} → "MKV 2개, SRT 1개" (없으면 빈 문자열)"""
        if not skipped:
            return ""
        return prefix + ", ".join(f"{ext} {n}개" for ext, n in
                                  sorted(skipped.items(), key=lambda kv: (-kv[1], kv[0])))

    def _notify_no_mp4(self, skipped: Dict[str, int], had_dirs: bool):
        """추가된 MP4 가 하나도 없을 때 이유를 알려 준다 (아무 반응이 없으면 고장으로 오해함)"""
        if skipped:
            QMessageBox.information(
                self, "MP4 파일만 처리합니다",
                "MP4 파일만 추가할 수 있습니다.\n\n"
                f"제외한 파일: {self._skipped_text(skipped)}\n\n"
                "MKV 등은 MP4 로 바꾼 뒤 추가해 주세요. "
                "(faststart 는 MP4 형식에만 있는 처리입니다)")
        elif had_dirs:
            QMessageBox.information(self, "MP4 파일 없음", "고른 폴더 안에 MP4 파일이 없습니다.")

    def _on_paths_dropped(self, paths: List[str]):
        files: List[str] = []
        dirs: List[str] = []
        skipped: Dict[str, int] = {}       # MP4 가 아니라 제외한 파일 (확장자별 개수)
        for p in paths:
            try:
                if os.path.isdir(p):
                    dirs.append(p)
                    for root, _, fnames in os.walk(p):
                        for fn in fnames:
                            if fn.lower().endswith(".mp4") and not is_temp_artifact(fn):
                                files.append(os.path.join(root, fn))
                elif os.path.isfile(p) and p.lower().endswith(".mp4") and not is_temp_artifact(p):
                    files.append(p)
                elif os.path.isfile(p) and not is_temp_artifact(p):
                    ext = os.path.splitext(p)[1].lstrip(".").upper() or "확장자 없음"
                    skipped[ext] = skipped.get(ext, 0) + 1
            except Exception as e:
                self._log(f"스캔 오류 {p}: {e}")

        if dirs:
            for d in dirs:
                if d in self.cfg.recent_dirs:
                    self.cfg.recent_dirs.remove(d)
                self.cfg.recent_dirs.insert(0, d)
            self.cfg.recent_dirs = self.cfg.recent_dirs[:10]
            self.settings.save()

        if not files:
            self._log("추가할 MP4 없음" + self._skipped_text(skipped, " / 제외(MP4 아님): "))
            self._notify_no_mp4(skipped, bool(dirs))
            return

        def norm(p):
            try:
                return os.path.normcase(os.path.normpath(os.path.abspath(p)))
            except Exception:
                return p

        existing = set(norm(p) for p in self._existing_paths())
        added = dup = 0
        for f in files:
            f = os.path.normpath(os.path.abspath(f))
            if norm(f) in existing:
                dup += 1
                continue
            self._add_item(f)
            existing.add(norm(f))
            added += 1
            cw = CheckWorker(f)
            cw.signals.state.connect(self._on_check_state)
            cw.signals.log.connect(self._on_check_log)
            self.check_pool.start(cw)
        self._log(f"추가: {added}개 / 중복: {dup}개"
                  + self._skipped_text(skipped, " / 제외(MP4 아님): "))
        if skipped and not self._processing_active:
            self.lbl_progress.setText(f"MP4 {added}개 추가 · MP4 가 아니라 제외: "
                                      + self._skipped_text(skipped))

    def _existing_paths(self):
        return [self.tree.topLevelItem(i).text(COL_PATH)
                for i in range(self.tree.topLevelItemCount())]

    def _add_item(self, filepath: str):
        it = QTreeWidgetItem()
        it.setText(COL_NAME,     os.path.basename(filepath))
        it.setToolTip(COL_NAME,  filepath)
        it.setText(COL_RES,      "...")
        it.setText(COL_CODEC,    "...")
        it.setText(COL_BITRATE,  "...")
        it.setText(COL_DURATION, "...")
        it.setText(COL_SIZE,     "...")
        it.setText(COL_STATUS,   TaskState.CHECKING.value)
        it.setText(COL_PATH,     filepath)
        it.setData(COL_PROGRESS, ProgressDelegate.PROG_ROLE, 0)
        self.tree.addTopLevelItem(it)
        self._items[filepath] = it

    def _find_item(self, filepath: str) -> Optional[QTreeWidgetItem]:
        return self._items.get(filepath)

    def _take_item(self, it: QTreeWidgetItem):
        self._items.pop(it.text(COL_PATH), None)
        self._meta.pop(it.text(COL_PATH), None)
        self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(it))

    def _on_check_state(self, path: str, state: str):
        """메타 검사 결과는 아직 '확인 중' 인 항목에만 반영 (처리 결과 덮어쓰기 방지)"""
        it = self._find_item(path)
        if it and it.text(COL_STATUS) == TaskState.CHECKING.value:
            it.setText(COL_STATUS, state)

    def _remove_selected(self):
        items = self.tree.selectedItems()
        if not items: return
        if self._processing_active:
            QMessageBox.information(self, "알림", "처리 중에는 제거 불가")
            return
        for it in items:
            self._take_item(it)

    def _clear_all(self):
        if self.tree.topLevelItemCount() == 0: return
        if self._processing_active:
            QMessageBox.information(self, "알림", "처리 중에는 제거 불가")
            return
        n = self.tree.topLevelItemCount()
        if QMessageBox.question(self, "확인", f"전체 {n}개 제거할까요?") == QMessageBox.Yes:
            self.tree.clear()
            self._items.clear()
            self._meta.clear()

    def _on_mode_changed(self, idx):
        self.cfg.process_mode = "serial" if idx == 0 else "parallel"
        self._update_pool()
        self.settings.save()

    def _on_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec_() == QDialog.Accepted:
            old_theme = self.cfg.theme
            dlg.apply_to(self.cfg)
            self.settings.save()
            self._update_pool()
            self.cb_mode.setCurrentIndex(0 if self.cfg.process_mode == "serial" else 1)
            if old_theme != self.cfg.theme:
                if self.cfg.theme == "dark":
                    Theme.apply_dark(QApplication.instance())
                else:
                    Theme.apply_light(QApplication.instance())

    def _on_about(self):
        QMessageBox.about(self, f"{APP_NAME} 정보",
            f"<h3>{APP_NAME} v{APP_VERSION}</h3>"
            f"<p>© {APP_BUILD_YEAR} {APP_AUTHOR} - "
            f'<a href="{APP_WEBSITE}" style="color:#FFA726;">{APP_WEBSITE}</a></p>')

    def _selected_or_all_paths(self, sel_only: bool) -> List[str]:
        if sel_only:
            its = self.tree.selectedItems()
        else:
            its = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        return [it.text(COL_PATH) for it in its]

    def _start(self, sel_only: bool):
        if self._processing_active:
            QMessageBox.information(self, "알림", "이미 처리 중")
            return
        paths = self._selected_or_all_paths(sel_only)
        if not paths:
            QMessageBox.information(self, "알림", "처리할 파일 없음")
            return
        if self.cfg.output_mode == OutputMode.SEPARATE_DIR.value:
            if not self.cfg.output_dir:
                QMessageBox.warning(self, "출력 폴더 미지정", "설정에서 폴더를 지정하세요.")
                return
            try:
                os.makedirs(self.cfg.output_dir, exist_ok=True)
            except Exception as e:
                QMessageBox.warning(self, "오류", f"출력 폴더 생성 실패: {e}")
                return

        self.controller.reset()
        with QMutexLocker(self._mutex):
            self._total = len(paths); self._done = 0
            self._success = 0; self._fail = 0
            self._processing_active = True

        self.pbar.setVisible(True)
        self.pbar.setMaximum(self._total)
        self.pbar.setValue(0)
        self._toggle_buttons(True)
        self._log(f"=== 처리 시작 (총 {self._total}개, 모드={self.cfg.process_mode}, "
                  f"출력={self.cfg.output_mode}, 오디오 보정={self.cfg.audio_sync_offset_sec:+.3f}s) ===")

        outputs = self._plan_output_paths(paths)
        for p in paths:
            it = self._find_item(p)
            if it:
                it.setText(COL_STATUS, TaskState.QUEUED.value)
                it.setData(COL_PROGRESS, ProgressDelegate.PROG_ROLE, 0)
            w = FileProcessor(p, self.cfg, self.controller, self.temp_dir,
                              output_path=outputs.get(p))
            w.signals.state.connect(self._on_state)
            w.signals.progress.connect(self._on_progress)
            w.signals.log.connect(self._log)
            w.signals.finished.connect(self._on_finished)
            self.pool.start(w)

    def _plan_output_paths(self, paths: List[str]) -> Dict[str, str]:
        """출력 경로를 미리 정하고, 같은 이름이 겹치면 '_2', '_3' 을 붙인다.
        (별도 폴더 출력 시 서로 다른 폴더의 같은 이름 파일이 서로 덮어쓰던 문제 수정)"""
        result: Dict[str, str] = {}
        used = set()
        for p in paths:
            base_out = FileProcessor.resolve_output_path(p, self.cfg)
            out = base_out
            same_as_src = os.path.normcase(os.path.abspath(out)) == os.path.normcase(os.path.abspath(p))
            if not same_as_src:
                stem, ext = os.path.splitext(base_out)
                n = 2
                while os.path.normcase(os.path.abspath(out)) in used:
                    out = f"{stem}_{n}{ext}"
                    n += 1
                if out != base_out:
                    self._log(f"출력 이름 중복 → {os.path.basename(out)} 로 저장: {p}")
            used.add(os.path.normcase(os.path.abspath(out)))
            result[p] = out
        return result

    def _on_pause(self):
        if not self._processing_active: return
        if self.btn_pause.text().startswith("⏸"):
            self.controller.pause()
            self.btn_pause.setText("▶ 재개")
            self.lbl_progress.setText("일시정지 중...")
        else:
            self.controller.resume()
            self.btn_pause.setText("⏸ 일시정지")
            self.lbl_progress.setText(f"처리 중 ({self._done}/{self._total})")

    def _on_cancel(self):
        if not self._processing_active: return
        if QMessageBox.question(self, "취소", "진행 중인 작업을 취소할까요?") != QMessageBox.Yes:
            return
        self.controller.cancel()

    def _on_state(self, path: str, state: str):
        it = self._find_item(path)
        if it:
            it.setText(COL_STATUS, state)

    def _on_progress(self, path: str, pct: float, eta: float, speed: float):
        it = self._find_item(path)
        if it:
            it.setData(COL_PROGRESS, ProgressDelegate.PROG_ROLE, pct)
        if eta > 0 and pct < 99.5:
            eta_s = str(timedelta(seconds=int(eta)))
            self.lbl_progress.setText(
                f"처리 중 ({self._done}/{self._total}) - {os.path.basename(path)} - "
                f"{int(pct)}% / ETA {eta_s} {speed:.1f} MB/s")

    def _on_finished(self, path: str, success: bool, msg: str):
        with QMutexLocker(self._mutex):
            self._done += 1
            if success: self._success += 1
            else: self._fail += 1
            done = self._done; total = self._total
            success_n = self._success; fail_n = self._fail
            all_done = done >= total
        self.pbar.setValue(done)
        self.lbl_progress.setText(f"처리 중 ({done}/{total}) - 성공 {success_n} / 실패 {fail_n}")
        fname = os.path.basename(path)
        self._log(f"[{done}/{total}] {'✓' if success else '✗'} {fname}: {msg}")

        if all_done:
            self._processing_active = False
            self.pbar.setVisible(False)
            self._toggle_buttons(False)
            self.btn_pause.setText("⏸ 일시정지")
            cancelled = self.controller.is_cancelled()
            self.controller.reset()
            summary = (f"처리 완료\n\n총 {total}개 / 성공 {success_n} / 실패 {fail_n}"
                       + ("\n(취소됨)" if cancelled else ""))
            self._log("=== " + summary.replace("\n\n", " - ").replace("\n", " ") + " ===")
            self._notify_finished(total, success_n, fail_n, cancelled)
            self.lbl_progress.setText("준비됨")

    def _notify_finished(self, total: int, success_n: int, fail_n: int, cancelled: bool):
        """완료 알림: 화면 우하단 토스트 (3초 후 자동으로 사라짐)"""
        body = f"총 {total}개 / 성공 {success_n} / 실패 {fail_n}"
        if cancelled:
            title, level = "처리가 취소되었습니다", "warning"
        elif fail_n > 0:
            title, level = "처리 완료 (일부 실패)", "error"
            body += "\n실패 내역은 보기 > 처리 로그에서 확인하세요."
        else:
            title, level = "처리 완료", "success"
        ToastNotification.show_toast(title, body, level, duration_ms=3000,
                                     on_click=self._bring_to_front)
        if not self.isActiveWindow():
            QApplication.alert(self, 3000)   # 작업표시줄 버튼 깜빡임

    def _bring_to_front(self):
        if self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _on_check_log(self, msg: str):
        if msg.startswith("__META__"):
            try:
                payload = json.loads(msg[len("__META__"):])
                p = payload["path"]
                m = payload["meta"]
                self._meta[p] = m
                it = self._find_item(p)
                if it:
                    w = int(m.get("w", 0)); h = int(m.get("h", 0))
                    vc = m.get("vc", ""); ac = m.get("ac", "")
                    codecs = " / ".join([c for c in (vc, ac) if c])
                    it.setText(COL_RES,      f"{w}x{h}" if (w and h) else "-")
                    it.setText(COL_CODEC,    codecs if codecs else "-")
                    it.setText(COL_BITRATE,  self._fmt_bitrate(m.get("br", 0)))
                    it.setText(COL_DURATION, self._fmt_dur(m.get("duration", 0)))
                    it.setText(COL_SIZE,     self._fmt_size(m.get("size", 0)))
            except Exception:
                pass
            return
        self._log(msg)

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")
        logger.info(msg)

    def _apply_filter(self, text: str):
        text = (text or "").lower().strip()
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            visible = (not text) or text in it.text(COL_NAME).lower() \
                                  or text in it.text(COL_STATUS).lower() \
                                  or text in it.text(COL_PATH).lower()
            it.setHidden(not visible)

    def _show_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if not item: return
        m = QMenu(self)
        a_diag = m.addAction("📊 파일 진단")
        a_open = m.addAction("📁 폴더 열기")
        a_copy = m.addAction("📋 경로 복사")
        a_retry = m.addAction("🔁 재처리")
        a_remove = m.addAction("🗑 제거")
        act = m.exec_(self.tree.viewport().mapToGlobal(pos))
        if act == a_diag:
            DiagnosticDialog(item.text(COL_PATH), self).exec_()
        elif act == a_open:
            self._open_explorer(item.text(COL_PATH))
        elif act == a_copy:
            QApplication.clipboard().setText(item.text(COL_PATH))
        elif act == a_retry:
            if not self._processing_active:
                self.tree.clearSelection(); item.setSelected(True)
                self._start(True)
        elif act == a_remove:
            if not self._processing_active:
                self._take_item(item)

    def _on_diagnose(self):
        """선택된 항목 또는 파일 다이얼로그로 진단"""
        sel = self.tree.selectedItems()
        if sel:
            DiagnosticDialog(sel[0].text(COL_PATH), self).exec_()
            return
        start = self.cfg.recent_dirs[0] if self.cfg.recent_dirs else ""
        f, _ = QFileDialog.getOpenFileName(self, "진단할 MP4 선택", start,
                                            "MP4 Files (*.mp4);;All Files (*)")
        if f:
            DiagnosticDialog(f, self).exec_()

    def _on_dbl(self, item, _col):
        status = item.text(COL_STATUS)
        if (TaskState.PENDING.value in status or TaskState.FAILED.value in status
                or TaskState.CANCELLED.value in status):
            if QMessageBox.question(self, "재처리",
                    f"'{item.text(COL_NAME)}' 파일을 처리할까요?") == QMessageBox.Yes:
                self.tree.clearSelection(); item.setSelected(True)
                self._start(True)

    def _open_explorer(self, filepath: str):
        try:
            if sys.platform == "win32":
                # Tiny 버전: subprocess 사용 안함 → os.startfile 로 폴더 열기
                folder = os.path.dirname(filepath)
                if os.path.isdir(folder):
                    os.startfile(folder)
            elif sys.platform == "darwin":
                os.system(f'open -R "{filepath}"')
            else:
                os.system(f'xdg-open "{os.path.dirname(filepath)}"')
        except Exception as e:
            self._log(f"탐색기 열기 실패: {e}")

    @staticmethod
    def _fmt_size(b):
        b = int(b or 0)
        if b <= 0: return "-"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}" if unit != "B" else f"{b} B"
            b /= 1024
        return f"{b:.1f} PB"

    @staticmethod
    def _fmt_dur(s):
        s = float(s or 0)
        if s <= 0: return "-"
        return str(timedelta(seconds=int(s)))

    @staticmethod
    def _fmt_bitrate(bps):
        bps = float(bps or 0)
        if bps <= 0: return "-"
        if bps >= 1_000_000:
            return f"{bps/1_000_000:.2f} Mbps"
        if bps >= 1000:
            return f"{bps/1000:.0f} kbps"
        return f"{int(bps)} bps"

    def _toggle_buttons(self, processing: bool):
        for b in (self.btn_add, self.btn_add_dir, self.btn_proc_sel, self.btn_proc_all,
                  self.btn_settings, self.btn_remove, self.btn_clear, self.cb_mode):
            b.setEnabled(not processing)
        self.btn_pause.setEnabled(processing)
        self.btn_cancel.setEnabled(processing)

    def _cleanup_orphan(self):
        try:
            now = time.time()
            for fn in os.listdir(self.temp_dir):
                fp = os.path.join(self.temp_dir, fn)
                try:
                    if os.path.isfile(fp) and now - os.path.getmtime(fp) > 3600:
                        os.remove(fp)
                except Exception:
                    pass
        except Exception:
            pass

    def closeEvent(self, e):
        if self._processing_active:
            if QMessageBox.question(self, "종료", "진행 중인 작업을 취소하고 종료할까요?") != QMessageBox.Yes:
                e.ignore(); return
            self.controller.cancel()
            t0 = time.monotonic()
            while self._processing_active and time.monotonic() - t0 < 5:
                QApplication.processEvents()
                time.sleep(0.05)
            self.pool.waitForDone(3000)
        self.check_pool.clear()
        for t in list(ToastNotification._active):
            t.close()
        self.settings.save()
        e.accept()


# =====================================================================
#  빠른 FastStart (탐색기 우클릭 "FastStart 처리하기")
#  - 메인 창 없이 화면 우하단에 파일명 + 진행률만 보이는 작은 창
#  - 여러 파일을 한꺼번에 우클릭해도 창 하나에 모아서 차례로 처리 (단일 인스턴스)
#  - 모두 끝나면 완료 표시 후 자동으로 닫힘
# =====================================================================
QUICK_ARG  = "--faststart"
QUICK_EXTS = (".mp4", ".m4v", ".mov")


class QuickRow(QFrame):
    """파일 한 개: 파일명 / 상태 / 얇은 진행률 막대"""
    NAME_WIDTH = 250

    def __init__(self, path: str, parent=None):
        super().__init__(parent)
        self.setObjectName("quickRow")
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 4, 0, 4)
        v.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        name = os.path.basename(path)
        self.lbl_name = QLabel()
        self.lbl_name.setObjectName("quickName")
        self.lbl_name.setText(self.lbl_name.fontMetrics().elidedText(name, Qt.ElideMiddle, self.NAME_WIDTH))
        self.lbl_name.setToolTip(path)
        self.lbl_status = QLabel("대기")
        self.lbl_status.setObjectName("quickStatus")
        self.lbl_status.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top.addWidget(self.lbl_name, 1)
        top.addWidget(self.lbl_status)
        v.addLayout(top)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(6)
        v.addWidget(self.bar)

    def set_progress(self, pct: float):
        self.bar.setValue(int(max(0.0, min(100.0, pct)) * 10))
        self.lbl_status.setText(f"{int(pct)}%")

    def set_status(self, text: str):
        self.lbl_status.setText(text)

    def set_done(self, ok: bool, msg: str):
        self.bar.setValue(1000)
        if ok:
            self.lbl_status.setText("이미 적용됨 ✓" if "이미" in msg else "완료 ✓")
            self.lbl_status.setStyleSheet("color:#81C784;")
            self.bar.setProperty("state", "done")
        else:
            self.lbl_status.setText("취소" if "취소" in msg else "실패")
            self.lbl_status.setStyleSheet("color:#EF9A9A;")
            self.lbl_status.setToolTip(msg)
            self.lbl_name.setToolTip(f"{self.lbl_name.toolTip()}\n실패 이유: {msg}")
            self.bar.setProperty("state", "fail")
        self.bar.style().unpolish(self.bar)
        self.bar.style().polish(self.bar)


class QuickWindow(QWidget):
    """우클릭 FastStart 전용 미니 창 (화면 우하단)"""
    WIDTH = 380
    MAX_VISIBLE_ROWS = 5

    def __init__(self, make_processor: Callable[..., QRunnable]):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(self.WIDTH)
        self._make = make_processor
        self.cfg = SettingsManager().cfg
        self.ctrl = ProcessController()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)          # 디스크 경합을 피하려고 한 번에 하나씩
        self.temp_dir = os.path.join(user_data_dir(), "temp")
        os.makedirs(self.temp_dir, exist_ok=True)
        self._rows: Dict[str, QuickRow] = {}
        self._pending = 0
        self._failed = 0
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self.close)

        self.setStyleSheet("""
            QFrame#quickFrame { background:#2b2d31; border:1px solid #45484e; border-radius:8px; }
            QLabel { color:#e8eaed; background:transparent; }
            QLabel#quickTitle { color:#ffffff; font-weight:700; font-size:10pt; }
            QLabel#quickName  { color:#e8eaed; font-size:9pt; }
            QLabel#quickStatus { color:#c4c8ce; font-size:9pt; }
            QPushButton#quickClose { color:#c4c8ce; background:transparent; border:none;
                                     font-size:11pt; padding:0 4px; }
            QPushButton#quickClose:hover { color:#ffffff; }
            QProgressBar { background:#1f2125; border:none; border-radius:3px; }
            QProgressBar::chunk { background:#FB8C00; border-radius:3px; }
            QProgressBar[state="done"]::chunk { background:#43A047; }
            QProgressBar[state="fail"]::chunk { background:#E35336; }
            QScrollArea { background:transparent; border:none; }
            QScrollArea > QWidget > QWidget { background:transparent; }
        """)

        frame = QFrame(self)
        frame.setObjectName("quickFrame")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        v = QVBoxLayout(frame)
        v.setContentsMargins(14, 10, 10, 12)
        v.setSpacing(4)

        head = QHBoxLayout()
        self.lbl_title = QLabel("FastStart 처리 중")
        self.lbl_title.setObjectName("quickTitle")
        btn_close = QPushButton("✕")
        btn_close.setObjectName("quickClose")
        btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setToolTip("닫기 (진행 중이면 취소)")
        btn_close.clicked.connect(self.close)
        head.addWidget(self.lbl_title, 1)
        head.addWidget(btn_close)
        v.addLayout(head)

        self._list = QWidget()
        self._list_layout = QVBoxLayout(self._list)
        self._list_layout.setContentsMargins(0, 0, 4, 0)
        self._list_layout.setSpacing(2)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setWidget(self._list)
        v.addWidget(self._scroll)

    # ---- 파일 추가 ----
    def add_files(self, paths: List[str]):
        added = 0
        for p in paths:
            p = os.path.normpath(os.path.abspath(p.strip().strip('"')))
            if not p or p in self._rows or not os.path.isfile(p) \
                    or not p.lower().endswith(QUICK_EXTS):
                continue
            row = QuickRow(p)
            self._list_layout.addWidget(row)
            self._rows[p] = row
            proc = self._make(p, self.cfg, self.ctrl, self.temp_dir)
            proc.signals.progress.connect(self._on_progress)
            proc.signals.finished.connect(self._on_finished)
            proc.signals.log.connect(lambda msg: logger.info(f"[빠른 FastStart]   {msg}"))
            self._pending += 1
            added += 1
            self.pool.start(proc)
            logger.info(f"[빠른 FastStart] 추가: {p}")
        if added:
            self._close_timer.stop()
            self._update_title()
            self._relayout()

    def has_files(self) -> bool:
        return bool(self._rows)

    # ---- 시그널 ----
    def _on_progress(self, path: str, pct: float, _eta: float, _speed: float):
        row = self._rows.get(path)
        if row and pct < 100:
            row.set_progress(pct)

    def _on_finished(self, path: str, ok: bool, msg: str):
        row = self._rows.get(path)
        if row:
            row.set_done(ok, msg)
        logger.info(f"[빠른 FastStart] {'성공' if ok else '실패'}: {path} ({msg})")
        self._pending -= 1
        if not ok:
            self._failed += 1
        self._update_title()
        if self._pending <= 0:
            # 실패가 있으면 이유를 볼 수 있게 조금 더 오래 둔다
            self._close_timer.start(6000 if self._failed else 2500)

    def _update_title(self):
        total = len(self._rows)
        done = total - self._pending
        if self._pending > 0:
            self.lbl_title.setText(f"FastStart 처리 중 ({done}/{total})")
        elif self._failed:
            self.lbl_title.setText(f"완료 (실패 {self._failed}개)")
        else:
            self.lbl_title.setText("FastStart 완료 ✓")

    # ---- 배치 ----
    def _relayout(self):
        rows = list(self._rows.values())
        row_h = rows[0].sizeHint().height() + self._list_layout.spacing() if rows else 0
        visible = min(len(rows), self.MAX_VISIBLE_ROWS)
        self._scroll.setFixedHeight(max(1, row_h * visible + 4))
        self.adjustSize()
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        self.move(area.right() - 16 - self.width(), area.bottom() - 16 - self.height())
        if rows:
            self._scroll.ensureWidgetVisible(rows[-1])

    def closeEvent(self, e):
        if self._pending > 0:
            self.ctrl.cancel()                  # 진행 중이면 취소 (임시 파일은 워커가 정리)
            self.pool.waitForDone(8000)
        super().closeEvent(e)
        QApplication.quit()


def run_quick_mode(app: QApplication, files: List[str],
                   make_processor: Callable[..., QRunnable]) -> int:
    """우클릭으로 실행된 경우. 이미 미니 창이 떠 있으면 파일만 넘기고 끝낸다."""
    from PyQt5.QtNetwork import QLocalServer, QLocalSocket
    server_name = f"{APP_USER_MODEL_ID}.quick.{os.environ.get('USERNAME', 'user')}"
    payload = "\n".join(files).encode("utf-8")

    server = None
    for _ in range(30):
        sock = QLocalSocket()
        sock.connectToServer(server_name)
        if sock.waitForConnected(200):
            # 끝 표시(\0) 를 붙여 보내고, 미니 창이 받았다는 응답(OK)을 확인한 뒤 종료
            sock.write(payload + b"\0")
            sock.flush()
            sock.waitForBytesWritten(2000)
            if sock.waitForReadyRead(3000) and bytes(sock.readAll()).startswith(b"OK"):
                sock.disconnectFromServer()
                return 0
            sock.abort()
            continue
        srv = QLocalServer()
        if srv.listen(server_name):
            server = srv
            break
        time.sleep(0.05)                        # 동시에 여러 개가 실행된 경우 잠시 후 재시도

    win = QuickWindow(make_processor)
    if server is not None:
        def on_new_connection():
            while server.hasPendingConnections():
                conn = server.nextPendingConnection()
                buf = bytearray()

                def on_ready(c=conn, b=buf):
                    b.extend(bytes(c.readAll()))
                    if b.endswith(b"\0"):
                        win.add_files(bytes(b[:-1]).decode("utf-8", "ignore").splitlines())
                        b.clear()
                        c.write(b"OK")
                        c.flush()

                conn.readyRead.connect(on_ready)
                conn.disconnected.connect(conn.deleteLater)
                if conn.bytesAvailable():
                    on_ready()
        server.newConnection.connect(on_new_connection)

    win.add_files(files)
    if not win.has_files():
        logger.info(f"[빠른 FastStart] 처리할 파일 없음: {files}")
        return 0
    win.show()
    return app.exec_()


def _make_quick_processor(path: str, cfg: AppConfig, ctrl: ProcessController,
                          temp_dir: str) -> QRunnable:
    return FileProcessor(path, cfg, ctrl, temp_dir)


# =====================================================================
def _load_app_icon() -> Optional[QIcon]:
    """app_icon.ico 를 다중 사이즈로 로드. 못 찾으면 None."""
    candidates = [
        os.path.join(app_base_dir(), "app_icon.ico"),
        os.path.join(os.getcwd(), "app_icon.ico"),
    ]
    icon_path = None
    for c in candidates:
        if os.path.isfile(c):
            icon_path = c
            break
    if not icon_path:
        return None
    icon = QIcon()
    # ICO 파일에 들어 있는 모든 사이즈를 명시적으로 추가
    for sz in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
        icon.addFile(icon_path, QSize(sz, sz))
    if icon.isNull():
        return QIcon(icon_path)
    return icon


def main():
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    # Windows 에서 작업표시줄/타이틀바 아이콘이 Python.exe 의 것으로 보이는 문제 방지
    if sys.platform == "win32":
        try:
            import ctypes
            # 설치 프로그램이 만든 바로가기의 AppUserModelID 와 같아야 함 (버전과 무관하게 고정)
            myappid = APP_USER_MODEL_ID
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(myappid)
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_ORG)

    app_icon = _load_app_icon()
    if app_icon is not None:
        app.setWindowIcon(app_icon)

    # 탐색기 우클릭 "FastStart 처리하기" → 메인 창 없이 미니 창으로 처리
    if QUICK_ARG in sys.argv:
        files = sys.argv[sys.argv.index(QUICK_ARG) + 1:]
        sys.exit(run_quick_mode(app, files, _make_quick_processor))

    sm = SettingsManager()
    if sm.cfg.theme == "dark":
        Theme.apply_dark(app)
    else:
        Theme.apply_light(app)

    win = MainWindow()
    if app_icon is not None:
        win.setWindowIcon(app_icon)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("Fatal error")
        raise
