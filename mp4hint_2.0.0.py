# -*- coding: utf-8 -*-
"""
==========================================================================
 MP4 HintBox Pro v2.0.0  -  Commercial Grade Edition
--------------------------------------------------------------------------
  Author : suldo.com
  Site   : https://suldo.com
  Engine : FFmpeg (FastStart) / MP4Box (Hint Track)
==========================================================================

핵심 개선 사항 (vs v1.0.5)
--------------------------------------------------------------------------
  1.  실시간 진행률  : FFmpeg -progress pipe:1 파싱으로 파일별 % / ETA / 속도 실시간 표시
  2.  Pause / Resume : psutil.suspend()/resume() 기반의 안전한 일시정지·재개
  3.  강제 취소      : 자식 프로세스 트리 즉시 종료 (좀비 방지)
  4.  mmap 파서      : 대용량 MP4 도 즉시 atom 검사 (faststart / hint track)
  5.  무결성 검증    : ffprobe 로 출력파일 검증 후 atomic replace 로 안전 교체
  6.  출력 모드      : 덮어쓰기 / 별도 폴더 / 같은 폴더 + 접미사 선택
  7.  폴더 재귀 스캔 : 폴더 드롭/추가 시 하위 MP4 모두 자동 추가
  8.  검색·필터      : 파일명 / 상태 즉시 필터링
  9.  모던 다크 UI   : QTreeWidget + ProgressDelegate, 컨텍스트 메뉴, 단축키
 10.  설정 영구 저장 : QSettings 로 사용자 설정 자동 복원
 11.  안전성 강화    : 디스크 여유 확인, 락 파일, 회전 로그, atomic 파일 작업
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
import platform
import subprocess
import threading
import queue
import mmap
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple, Dict, Any, Callable

import psutil

from PyQt5.QtCore import (
    Qt, QThread, pyqtSignal, QThreadPool, QRunnable, QObject, QUrl, QMutex,
    QMutexLocker, QSettings, QTimer, QSize, QRect, QEvent, pyqtSlot,
    QSortFilterProxyModel, QRegExp, QPropertyAnimation
)
from PyQt5.QtGui import (
    QFont, QPainter, QPen, QColor, QPalette, QIcon, QPixmap, QBrush,
    QKeySequence, QLinearGradient
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QPushButton, QLabel, QProgressBar,
    QFileDialog, QVBoxLayout, QHBoxLayout, QWidget, QComboBox, QMessageBox,
    QTextEdit, QSplitter, QCheckBox, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QLineEdit, QDialog, QDialogButtonBox, QFormLayout, QSpinBox, QGroupBox,
    QStyledItemDelegate, QStyle, QStyleOptionProgressBar, QMenu, QAction,
    QToolBar, QStatusBar, QFrame, QGridLayout, QRadioButton,
    QButtonGroup, QSizePolicy, QAbstractItemView, QShortcut, QDoubleSpinBox,
    QScrollArea
)


# =====================================================================
#  상수 / 메타데이터
# =====================================================================
APP_NAME       = "MP4 HintBox Pro"
APP_VERSION    = "2.1.0"
APP_ORG        = "EV7lab"
APP_WEBSITE    = "https://www.mp4hintbox.co.kr"
APP_AUTHOR     = "EV7lab"
APP_BUILD_YEAR = "2026"
APP_USER_MODEL_ID = "EV7lab.MP4HintBoxPro"

# 트리뷰 컬럼
COL_NAME      = 0
COL_RES       = 1   # 해상도 (NEW)
COL_CODEC     = 2   # 코덱 (NEW)
COL_BITRATE   = 3   # 비트레이트 (NEW)
COL_DURATION  = 4
COL_SIZE      = 5
COL_STATUS    = 6
COL_PROGRESS  = 7
COL_PATH      = 8  # 경로 (숨김 데이터)

# Windows STARTUPINFO (콘솔 숨김)
STARTUPINFO = None
CREATE_FLAGS = 0
if sys.platform == "win32":
    STARTUPINFO = subprocess.STARTUPINFO()
    STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    CREATE_FLAGS = subprocess.CREATE_NO_WINDOW


# =====================================================================
#  경로 / 리소스 헬퍼
# =====================================================================
def app_base_dir() -> str:
    """실행파일(또는 .py) 의 기준 디렉터리 - 동봉된 ffmpeg/MP4Box 경로용"""
    if getattr(sys, "frozen", False):
        # PyInstaller --onefile 의 경우 _MEIPASS 가 존재
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def user_data_dir() -> str:
    """사용자별 데이터 디렉터리 (로그/임시/설정 캐시 등)"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.path.expanduser("~/.local/share")
    p = os.path.join(base, "MP4HintBoxPro")
    os.makedirs(p, exist_ok=True)
    return p


def find_executable(name: str, fallback_local: str) -> str:
    """동봉 실행파일 우선, 없으면 PATH 에서 탐색.
    (PATH 우선이면 사용자 PC 의 오래된 ffmpeg 가 동봉한 최신 버전 대신 쓰이는 문제가 있었음)"""
    local = os.path.join(app_base_dir(), fallback_local)
    if os.path.isfile(local):
        return local
    found = shutil.which(name)
    if found:
        return found
    return fallback_local  # 못 찾아도 이름은 반환 (의존성 체크에서 잡힘)


# =====================================================================
#  로깅
# =====================================================================
def setup_logging() -> logging.Logger:
    log_dir = user_data_dir()
    log_file = os.path.join(log_dir, "mp4hintbox.log")
    handler = RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 중복 핸들러 방지
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(handler)
    # 콘솔 핸들러 (디버그용)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(sh)
    return logging.getLogger("MP4HintBox")


logger = setup_logging()


# =====================================================================
#  열거형 / 데이터 모델
# =====================================================================
class Engine(Enum):
    FFMPEG = "ffmpeg"
    MP4BOX = "mp4box"


class OutputMode(Enum):
    OVERWRITE      = "overwrite"        # 원본 덮어쓰기 (atomic)
    SEPARATE_DIR   = "separate_dir"     # 사용자 지정 출력 폴더
    SUFFIX         = "suffix"           # 같은 폴더 + 접미사


class TaskState(Enum):
    PENDING    = "대기 중"
    CHECKING   = "확인 중..."
    QUEUED     = "큐 대기"
    PROCESSING = "처리 중"
    PAUSED     = "일시정지"
    DONE       = "완료 ✓"
    SKIPPED    = "이미 처리됨 ⏭"
    FAILED     = "실패 ✗"
    CANCELLED  = "취소됨"


@dataclass
class MediaInfo:
    duration: float = 0.0          # 초
    size: int = 0                  # 바이트
    bit_rate: int = 0              # bps (전체 평균)
    has_faststart: bool = False
    has_hint_track: bool = False
    video_codec: str = ""
    audio_codec: str = ""
    width: int = 0
    height: int = 0


@dataclass
class AppConfig:
    """사용자 설정 (QSettings 와 1:1 매핑)"""
    engine: str = Engine.FFMPEG.value
    process_mode: str = "serial"       # serial / parallel
    max_parallel: int = 4
    hw_accel_enabled: bool = False
    backup_enabled: bool = False
    output_mode: str = OutputMode.OVERWRITE.value
    output_dir: str = ""
    output_suffix: str = "_hinted"
    timeout: int = 7200                # 2h per file
    verify_output: bool = True
    auto_cleanup_temp: bool = True
    theme: str = "dark"                # dark / light
    native_faststart: bool = True      # FFmpeg 우회 고속 처리 (기본 ON)
    audio_sync_offset_sec: float = 0.00  # Native 처리 시 오디오 싱크 보정 (양수=오디오 앞으로)
    flatten_video_stts: bool = False     # 비디오 stts 평탄화 (VFR → 의사 CFR, 재인코딩 X)
    show_log: bool = False             # 처리 로그 패널 표시 (기본 숨김)
    preserve_mtime: bool = True        # 출력 파일에 원본 수정 날짜 유지
    recent_dirs: List[str] = field(default_factory=list)


# =====================================================================
#  설정 관리자 (QSettings 영구 저장)
# =====================================================================
def _version_tuple(v: str) -> Tuple[int, ...]:
    try:
        return tuple(int(x) for x in (v or "0").split("."))
    except ValueError:
        return (0,)


class SettingsManager:
    def __init__(self):
        self.q = QSettings(APP_ORG, APP_NAME)
        self.cfg = AppConfig()
        self.load()

    def load(self):
        c = self.cfg
        c.engine            = self.q.value("engine", c.engine, str)
        c.process_mode      = self.q.value("process_mode", c.process_mode, str)
        c.max_parallel      = int(self.q.value("max_parallel", c.max_parallel))
        c.hw_accel_enabled  = self.q.value("hw_accel_enabled", c.hw_accel_enabled, bool)
        c.backup_enabled    = self.q.value("backup_enabled", c.backup_enabled, bool)
        c.output_mode       = self.q.value("output_mode", c.output_mode, str)
        c.output_dir        = self.q.value("output_dir", c.output_dir, str)
        c.output_suffix     = self.q.value("output_suffix", c.output_suffix, str)
        c.timeout           = int(self.q.value("timeout", c.timeout))
        c.verify_output     = self.q.value("verify_output", c.verify_output, bool)
        c.auto_cleanup_temp = self.q.value("auto_cleanup_temp", c.auto_cleanup_temp, bool)
        c.theme             = self.q.value("theme", c.theme, str)
        c.native_faststart  = self.q.value("native_faststart", c.native_faststart, bool)
        try:
            c.audio_sync_offset_sec = float(self.q.value("audio_sync_offset_sec", c.audio_sync_offset_sec))
        except (TypeError, ValueError):
            pass
        c.flatten_video_stts = self.q.value("flatten_video_stts", c.flatten_video_stts, bool)
        c.show_log          = self.q.value("show_log", c.show_log, bool)
        c.preserve_mtime    = self.q.value("preserve_mtime", c.preserve_mtime, bool)
        recent             = self.q.value("recent_dirs", "", str)
        c.recent_dirs       = [p for p in (recent or "").split("|") if p]

        # ── 버전 업그레이드 마이그레이션 ──
        # v2.0.3 ~ v2.0.5 는 audio_sync_offset_sec 의 기본값이 0.15 였음.
        # 이는 정상 파일도 강제로 audio elst.media_time 을 +0.15s 만큼 시프트하여
        # 오히려 싱크를 깨뜨리는 문제가 있었음. v2.0.6 부터 기본 0.0.
        # 사용자가 0.15 로 저장된 상태이면 (옛 기본값) 0.0 으로 자동 리셋.
        seen_version = self.q.value("seen_version", "", str)
        if seen_version != APP_VERSION:
            if seen_version in ("", "2.0.3", "2.0.4", "2.0.5"):
                if abs(c.audio_sync_offset_sec - 0.15) < 0.001:
                    c.audio_sync_offset_sec = 0.0
                    self.q.setValue("audio_sync_offset_sec", 0.0)
            # v2.1.0: 가속 기본값을 CPU(Stream Copy) 로. FastStart 에는 재인코딩이 필요 없고
            # GPU 트랜스코드는 느리고 화질이 바뀌므로, 이전 버전에서 켜 둔 값도 한 번 끈다.
            if _version_tuple(seen_version) < (2, 1, 0) and c.hw_accel_enabled:
                c.hw_accel_enabled = False
                self.q.setValue("hw_accel_enabled", False)
            self.q.setValue("seen_version", APP_VERSION)

    def save(self):
        c = self.cfg
        self.q.setValue("engine", c.engine)
        self.q.setValue("process_mode", c.process_mode)
        self.q.setValue("max_parallel", c.max_parallel)
        self.q.setValue("hw_accel_enabled", c.hw_accel_enabled)
        self.q.setValue("backup_enabled", c.backup_enabled)
        self.q.setValue("output_mode", c.output_mode)
        self.q.setValue("output_dir", c.output_dir)
        self.q.setValue("output_suffix", c.output_suffix)
        self.q.setValue("timeout", c.timeout)
        self.q.setValue("verify_output", c.verify_output)
        self.q.setValue("auto_cleanup_temp", c.auto_cleanup_temp)
        self.q.setValue("theme", c.theme)
        self.q.setValue("native_faststart", c.native_faststart)
        self.q.setValue("audio_sync_offset_sec", c.audio_sync_offset_sec)
        self.q.setValue("flatten_video_stts", c.flatten_video_stts)
        self.q.setValue("show_log", c.show_log)
        self.q.setValue("preserve_mtime", c.preserve_mtime)
        self.q.setValue("recent_dirs", "|".join(c.recent_dirs[:10]))
        self.q.sync()


# =====================================================================
#  테마
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
            QLineEdit, QComboBox, QSpinBox { padding:5px; border:1px solid #4a4d52;
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
#  MP4 Atom 파서  (mmap 기반 - 대용량도 즉시)
# =====================================================================
class MP4Parser:
    """ISOBMFF (MP4) atom 박스 파서. 로컬에서 faststart / hint track 빠르게 검사."""

    @staticmethod
    def _iter_top_atoms(mm: mmap.mmap, max_pos: int):
        """top-level atom 순회: (offset, type, size) yield"""
        size_total = len(mm)
        pos = 0
        while pos + 8 <= size_total and pos < max_pos:
            try:
                size = struct.unpack(">I", mm[pos:pos+4])[0]
                atom_type = mm[pos+4:pos+8].decode("ascii", errors="ignore")
            except Exception:
                return
            if size == 1:
                # 64-bit largesize
                if pos + 16 > size_total:
                    return
                size = struct.unpack(">Q", mm[pos+8:pos+16])[0]
            elif size == 0:
                # 마지막 atom - 파일 끝까지
                size = size_total - pos
            if size < 8:
                return
            yield pos, atom_type, size
            pos += size

    @staticmethod
    def has_faststart(filepath: str) -> bool:
        """moov 가 mdat 보다 앞에 있는가?"""
        try:
            sz = os.path.getsize(filepath)
            if sz < 16:
                return False
            with open(filepath, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    moov_pos = -1
                    mdat_pos = -1
                    for offset, atype, _size in MP4Parser._iter_top_atoms(mm, sz):
                        if atype == "moov" and moov_pos == -1:
                            moov_pos = offset
                        elif atype == "mdat" and mdat_pos == -1:
                            mdat_pos = offset
                        if moov_pos != -1 and mdat_pos != -1:
                            break
                    if moov_pos == -1:
                        return False
                    if mdat_pos == -1:
                        # moov 만 발견 - 일반적으로 faststart 됨
                        return True
                    return moov_pos < mdat_pos
        except Exception as e:
            logger.warning(f"faststart 검사 실패 ({filepath}): {e}")
            return False

    @staticmethod
    def has_hint_track(filepath: str) -> bool:
        """moov 박스 안에 trak/hdlr 가 'hint' 인 트랙이 있는가? (전체 스캔)"""
        try:
            with open(filepath, "rb") as f:
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    sz = len(mm)
                    # moov atom 찾기
                    for offset, atype, asize in MP4Parser._iter_top_atoms(mm, sz):
                        if atype == "moov":
                            return MP4Parser._scan_for_hint_in_moov(mm, offset+8, offset+asize)
            return False
        except Exception as e:
            logger.warning(f"hint track 검사 실패 ({filepath}): {e}")
            return False

    @staticmethod
    def _scan_for_hint_in_moov(mm: mmap.mmap, start: int, end: int) -> bool:
        """moov 내부를 재귀적으로 탐색하여 hdlr handler_type == 'hint' 검색."""
        pos = start
        while pos + 8 <= end:
            try:
                size = struct.unpack(">I", mm[pos:pos+4])[0]
                atype = mm[pos+4:pos+8].decode("ascii", errors="ignore")
            except Exception:
                return False
            head = 8
            if size == 1:
                if pos + 16 > end:
                    return False
                size = struct.unpack(">Q", mm[pos+8:pos+16])[0]
                head = 16
            if size < 8 or pos + size > end:
                return False
            if atype == "hdlr":
                # FullBox (4 bytes version+flags) + 4 reserved + 4 handler_type
                # hdlr payload starts at pos + head
                # handler_type is at offset pos+head+8 .. pos+head+12
                ht_start = pos + head + 8
                if ht_start + 4 <= pos + size:
                    handler_type = mm[ht_start:ht_start+4].decode("ascii", errors="ignore")
                    if handler_type == "hint":
                        return True
            elif atype in ("trak", "mdia", "udta"):
                # 자식 박스 재귀
                if MP4Parser._scan_for_hint_in_moov(mm, pos+head, pos+size):
                    return True
            pos += size
        return False


# =====================================================================
#  ffprobe / 미디어 정보
# =====================================================================
class MediaProbe:
    @staticmethod
    def get_info(filepath: str, ffprobe_path: str) -> MediaInfo:
        info = MediaInfo()
        try:
            info.size = os.path.getsize(filepath)
        except Exception:
            pass
        try:
            cmd = [ffprobe_path, "-v", "quiet", "-print_format", "json",
                   "-show_format", "-show_streams", filepath]
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="ignore",
                                 timeout=30, startupinfo=STARTUPINFO,
                                 creationflags=CREATE_FLAGS)
            if res.returncode == 0 and res.stdout:
                data = json.loads(res.stdout)
                fmt = data.get("format", {})
                try:
                    info.duration = float(fmt.get("duration", 0) or 0)
                except (TypeError, ValueError):
                    info.duration = 0.0
                try:
                    info.bit_rate = int(float(fmt.get("bit_rate", 0) or 0))
                except (TypeError, ValueError):
                    info.bit_rate = 0
                for s in data.get("streams", []):
                    ct = s.get("codec_type", "")
                    if ct == "video" and not info.video_codec:
                        info.video_codec = s.get("codec_name", "")
                        info.width  = int(s.get("width", 0) or 0)
                        info.height = int(s.get("height", 0) or 0)
                    elif ct == "audio" and not info.audio_codec:
                        info.audio_codec = s.get("codec_name", "")
        except Exception as e:
            logger.warning(f"ffprobe 실패 ({filepath}): {e}")
        info.has_faststart  = MP4Parser.has_faststart(filepath)
        info.has_hint_track = MP4Parser.has_hint_track(filepath)
        # ffprobe 가 bit_rate 를 못 줬으면 size/duration 로 추산
        if info.bit_rate <= 0 and info.duration > 0 and info.size > 0:
            info.bit_rate = int(info.size * 8 / info.duration)
        return info

    @staticmethod
    def verify_output(filepath: str, ffprobe_path: str,
                      expected_duration: float = 0.0,
                      tolerance: float = 1.5) -> bool:
        """출력파일이 ffprobe 로 정상적으로 열리고 duration 이 비슷한지 검증."""
        try:
            cmd = [ffprobe_path, "-v", "error", "-show_entries",
                   "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
                   filepath]
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 encoding="utf-8", errors="ignore",
                                 timeout=30, startupinfo=STARTUPINFO,
                                 creationflags=CREATE_FLAGS)
            if res.returncode != 0:
                return False
            try:
                got = float(res.stdout.strip() or 0)
            except ValueError:
                return False
            if got <= 0:
                return False
            if expected_duration > 0:
                return abs(got - expected_duration) <= max(tolerance, expected_duration * 0.05)
            return True
        except Exception as e:
            logger.warning(f"출력 검증 실패: {e}")
            return False


# =====================================================================
#  Native FastStart  (qtfaststart 알고리즘 - FFmpeg 우회, 최고속)
# =====================================================================
class NativeFastStart:
    """
    moov atom 을 직접 재배치하여 faststart 를 구현.
    FFmpeg subprocess 호출 / demux/remux 오버헤드를 제거하여
    디스크 I/O 한도까지 처리 속도를 끌어올림 (보통 FFmpeg 대비 2~5배 빠름).
    스트림 카피 (= GPU 트랜스코드 미사용) 케이스에서만 사용 가능.
    """

    CHUNK_SIZE = 8 * 1024 * 1024  # 8MB 청크 - 시퀀셜 I/O 최적화

    @staticmethod
    def _read_top_atoms(f, end_pos: int) -> List[Tuple[int, str, int, int]]:
        """top-level atom 목록 (offset, type, size, header_size)"""
        atoms: List[Tuple[int, str, int, int]] = []
        f.seek(0)
        pos = 0
        while pos < end_pos:
            f.seek(pos)
            head = f.read(8)
            if len(head) < 8:
                break
            size = struct.unpack(">I", head[:4])[0]
            atype = head[4:8].decode("ascii", errors="ignore")
            header_size = 8
            if size == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    break
                size = struct.unpack(">Q", ext)[0]
                header_size = 16
            elif size == 0:
                size = end_pos - pos
            if size < 8:
                break
            atoms.append((pos, atype, size, header_size))
            pos += size
        return atoms

    @staticmethod
    def _adjust_moov_offsets(moov_bytes: bytes, delta: int) -> bytes:
        """moov 안의 모든 stco/co64 entry 에 delta(=moov_size) 더하기."""
        result = bytearray(moov_bytes)

        def recurse(pos: int, end: int):
            while pos + 8 <= end:
                size = struct.unpack(">I", result[pos:pos+4])[0]
                atype = bytes(result[pos+4:pos+8]).decode("ascii", errors="ignore")
                header_size = 8
                if size == 1:
                    if pos + 16 > end:
                        return
                    size = struct.unpack(">Q", result[pos+8:pos+16])[0]
                    header_size = 16
                if size < 8 or pos + size > end:
                    return
                if atype in ("stco", "co64"):
                    # FullBox: version(1) + flags(3) + entry_count(4) + entries
                    count_pos = pos + header_size + 4
                    entries_pos = count_pos + 4
                    count = struct.unpack(">I", result[count_pos:count_pos+4])[0]
                    if atype == "stco":
                        for i in range(count):
                            ep = entries_pos + i * 4
                            old = struct.unpack(">I", result[ep:ep+4])[0]
                            new = old + delta
                            if new > 0xFFFFFFFF:
                                raise ValueError("stco 오프셋이 32bit 범위 초과 (co64 필요)")
                            struct.pack_into(">I", result, ep, new)
                    else:  # co64
                        for i in range(count):
                            ep = entries_pos + i * 8
                            old = struct.unpack(">Q", result[ep:ep+8])[0]
                            struct.pack_into(">Q", result, ep, old + delta)
                elif atype in ("trak", "mdia", "minf", "stbl", "edts", "udta"):
                    recurse(pos + header_size, pos + size)
                pos += size

        # moov 의 자식 박스부터 재귀
        recurse(8, len(result))
        return bytes(result)

    @staticmethod
    def process(input_path: str, output_path: str,
                ctrl,
                on_progress: Callable[[float, float, float], None],
                on_log: Callable[[str], None],
                audio_sync_offset_sec: float = 0.0,
                flatten_video_stts: bool = False) -> Tuple[bool, str]:
        """반환: (success, message)
           audio_sync_offset_sec: 오디오 elst.media_time 보정값 (양수=오디오 빨라짐)
           flatten_video_stts: 비디오 stts 의 모든 sample_delta 를 평균값으로 통일 (VFR→CFR)"""
        try:
            file_size = os.path.getsize(input_path)
            if file_size < 32:
                return False, "파일 너무 작음"

            on_log(f"  native: 파일 크기 {file_size/1024/1024:.1f}MB - atom 분석 시작")

            with open(input_path, "rb") as fin:
                atoms = NativeFastStart._read_top_atoms(fin, file_size)

            ftyp = next((a for a in atoms if a[1] == "ftyp"), None)
            moov = next((a for a in atoms if a[1] == "moov"), None)
            mdat = next((a for a in atoms if a[1] == "mdat"), None)

            if not ftyp:
                return False, "ftyp atom 없음 (유효한 MP4 가 아님)"
            if not moov:
                return False, "moov atom 없음"
            if not mdat:
                return False, "mdat atom 없음"

            # ── 안전성 검사: stco offset 보정이 부정확할 수 있는 구조는 폴백 ──
            mdat_count = sum(1 for a in atoms if a[1] == "mdat")
            moov_count = sum(1 for a in atoms if a[1] == "moov")
            moof_count = sum(1 for a in atoms if a[1] == "moof")
            meta_top   = sum(1 for a in atoms if a[1] == "meta")  # iloc 가능성

            if moof_count > 0:
                return False, f"fragmented MP4 (moof × {moof_count}) - native 처리 불가"
            if mdat_count > 1:
                return False, f"다중 mdat 감지 (mdat × {mdat_count}) - 싱크 깨짐 위험으로 native 처리 불가"
            if moov_count > 1:
                return False, f"다중 moov 감지 (moov × {moov_count}) - native 처리 불가"
            if meta_top > 0:
                # 최상위 meta 박스의 iloc 안에 파일 오프셋이 있을 수 있음
                if NativeFastStart._meta_has_iloc(input_path, atoms):
                    return False, "최상위 meta/iloc 감지 - 아이템 오프셋 보정 미지원, native 처리 불가"

            # 이미 faststart 인 경우
            if moov[0] < mdat[0]:
                on_log("  native: 이미 faststart - 단순 복사")
                if not NativeFastStart._copy(input_path, output_path, file_size, ctrl, on_progress):
                    return False, "취소됨"
                return True, "이미 faststart (복사 완료)"

            on_log(f"  native: moov={moov[2]/1024:.1f}KB @ {moov[0]} → 선두로 이동")

            # moov 읽기
            with open(input_path, "rb") as fin:
                fin.seek(moov[0])
                moov_data = fin.read(moov[2])

            # 오디오 싱크 오프셋 적용 (in-place, 박스 크기 변경 없음)
            if abs(audio_sync_offset_sec) > 0.001:
                moov_data = NativeFastStart._apply_audio_offset(
                    moov_data, audio_sync_offset_sec, on_log)

            # 비디오 stts 평탄화 (VFR → 의사 CFR, in-place)
            if flatten_video_stts:
                moov_data = NativeFastStart._flatten_video_stts(moov_data, on_log)

            # stco/co64 오프셋 보정
            try:
                moov_data_new = NativeFastStart._adjust_moov_offsets(moov_data, moov[2])
            except ValueError as e:
                return False, f"native 처리 불가 ({e})"

            # 출력 작성
            start = time.monotonic()
            written = 0

            with open(input_path, "rb") as fin, open(output_path, "wb") as fout:
                # 1) ftyp 복사
                fin.seek(ftyp[0])
                fout.write(fin.read(ftyp[2]))
                written += ftyp[2]

                # 2) 보정된 moov 기록 (선두로 이동)
                fout.write(moov_data_new)
                written += len(moov_data_new)

                # 3) 나머지 atom 들 (ftyp / 원본 moov 제외) - 시퀀셜 복사
                for atom in atoms:
                    offset, atype, size, _hs = atom
                    if atom == ftyp or atom == moov:
                        continue
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
                        on_progress(pct, eta, bps / (1024 * 1024))  # MB/s 를 speed 슬롯에

            out_size = os.path.getsize(output_path)
            if out_size != file_size:
                return False, f"출력 크기 불일치 (입력 {file_size:,} / 출력 {out_size:,} bytes)"
            on_progress(100.0, 0, 0)
            elapsed = max(0.001, time.monotonic() - start)
            on_log(f"  native: 완료 ({elapsed:.2f}s, 평균 {written/elapsed/1024/1024:.1f} MB/s)")
            return True, "OK"

        except Exception as e:
            return False, f"native 처리 예외: {e}"

    # ─────── Audio sync offset (in-place elst 수정) ───────
    @staticmethod
    def _find_box(data, start: int, end: int, target_type: str,
                  recurse_into: tuple = ()) -> Optional[Tuple[int, int, int]]:
        """data[start:end] 에서 target_type 박스 탐색. (offset, size, header_size) 반환."""
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
            if atype == target_type:
                return (pos, size, header_size)
            if atype in recurse_into:
                found = NativeFastStart._find_box(data, pos + header_size,
                                                   pos + size, target_type, recurse_into)
                if found:
                    return found
            pos += size
        return None

    @staticmethod
    def _find_audio_trak(moov) -> Optional[Tuple[int, int, int]]:
        """moov 에서 hdlr.handler_type == 'soun' 인 첫 trak 박스 반환."""
        pos = 8
        end = len(moov)
        while pos + 8 <= end:
            try:
                size = struct.unpack(">I", moov[pos:pos+4])[0]
                atype = bytes(moov[pos+4:pos+8]).decode("ascii", errors="ignore")
            except Exception:
                return None
            header_size = 8
            if size == 1:
                if pos + 16 > end:
                    return None
                size = struct.unpack(">Q", moov[pos+8:pos+16])[0]
                header_size = 16
            if size < 8 or pos + size > end:
                return None
            if atype == "trak":
                mdia = NativeFastStart._find_box(moov, pos + header_size,
                                                  pos + size, "mdia")
                if mdia:
                    hdlr = NativeFastStart._find_box(moov, mdia[0] + mdia[2],
                                                     mdia[0] + mdia[1], "hdlr")
                    if hdlr:
                        # hdlr payload: version(1)+flags(3) + pre_defined(4) + handler_type(4)
                        ht_pos = hdlr[0] + hdlr[2] + 4 + 4
                        if ht_pos + 4 <= hdlr[0] + hdlr[1]:
                            ht = bytes(moov[ht_pos:ht_pos+4]).decode("ascii", errors="ignore")
                            if ht == "soun":
                                return (pos, size, header_size)
            pos += size
        return None

    @staticmethod
    def _get_mdhd_timescale(moov, trak_start: int, trak_size: int,
                             trak_header: int) -> Optional[int]:
        """trak 안의 mdia/mdhd 에서 media timescale 반환."""
        mdia = NativeFastStart._find_box(moov, trak_start + trak_header,
                                          trak_start + trak_size, "mdia")
        if not mdia:
            return None
        mdhd = NativeFastStart._find_box(moov, mdia[0] + mdia[2],
                                          mdia[0] + mdia[1], "mdhd")
        if not mdhd:
            return None
        # FullBox: version(1) + flags(3)
        version = moov[mdhd[0] + mdhd[2]]
        if version == 1:
            ts_pos = mdhd[0] + mdhd[2] + 4 + 8 + 8  # ver/flags + ctime(8) + mtime(8)
        else:
            ts_pos = mdhd[0] + mdhd[2] + 4 + 4 + 4  # ver/flags + ctime(4) + mtime(4)
        if ts_pos + 4 > mdhd[0] + mdhd[1]:
            return None
        return struct.unpack(">I", moov[ts_pos:ts_pos+4])[0]

    @staticmethod
    def _modify_audio_elst_inplace(moov, trak_start: int, trak_size: int,
                                    trak_header: int, offset_units: int) -> bool:
        """trak/edts/elst 의 첫 엔트리 media_time 에 offset_units 을 더함 (in-place)."""
        edts = NativeFastStart._find_box(moov, trak_start + trak_header,
                                          trak_start + trak_size, "edts")
        if not edts:
            return False
        elst = NativeFastStart._find_box(moov, edts[0] + edts[2],
                                          edts[0] + edts[1], "elst")
        if not elst:
            return False
        version = moov[elst[0] + elst[2]]
        # 엔트리 카운트 위치
        count_pos = elst[0] + elst[2] + 4
        if count_pos + 4 > elst[0] + elst[1]:
            return False
        count = struct.unpack(">I", moov[count_pos:count_pos+4])[0]
        if count == 0:
            return False
        entry_pos = count_pos + 4
        if version == 0:
            # entry: seg_dur(4u) + media_time(4i) + rate(4)
            mt_pos = entry_pos + 4
            if mt_pos + 4 > elst[0] + elst[1]:
                return False
            old = struct.unpack(">i", moov[mt_pos:mt_pos+4])[0]
            if old == -1:  # 빈 edit (empty edit) - 건드리지 않음
                return False
            new = max(0, old + offset_units)
            struct.pack_into(">i", moov, mt_pos, new)
            return True
        elif version == 1:
            # entry: seg_dur(8u) + media_time(8i) + rate(4)
            mt_pos = entry_pos + 8
            if mt_pos + 8 > elst[0] + elst[1]:
                return False
            old = struct.unpack(">q", moov[mt_pos:mt_pos+8])[0]
            if old == -1:
                return False
            new = max(0, old + offset_units)
            struct.pack_into(">q", moov, mt_pos, new)
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
                mdia = NativeFastStart._find_box(moov, pos + hs, pos + size, "mdia")
                if mdia:
                    hdlr = NativeFastStart._find_box(moov, mdia[0] + mdia[2],
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

        mdia = NativeFastStart._find_box(result, ts_start + ts_hs,
                                          ts_start + ts_size, "mdia")
        if not mdia: return moov_bytes
        minf = NativeFastStart._find_box(result, mdia[0] + mdia[2],
                                          mdia[0] + mdia[1], "minf")
        if not minf: return moov_bytes
        stbl = NativeFastStart._find_box(result, minf[0] + minf[2],
                                          minf[0] + minf[1], "stbl")
        if not stbl: return moov_bytes
        stts = NativeFastStart._find_box(result, stbl[0] + stbl[2],
                                          stbl[0] + stbl[1], "stts")
        if not stts:
            on_log("⚠ stts 미발견 - 평탄화 스킵")
            return moov_bytes

        cpos = stts[0] + stts[2] + 4
        if cpos + 4 > stts[0] + stts[1]: return moov_bytes
        count = struct.unpack(">I", result[cpos:cpos+4])[0]
        if count == 0: return moov_bytes
        espos = cpos + 4

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

        if len(deltas_seen) == 1:
            on_log(f"  stts 이미 균일 ({list(deltas_seen)[0]} ticks) - 평탄화 불필요")
            return moov_bytes

        new_delta = max(1, round(total_duration / total_samples))

        for i in range(count):
            ep = espos + i * 8
            if ep + 8 > stts[0] + stts[1]: break
            struct.pack_into(">I", result, ep + 4, new_delta)

        new_total = total_samples * new_delta
        drift = new_total - total_duration

        # mdhd.duration 동기화
        mdhd = NativeFastStart._find_box(result, mdia[0] + mdia[2],
                                          mdia[0] + mdia[1], "mdhd")
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
               f"(samples={total_samples:,}, drift={drift:+d} ticks)")
        return bytes(result)

    @staticmethod
    def _apply_audio_offset(moov_bytes: bytes, offset_sec: float,
                             on_log: Callable[[str], None]) -> bytes:
        """moov 안의 audio trak 의 elst 를 in-place 수정. 박스 크기 변경 없음.
           오프셋이 0 이거나 elst 가 없으면 원본 그대로 반환."""
        if abs(offset_sec) < 0.001:
            return moov_bytes
        result = bytearray(moov_bytes)
        audio = NativeFastStart._find_audio_trak(result)
        if not audio:
            on_log(f"  ⚠ audio trak 미발견 - 싱크 보정 스킵")
            return moov_bytes
        trak_start, trak_size, trak_header = audio
        ts = NativeFastStart._get_mdhd_timescale(result, trak_start, trak_size, trak_header)
        if not ts:
            on_log(f"  ⚠ mdhd timescale 획득 실패 - 싱크 보정 스킵")
            return moov_bytes
        offset_units = int(round(offset_sec * ts))
        ok = NativeFastStart._modify_audio_elst_inplace(result, trak_start,
                                                         trak_size, trak_header,
                                                         offset_units)
        if not ok:
            on_log(f"  ⚠ audio elst 미발견 또는 형식 미지원 - 싱크 보정 스킵")
            return moov_bytes
        on_log(f"  ✓ 오디오 싱크 보정: +{offset_sec:.3f}s ({offset_units} units @ {ts}Hz)")
        return bytes(result)

    @staticmethod
    def _meta_has_iloc(filepath: str, atoms: List[Tuple[int, str, int, int]]) -> bool:
        """최상위 meta 박스 안에 iloc 박스가 있는지 검사 (HEIF/HEIC 류)."""
        try:
            for offset, atype, size, header_size in atoms:
                if atype != "meta":
                    continue
                with open(filepath, "rb") as f:
                    # meta 는 FullBox 라 4바이트 version+flags 가 header_size 뒤에 있음
                    f.seek(offset + header_size + 4)
                    end = offset + size
                    pos = offset + header_size + 4
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

    @staticmethod
    def _copy(src: str, dst: str, size: int, ctrl,
              on_progress: Callable[[float, float, float], None]) -> bool:
        """단순 복사 (이미 faststart 인 경우). 취소되면 False."""
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
class MP4Reader:
    """진단용 헬퍼 (MP4Parser 와 별도 namespace)"""
    @staticmethod
    def iter_top_atoms(mm, end_pos: int):
        pos = 0
        while pos + 8 <= end_pos:
            try:
                size = struct.unpack(">I", mm[pos:pos+4])[0]
                atype = bytes(mm[pos+4:pos+8]).decode("ascii", errors="ignore")
            except Exception:
                return
            hs = 8
            if size == 1:
                if pos + 16 > end_pos: return
                size = struct.unpack(">Q", mm[pos+8:pos+16])[0]
                hs = 16
            elif size == 0:
                size = end_pos - pos
            if size < 8 or pos + size > end_pos:
                return
            yield (pos, atype, size, hs)
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
            hs = 8
            if size == 1:
                if pos + 16 > end: return None
                size = struct.unpack(">Q", data[pos+8:pos+16])[0]
                hs = 16
            if size < 8 or pos + size > end:
                return None
            if atype == target:
                return (pos, size, hs)
            if atype in recurse_into:
                found = MP4Reader.find_box(data, pos + hs, pos + size, target, recurse_into)
                if found: return found
            pos += size
        return None


class MP4Diagnostics:
    """파일 stts/ctts/mvhd/mdhd/elst 등을 파싱해 VFR / B-프레임 / 싱크 진단"""

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
    def _analyze_mmap(mm, size, out):
        moov_pos = -1; mdat_pos = -1
        mdat_count = 0; moov_count = 0
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
            out['mvhd']   = MP4Diagnostics._parse_mvhd(mm, moov_pos, moov_size)
            out['tracks'] = MP4Diagnostics._parse_tracks(mm, moov_pos, moov_size)

    @staticmethod
    def _parse_mvhd(mm, moov_offset, moov_size):
        mvhd = MP4Reader.find_box(mm, moov_offset + 8, moov_offset + moov_size, "mvhd")
        if not mvhd: return None
        try:
            v = mm[mvhd[0] + mvhd[2]]
            if v == 1:
                ts = mvhd[0] + mvhd[2] + 4 + 8 + 8
                timescale = struct.unpack(">I", mm[ts:ts+4])[0]
                duration  = struct.unpack(">Q", mm[ts+4:ts+12])[0]
            else:
                ts = mvhd[0] + mvhd[2] + 4 + 4 + 4
                timescale = struct.unpack(">I", mm[ts:ts+4])[0]
                duration  = struct.unpack(">I", mm[ts+4:ts+8])[0]
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
            if size < 8 or pos + size > end: break
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
        tkhd = MP4Reader.find_box(mm, pos + hs, trak_end, "tkhd")
        if tkhd:
            try:
                wh_pos = tkhd[0] + tkhd[1] - 8
                w = struct.unpack(">I", mm[wh_pos:wh_pos+4])[0]
                h = struct.unpack(">I", mm[wh_pos+4:wh_pos+8])[0]
                info['width']  = w >> 16
                info['height'] = h >> 16
            except Exception:
                pass
        mdia = MP4Reader.find_box(mm, pos + hs, trak_end, "mdia")
        if not mdia: return info
        mdhd = MP4Reader.find_box(mm, mdia[0] + mdia[2], mdia[0] + mdia[1], "mdhd")
        if mdhd:
            try:
                v = mm[mdhd[0] + mdhd[2]]
                if v == 1:
                    ts = mdhd[0] + mdhd[2] + 4 + 8 + 8
                    info['timescale'] = struct.unpack(">I", mm[ts:ts+4])[0]
                    info['duration']  = struct.unpack(">Q", mm[ts+4:ts+12])[0]
                else:
                    ts = mdhd[0] + mdhd[2] + 4 + 4 + 4
                    info['timescale'] = struct.unpack(">I", mm[ts:ts+4])[0]
                    info['duration']  = struct.unpack(">I", mm[ts+4:ts+8])[0]
            except Exception:
                pass
        hdlr = MP4Reader.find_box(mm, mdia[0] + mdia[2], mdia[0] + mdia[1], "hdlr")
        if hdlr:
            try:
                ht = mm[hdlr[0] + hdlr[2] + 4 + 4: hdlr[0] + hdlr[2] + 4 + 8].decode("ascii", errors="ignore")
                info['type'] = {'vide': 'video', 'soun': 'audio', 'subt': 'subtitle',
                                'text': 'text', 'hint': 'hint'}.get(ht, ht)
            except Exception:
                pass
        minf = MP4Reader.find_box(mm, mdia[0] + mdia[2], mdia[0] + mdia[1], "minf")
        if not minf: return info
        stbl = MP4Reader.find_box(mm, minf[0] + minf[2], minf[0] + minf[1], "stbl")
        if not stbl: return info
        stsd = MP4Reader.find_box(mm, stbl[0] + stbl[2], stbl[0] + stbl[1], "stsd")
        if stsd:
            try:
                ep = stsd[0] + stsd[2] + 4 + 4
                if ep + 8 <= stsd[0] + stsd[1]:
                    info['codec'] = mm[ep+4:ep+8].decode("ascii", errors="ignore").strip("\0")
            except Exception:
                pass
        stts = MP4Reader.find_box(mm, stbl[0] + stbl[2], stbl[0] + stbl[1], "stts")
        if stts:
            try:
                cpos = stts[0] + stts[2] + 4
                count = struct.unpack(">I", mm[cpos:cpos+4])[0]
                info['stts_total'] = count
                espos = cpos + 4
                distrib = {}; total_samples = 0; total_duration = 0
                for i in range(count):
                    ep = espos + i * 8
                    if ep + 8 > stts[0] + stts[1]: break
                    sc = struct.unpack(">I", mm[ep:ep+4])[0]
                    sd = struct.unpack(">I", mm[ep+4:ep+8])[0]
                    if i < 50:
                        info['stts_entries'].append((sc, sd))
                    distrib[sd] = distrib.get(sd, 0) + sc
                    total_samples  += sc
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
        ctts = MP4Reader.find_box(mm, stbl[0] + stbl[2], stbl[0] + stbl[1], "ctts")
        if ctts:
            try:
                cpos = ctts[0] + ctts[2] + 4
                count = struct.unpack(">I", mm[cpos:cpos+4])[0]
                info['ctts_total'] = count
                info['has_b_frames'] = count > 0
            except Exception:
                pass
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
            for e in r['errors']: L.append(f"  ✗ {e}")
            return "\n".join(L)
        L.append("")
        L.append("[기본 정보]")
        L.append(f"  • 파일 크기   : {r['size']:,} bytes ({r['size']/1024/1024:.2f} MB)")
        L.append(f"  • Top atoms   : {len(r['top_atoms'])}개 ({', '.join(a['type'] for a in r['top_atoms'][:8])}{'...' if len(r['top_atoms']) > 8 else ''})")
        L.append(f"  • FastStart   : {'✓ 적용됨' if r['is_faststart'] else '✗ 미적용'}")
        if r['is_fragmented']: L.append(f"  • Fragmented  : ⚠ moof 감지 (NativeFastStart 거부)")
        if r['multi_mdat']:    L.append(f"  • multi-mdat  : ⚠ 다중 mdat (NativeFastStart 거부)")
        if r['multi_moov']:    L.append(f"  • multi-moov  : ⚠ 다중 moov (NativeFastStart 거부)")
        if r['mvhd']:
            m = r['mvhd']
            L.append("")
            L.append("[Movie Header (mvhd)]")
            L.append(f"  • 영화 timescale : {m['timescale']} Hz")
            L.append(f"  • 영화 duration  : {m['duration']} ticks ({m['duration_sec']:.3f} 초)")
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
        L.append("")
        L.append("[종합 진단]")
        warnings = []
        for t in r['tracks']:
            if t['type'] == 'video' and not t['is_cfr'] and t['stts_total'] > 0:
                warnings.append(
                    "⚠ 비디오 트랙이 VFR (가변 프레임 레이트) 입니다.\n"
                    "    → frame duration 이 일정하지 않아 미세한 jitter / 떨림으로 인지될 수 있습니다.\n"
                    "    → NativeFastStart 는 원본 stts 를 그대로 보존하므로 이 특성이 출력에도 유지됩니다.\n"
                    "    → 매끄럽게 만들려면 비디오 재인코딩 (PotEncoder / FFmpeg trans-code) 이 필요합니다."
                )
            if t['type'] == 'audio' and len(t['unique_durations']) > 1:
                warnings.append("ℹ 오디오 sample duration 이 변동합니다 (드물게 보임). 일반적으로 재생에 영향 없음.")
            if t['elst_entries']:
                first = t['elst_entries'][0]
                if first[1] not in (0, -1):
                    sec = first[1] / t['timescale'] if t['timescale'] else 0
                    warnings.append(
                        f"ℹ {t['type']} 트랙에 media_time={first[1]} ({sec:+.3f}초) 의 elst 가 존재.\n"
                        f"    → 이 트랙은 시작 시 {sec:+.3f}초 offset 으로 재생됨 (의도된 싱크 보정일 수 있음)."
                    )
        if r['is_fragmented']: warnings.append("⚠ Fragmented MP4 - NativeFastStart 거부.")
        if r['multi_mdat']:    warnings.append("⚠ multi-mdat - NativeFastStart 거부.")
        if r['multi_moov']:    warnings.append("⚠ multi-moov - NativeFastStart 거부.")
        if not warnings:
            L.append("  ✓ 특이사항 없음. NativeFastStart 결과는 원본과 byte-perfect 한 timing 을 유지합니다.")
        else:
            for w in warnings: L.append(f"  {w}")
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
#  GPU / 하드웨어 가속 감지
# =====================================================================
class HWDetector:
    @staticmethod
    def detect(ffmpeg_path: str) -> Tuple[str, List[str], int]:
        """반환: (vendor, available_codecs, cpu_cores)"""
        vendor = "none"
        codecs = []
        cpu_cores = psutil.cpu_count(logical=True) or 1

        if platform.system() == "Windows":
            try:
                import winreg
                for i in range(8):
                    try:
                        key_path = (rf"SYSTEM\CurrentControlSet\Control\Class"
                                    rf"\{{4d36e968-e325-11ce-bfc1-08002be10318}}\{i:04d}")
                        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path)
                        try:
                            desc = winreg.QueryValueEx(key, "DriverDesc")[0].upper()
                        finally:
                            winreg.CloseKey(key)
                        if "NVIDIA" in desc:
                            vendor = "nvidia"; break
                        if "INTEL" in desc:
                            vendor = "intel"
                        elif "AMD" in desc or "RADEON" in desc:
                            if vendor == "none":
                                vendor = "amd"
                    except (FileNotFoundError, OSError):
                        continue
                    except Exception:
                        continue
            except Exception as e:
                logger.warning(f"GPU 감지 실패: {e}")

        try:
            res = subprocess.run([ffmpeg_path, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, encoding="utf-8",
                                 errors="ignore", timeout=10,
                                 startupinfo=STARTUPINFO, creationflags=CREATE_FLAGS)
            if res.returncode == 0:
                out = res.stdout.lower()
                if "h264_nvenc" in out: codecs.append("nvidia")
                if "h264_qsv"   in out: codecs.append("intel")
                if "h264_amf"   in out: codecs.append("amd")
        except Exception as e:
            logger.warning(f"FFmpeg 인코더 감지 실패: {e}")
        return vendor, codecs, cpu_cores

    @staticmethod
    def best_match(vendor: str, codecs: List[str]) -> str:
        if vendor in codecs:
            return vendor
        return "none"


# =====================================================================
#  처리 컨트롤러 (Pause / Resume / Cancel)
# =====================================================================
class ProcessController(QObject):
    """전역 처리 상태. 모든 워커가 공유하여 일시정지/취소 신호를 본다."""
    state_changed = pyqtSignal(str)  # "running"/"paused"/"cancelled"/"idle"

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._paused = threading.Event()
        self._paused.set()       # set = NOT paused (실행 중)
        self._cancelled = False
        self._active_pids: Dict[int, psutil.Process] = {}

    # 외부 제어
    def pause(self):
        with self._lock:
            self._paused.clear()
            for proc in list(self._active_pids.values()):
                self._safe_suspend(proc)
        self.state_changed.emit("paused")
        logger.info("처리 일시정지")

    def resume(self):
        with self._lock:
            self._paused.set()
            for proc in list(self._active_pids.values()):
                self._safe_resume(proc)
        self.state_changed.emit("running")
        logger.info("처리 재개")

    def cancel(self):
        with self._lock:
            self._cancelled = True
            self._paused.set()  # 대기 중 워커 깨우기
            for proc in list(self._active_pids.values()):
                self._kill_tree(proc)
        self.state_changed.emit("cancelled")
        logger.info("처리 취소")

    def reset(self):
        with self._lock:
            self._cancelled = False
            self._paused.set()
            self._active_pids.clear()
        self.state_changed.emit("idle")

    # 워커가 사용
    def is_cancelled(self) -> bool:
        return self._cancelled

    def wait_if_paused(self, timeout: float = 0.5) -> None:
        # cancellation 도 즉시 통과되도록 짧게 폴링
        while not self._paused.wait(timeout):
            if self._cancelled:
                return

    def register(self, proc: psutil.Process):
        with self._lock:
            self._active_pids[proc.pid] = proc
            if not self._paused.is_set():
                self._safe_suspend(proc)

    def unregister(self, pid: int):
        with self._lock:
            self._active_pids.pop(pid, None)

    @staticmethod
    def _safe_suspend(proc: psutil.Process):
        try:
            proc.suspend()
            for c in proc.children(recursive=True):
                try: c.suspend()
                except Exception: pass
        except Exception:
            pass

    @staticmethod
    def _safe_resume(proc: psutil.Process):
        try:
            proc.resume()
            for c in proc.children(recursive=True):
                try: c.resume()
                except Exception: pass
        except Exception:
            pass

    @staticmethod
    def _kill_tree(proc: psutil.Process):
        try:
            for c in proc.children(recursive=True):
                try: c.kill()
                except Exception: pass
            proc.kill()
        except Exception:
            pass


# =====================================================================
#  엔진 래퍼 (FFmpeg / MP4Box) - 실시간 진행률
# =====================================================================
class EngineRunner:
    """단일 파일 처리. 실시간 진행률 콜백 / pause·cancel / 타임아웃 지원."""

    PROGRESS_RE_FFMPEG = re.compile(r"^([a-zA-Z_]+)=(.+)$")
    # MP4Box 진행률: "45%" 또는 "(45/100)" 두 형식 모두 지원
    PROGRESS_RE_MP4BOX = re.compile(r"(\d+(?:\.\d+)?)\s*%|\((\d+)/(\d+)\)")

    HW_ENCODERS = {"nvidia": "h264_nvenc", "amd": "h264_amf", "intel": "h264_qsv"}

    def __init__(self, controller: ProcessController, ffmpeg_path: str,
                 mp4box_path: str, ffprobe_path: str, timeout: int):
        self.ctrl = controller
        self.ffmpeg = ffmpeg_path
        self.mp4box = mp4box_path
        self.ffprobe = ffprobe_path
        self.timeout = timeout

    # --------------- FFmpeg ---------------
    def run_ffmpeg(self, input_path: str, output_path: str,
                   duration: float, hw_accel: str,
                   on_progress: Callable[[float, float, float], None],
                   on_log: Callable[[str], None]) -> Tuple[bool, str]:
        """faststart 처리. on_progress(percent, eta_sec, speed)"""
        # 입력단: PTS 정상화 (싱크 보존)
        cmd = [self.ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
               "-fflags", "+genpts",
               "-thread_queue_size", "1024", "-i", input_path]
        # 모든 스트림/메타 보존
        cmd += ["-map", "0", "-map_metadata", "0"]

        encoder = self.HW_ENCODERS.get(hw_accel)
        if encoder:
            # GPU 트랜스코드: 비디오만 재인코딩, 오디오/자막/데이터 스트림은 그대로 복사.
            # -vsync 는 최신 FFmpeg 에서 제거됨 → -fps_mode 사용 (프레임 타이밍 그대로 통과)
            cmd += ["-c", "copy", "-c:v", encoder, "-fps_mode", "passthrough"]
        else:
            # 카피 모드: 원본 타임스탬프 그대로 복사
            cmd += ["-c", "copy", "-copyts"]

        cmd += ["-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                "-f", "mp4",
                "-progress", "pipe:1", "-nostats",
                output_path]

        on_log(f"$ {' '.join(self._shquote(c) for c in cmd)}")

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=1, universal_newlines=True, encoding="utf-8",
                errors="ignore", startupinfo=STARTUPINFO, creationflags=CREATE_FLAGS,
            )
        except FileNotFoundError as e:
            return False, f"실행파일 없음: {e}"
        except Exception as e:
            return False, f"FFmpeg 실행 실패: {e}"

        ps_proc = self._register(proc)

        # stderr 스레드 (에러 수집)
        err_lines: List[str] = []
        def drain_stderr():
            try:
                if proc.stderr:
                    for line in proc.stderr:
                        line = line.rstrip()
                        if line:
                            err_lines.append(line)
                            on_log(f"  ffmpeg: {line}")
            except Exception:
                pass
        et = threading.Thread(target=drain_stderr, daemon=True)
        et.start()

        lines = self._start_line_reader(proc.stdout)
        clock = _ActiveClock()
        last_percent = 0.0
        speed = 0.0

        try:
            while True:
                if self.ctrl.is_cancelled():
                    self._terminate(proc)
                    return False, "취소됨"
                clock.pause_while(self.ctrl)

                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    # 출력이 없더라도 타임아웃은 반드시 검사 (FFmpeg 행(hang) 대비)
                    if self.timeout and clock.elapsed() > self.timeout:
                        self._terminate(proc)
                        return False, "타임아웃"
                    continue
                if line is None:      # stdout EOF = 프로세스 종료
                    break
                m = self.PROGRESS_RE_FFMPEG.match(line.strip())
                if not m:
                    continue
                k, v = m.group(1), m.group(2)
                if k == "out_time_us":
                    try:
                        cur = int(v) / 1_000_000.0
                    except ValueError:
                        continue      # "N/A" 등
                    if duration > 0 and cur >= 0:
                        pct = max(0.0, min(99.5, cur / duration * 100.0))
                        elapsed = max(0.001, clock.elapsed())
                        speed = cur / elapsed
                        eta = (duration - cur) / speed if speed > 0 else 0
                        if pct - last_percent >= 0.5 or pct >= 99:
                            on_progress(pct, eta, speed)
                            last_percent = pct
                elif k == "progress" and v.strip() == "end":
                    break

            # 종료 대기
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._terminate(proc)
                return False, "프로세스 종료 대기 초과"

            et.join(timeout=2)
            if proc.returncode != 0:
                tail = "\n".join(err_lines[-5:]) if err_lines else "(no stderr)"
                return False, f"FFmpeg 오류 (rc={proc.returncode})\n{tail}"

            on_progress(100.0, 0, speed)
            return True, "OK"
        finally:
            if ps_proc is not None:
                self.ctrl.unregister(proc.pid)

    # --------------- MP4Box ---------------
    def run_mp4box(self, input_path: str, output_path: str,
                   duration: float,
                   on_progress: Callable[[float, float, float], None],
                   on_log: Callable[[str], None]) -> Tuple[bool, str]:
        # 싱크 보존을 위한 옵션:
        #   -inter 500  : 500ms 단위로 A/V 청크 인터리브 (싱크/스트리밍 안정성)
        #   -mtu 1500   : 표준 RTP MTU 로 packetization 안정화
        #   -rate 90000 : 표준 RTP 클럭 rate
        cmd = [self.mp4box,
               "-inter", "500",
               "-mtu", "1500",
               "-rate", "90000",
               "-hint", input_path,
               "-out", output_path]
        on_log(f"$ {' '.join(self._shquote(c) for c in cmd)}")

        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1, universal_newlines=True, encoding="utf-8",
                errors="ignore", startupinfo=STARTUPINFO, creationflags=CREATE_FLAGS,
            )
        except FileNotFoundError as e:
            return False, f"실행파일 없음: {e}"
        except Exception as e:
            return False, f"MP4Box 실행 실패: {e}"

        ps_proc = self._register(proc)
        lines = self._start_line_reader(proc.stdout)
        clock = _ActiveClock()
        out_buf: List[str] = []
        last_pct = 0.0
        last_logged = ""

        try:
            while True:
                if self.ctrl.is_cancelled():
                    self._terminate(proc)
                    return False, "취소됨"
                clock.pause_while(self.ctrl)

                try:
                    line = lines.get(timeout=0.5)
                except queue.Empty:
                    if self.timeout and clock.elapsed() > self.timeout:
                        self._terminate(proc)
                        return False, "타임아웃"
                    continue
                if line is None:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                m = self.PROGRESS_RE_MP4BOX.search(stripped)
                if m:
                    try:
                        if m.group(1) is not None:
                            pct = float(m.group(1))
                        else:
                            total = float(m.group(3)) or 100.0
                            pct = float(m.group(2)) / total * 100.0
                    except (TypeError, ValueError, ZeroDivisionError):
                        pct = -1
                    if pct > last_pct:
                        elapsed = max(0.001, clock.elapsed())
                        speed = pct / elapsed
                        eta = (100 - pct) / speed if speed > 0 else 0
                        on_progress(min(pct, 99.5), eta, 0)
                        last_pct = pct
                    continue          # 진행률 줄은 로그에 쌓지 않음 (스팸 방지)
                out_buf.append(stripped)
                if stripped != last_logged:
                    on_log(f"  mp4box: {stripped}")
                    last_logged = stripped

            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._terminate(proc)
                return False, "프로세스 종료 대기 초과"

            if proc.returncode != 0:
                tail = "\n".join(out_buf[-5:]) if out_buf else "(no output)"
                return False, f"MP4Box 오류 (rc={proc.returncode})\n{tail}"

            on_progress(100.0, 0, 0)
            return True, "OK"
        finally:
            if ps_proc is not None:
                self.ctrl.unregister(proc.pid)

    # --------------- Helpers ---------------
    def _register(self, proc: subprocess.Popen) -> Optional[psutil.Process]:
        try:
            ps_proc = psutil.Process(proc.pid)
            self.ctrl.register(ps_proc)
            return ps_proc
        except Exception:
            return None

    @staticmethod
    def _start_line_reader(stream) -> "queue.Queue[Optional[str]]":
        """별도 스레드에서 줄 단위로 읽어 큐에 넣는다. EOF 시 None.
        (Windows 는 파이프 select 가 안 되어 readline 이 블로킹 → 기존 방식은
         FFmpeg 가 멈추면 타임아웃 검사가 영영 실행되지 않던 문제 수정)"""
        q: "queue.Queue[Optional[str]]" = queue.Queue()

        def _pump():
            try:
                if stream is not None:
                    for line in iter(stream.readline, ""):
                        q.put(line)
            except Exception:
                pass
            finally:
                q.put(None)

        threading.Thread(target=_pump, daemon=True).start()
        return q

    @staticmethod
    def _shquote(s: str) -> str:
        if " " in s or "\t" in s:
            return f'"{s}"'
        return s

    @staticmethod
    def _terminate(proc: subprocess.Popen):
        try:
            ps = psutil.Process(proc.pid)
            for c in ps.children(recursive=True):
                try: c.kill()
                except Exception: pass
            ps.kill()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


class _ActiveClock:
    """일시정지 시간을 뺀 '실제 처리 시간' 측정 (속도/ETA/타임아웃 계산용)."""

    def __init__(self):
        self._start = time.monotonic()
        self._paused_total = 0.0

    def pause_while(self, ctrl) -> None:
        t = time.monotonic()
        ctrl.wait_if_paused(0.2)
        waited = time.monotonic() - t
        if waited > 0.05:
            self._paused_total += waited

    def elapsed(self) -> float:
        return time.monotonic() - self._start - self._paused_total


# =====================================================================
#  Worker  (단일 파일 처리)
# =====================================================================
# 작업(QRunnable)이 끝나며 신호 객체가 먼저 지워지면 아직 전달되지 않은 신호가
# 사라질 수 있음 (PySide6 에서 실제로 발생). 마지막 신호 뒤 2초간 참조를 붙잡아 둔다.
_ALIVE_SIGNALS: set = set()


class WorkerSignals(QObject):
    state    = pyqtSignal(str, str)         # (path, TaskState.value)
    progress = pyqtSignal(str, float, float, float)  # (path, pct, eta, speed)
    log      = pyqtSignal(str)
    finished = pyqtSignal(str, bool, str)   # (path, success, msg)

    def __init__(self, last: str = "finished"):
        super().__init__()
        _ALIVE_SIGNALS.add(self)
        getattr(self, last).connect(self._release)

    def _release(self, *_args):
        QTimer.singleShot(2000, lambda: _ALIVE_SIGNALS.discard(self))


TEMP_PREFIX = ".mp4hb_"
TEMP_SUFFIX = ".tmp.mp4"


def mp4_valid_end(path: str) -> int:
    """정상적으로 파싱되는 최상위 atom 들이 끝나는 위치.
    파일 크기보다 작으면 그 뒤는 MP4 블록이 아닌 꼬리 데이터.
    moov/mdat 가 모두 정상 범위에 있을 때만 값을 돌려주고, 아니면 0."""
    try:
        size = os.path.getsize(path)
        end = 0
        seen = set()
        with open(path, "rb") as f:
            with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                for pos, atype, asize, _hs in MP4Reader.iter_top_atoms(mm, size):
                    end = pos + asize
                    seen.add(atype)
        return end if {"moov", "mdat"} <= seen else 0
    except Exception:
        return 0


def copy_prefix(src: str, dst: str, length: int, ctrl) -> bool:
    """src 의 앞 length 바이트만 dst 로 복사. 취소되면 False."""
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        remaining = length
        while remaining > 0:
            if ctrl.is_cancelled():
                return False
            ctrl.wait_if_paused(0.2)
            buf = fin.read(min(8 * 1024 * 1024, remaining))
            if not buf:
                break
            fout.write(buf)
            remaining -= len(buf)
    return True


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
    """단일 MP4 파일을 처리. 직렬 모드는 thread pool size=1 로 사용."""

    def __init__(self, filepath: str, engine: str, hw_accel: str,
                 cfg: AppConfig, ctrl: ProcessController,
                 ffmpeg: str, mp4box: str, ffprobe: str,
                 temp_dir: str, output_path: Optional[str] = None):
        super().__init__()
        self.setAutoDelete(True)
        self.filepath  = filepath
        self.engine    = engine
        self.hw_accel  = hw_accel
        self.cfg       = cfg
        self.ctrl      = ctrl
        self.ffmpeg    = ffmpeg
        self.mp4box    = mp4box
        self.ffprobe   = ffprobe
        self.temp_dir  = temp_dir
        self.output_path = output_path   # 메인창에서 미리 계산한 출력 경로 (이름 충돌 해소 포함)
        self.signals   = WorkerSignals()

    # ------ 출력 경로 결정 ------
    @staticmethod
    def resolve_output_path(filepath: str, cfg: AppConfig) -> str:
        mode = cfg.output_mode
        src = Path(filepath)
        if mode == OutputMode.SEPARATE_DIR.value and cfg.output_dir:
            return str(Path(cfg.output_dir) / src.name)
        if mode == OutputMode.SUFFIX.value:
            suf = cfg.output_suffix or "_hinted"
            return str(src.with_name(src.stem + suf + src.suffix))
        # OVERWRITE
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
            info = MediaProbe.get_info(path, self.ffprobe)
            try:
                src_stat = os.stat(path)
            except OSError:
                src_stat = None
            keep_times = src_stat if self.cfg.preserve_mtime else None

            final_out = self.output_path or self.resolve_output_path(path, self.cfg)
            in_place = os.path.normcase(os.path.abspath(final_out)) == \
                       os.path.normcase(os.path.abspath(path))

            # 이미 처리됨 검사
            already = (self.engine == Engine.FFMPEG.value and info.has_faststart) or \
                      (self.engine == Engine.MP4BOX.value and info.has_hint_track)
            if already:
                if not in_place:
                    # 별도 폴더 / 접미사 모드: 결과물이 빠지지 않도록 그대로 복사
                    os.makedirs(os.path.dirname(os.path.abspath(final_out)), exist_ok=True)
                    shutil.copy2(path, final_out)
                    self.signals.progress.emit(path, 100.0, 0, 0)
                    self.signals.state.emit(path, TaskState.SKIPPED.value)
                    self.signals.finished.emit(path, True, "이미 처리됨 (출력 위치로 복사)")
                    return
                self.signals.state.emit(path, TaskState.SKIPPED.value)
                self.signals.progress.emit(path, 100.0, 0, 0)
                self.signals.finished.emit(path, True, "이미 처리됨")
                return

            temp_out = make_temp_path(final_out, self.temp_dir)

            # 디스크 여유 확인 - 실제로 임시 파일이 만들어지는 드라이브 기준 (원본 크기의 1.2배)
            need = max(int(info.size * 1.2), 64 * 1024 * 1024)
            try:
                free = shutil.disk_usage(os.path.dirname(temp_out)).free
                if free < need:
                    self._fail(path, f"디스크 여유 공간 부족 (필요 {need//1024//1024}MB / "
                                     f"가용 {free//1024//1024}MB)")
                    return
            except Exception:
                pass

            self.signals.state.emit(path, TaskState.PROCESSING.value)

            backup_path = None
            if self.cfg.backup_enabled and in_place:
                backup_dir = os.path.join(user_data_dir(), "backups")
                os.makedirs(backup_dir, exist_ok=True)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_path = os.path.join(backup_dir, f"{ts}_{Path(path).name}")
                try:
                    shutil.copy2(path, backup_path)
                    self.signals.log.emit(f"백업 생성: {backup_path}")
                except Exception as e:
                    self.signals.log.emit(f"백업 실패: {e}")
                    backup_path = None

            runner = EngineRunner(self.ctrl, self.ffmpeg, self.mp4box,
                                  self.ffprobe, self.cfg.timeout)

            def on_progress(pct: float, eta: float, speed: float):
                self.signals.progress.emit(path, pct, eta, speed)

            def on_log(msg: str):
                self.signals.log.emit(msg)

            # ─── 엔진 분기 ───
            # CPU 카피 + FFmpeg + native_faststart 옵션 → 자체 구현(빠름)
            use_native = (
                self.engine == Engine.FFMPEG.value
                and self.hw_accel == "none"
                and self.cfg.native_faststart
            )

            ok = False
            msg = ""
            verified = False
            trimmed_path = ""
            try:
                if use_native:
                    on_log("⚡ Native FastStart 모드 (FFmpeg 우회)")
                    ok, msg = NativeFastStart.process(
                        path, temp_out, self.ctrl,
                        on_progress, on_log,
                        audio_sync_offset_sec=self.cfg.audio_sync_offset_sec,
                        flatten_video_stts=self.cfg.flatten_video_stts)
                    # native 가 성공해도 출력 검증 한 번 더 (불완전 파일 폴백용)
                    if ok and self.cfg.verify_output and os.path.isfile(temp_out):
                        if MediaProbe.verify_output(temp_out, self.ffprobe,
                                                    info.duration):
                            verified = True
                        else:
                            ok = False
                            msg = "native 출력 검증 실패"
                    # native 가 처리할 수 없는 케이스 → FFmpeg 폴백
                    if not ok and not self.ctrl.is_cancelled():
                        on_log(f"  native 실패({msg}) → FFmpeg 폴백")
                        self._cleanup(temp_out)
                        ok, msg = runner.run_ffmpeg(path, temp_out, info.duration,
                                                    self.hw_accel, on_progress, on_log)
                elif self.engine == Engine.FFMPEG.value:
                    ok, msg = runner.run_ffmpeg(path, temp_out, info.duration,
                                                self.hw_accel, on_progress, on_log)
                else:
                    # MP4Box 는 파일 끝의 비표준 꼬리 데이터를 '불완전한 박스'로 보고 열기를 거부함
                    # → 꼬리를 뺀 임시 사본을 만들어 넘긴다 (원본은 건드리지 않음)
                    mp4box_src = path
                    valid_end = mp4_valid_end(path)
                    if valid_end and valid_end < info.size:
                        on_log(f"  파일 끝 비표준 데이터 {info.size - valid_end:,} bytes 감지 "
                               f"→ 제외한 임시 사본으로 MP4Box 실행")
                        trimmed_path = make_temp_path(final_out, self.temp_dir)
                        if not copy_prefix(path, trimmed_path, valid_end, self.ctrl):
                            raise RuntimeError("취소됨")
                        mp4box_src = trimmed_path
                    ok, msg = runner.run_mp4box(mp4box_src, temp_out, info.duration,
                                                on_progress, on_log)
            except Exception as e:
                ok, msg = False, f"엔진 실행 예외: {e}"
            finally:
                self._cleanup(trimmed_path)

            if self.ctrl.is_cancelled():
                self._cleanup(temp_out)
                self.signals.state.emit(path, TaskState.CANCELLED.value)
                self.signals.finished.emit(path, False, "취소됨")
                return

            if not ok or not os.path.isfile(temp_out):
                self._cleanup(temp_out)
                self._fail(path, msg or "처리 실패")
                return

            # 무결성 검증 (native 경로는 위에서 이미 검증했으므로 FFmpeg/MP4Box 결과만)
            if self.cfg.verify_output and not verified:
                if not MediaProbe.verify_output(temp_out, self.ffprobe, info.duration):
                    self._cleanup(temp_out)
                    self._fail(path, "출력 무결성 검증 실패")
                    return

            # atomic replace / move
            try:
                move_into_place(temp_out, final_out)
            except Exception as e:
                self._cleanup(temp_out)
                # 백업 복원 (덮어쓰기 도중 실패해 원본이 손상된 경우 대비)
                if backup_path and os.path.isfile(backup_path):
                    try:
                        shutil.copy2(backup_path, path)
                        self.signals.log.emit("백업에서 복원됨")
                    except Exception as ex:
                        self.signals.log.emit(f"백업 복원 실패: {ex}")
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
    def _cleanup(path: str):
        try:
            if path and os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass


# =====================================================================
#  CheckWorker  (메타 데이터 / 상태 검사)
# =====================================================================
class CheckWorker(QRunnable):
    def __init__(self, filepath: str, engine: str, ffprobe: str):
        super().__init__()
        self.setAutoDelete(True)
        self.filepath = filepath
        self.engine = engine
        self.ffprobe = ffprobe
        self.signals = WorkerSignals(last="state")

    def run(self):
        try:
            info = MediaProbe.get_info(self.filepath, self.ffprobe)
            if self.engine == Engine.FFMPEG.value:
                done = info.has_faststart
            else:
                done = info.has_hint_track
            state = TaskState.SKIPPED.value if done else TaskState.PENDING.value
            # state 시그널 + 메타 정보를 log 로 흘려보냄 (JSON 래핑으로 안전하게)
            payload = json.dumps({
                "path": self.filepath,
                "meta": {
                    "size": info.size, "duration": info.duration,
                    "vc": info.video_codec, "ac": info.audio_codec,
                    "w": info.width, "h": info.height,
                    "fs": info.has_faststart, "ht": info.has_hint_track,
                    "br": info.bit_rate,
                }
            }, ensure_ascii=False)
            self.signals.log.emit(f"__META__{payload}")
            self.signals.state.emit(self.filepath, state)
        except Exception as e:
            self.signals.state.emit(self.filepath, TaskState.FAILED.value)
            self.signals.log.emit(f"체크 실패 ({self.filepath}): {e}")


# =====================================================================
#  ProgressDelegate - 트리뷰 셀에 진행률 바 그리기
# =====================================================================
class ProgressDelegate(QStyledItemDelegate):
    """행 높이에 맞춰 진행률바 + 작은 텍스트를 직접 렌더링"""
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

        # 배경 트랙
        painter.setPen(QPen(QColor(60, 62, 66), 1))
        painter.setBrush(QBrush(QColor(31, 33, 37)))
        painter.drawRoundedRect(rect, 3, 3)

        # 채워진 부분 (오렌지 그라데이션)
        pct = max(0, min(100, int(progress)))
        if pct > 0:
            fill_w = max(0, int(rect.width() * pct / 100.0))
            fill_rect = QRect(rect.x(), rect.y(), fill_w, rect.height())
            # 진행 중: #FB8C00 (orange 600), 완료: #F57C00 (orange 700)
            grad = QLinearGradient(fill_rect.topLeft(), fill_rect.bottomLeft())
            if pct < 100:
                grad.setColorAt(0.0, QColor(255, 167, 38))   # #FFA726
                grad.setColorAt(1.0, QColor(251, 140, 0))    # #FB8C00
            else:
                grad.setColorAt(0.0, QColor(251, 140, 0))    # #FB8C00
                grad.setColorAt(1.0, QColor(230, 81,  0))    # #E65100
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(grad))
            painter.drawRoundedRect(fill_rect, 3, 3)

        # 텍스트 (행 높이에 맞춰 폰트 자동 축소)
        max_h = rect.height()
        # 약 70% 정도가 폰트 픽셀 크기로 적당
        target_px = max(8, min(13, int(max_h * 0.72)))
        f = painter.font()
        f.setPixelSize(target_px)
        f.setBold(False)
        painter.setFont(f)
        painter.setPen(QColor(232, 234, 237))
        painter.drawText(rect, Qt.AlignCenter, f"{pct}%")

        painter.restore()

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        # 진행률 바가 충분히 보이도록 최소 높이 보장
        if s.height() < 22:
            s.setHeight(22)
        return s


# =====================================================================
#  드래그&드롭 트리위젯
# =====================================================================
class DnDTreeWidget(QTreeWidget):
    files_dropped = pyqtSignal(list)  # list[str] 경로 (파일/폴더 모두 가능)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self._drag_active = False

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
        self._drag_active = False
        self.viewport().update()
        e.accept()

    def dropEvent(self, e):
        self._drag_active = False
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
        if self._drag_active:
            p = QPainter(self.viewport())
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QPen(QColor(0, 122, 204, 200), 3, Qt.DashLine))
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
#  설정 다이얼로그
# =====================================================================
class SettingsDialog(QDialog):
    def __init__(self, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("설정")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)

        # ---- 출력 모드 ----
        gb_out = QGroupBox("출력 동작")
        ol = QGridLayout(gb_out)

        self.rb_overwrite = QRadioButton("원본 덮어쓰기 (atomic replace, 권장)")
        self.rb_separate  = QRadioButton("별도 폴더 출력")
        self.rb_suffix    = QRadioButton("같은 폴더 + 접미사")

        bg = QButtonGroup(self)
        bg.addButton(self.rb_overwrite); bg.addButton(self.rb_separate); bg.addButton(self.rb_suffix)
        ol.addWidget(self.rb_overwrite, 0, 0, 1, 3)
        ol.addWidget(self.rb_separate,  1, 0, 1, 3)

        self.le_outdir = QLineEdit(cfg.output_dir)
        self.btn_browse = QPushButton("폴더 선택...")
        self.btn_browse.clicked.connect(self._browse_outdir)
        ol.addWidget(QLabel("    출력 폴더:"), 2, 0)
        ol.addWidget(self.le_outdir, 2, 1)
        ol.addWidget(self.btn_browse, 2, 2)

        ol.addWidget(self.rb_suffix, 3, 0, 1, 3)
        self.le_suffix = QLineEdit(cfg.output_suffix)
        ol.addWidget(QLabel("    접미사:"), 4, 0)
        ol.addWidget(self.le_suffix, 4, 1, 1, 2)

        layout.addWidget(gb_out)

        # ---- 처리 옵션 ----
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

        self.sp_timeout = QSpinBox()
        self.sp_timeout.setRange(60, 86400)
        self.sp_timeout.setSuffix("  초")
        self.sp_timeout.setValue(cfg.timeout)
        pl.addRow("파일별 타임아웃:", self.sp_timeout)

        self.chk_verify = QCheckBox("출력 무결성 검증 (ffprobe)")
        self.chk_verify.setChecked(cfg.verify_output)
        pl.addRow(self.chk_verify)

        self.chk_backup = QCheckBox("처리 전 백업 생성")
        self.chk_backup.setChecked(cfg.backup_enabled)
        pl.addRow(self.chk_backup)

        self.chk_mtime = QCheckBox("원본 파일의 수정 날짜 유지")
        self.chk_mtime.setChecked(cfg.preserve_mtime)
        self.chk_mtime.setToolTip("처리 후에도 탐색기의 '수정한 날짜'가 원본과 같게 유지됩니다.")
        pl.addRow(self.chk_mtime)

        self.chk_hw = QCheckBox("하드웨어 가속 사용 (GPU 트랜스코드)")
        self.chk_hw.setChecked(cfg.hw_accel_enabled)
        pl.addRow(self.chk_hw)

        self.chk_native = QCheckBox("Native FastStart 사용 (FFmpeg 우회 - 2~5배 빠름)")
        self.chk_native.setChecked(cfg.native_faststart)
        self.chk_native.setToolTip(
            "CPU 카피 모드의 faststart 처리 시 FFmpeg 대신\n"
            "moov atom 을 직접 재배치하여 처리 속도를 2~5배로 높입니다.\n\n"
            "안전성 검사:\n"
            "  • 단일 mdat / 비-fragmented MP4 만 처리\n"
            "  • multi-mdat / moof / iloc 등 특수 구조는 자동으로 FFmpeg 폴백\n\n"
            "GPU 트랜스코드 또는 MP4Box 엔진에서는 자동 비활성.")
        pl.addRow(self.chk_native)

        # 오디오 싱크 오프셋 (Native 처리 시에만 적용)
        self.sp_audio_offset = QDoubleSpinBox()
        self.sp_audio_offset.setRange(0.000, 5.000)
        self.sp_audio_offset.setSingleStep(0.05)
        self.sp_audio_offset.setDecimals(3)
        self.sp_audio_offset.setSuffix("  초")
        self.sp_audio_offset.setValue(float(cfg.audio_sync_offset_sec))
        self.sp_audio_offset.setToolTip(
            "【권장: 0.000】\n\n"
            "NativeFastStart 는 stts/ctts/elst/mvhd 등 모든 타이밍 메타를\n"
            "그대로 보존하므로 일반적으로 보정이 필요 없습니다.\n\n"
            "특정 파일에서만 일관된 싱크 드리프트가 보일 때\n"
            "양수 값을 입력하면 audio elst.media_time 을\n"
            "지정 초만큼 앞당겨 보정합니다. (FFmpeg/MP4Box 에는 미적용)\n\n"
            "값을 잘못 설정하면 오히려 정상 파일의 싱크를 깨뜨릴 수 있으니\n"
            "특별한 이유가 없다면 0 으로 두세요.")
        pl.addRow("오디오 싱크 보정:", self.sp_audio_offset)

        self.chk_flatten = QCheckBox("비디오 stts 평탄화 (VFR → CFR, 떨림 제거)")
        self.chk_flatten.setChecked(cfg.flatten_video_stts)
        self.chk_flatten.setToolTip(
            "비디오 stts 의 모든 sample_delta 를 평균값으로 일괄 변경하여\n"
            "VFR (가변 프레임 듀레이션) 을 의사 CFR 로 만듭니다.\n\n"
            "효과: 990↔1020 ticks 식으로 교대하는 미세 jitter 제거\n"
            "방식: 박스 크기 변경 없이 in-place 수정 (재인코딩 X, 매우 빠름)\n"
            "부작용: 총 duration 미세 변경 (~10ms 내외)\n\n"
            "원본이 이미 CFR 이면 자동 스킵됩니다.\n"
            "WEB-DL / 방송 녹화 등 NTSC 29.97 fps VFR 파일에 권장.\n"
            "Native FastStart 사용 시에만 적용됩니다 (FFmpeg/MP4Box 모드 미적용).")
        pl.addRow(self.chk_flatten)

        layout.addWidget(gb_proc)

        # ---- 외관 ----
        gb_ui = QGroupBox("외관")
        ul = QFormLayout(gb_ui)
        self.cb_theme = QComboBox()
        self.cb_theme.addItems(["다크", "라이트"])
        self.cb_theme.setCurrentIndex(0 if cfg.theme == "dark" else 1)
        ul.addRow("테마:", self.cb_theme)
        layout.addWidget(gb_ui)

        # ---- 버튼 ----
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        layout.addWidget(bb)

        # 초기 라디오 상태
        if cfg.output_mode == OutputMode.SEPARATE_DIR.value:
            self.rb_separate.setChecked(True)
        elif cfg.output_mode == OutputMode.SUFFIX.value:
            self.rb_suffix.setChecked(True)
        else:
            self.rb_overwrite.setChecked(True)

    def _browse_outdir(self):
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
        cfg.output_dir       = self.le_outdir.text().strip()
        cfg.output_suffix    = self.le_suffix.text().strip() or "_hinted"
        cfg.process_mode     = "serial" if self.cb_mode.currentIndex() == 0 else "parallel"
        cfg.max_parallel     = self.sp_parallel.value()
        cfg.timeout          = self.sp_timeout.value()
        cfg.verify_output    = self.chk_verify.isChecked()
        cfg.backup_enabled   = self.chk_backup.isChecked()
        cfg.preserve_mtime   = self.chk_mtime.isChecked()
        cfg.hw_accel_enabled = self.chk_hw.isChecked()
        cfg.native_faststart = self.chk_native.isChecked()
        cfg.audio_sync_offset_sec = float(self.sp_audio_offset.value())
        cfg.flatten_video_stts = self.chk_flatten.isChecked()
        cfg.theme            = "dark" if self.cb_theme.currentIndex() == 0 else "light"


# =====================================================================
#  메인 윈도우
# =====================================================================
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = SettingsManager()
        self.cfg = self.settings.cfg

        # 임시 디렉터리
        self.temp_dir = os.path.join(user_data_dir(), "temp")
        os.makedirs(self.temp_dir, exist_ok=True)
        self._cleanup_orphan_temp()

        # 외부 도구 경로 (PATH → bundled)
        self.ffmpeg_path  = find_executable("ffmpeg",  "ffmpeg.exe")
        self.ffprobe_path = find_executable("ffprobe", "ffprobe.exe")
        self.mp4box_path  = find_executable("MP4Box",  "mp4box.exe")

        # GPU
        self.hw_vendor, self.hw_codecs, self.cpu_cores = HWDetector.detect(self.ffmpeg_path)
        self.hw_best = HWDetector.best_match(self.hw_vendor, self.hw_codecs)

        # 처리 컨트롤러
        self.controller = ProcessController()
        self.pool = QThreadPool()
        self._update_pool_size()
        # 메타 정보 검사는 별도 풀 → 직렬 처리 중에도 목록 정보가 바로 채워짐
        self.check_pool = QThreadPool()
        self.check_pool.setMaxThreadCount(max(2, min(4, (os.cpu_count() or 2))))
        # 경로 → 트리 항목 (진행률 시그널마다 전체 목록을 훑던 O(n) 탐색 제거)
        self._items: Dict[str, QTreeWidgetItem] = {}

        # 진행 카운터
        self._mutex = QMutex()
        self._total = 0
        self._done = 0
        self._success = 0
        self._fail = 0
        self._processing_active = False

        # 메타데이터 캐시 (path -> MediaInfo)
        self._meta: Dict[str, Dict[str, Any]] = {}

        self._build_ui()
        self._build_menu()
        self._build_shortcuts()
        self._check_dependencies()
        self._apply_status_message()

    # ---------------- UI ----------------
    def _build_ui(self):
        self.setWindowTitle(f"{APP_NAME} v{APP_VERSION}")
        self.resize(1100, 680)

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
        self.btn_proc_sel.clicked.connect(lambda: self._start_processing(selected_only=True))
        self.btn_proc_all.clicked.connect(lambda: self._start_processing(selected_only=False))
        self.btn_pause.clicked.connect(self._on_pause_resume)
        self.btn_cancel.clicked.connect(self._on_cancel)
        self.btn_settings.clicked.connect(self._on_settings)
        v.addLayout(top)

        # ---- 옵션 행: 엔진 / 가속 / 방식 ......... 검색 ----
        opt = QHBoxLayout()
        opt.setSpacing(6)
        opt.addWidget(QLabel("엔진"))
        self.cb_engine = QComboBox()
        self.cb_engine.addItems(["FFmpeg (FastStart)", "MP4Box (Hint Track)"])
        self.cb_engine.setCurrentIndex(0 if self.cfg.engine == Engine.FFMPEG.value else 1)
        self.cb_engine.currentIndexChanged.connect(self._on_engine_changed)
        opt.addWidget(self.cb_engine)

        opt.addSpacing(8)
        opt.addWidget(QLabel("가속"))
        self.cb_hw = QComboBox()
        self.cb_hw.addItems(["CPU (Stream Copy)", "GPU 트랜스코드"])
        self.cb_hw.setCurrentIndex(1 if self.cfg.hw_accel_enabled else 0)
        self.cb_hw.setEnabled(self.hw_best != "none" and self.cfg.engine == Engine.FFMPEG.value)
        self.cb_hw.currentIndexChanged.connect(self._on_hw_changed)
        opt.addWidget(self.cb_hw)

        opt.addSpacing(8)
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

        # ---- 스플리터 ----
        self.splitter = QSplitter(Qt.Vertical)
        sp = self.splitter
        v.addWidget(sp, 1)

        # ---- 파일 트리 ----
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
        self.tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        sp.addWidget(self.tree)

        # ---- 로그 (기본 숨김 - 보기 메뉴에서 토글) ----
        self.log_box = QWidget()
        lv = QVBoxLayout(self.log_box)
        lv.setContentsMargins(0, 4, 0, 0)
        lv.addWidget(QLabel("처리 로그:"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(QFont("Consolas", 9))
        self.log.document().setMaximumBlockCount(5000)
        lv.addWidget(self.log)
        sp.addWidget(self.log_box)
        if self.cfg.show_log:
            sp.setSizes([460, 200])
        else:
            self.log_box.setVisible(False)
            sp.setSizes([1, 0])

        # ---- 하단 진행률 / 상태 ----
        bot = QHBoxLayout()
        self.lbl_progress = QLabel("준비됨")
        self.pbar = QProgressBar()
        self.pbar.setVisible(False)
        self.pbar.setMaximumHeight(18)
        bot.addWidget(self.lbl_progress, 1)
        bot.addWidget(self.pbar, 2)
        v.addLayout(bot)

        # 상태바
        sb = QStatusBar()
        self.setStatusBar(sb)

        self._site_label = QLabel(f'<a href="{APP_WEBSITE}" style="color:#66b2ff;">Official Website</a>')
        self._site_label.setOpenExternalLinks(True)
        sb.addPermanentWidget(self._site_label)
        self.lbl_hw = QLabel()
        sb.addWidget(self.lbl_hw)

    def _build_menu(self):
        mb = self.menuBar()
        m_file = mb.addMenu("&파일")
        a_add  = QAction("파일 추가...", self); a_add.setShortcut("Ctrl+O"); a_add.triggered.connect(self._on_add_files)
        a_dir  = QAction("폴더 추가 (재귀)...", self); a_dir.setShortcut("Ctrl+Shift+O"); a_dir.triggered.connect(self._on_add_dir)
        a_quit = QAction("종료", self); a_quit.setShortcut("Ctrl+Q"); a_quit.triggered.connect(self.close)
        m_file.addAction(a_add); m_file.addAction(a_dir); m_file.addSeparator(); m_file.addAction(a_quit)

        m_run = mb.addMenu("&처리")
        a_run = QAction("전체 처리", self); a_run.setShortcut("F5"); a_run.triggered.connect(lambda: self._start_processing(False))
        a_sel = QAction("선택 처리", self); a_sel.setShortcut("Ctrl+R"); a_sel.triggered.connect(lambda: self._start_processing(True))
        a_pau = QAction("일시정지/재개", self); a_pau.setShortcut("Space"); a_pau.triggered.connect(self._on_pause_resume)
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
        self._sc_delete = QShortcut(QKeySequence("Delete"), self)
        self._sc_delete.activated.connect(self._remove_selected)
        self._sc_select_all = QShortcut(QKeySequence("Ctrl+A"), self.tree)
        self._sc_select_all.activated.connect(self.tree.selectAll)

    def _on_toggle_log(self, checked: bool):
        self.cfg.show_log = checked
        self.settings.save()
        self.log_box.setVisible(checked)
        if checked:
            total_h = max(400, self.splitter.height())
            self.splitter.setSizes([int(total_h * 0.7), int(total_h * 0.3)])
        else:
            self.splitter.setSizes([1, 0])

    # ---------------- 의존성 / 상태 ----------------
    def _check_dependencies(self):
        missing = []
        if not (shutil.which(self.ffmpeg_path) or os.path.isfile(self.ffmpeg_path)):
            missing.append("FFmpeg")
        if not (shutil.which(self.mp4box_path) or os.path.isfile(self.mp4box_path)):
            missing.append("MP4Box")
        if missing:
            QMessageBox.warning(self, "의존성 누락",
                f"다음 실행파일을 찾을 수 없습니다:\n  {', '.join(missing)}\n\n"
                f"PATH 또는 프로그램 폴더에 함께 두어야 합니다.")

    def _apply_status_message(self):
        if self.hw_best != "none":
            self.lbl_hw.setText(f"GPU: {self.hw_best.upper()} 가능 | CPU 코어: {self.cpu_cores}")
        else:
            self.lbl_hw.setText(f"GPU 미감지 | CPU 코어: {self.cpu_cores}")
        self._log(f"FFmpeg: {self.ffmpeg_path}")
        self._log(f"MP4Box: {self.mp4box_path}")
        self._log(f"FFprobe: {self.ffprobe_path}")
        self._log(f"하드웨어: vendor={self.hw_vendor}, available={self.hw_codecs}, best={self.hw_best}")

    def _update_pool_size(self):
        if self.cfg.process_mode == "parallel":
            self.pool.setMaxThreadCount(max(1, min(self.cfg.max_parallel, 32)))
        else:
            self.pool.setMaxThreadCount(1)

    # ---------------- 파일 추가 / 제거 ----------------
    def _on_add_files(self):
        start_dir = self.cfg.recent_dirs[0] if self.cfg.recent_dirs else ""
        files, _ = QFileDialog.getOpenFileNames(self, "MP4 파일 선택", start_dir,
                                                "MP4 Files (*.mp4);;All Files (*)")
        if files:
            self._on_paths_dropped(files)

    def _on_add_dir(self):
        start_dir = self.cfg.recent_dirs[0] if self.cfg.recent_dirs else ""
        d = QFileDialog.getExistingDirectory(self, "폴더 선택 (하위 MP4 모두 추가)", start_dir)
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
        dirs_seen: List[str] = []
        skipped: Dict[str, int] = {}       # MP4 가 아니라 제외한 파일 (확장자별 개수)
        for p in paths:
            try:
                if os.path.isdir(p):
                    dirs_seen.append(p)
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
                self._log(f"경로 스캔 오류 {p}: {e}")

        if dirs_seen:
            for d in dirs_seen:
                if d in self.cfg.recent_dirs:
                    self.cfg.recent_dirs.remove(d)
                self.cfg.recent_dirs.insert(0, d)
            self.cfg.recent_dirs = self.cfg.recent_dirs[:10]
            self.settings.save()

        if not files:
            self._log("추가할 MP4 파일이 없습니다." + self._skipped_text(skipped, " / 제외(MP4 아님): "))
            self._notify_no_mp4(skipped, bool(dirs_seen))
            return

        # 기존 경로도 동일한 정규화 규칙으로 비교
        def _norm(p: str) -> str:
            try:
                return os.path.normcase(os.path.normpath(os.path.abspath(p)))
            except Exception:
                return p

        existing = set(_norm(p) for p in self._existing_paths())
        added = 0
        dup = 0
        for f in files:
            f = os.path.normpath(os.path.abspath(f))
            if _norm(f) in existing:
                dup += 1
                continue
            self._add_tree_item(f)
            existing.add(_norm(f))
            added += 1
            # 비동기 메타 체크
            self._start_check(f, self._current_engine())
        self._log(f"파일 추가: {added}개 / 중복 제외: {dup}개"
                  + self._skipped_text(skipped, " / 제외(MP4 아님): "))
        if skipped and not self._processing_active:
            self.lbl_progress.setText(f"MP4 {added}개 추가 · MP4 가 아니라 제외: "
                                      + self._skipped_text(skipped))

    def _start_check(self, path: str, engine: str):
        cw = CheckWorker(path, engine, self.ffprobe_path)
        cw.signals.state.connect(self._on_check_state)
        cw.signals.log.connect(self._on_check_log)
        self.check_pool.start(cw)

    def _on_check_state(self, path: str, state: str):
        """메타 검사 결과는 아직 '확인 중' 인 항목에만 반영
        (처리가 먼저 끝난 항목의 '완료' 상태를 덮어쓰지 않도록)"""
        it = self._find_item(path)
        if it and it.text(COL_STATUS) == TaskState.CHECKING.value:
            it.setText(COL_STATUS, state)

    def _existing_paths(self) -> List[str]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            out.append(it.text(COL_PATH))
        return out

    def _add_tree_item(self, filepath: str):
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

    def _remove_selected(self):
        items = self.tree.selectedItems()
        if not items: return
        if self._processing_active:
            QMessageBox.information(self, "알림", "처리 중에는 항목을 제거할 수 없습니다.")
            return
        for it in items:
            self._take_item(it)
        self._log(f"{len(items)}개 항목 제거")

    def _clear_all(self):
        if self.tree.topLevelItemCount() == 0:
            return
        if self._processing_active:
            QMessageBox.information(self, "알림", "처리 중에는 항목을 제거할 수 없습니다.")
            return
        n = self.tree.topLevelItemCount()
        if QMessageBox.question(self, "확인", f"전체 {n}개 항목을 제거할까요?") == QMessageBox.Yes:
            self.tree.clear()
            self._items.clear()
            self._meta.clear()
            self._log(f"{n}개 항목 전체 제거")

    # ---------------- 옵션 변경 ----------------
    def _current_engine(self) -> str:
        return Engine.FFMPEG.value if self.cb_engine.currentIndex() == 0 else Engine.MP4BOX.value

    def _on_engine_changed(self, idx: int):
        self.cfg.engine = self._current_engine()
        is_ffmpeg = self.cfg.engine == Engine.FFMPEG.value
        self.cb_hw.setEnabled(is_ffmpeg and self.hw_best != "none")
        self.settings.save()
        self._log(f"엔진 변경: {self.cb_engine.currentText()}")
        # 모든 항목 재체크
        for i in range(self.tree.topLevelItemCount()):
            it = self.tree.topLevelItem(i)
            it.setText(COL_STATUS, TaskState.CHECKING.value)
            it.setData(COL_PROGRESS, ProgressDelegate.PROG_ROLE, 0)
            self._start_check(it.text(COL_PATH), self.cfg.engine)

    def _on_hw_changed(self, idx: int):
        self.cfg.hw_accel_enabled = (idx == 1)
        self.settings.save()

    def _on_mode_changed(self, idx: int):
        self.cfg.process_mode = "serial" if idx == 0 else "parallel"
        self._update_pool_size()
        self.settings.save()
        self._log(f"처리 방식 변경: {'직렬' if idx == 0 else '병렬'}")

    def _on_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        if dlg.exec_() == QDialog.Accepted:
            old_theme = self.cfg.theme
            dlg.apply_to(self.cfg)
            self.settings.save()
            self._update_pool_size()
            # 콤보 동기화
            self.cb_engine.setCurrentIndex(0 if self.cfg.engine == Engine.FFMPEG.value else 1)
            self.cb_mode.setCurrentIndex(0 if self.cfg.process_mode == "serial" else 1)
            self.cb_hw.setCurrentIndex(1 if self.cfg.hw_accel_enabled else 0)
            self.cb_hw.setEnabled(self.cfg.engine == Engine.FFMPEG.value and self.hw_best != "none")
            # 테마 변경
            if old_theme != self.cfg.theme:
                if self.cfg.theme == "dark":
                    Theme.apply_dark(QApplication.instance())
                else:
                    Theme.apply_light(QApplication.instance())
            self._log("설정이 적용되었습니다.")

    def _on_about(self):
        QMessageBox.about(self, f"{APP_NAME} 정보",
            f"<h3>{APP_NAME} v{APP_VERSION}</h3>"
            f"<p>© {APP_BUILD_YEAR} {APP_AUTHOR} - "
            f'<a href="{APP_WEBSITE}" style="color:#66b2ff;">{APP_WEBSITE}</a></p>'
            f"<p>Engines: FFmpeg, MP4Box (GPAC), Native FastStart</p>")

    # ---------------- 처리 시작 / 일시정지 / 취소 ----------------
    def _selected_or_all_paths(self, selected_only: bool) -> List[str]:
        if selected_only:
            its = self.tree.selectedItems()
        else:
            its = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        return [it.text(COL_PATH) for it in its]

    def _start_processing(self, selected_only: bool):
        if self._processing_active:
            QMessageBox.information(self, "알림", "이미 처리 중입니다.")
            return
        paths = self._selected_or_all_paths(selected_only)
        if not paths:
            QMessageBox.information(self, "알림",
                "처리할 파일이 선택되지 않았습니다." if selected_only else "처리할 파일이 없습니다.")
            return

        engine = self.cfg.engine
        hw = "none"
        if engine == Engine.FFMPEG.value and self.cfg.hw_accel_enabled and self.hw_best != "none":
            hw = self.hw_best

        # 출력 폴더 모드 검증
        if self.cfg.output_mode == OutputMode.SEPARATE_DIR.value:
            if not self.cfg.output_dir:
                QMessageBox.warning(self, "출력 폴더 미지정",
                    "별도 폴더 출력 모드인데 폴더가 지정되지 않았습니다.\n설정에서 폴더를 지정하세요.")
                return
            try:
                os.makedirs(self.cfg.output_dir, exist_ok=True)
            except Exception as e:
                QMessageBox.warning(self, "오류", f"출력 폴더 생성 실패: {e}")
                return

        # 컨트롤러 / 카운터 리셋
        self.controller.reset()
        with QMutexLocker(self._mutex):
            self._total = len(paths)
            self._done = 0
            self._success = 0
            self._fail = 0
            self._processing_active = True

        self.pbar.setVisible(True)
        self.pbar.setMaximum(self._total)
        self.pbar.setValue(0)
        self._toggle_buttons(processing=True)

        self._log(f"=== 처리 시작 (총 {self._total}개, 엔진={engine}, "
                  f"가속={hw}, 모드={self.cfg.process_mode}, 출력={self.cfg.output_mode}) ===")

        outputs = self._plan_output_paths(paths)

        # 워커 큐잉
        for p in paths:
            it = self._find_item(p)
            if it:
                it.setText(COL_STATUS, TaskState.QUEUED.value)
                it.setData(COL_PROGRESS, ProgressDelegate.PROG_ROLE, 0)
            w = FileProcessor(p, engine, hw, self.cfg, self.controller,
                              self.ffmpeg_path, self.mp4box_path, self.ffprobe_path,
                              self.temp_dir, output_path=outputs.get(p))
            w.signals.state.connect(self._on_state_change)
            w.signals.progress.connect(self._on_progress)
            w.signals.log.connect(self._log)
            w.signals.finished.connect(self._on_file_finished)
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

    def _on_pause_resume(self):
        if not self._processing_active:
            return
        if self.btn_pause.text().startswith("⏸"):
            self.controller.pause()
            self.btn_pause.setText("▶ 재개")
            self.lbl_progress.setText("일시정지 중...")
        else:
            self.controller.resume()
            self.btn_pause.setText("⏸ 일시정지")
            self.lbl_progress.setText(f"처리 중 ({self._done}/{self._total})")

    def _on_cancel(self):
        if not self._processing_active:
            return
        if QMessageBox.question(self, "취소", "진행 중인 모든 작업을 취소할까요?") != QMessageBox.Yes:
            return
        self.controller.cancel()
        self._log("취소 요청 - 진행 중인 자식 프로세스 종료 시도")

    # ---------------- 시그널 처리 ----------------
    def _on_state_change(self, path: str, state: str):
        it = self._find_item(path)
        if not it: return
        it.setText(COL_STATUS, state)

    def _on_progress(self, path: str, pct: float, eta: float, speed: float):
        it = self._find_item(path)
        if not it: return
        it.setData(COL_PROGRESS, ProgressDelegate.PROG_ROLE, pct)
        if eta > 0 and pct < 99.5:
            eta_s = str(timedelta(seconds=int(eta)))
            speed_s = f"{speed:.2f}x" if speed > 0 else ""
            self.lbl_progress.setText(
                f"처리 중 ({self._done}/{self._total}) - {os.path.basename(path)} - "
                f"{int(pct)}% / ETA {eta_s} {speed_s}")

    def _on_file_finished(self, path: str, success: bool, msg: str):
        with QMutexLocker(self._mutex):
            self._done += 1
            if success:
                self._success += 1
            else:
                self._fail += 1
            done = self._done
            total = self._total
            success_n = self._success
            fail_n = self._fail
            all_done = done >= total

        self.pbar.setValue(done)
        self.lbl_progress.setText(f"처리 중 ({done}/{total}) - 성공 {success_n} / 실패 {fail_n}")

        fname = os.path.basename(path)
        if success:
            self._log(f"[{done}/{total}] ✓ {fname}: {msg}")
        else:
            self._log(f"[{done}/{total}] ✗ {fname}: {msg}")

        if all_done:
            self._processing_active = False
            self.pbar.setVisible(False)
            self._toggle_buttons(processing=False)
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

    # ---------------- 메타 정보 / 로그 ----------------
    def _on_check_log(self, msg: str):
        if msg.startswith("__META__"):
            try:
                payload = json.loads(msg[len("__META__"):])
                p = payload["path"]
                meta = payload["meta"]
                self._meta[p] = meta
                it = self._find_item(p)
                if it:
                    sz = meta.get("size", 0)
                    dur = meta.get("duration", 0)
                    w  = int(meta.get("w", 0)); h = int(meta.get("h", 0))
                    vc = meta.get("vc", "");    ac = meta.get("ac", "")
                    br = meta.get("br", 0)
                    codecs = " / ".join([c for c in (vc, ac) if c])
                    it.setText(COL_RES,      f"{w}x{h}" if (w and h) else "-")
                    it.setText(COL_CODEC,    codecs if codecs else "-")
                    it.setText(COL_BITRATE,  self._fmt_bitrate(br))
                    it.setText(COL_DURATION, self._fmt_dur(dur))
                    it.setText(COL_SIZE,     self._fmt_size(sz))
                    if dur > 0 or sz > 0:
                        tip = (f"경로: {p}\n"
                               f"비디오: {vc} {w}x{h}\n"
                               f"오디오: {ac}\n"
                               f"비트레이트: {self._fmt_bitrate(br)}\n"
                               f"FastStart: {meta.get('fs')} / Hint: {meta.get('ht')}")
                        it.setToolTip(COL_NAME, tip)
            except Exception:
                pass
            return
        self._log(msg)

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")
        logger.info(msg)

    # ---------------- 필터 / 컨텍스트 메뉴 ----------------
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
        a_remove = m.addAction("🗑 목록에서 제거")
        act = m.exec_(self.tree.viewport().mapToGlobal(pos))
        if act == a_diag:
            DiagnosticDialog(item.text(COL_PATH), self).exec_()
        elif act == a_open:
            self._open_in_explorer(item.text(COL_PATH))
        elif act == a_copy:
            QApplication.clipboard().setText(item.text(COL_PATH))
            self._log(f"클립보드 복사: {item.text(COL_PATH)}")
        elif act == a_retry:
            if self._processing_active:
                QMessageBox.information(self, "알림", "처리 중에는 재시작할 수 없습니다.")
            else:
                self.tree.clearSelection()
                item.setSelected(True)
                self._start_processing(selected_only=True)
        elif act == a_remove:
            if self._processing_active:
                QMessageBox.information(self, "알림", "처리 중에는 제거할 수 없습니다.")
            else:
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

    def _on_item_double_clicked(self, item, _col):
        status = item.text(COL_STATUS)
        if (TaskState.PENDING.value in status or TaskState.FAILED.value in status
                or TaskState.CANCELLED.value in status):
            if QMessageBox.question(self, "재처리",
                    f"'{item.text(COL_NAME)}' 파일을 처리할까요?") == QMessageBox.Yes:
                self.tree.clearSelection()
                item.setSelected(True)
                self._start_processing(selected_only=True)

    def _open_in_explorer(self, filepath: str):
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", filepath],
                                 startupinfo=STARTUPINFO, creationflags=CREATE_FLAGS)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", filepath])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(filepath)])
        except Exception as e:
            self._log(f"탐색기 열기 실패: {e}")

    # ---------------- 유틸 ----------------
    @staticmethod
    def _fmt_size(b: int) -> str:
        b = int(b or 0)
        if b <= 0: return "-"
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if b < 1024:
                return f"{b:.1f} {unit}" if unit != "B" else f"{b} B"
            b /= 1024
        return f"{b:.1f} PB"

    @staticmethod
    def _fmt_dur(s: float) -> str:
        s = float(s or 0)
        if s <= 0: return "-"
        return str(timedelta(seconds=int(s)))

    @staticmethod
    def _fmt_bitrate(bps) -> str:
        try:
            bps = float(bps or 0)
        except (TypeError, ValueError):
            return "-"
        if bps <= 0: return "-"
        if bps >= 1_000_000:
            return f"{bps/1_000_000:.2f} Mbps"
        if bps >= 1000:
            return f"{bps/1000:.0f} kbps"
        return f"{int(bps)} bps"

    def _toggle_buttons(self, processing: bool):
        self.btn_add.setEnabled(not processing)
        self.btn_add_dir.setEnabled(not processing)
        self.btn_proc_sel.setEnabled(not processing)
        self.btn_proc_all.setEnabled(not processing)
        self.btn_settings.setEnabled(not processing)
        self.btn_remove.setEnabled(not processing)
        self.btn_clear.setEnabled(not processing)
        self.cb_engine.setEnabled(not processing)
        self.cb_hw.setEnabled(not processing and self.cfg.engine == Engine.FFMPEG.value and self.hw_best != "none")
        self.cb_mode.setEnabled(not processing)
        self.btn_pause.setEnabled(processing)
        self.btn_cancel.setEnabled(processing)

    def _cleanup_orphan_temp(self):
        """이전 실행에서 남은 임시 파일 정리"""
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

    # ---------------- 종료 ----------------
    def closeEvent(self, e):
        if self._processing_active:
            if QMessageBox.question(self, "종료",
                    "처리가 진행 중입니다. 모두 취소하고 종료할까요?") != QMessageBox.Yes:
                e.ignore()
                return
            self.controller.cancel()
            # 잠시 대기
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
    """우클릭 처리: 항상 FastStart (Native 우선, 안 되면 FFmpeg 스트림 카피) - GPU 미사용"""
    return FileProcessor(path, Engine.FFMPEG.value, "none", cfg, ctrl,
                         find_executable("ffmpeg", "ffmpeg.exe"),
                         find_executable("MP4Box", "mp4box.exe"),
                         find_executable("ffprobe", "ffprobe.exe"), temp_dir)


# =====================================================================
#  진입점
# =====================================================================
def _load_app_icon() -> Optional[QIcon]:
    """app_icon.ico 다중 사이즈 로드"""
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
    for sz in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
        icon.addFile(icon_path, QSize(sz, sz))
    if icon.isNull():
        return QIcon(icon_path)
    return icon


def main():
    # 고DPI
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    # Windows: 작업표시줄/타이틀바가 Python.exe 의 것으로 보이는 문제 방지
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

    # 테마 적용 (설정 로드 후)
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
