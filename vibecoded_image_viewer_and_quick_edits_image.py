#sudo pacman -S python python-pillow python-pyqt6 
#NEED TO INSTALL THESE ^^^^^^^^^^^^^^^^^^^^^^^^^^


import os
import random
import subprocess
import sys
from collections import deque

from PIL import Image, UnidentifiedImageError
from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QThread, Qt, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QColor, QCursor, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)


BTN_TEXT_OPEN = "⎔"
BTN_TEXT_PREV = "←"
BTN_TEXT_NEXT = "→"
BTN_TEXT_ROTATE_LEFT = "⟲"
BTN_TEXT_ROTATE_RIGHT = "⟳"
BTN_TEXT_CROP = "✃"
BTN_TEXT_RESET_CROP = "↺"
BTN_TEXT_SAVE = "↓"
BTN_TEXT_FULLSCREEN = "⇱"
BTN_TEXT_SLIDESHOW = "⏱"
BTN_TEXT_INFO = "💬" #💬ⓘ𝕚 are not bad
BTN_TEXT_MINIFY = "→←"
BTN_TEXT_EXIT = "✕"

SLIDESHOW_INTERVAL_MS = 5500

UI_FONT_SIZE = "16px"
BTN_WIDTH = 36
BTN_HEIGHT = 36

SLIDER_HANDLE_COLOR = "#47567a"
SLIDER_HOVER_COLOR = "#BFBFBF"


class MainImageWorker(QObject):
    finished = pyqtSignal(int, QImage, object, str)

    @pyqtSlot(int, str, float)
    def process_image(self, request_id, image_path, rotation_angle):
        try:
            with Image.open(image_path) as img:
                img.load()
                pil_img = img.copy()

            if rotation_angle != 0:
                pil_img = pil_img.rotate(-rotation_angle, expand=True)

            if pil_img.mode != "RGBA":
                display_img = pil_img.convert("RGBA")
            else:
                display_img = pil_img

            data = display_img.tobytes("raw", "RGBA")
            qimage = QImage(
                data,
                display_img.size[0],
                display_img.size[1],
                QImage.Format.Format_RGBA8888
            ).copy()

            self.finished.emit(request_id, qimage, pil_img, "")

        except (UnidentifiedImageError, OSError, Exception) as err:
            self.finished.emit(request_id, QImage(), None, str(err))


class CacheWorker(QObject):
    cache_finished = pyqtSignal(str, QImage, object)

    @pyqtSlot(str)
    def preload_image(self, image_path):
        try:
            with Image.open(image_path) as img:
                img.load()
                pil_img = img.copy()

            if pil_img.mode != "RGBA":
                display_img = pil_img.convert("RGBA")
            else:
                display_img = pil_img

            data = display_img.tobytes("raw", "RGBA")
            qimage = QImage(
                data,
                display_img.size[0],
                display_img.size[1],
                QImage.Format.Format_RGBA8888
            ).copy()

            self.cache_finished.emit(image_path, qimage, pil_img)
        except Exception:
            pass


class ImageCanvas(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selection_rect = QRect()
        self.is_selecting = False
        self.is_panning = False
        self.pan_start_pos = QPoint()
        self.selection_start_pos = QPoint()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_panning = True
            self.pan_start_pos = event.globalPosition().toPoint()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            self.is_selecting = True
            self.selection_start_pos = event.position().toPoint()
            self.selection_rect = QRect(self.selection_start_pos, self.selection_start_pos)
            self.update()

    def mouseMoveEvent(self, event):
        if self.is_panning:
            delta = event.globalPosition().toPoint() - self.pan_start_pos
            self.pan_start_pos = event.globalPosition().toPoint()

            scroll_area = self.get_scroll_area()
            if scroll_area:
                scroll_area.horizontalScrollBar().setValue(
                    scroll_area.horizontalScrollBar().value() - delta.x()
                )
                scroll_area.verticalScrollBar().setValue(
                    scroll_area.verticalScrollBar().value() - delta.y()
                )
        elif self.is_selecting:
            current_pos = event.position().toPoint()
            self.selection_rect = QRect(self.selection_start_pos, current_pos).normalized()
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            self.is_selecting = False
            current_pos = event.position().toPoint()
            self.selection_rect = QRect(self.selection_start_pos, current_pos).normalized()
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.selection_rect.isNull() and self.pixmap():
            painter = QPainter(self)
            pen = QPen(QColor("#007acc"), 2, Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(QColor(0, 122, 204, 50))
            painter.drawRect(self.selection_rect)

    def get_scroll_area(self):
        parent = self.parent()
        while parent:
            if isinstance(parent, QScrollArea):
                return parent
            parent = parent.parent()
        return None

    def clear_selection(self):
        self.selection_rect = QRect()
        self.update()


class ImageViewer(QMainWindow):
    request_processing = pyqtSignal(int, str, float)
    request_cache = pyqtSignal(str)

    def __init__(self, initial_path=None):
        super().__init__()
        self.setWindowTitle("Image Viewer")
        self.resize(1100, 750)

        self.supported_extensions = (
            ".jpg", ".jpeg", ".png",
            ".webp", ".bmp", ".gif",
            ".tiff", ".tif", ".ico"
        )
        self.image_files = []
        self.current_index = -1
        
        self.current_pil_image = None
        self.base_pixmap = None
        self.original_save_kwargs = {}

        self.image_cache = {}
        self.preload_queue = deque()
        
        self.slideshow_history = []
        self.slideshow_history_pos = -1

        self.rotation_angle = 0
        self.zoom_factor = 1.0
        self.is_custom_zoom = False

        self.request_counter = 0

        self.load_timer = QTimer(self)
        self.load_timer.setSingleShot(True)
        self.load_timer.timeout.connect(self.trigger_worker)

        self.idle_cache_timer = QTimer(self)
        self.idle_cache_timer.setSingleShot(True)
        self.idle_cache_timer.timeout.connect(self.process_next_idle_cache)

        self.slideshow_timer = QTimer(self)
        self.slideshow_timer.setInterval(SLIDESHOW_INTERVAL_MS)
        self.slideshow_timer.timeout.connect(self.show_random_image)

        self.smooth_zoom_timer = QTimer(self)
        self.smooth_zoom_timer.setSingleShot(True)
        self.smooth_zoom_timer.timeout.connect(lambda: self.update_image_display(smooth=True))

        self.osd_timer = QTimer(self)
        self.osd_timer.setSingleShot(True)
        self.osd_timer.timeout.connect(self.hide_osd)

        self.simulated_progress_timer = QTimer(self)
        self.simulated_progress_timer.setInterval(20)
        self.simulated_progress_timer.timeout.connect(self.increment_simulated_progress)
        self.current_progress_value = 0

        self._was_fullscreen = False

        self.init_ui()
        self.apply_dark_theme()
        self.setup_worker_threads()

        QApplication.instance().installEventFilter(self)

        if initial_path:
            self.load_from_path(initial_path)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            is_now_fullscreen = self.isFullScreen()
            if is_now_fullscreen != self._was_fullscreen:
                self._was_fullscreen = is_now_fullscreen
                if is_now_fullscreen:
                    self.controls_wrapper.hide()
                    self.setStyleSheet(self.get_stylesheet(fullscreen=True))
                else:
                    self.controls_wrapper.show()
                    self.setStyleSheet(self.get_stylesheet(fullscreen=False))
                QTimer.singleShot(50, self.reset_to_fit_screen)
                QTimer.singleShot(50, self.reposition_osd)
                QTimer.singleShot(150, self.reposition_osd)
                QTimer.singleShot(300, self.reposition_osd)
                QTimer.singleShot(50, self.reposition_progress_bar)
                QTimer.singleShot(150, self.reposition_progress_bar)
                QTimer.singleShot(300, self.reposition_progress_bar)
        super().changeEvent(event)

    def setup_worker_threads(self):
        self.main_worker_thread = QThread()
        self.main_worker = MainImageWorker()
        self.main_worker.moveToThread(self.main_worker_thread)
        self.request_processing.connect(self.main_worker.process_image)
        self.main_worker.finished.connect(self.on_image_processed)
        self.main_worker_thread.start()

        self.cache_worker_thread = QThread()
        self.cache_worker = CacheWorker()
        self.cache_worker.moveToThread(self.cache_worker_thread)
        self.request_cache.connect(self.cache_worker.preload_image)
        self.cache_worker.cache_finished.connect(self.on_cache_processed)
        self.cache_worker_thread.start()

    def closeEvent(self, event):
        self.main_worker_thread.quit()
        self.cache_worker_thread.quit()
        self.main_worker_thread.wait()
        self.cache_worker_thread.wait()
        super().closeEvent(event)

    def init_ui(self):
        self.central_widget = QWidget()
        self.central_widget.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(self.central_widget)

        main_layout = QVBoxLayout()
        self.central_widget.setLayout(main_layout)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area.setWidgetResizable(False)
        self.scroll_area.setContentsMargins(0, 0, 0, 0)
        self.scroll_area.setViewportMargins(0, 0, 0, 0)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setLineWidth(0)

        self.image_label = ImageCanvas()
        self.image_label.setText("Open an image to start")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll_area.setWidget(self.image_label)
        
        main_layout.addWidget(self.scroll_area, stretch=1)

        self.controls_wrapper = QWidget()
        self.controls_wrapper.setObjectName("controlsWrapper")
        wrapper_layout = QHBoxLayout()
        self.controls_wrapper.setLayout(wrapper_layout)
        wrapper_layout.setContentsMargins(0, 0, 0, 0)
        wrapper_layout.setSpacing(0)

        self.controls_widget = QWidget()
        self.controls_widget.setObjectName("controlsWidget")
        controls_layout = QHBoxLayout()
        self.controls_widget.setLayout(controls_layout)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(1)
        controls_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        wrapper_layout.addStretch()
        wrapper_layout.addWidget(self.controls_widget)
        wrapper_layout.addStretch()

        self.buttons = []

        self.btn_open = QPushButton(BTN_TEXT_OPEN)
        self.btn_open.clicked.connect(self.open_file_dialog)
        self.buttons.append(self.btn_open)

        self.btn_prev = QPushButton(BTN_TEXT_PREV)
        self.btn_prev.setAutoRepeat(True)
        self.btn_prev.setAutoRepeatDelay(300)
        self.btn_prev.setAutoRepeatInterval(100)
        self.btn_prev.clicked.connect(self.show_previous_image)
        self.btn_prev.setEnabled(False)
        self.buttons.append(self.btn_prev)

        self.btn_next = QPushButton(BTN_TEXT_NEXT)
        self.btn_next.setAutoRepeat(True)
        self.btn_next.setAutoRepeatDelay(300)
        self.btn_next.setAutoRepeatInterval(100)
        self.btn_next.clicked.connect(self.show_next_image)
        self.btn_next.setEnabled(False)
        self.buttons.append(self.btn_next)

        self.btn_rotate_left = QPushButton(BTN_TEXT_ROTATE_LEFT)
        self.btn_rotate_left.clicked.connect(self.rotate_left)
        self.btn_rotate_left.setEnabled(False)
        self.buttons.append(self.btn_rotate_left)

        self.btn_rotate_right = QPushButton(BTN_TEXT_ROTATE_RIGHT)
        self.btn_rotate_right.clicked.connect(self.rotate_right)
        self.btn_rotate_right.setEnabled(False)
        self.buttons.append(self.btn_rotate_right)

        self.btn_crop = QPushButton(BTN_TEXT_CROP)
        self.btn_crop.clicked.connect(self.crop_selection)
        self.btn_crop.setEnabled(False)
        self.buttons.append(self.btn_crop)

        self.btn_reset_crop = QPushButton(BTN_TEXT_RESET_CROP)
        self.btn_reset_crop.clicked.connect(self.reset_crop_or_selection)
        self.btn_reset_crop.setEnabled(False)
        self.buttons.append(self.btn_reset_crop)

        self.btn_save = QPushButton(BTN_TEXT_SAVE)
        self.btn_save.clicked.connect(self.save_image)
        self.btn_save.setEnabled(False)
        self.buttons.append(self.btn_save)
        
        self.btn_fullscreen = QPushButton(BTN_TEXT_FULLSCREEN)
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        self.buttons.append(self.btn_fullscreen)

        self.btn_slideshow = QPushButton(BTN_TEXT_SLIDESHOW)
        self.btn_slideshow.clicked.connect(self.toggle_slideshow)
        self.btn_slideshow.setEnabled(False)
        self.buttons.append(self.btn_slideshow)

        self.btn_info = QPushButton(BTN_TEXT_INFO)
        self.btn_info.clicked.connect(self.show_image_info)
        self.btn_info.setEnabled(False)
        self.buttons.append(self.btn_info)

        self.btn_minify = QPushButton(BTN_TEXT_MINIFY)
        self.btn_minify.clicked.connect(self.minify_image)
        self.btn_minify.setEnabled(False)
        self.buttons.append(self.btn_minify)

        self.btn_exit = QPushButton(BTN_TEXT_EXIT)
        self.btn_exit.clicked.connect(self.close)
        self.buttons.append(self.btn_exit)

        for btn in self.buttons:
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setFixedWidth(BTN_WIDTH)
            btn.setFixedHeight(BTN_HEIGHT)
            controls_layout.addWidget(btn)

        main_layout.addWidget(self.controls_wrapper)

        self.osd_label = QLabel(self.central_widget)
        self.osd_label.setStyleSheet("""
            background-color: rgba(0, 0, 0, 180);
            color: white;
            padding: 8px 16px;
            font-size: 16px;
            font-weight: bold;
            border-radius: 8px;
        """)
        self.osd_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.osd_label.hide()

        self.progress_bar = QProgressBar(self.central_widget)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()

    def get_stylesheet(self, fullscreen=False):
        bg_color = "#000000"
        strip_color = "#181818"
        return f"""
            QMainWindow {{
                background-color: {bg_color};
                border: none;
            }}
            QWidget {{
                background-color: {bg_color};
                color: #e0e0e0;
                font-family: sans-serif;
                font-size: {UI_FONT_SIZE};
            }}
            QWidget#controlsWrapper {{
                background-color: {strip_color};
            }}
            QWidget#controlsWidget {{
                background-color: {strip_color};
            }}
            QScrollArea {{
                background-color: {bg_color};
                border: none;
                outline: none;
                margin: 0px;
                padding: 0px;
            }}
            QScrollArea::viewport {{
                background-color: {bg_color};
                border: none;
                margin: 0px;
                padding: 0px;
            }}
            QScrollArea::corner {{
                background: {bg_color};
            }}
            QLabel {{
                color: #b0b0b0;
                background-color: {bg_color};
                border: none;
            }}
            QProgressBar {{
                background-color: transparent;
                border: none;
            }}
            QProgressBar::chunk {{
                background-color: #007acc;
            }}
            QPushButton {{
                background-color: #242424;
                color: #e0e0e0;
                border: none;
                border-right: 1px solid #1a1a1a;
                border-radius: 0px;
                padding: 0px;
                text-align: center;
            }}
            QPushButton:last-of-type {{
                border-right: none;
            }}
            QPushButton:hover {{
                background-color: #323232;
            }}
            QPushButton:pressed {{
                background-color: #005999;
            }}
            QPushButton:disabled {{
                background-color: #1a1a1a;
                color: #555555;
            }}
            QScrollBar:vertical {{
                background: {bg_color};
                border: none;
                width: 10px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical {{
                background: {SLIDER_HANDLE_COLOR};
                border: none;
                border-radius: 5px;
                min-height: 20px;
                margin: 0px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: {SLIDER_HOVER_COLOR};
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: {bg_color};
                border: none;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                background: none;
                border: none;
                height: 0px;
            }}
            QScrollBar:horizontal {{
                background: {bg_color};
                border: none;
                height: 10px;
                margin: 0px;
            }}
            QScrollBar::handle:horizontal {{
                background: {SLIDER_HANDLE_COLOR};
                border: none;
                border-radius: 5px;
                min-width: 20px;
                margin: 0px;
            }}
            QScrollBar::handle:horizontal:hover {{
                background: {SLIDER_HOVER_COLOR};
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: {bg_color};
                border: none;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                background: none;
                border: none;
                width: 0px;
            }}
        """

    def apply_dark_theme(self):
        self.setStyleSheet(self.get_stylesheet(fullscreen=self.isFullScreen()))

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.MiddleButton:
                self.toggle_fullscreen()
                return True
        elif event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Right):
                if event.key() == Qt.Key.Key_Left:
                    self.show_previous_image()
                else:
                    self.show_next_image()
                return True
            elif event.key() == Qt.Key.Key_L:
                if not self.slideshow_timer.isActive():
                    self.rotate_left()
                return True
            elif event.key() == Qt.Key.Key_R:
                if not self.slideshow_timer.isActive():
                    self.rotate_right()
                return True
            elif event.key() == Qt.Key.Key_C:
                if not self.slideshow_timer.isActive():
                    self.crop_selection()
                return True
            elif event.key() == Qt.Key.Key_S:
                self.save_image()
                return True
            elif event.key() == Qt.Key.Key_I:
                self.show_image_info()
                return True
            elif event.key() == Qt.Key.Key_M:
                if not self.slideshow_timer.isActive():
                    self.minify_image()
                return True
            elif event.key() == Qt.Key.Key_Space:
                if self.slideshow_timer.isActive():
                    self.toggle_slideshow()
                else:
                    self.reset_crop_or_selection()
                return True
            elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.toggle_fullscreen()
                return True
            elif event.key() == Qt.Key.Key_Escape and self.isFullScreen():
                self.toggle_fullscreen()
                return True
            elif event.key() == Qt.Key.Key_H and (event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self.reset_to_actual_size_or_fit()
                return True
            elif event.key() == Qt.Key.Key_Delete:
                self.trash_current_image()
                return True

        elif event.type() == QEvent.Type.Wheel:
            if self.scroll_area.underMouse() or self.image_label.underMouse():
                self.handle_wheel_zoom(event)
                return True

        return super().eventFilter(source, event)
    
    def show_osd(self, text, duration=500):
        self.osd_label.setText(text)
        self.osd_label.adjustSize()
        self.osd_label.show()
        self.osd_label.raise_()
        self.reposition_osd()
        self.osd_timer.start(duration)

    def hide_osd(self):
        self.osd_label.hide()

    def reposition_osd(self):
        if self.osd_label.isHidden():
            return

        self.osd_label.adjustSize()

        x = (self.central_widget.width() - self.osd_label.width()) // 2
        y = self.central_widget.height() - self.osd_label.height() - 45

        self.osd_label.move(x, y)

    def reposition_progress_bar(self):
        geom = self.scroll_area.geometry()
        self.progress_bar.setGeometry(geom.x(), geom.y() + geom.height() - 4, geom.width(), 4)

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()
            QTimer.singleShot(0, self.reposition_osd)

    def toggle_slideshow(self):
        if self.slideshow_timer.isActive():
            self.slideshow_timer.stop()
            self.btn_slideshow.setStyleSheet("")
            self.show_osd("Slideshow Stopped", duration=1000)
            self.update_button_states()
            self.schedule_idle_cache()
        else:
            if not self.image_files:
                return
            self.idle_cache_timer.stop()
            self.preload_queue.clear()
            self.image_cache.clear()

            self.slideshow_history = [self.current_index]
            self.slideshow_history_pos = 0

            self.slideshow_timer.start()
            self.btn_slideshow.setStyleSheet("background-color: #005999; color: #ffffff;")
            self.show_osd("Slideshow Started", duration=1000)
            self.update_button_states()
            self.show_random_image()

    def update_button_states(self):
        has_images = len(self.image_files) > 0 and 0 <= self.current_index < len(self.image_files)
        is_slideshow = self.slideshow_timer.isActive()

        self.btn_open.setEnabled(True)
        self.btn_prev.setEnabled(has_images)
        self.btn_next.setEnabled(has_images)
        self.btn_slideshow.setEnabled(has_images)
        self.btn_info.setEnabled(has_images)
        self.btn_fullscreen.setEnabled(True)
        self.btn_exit.setEnabled(True)

        if has_images and not is_slideshow:
            self.btn_rotate_left.setEnabled(True)
            self.btn_rotate_right.setEnabled(True)
            self.btn_crop.setEnabled(True)
            self.btn_reset_crop.setEnabled(True)
            self.btn_save.setEnabled(True)
            self.btn_minify.setEnabled(True)
        else:
            self.btn_rotate_left.setEnabled(False)
            self.btn_rotate_right.setEnabled(False)
            self.btn_crop.setEnabled(False)
            self.btn_reset_crop.setEnabled(False)
            self.btn_save.setEnabled(False)
            self.btn_minify.setEnabled(False)

    def show_image_info(self):
        if not self.image_files or not (0 <= self.current_index < len(self.image_files)):
            return
        
        image_path = self.image_files[self.current_index]
        try:
            file_size_bytes = os.path.getsize(image_path)
            file_size_mb = file_size_bytes / (1024 * 1024)
            
            if self.current_pil_image:
                width, height = self.current_pil_image.size
            else:
                with Image.open(image_path) as img:
                    width, height = img.size

            info_text = f"Resolution: {width} × {height} px  |  Size: {file_size_mb:.2f} MB"
            self.show_osd(info_text, duration=3000)
        except Exception as e:
            self.show_osd(f"Could not load image info:\n{str(e)}", duration=2000)

    def minify_image(self):
        if self.slideshow_timer.isActive():
            return
        if self.current_pil_image is None:
            return

        width, height = self.current_pil_image.size
        long_edge = max(width, height)

        if long_edge <= 1920:
            self.show_osd("too tiny to Minify!", duration=1500)
            return

        target_size = 1920
        if width >= height:
            new_w = target_size
            new_h = int(height * (target_size / width))
        else:
            new_h = target_size
            new_w = int(width * (target_size / height))

        try:
            resample_method = Image.Resampling.LANCZOS
        except AttributeError:
            resample_method = Image.LANCZOS

        self.current_pil_image = self.current_pil_image.resize((new_w, new_h), resample_method)
        self.rotation_angle = 0
        self.image_label.clear_selection()

        display_img = self.current_pil_image
        if display_img.mode != "RGBA":
            display_img = display_img.convert("RGBA")

        data = display_img.tobytes("raw", "RGBA")
        qimage = QImage(
            data,
            display_img.size[0],
            display_img.size[1],
            QImage.Format.Format_RGBA8888
        ).copy()

        self.base_pixmap = QPixmap.fromImage(qimage)
        self.update_image_display(smooth=True)
        self.show_osd("image Minified to 1080p", duration=1500)

    def show_random_image(self):
        if not self.image_files:
            return
        if len(self.image_files) == 1:
            next_idx = 0
        else:
            next_idx = self.current_index
            while next_idx == self.current_index:
                next_idx = random.randint(0, len(self.image_files) - 1)
        self.current_index = next_idx
        
        if self.slideshow_history:
            self.slideshow_history = self.slideshow_history[:self.slideshow_history_pos + 1]
        self.slideshow_history.append(self.current_index)
        self.slideshow_history_pos = len(self.slideshow_history) - 1

        self.trigger_navigation()

    def open_file_dialog(self):
        if self.slideshow_timer.isActive():
            self.toggle_slideshow()
            
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Image File",
            "",
            "Image Files (*.jpeg *.jpg *.png *.webp *.bmp *.gif *.tiff *.tif *.ico)"
        )
        if file_path:
            self.load_from_path(file_path)

    def load_from_path(self, path):
        path = os.path.normpath(path)
        if os.path.isdir(path):
            folder_path = path
            selected_file = ""
        elif os.path.isfile(path):
            folder_path = os.path.dirname(path)
            selected_file = path
        else:
            return
        self.scan_folder(folder_path, selected_file)

    def scan_folder(self, folder_path, selected_file):
        try:
            all_files = os.listdir(folder_path)
        except OSError:
            return

        self.image_files = []
        self.image_cache.clear()

        for f in sorted(all_files):
            if f.lower().endswith(self.supported_extensions):
                full_path = os.path.normpath(os.path.join(folder_path, f))
                self.image_files.append(full_path)

        if selected_file in self.image_files:
            self.current_index = self.image_files.index(selected_file)
        else:
            self.current_index = 0 if self.image_files else -1

        self.load_current_image()

    def load_current_image(self):
        if 0 <= self.current_index < len(self.image_files):
            self.rotation_angle = 0
            self.zoom_factor = 1.0
            self.is_custom_zoom = False
            self.image_label.clear_selection()
            self.base_pixmap = None
            self.current_pil_image = None
            self.original_save_kwargs = {}

            image_path = self.image_files[self.current_index]
            
            ext = os.path.splitext(image_path)[1]
            if ext.lower() in (".jpg", ".jpeg"):
                self.original_save_kwargs = {"quality": 97, "subsampling": 0}
            else:
                self.original_save_kwargs = {}

            self.update_button_states()
            self.setWindowTitle(f"Loading: {os.path.basename(image_path)} ({self.current_index + 1}/{len(self.image_files)})")
            
            self.trigger_worker()

    def trigger_navigation(self):
        if self.current_index < 0 or self.current_index >= len(self.image_files):
            return
        filename = os.path.basename(self.image_files[self.current_index])
        self.show_osd(f"{self.current_index + 1} / {len(self.image_files)} - {filename}")
        
        self.zoom_factor = 1.0
        self.is_custom_zoom = False
        self.rotation_angle = 0
        self.image_label.clear_selection()
        self.base_pixmap = None
        self.current_pil_image = None
        self.original_save_kwargs = {}

        self.update_button_states()
        
        self.load_timer.start(75)

    def schedule_idle_cache(self):
        if self.slideshow_timer.isActive():
            return
        self.idle_cache_timer.stop()
        if not self.image_files or self.current_index < 0:
            return

        total = len(self.image_files)
        target_paths = set()

        for i in range(1, 4):
            idx = (self.current_index - i) % total
            target_paths.add(self.image_files[idx])

        for i in range(1, 7):
            idx = (self.current_index + i) % total
            target_paths.add(self.image_files[idx])

        cached_keys = list(self.image_cache.keys())
        for path in cached_keys:
            if path not in target_paths and path != self.image_files[self.current_index]:
                del self.image_cache[path]

        self.preload_queue.clear()
        for path in target_paths:
            if path not in self.image_cache:
                self.preload_queue.append(path)

        if self.preload_queue:
            self.idle_cache_timer.start(150)

    def process_next_idle_cache(self):
        if self.preload_queue:
            path = self.preload_queue.popleft()
            self.request_cache.emit(path)

    @pyqtSlot(str, QImage, object)
    def on_cache_processed(self, image_path, qimage, pil_img):
        if qimage and not qimage.isNull() and pil_img:
            self.image_cache[image_path] = (qimage, pil_img)
        if self.preload_queue:
            self.idle_cache_timer.start(50)

    def trigger_worker(self):
        if 0 <= self.current_index < len(self.image_files):
            image_path = self.image_files[self.current_index]
            
            if self.rotation_angle == 0 and image_path in self.image_cache:
                qimage, pil_img = self.image_cache[image_path]
                self.request_counter += 1
                self.on_image_processed(self.request_counter, qimage, pil_img.copy(), "")
                return

            self.request_counter += 1
            
            self.current_progress_value = 0
            self.progress_bar.setValue(0)
            self.reposition_progress_bar()
            self.progress_bar.show()
            self.progress_bar.raise_()
            self.simulated_progress_timer.start()
            
            self.request_processing.emit(
                self.request_counter,
                image_path,
                float(self.rotation_angle)
            )

    def increment_simulated_progress(self):
        if self.current_progress_value < 90:
            self.current_progress_value += 5
            self.progress_bar.setValue(self.current_progress_value)

    @pyqtSlot(int, QImage, object, str)
    def on_image_processed(self, request_id, qimage, pil_img, error_msg):
        if request_id != self.request_counter:
            return

        self.simulated_progress_timer.stop()
        self.progress_bar.setValue(100)
        QTimer.singleShot(150, self.progress_bar.hide)

        image_path = self.image_files[self.current_index]

        if error_msg:
            self.current_pil_image = None
            self.base_pixmap = None
            self.image_label.clear()
            self.image_label.setText(f"Corrupted or invalid image file:\n{os.path.basename(image_path)}\n{error_msg}")
            
            self.update_button_states()

            self.setWindowTitle(f"Image Viewer - [Corrupted] {os.path.basename(image_path)} ({self.current_index + 1}/{len(self.image_files)})")
        else:
            self.current_pil_image = pil_img
            self.base_pixmap = QPixmap.fromImage(qimage)
            self.setWindowTitle(f"Image Viewer - {os.path.basename(image_path)} ({self.current_index + 1}/{len(self.image_files)})")
            
            self.update_button_states()
            
            self.update_image_display(smooth=True)
            self.schedule_idle_cache()

    def update_image_display(self, smooth=True):
        if not self.base_pixmap or self.base_pixmap.isNull():
            return

        transform_mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
        viewport_size = self.scroll_area.viewport().size()

        if not self.is_custom_zoom:
            scaled_pixmap = self.base_pixmap.scaled(
                viewport_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                transform_mode
            )
            self.image_label.resize(viewport_size)
        else:
            target_w = int(self.base_pixmap.width() * self.zoom_factor)
            target_h = int(self.base_pixmap.height() * self.zoom_factor)
            
            if target_w > 0 and target_h > 0:
                scaled_pixmap = self.base_pixmap.scaled(
                    target_w, target_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    transform_mode
                )
            else:
                scaled_pixmap = self.base_pixmap

            label_w = max(viewport_size.width(), scaled_pixmap.width())
            label_h = max(viewport_size.height(), scaled_pixmap.height())
            self.image_label.resize(label_w, label_h)

        self.image_label.setPixmap(scaled_pixmap)

    def crop_selection(self):
        if self.slideshow_timer.isActive():
            return
        if self.current_pil_image is None or self.image_label.selection_rect.isEmpty():
            return

        pixmap = self.image_label.pixmap()
        if not pixmap or pixmap.width() == 0 or pixmap.height() == 0:
            return

        label_size = self.image_label.size()
        pixmap_size = pixmap.size()

        offset_x = (label_size.width() - pixmap_size.width()) // 2
        offset_y = (label_size.height() - pixmap_size.height()) // 2

        sel_rect = self.image_label.selection_rect
        adj_x = sel_rect.x() - offset_x
        adj_y = sel_rect.y() - offset_y

        crop_x1 = max(0, min(adj_x, pixmap_size.width()))
        crop_y1 = max(0, min(adj_y, pixmap_size.height()))
        crop_x2 = max(0, min(adj_x + sel_rect.width(), pixmap_size.width()))
        crop_y2 = max(0, min(adj_y + sel_rect.height(), pixmap_size.height()))

        if crop_x2 - crop_x1 <= 0 or crop_y2 - crop_y1 <= 0:
            return

        scale_x = self.current_pil_image.width / pixmap_size.width()
        scale_y = self.current_pil_image.height / pixmap_size.height()

        real_x1 = int(crop_x1 * scale_x)
        real_y1 = int(crop_y1 * scale_y)
        real_x2 = int(crop_x2 * scale_x)
        real_y2 = int(crop_y2 * scale_y)

        real_x2 = max(real_x1 + 1, real_x2)
        real_y2 = max(real_y1 + 1, real_y2)

        self.current_pil_image = self.current_pil_image.crop((real_x1, real_y1, real_x2, real_y2))
        self.rotation_angle = 0
        self.image_label.clear_selection()
        
        display_img = self.current_pil_image
        if display_img.mode != "RGBA":
            display_img = display_img.convert("RGBA")
            
        data = display_img.tobytes("raw", "RGBA")
        qimage = QImage(
            data,
            display_img.size[0],
            display_img.size[1],
            QImage.Format.Format_RGBA8888
        ).copy()

        self.base_pixmap = QPixmap.fromImage(qimage)
        self.update_image_display(smooth=True)
        self.show_osd("Cropped in memory (Click Save to write)", duration=1500)

    def reset_crop_or_selection(self):
        if self.slideshow_timer.isActive():
            self.toggle_slideshow()
            return
            
        if 0 <= self.current_index < len(self.image_files):
            self.image_label.clear_selection()
            self.rotation_angle = 0
            self.is_custom_zoom = False
            self.zoom_factor = 1.0
            path = self.image_files[self.current_index]
            if path in self.image_cache:
                del self.image_cache[path]
            self.load_current_image()
            self.show_osd("Reset", duration=1000)

    def rotate_left(self):
        if self.slideshow_timer.isActive():
            return
        if self.current_pil_image:
            self.rotation_angle = (self.rotation_angle - 90) % 360
            self.image_label.clear_selection()
            self.trigger_worker()

    def rotate_right(self):
        if self.slideshow_timer.isActive():
            return
        if self.current_pil_image:
            self.rotation_angle = (self.rotation_angle + 90) % 360
            self.image_label.clear_selection()
            self.trigger_worker()

    def save_image(self):
        if self.slideshow_timer.isActive():
            self.toggle_slideshow()
            
        if self.current_pil_image is None or self.current_index < 0:
            return

        current_path = self.image_files[self.current_index]
        
        no_alpha_formats = (".jpg", ".jpeg", ".bmp")
        save_img = self.current_pil_image
        if current_path.lower().endswith(no_alpha_formats):
            if save_img.mode in ("RGBA", "P"):
                save_img = save_img.convert("RGB")

        try:
            self.base_pixmap = None
            
            ext = os.path.splitext(current_path)[1]
            temp_path = current_path + ".tmp_save" + ext
            
            save_kwargs = dict(self.original_save_kwargs)

            save_img.save(temp_path, **save_kwargs)
            
            if os.path.exists(current_path):
                os.remove(current_path)
            os.rename(temp_path, current_path)

            if current_path in self.image_cache:
                del self.image_cache[current_path]

            self.show_osd("Saved successfully", duration=1500)
            self.load_current_image()
        except Exception as e:
            ext = os.path.splitext(current_path)[1]
            temp_path = current_path + ".tmp_save" + ext
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            self.show_osd(f"Failed to save:\n{str(e)}", duration=3000)

    def trash_current_image(self):
        if not self.image_files or not (0 <= self.current_index < len(self.image_files)):
            return

        image_path = self.image_files[self.current_index]
        
        try:
            self.base_pixmap = None
            self.current_pil_image = None
            
            if image_path in self.image_cache:
                del self.image_cache[image_path]

            abs_path = os.path.abspath(image_path)
            result = subprocess.run(["gio", "trash", abs_path], capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "gio trash failed")

            self.show_osd("Moved to trash", duration=1200)

            del self.image_files[self.current_index]

            if not self.image_files:
                if self.slideshow_timer.isActive():
                    self.toggle_slideshow()
                self.current_index = -1
                self.image_label.clear()
                self.image_label.setText("Open an image to start")
                self.setWindowTitle("Image Viewer")
                self.update_button_states()
            else:
                if self.slideshow_timer.isActive():
                    self.slideshow_history = []
                    self.slideshow_history_pos = -1

                if self.current_index >= len(self.image_files):
                    self.current_index = len(self.image_files) - 1
                self.load_current_image()

        except Exception as e:
            self.show_osd(f"Failed to trash:\n{str(e)}", duration=2500)

    def show_previous_image(self):
        if not self.image_files:
            return
            
        if self.slideshow_timer.isActive():
            self.slideshow_timer.start() 
            if self.slideshow_history_pos > 0:
                self.slideshow_history_pos -= 1
                self.current_index = self.slideshow_history[self.slideshow_history_pos]
                self.trigger_navigation()
            else:
                self.show_osd("Reached the end of the available randomized pool", duration=2000)
        else:
            self.current_index = (self.current_index - 1) % len(self.image_files)
            self.trigger_navigation()

    def show_next_image(self):
        if not self.image_files:
            return
            
        if self.slideshow_timer.isActive():
            self.slideshow_timer.start() 
            if self.slideshow_history_pos < len(self.slideshow_history) - 1:
                self.slideshow_history_pos += 1
                self.current_index = self.slideshow_history[self.slideshow_history_pos]
                self.trigger_navigation()
            else:
                self.show_random_image()
        else:
            self.current_index = (self.current_index + 1) % len(self.image_files)
            self.trigger_navigation()

    def handle_wheel_zoom(self, event):
        if not self.base_pixmap or self.base_pixmap.isNull():
            return

        angle_delta = event.angleDelta().y()
        if angle_delta == 0:
            return

        old_zoom = self.zoom_factor
        if not self.is_custom_zoom:
            self.is_custom_zoom = True
            current_pixmap = self.image_label.pixmap()
            if current_pixmap and self.base_pixmap.width() > 0:
                old_zoom = current_pixmap.width() / self.base_pixmap.width()
            else:
                old_zoom = 1.0
            self.zoom_factor = old_zoom

        viewport_size = self.scroll_area.viewport().size()
        fit_scale_w = viewport_size.width() / float(self.base_pixmap.width())
        fit_scale_h = viewport_size.height() / float(self.base_pixmap.height())
        fit_to_screen_zoom = min(fit_scale_w, fit_scale_h)

        max_allowed_zoom = max(1.0, fit_to_screen_zoom)

        if angle_delta > 0:
            self.zoom_factor *= 1.15
        else:
            self.zoom_factor /= 1.15

        self.zoom_factor = max(0.05, min(max_allowed_zoom, self.zoom_factor))

        cursor_pos = event.position().toPoint()
        h_bar = self.scroll_area.horizontalScrollBar()
        v_bar = self.scroll_area.verticalScrollBar()

        content_x = h_bar.value() + cursor_pos.x()
        content_y = v_bar.value() + cursor_pos.y()

        ratio = self.zoom_factor / old_zoom

        self.update_image_display(smooth=False)

        h_bar.setValue(int(content_x * ratio - cursor_pos.x()))
        v_bar.setValue(int(content_y * ratio - cursor_pos.y()))
        
        self.smooth_zoom_timer.start(150)

    def reset_to_fit_screen(self):
        if self.base_pixmap:
            self.is_custom_zoom = False
            self.zoom_factor = 1.0
            self.update_image_display(smooth=True)
            self.image_label.update()

    def reset_to_actual_size_or_fit(self):
        if not self.base_pixmap or self.base_pixmap.isNull():
            return

        is_already_actual = self.is_custom_zoom and abs(self.zoom_factor - 1.0) < 1e-5

        if is_already_actual:
            self.reset_to_fit_screen()
            self.show_osd("Fit to Screen", duration=1000)
            return

        self.is_custom_zoom = True
        self.zoom_factor = 1.0

        self.update_image_display(smooth=True)

        h_bar = self.scroll_area.horizontalScrollBar()
        v_bar = self.scroll_area.verticalScrollBar()
        
        self.scroll_area.viewport().update()
        QApplication.processEvents()

        h_bar.setValue((h_bar.minimum() + h_bar.maximum()) // 2)
        v_bar.setValue((v_bar.minimum() + v_bar.maximum()) // 2)
        
        self.show_osd("1:1 Actual Size", duration=1000)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reposition_osd()
        self.reposition_progress_bar()
        if not self.is_custom_zoom:
            self.update_image_display(smooth=True)


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    initial_path = sys.argv[1] if len(sys.argv) > 1 else None
    
    viewer = ImageViewer(initial_path)
    viewer.show()
    sys.exit(app.exec())
