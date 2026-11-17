#sudo pacman -S python python-pillow python-pyqt6 glib2 python-pillow-heif python-pillow-jpegxl-plugin
#NEED TO INSTALL THESE ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
# (glib2 provides gio, used for the trash feature; the last two are optional: HEIC / HEIF and JPEG XL support,
#  picked up automatically when installed)

import math
import os
import shutil
import sys
import threading
import time
from collections import deque

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, QRectF, QSize, QThread, Qt, pyqtSignal, pyqtSlot, QTimer
from PyQt6.QtGui import QColor, QCursor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
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
BTN_TEXT_SHARPEN = "✦"
BTN_TEXT_CROP = "✃"
BTN_TEXT_RESET_CROP = "↺"
BTN_TEXT_SAVE = "↓"
BTN_TEXT_FULLSCREEN = "⇱"
BTN_TEXT_SLIDESHOW = "⏱"
BTN_TEXT_INFO = "💬" #💬ⓘ𝕚 are not bad
BTN_TEXT_MINIFY = "→←"
BTN_TEXT_EXIT = "✕"

SLIDESHOW_INTERVAL_MS = 5500   # default; right-click the slideshow button for others
SLIDESHOW_PRESETS_MS = [1000, 2000, 3000, 4000, 5500, 8000, 10000, 15000,
                        30000, 60000, 120000, 300000]

# Memory: preloaded neighbours (plus the image on screen) may use about this much RAM.
# Counted per image as RGBA QImage + PIL image, e.g. 150 MP is roughly 1 GB. Nearest
# images are cached first; when the next one wouldn't fit, caching simply stops, so a
# folder of huge files ends up holding only the image being viewed (and what fits).
CACHE_MEMORY_LIMIT_BYTES = 4 * 1024**3

# Smooth scaling is done by Pillow in this many pool threads (Pillow releases the GIL),
# so panning/zooming a huge image keeps the window responsive and uses several cores.
RENDER_THREADS = max(2, min(4, os.cpu_count() or 2))
RENDER_MODES = ("RGB", "RGBA", "L", "LA")      # PIL modes Pillow resizes with proper filtering
ASYNC_SMOOTH_MIN_PIXELS = 12_000_000           # fit-to-window smoothing of images this big runs in the background

# Quick preview: a JPEG this big that isn't preloaded yet is first decoded at 1/8 size
# (the JPEG decoder can skip most of the work that way) and shown within a fraction of
# a second, while the full-resolution decode carries on in the background.
PREVIEW_MIN_PIXELS = 4_000_000
PREVIEW_DENOMINATOR = 8
PREVIEW_COLOR = "#e8891a"        # progress bar colour while the preview itself is being decoded (blue afterwards)
# Previews are tiny (about 10 MB for a 150 MP photo), so they are built ahead of time, by
# their own threads, for more neighbours than the full-size cache can ever hold, and they
# do not count towards CACHE_MEMORY_LIMIT_BYTES. Stepping to a picture that isn't fully
# cached then shows its preview instantly. Keep arrow keys pressed -> they flick past.
PREVIEW_THREADS = 2
PREVIEW_CACHE_AHEAD = 12
PREVIEW_CACHE_BEHIND = 3

HINT_DELAY_MS = 1000           # hover time before a button's description shows in the OSD

# Sharpening (the "S" button / Shift+S): unsharp mask on LUMINANCE only (colour is left
# untouched), optionally limited to edges.
SHARPEN_RADIUS = 1.4             # blur radius in px; small = fine crisp detail (like Irfan/LR ~1.0), large = broad "texture" look (try 0.5 - 3)
SHARPEN_PERCENT = 120            # strength; with small radii 80 - 200 is normal
SHARPEN_THRESHOLD = 0            # min brightness difference (0-255) to sharpen
SHARPEN_MASKING = 0             # 0 = sharpen everything; higher = only strong edges, flat areas / noise / skies untouched (like LR's Masking, 0 - 100)

UI_FONT_SIZE = "16px"
BTN_WIDTH = 36
BTN_HEIGHT = 36

SLIDER_HANDLE_COLOR = "#47567a"
SLIDER_HOVER_COLOR = "#BFBFBF"


class ProgressFile:
    """File wrapper that reports how far through the file the decoder has read.

    Decoders such as JPEG / PNG / BMP / TIFF / GIF pull the file in blocks while
    decoding, so the read position is a faithful measure of decoding progress.
    Reads are only counted once `counting` is switched on (right before load()),
    so header parsing doesn't register. Formats that read the whole file up front
    and decode from memory (WebP) report nothing until the decode is done.
    """

    def __init__(self, f, size, callback):
        self._f = f
        self._size = max(1, size)
        self._callback = callback
        self._last = -1
        self.counting = False

    def read(self, n=-1):
        data = self._f.read(n)
        if self.counting:
            pct = min(100, self._f.tell() * 100 // self._size)
            if pct > self._last:
                self._last = pct
                self._callback(pct)
        return data

    def seek(self, *args):
        return self._f.seek(*args)

    def tell(self):
        return self._f.tell()

    def __getattr__(self, name):
        return getattr(self._f, name)


def pil_to_qimage(pil_img):
    """RGBA8888 QImage from a PIL image.

    Converts in strips straight into the QImage, so no second full-size RGBA copy of the
    picture is ever held in RAM (for 150 MP that saves well over a GB while loading).
    Falls back to the plain conversion if the strip copy isn't possible.
    """
    w, h = pil_img.size
    try:
        qimage = QImage(w, h, QImage.Format.Format_RGBA8888)
        if qimage.isNull() or qimage.bytesPerLine() != w * 4:
            raise ValueError("unexpected QImage layout")
        ptr = qimage.bits()
        ptr.setsize(qimage.sizeInBytes())
        view = memoryview(ptr).cast("B")
        row_bytes = w * 4
        step = max(1, (8 * 1024 * 1024) // row_bytes)
        for y in range(0, h, step):
            y2 = min(h, y + step)
            strip = pil_img.crop((0, y, w, y2))
            if strip.mode != "RGBA":
                strip = strip.convert("RGBA")
            view[y * row_bytes:y2 * row_bytes] = strip.tobytes("raw", "RGBA")
        return qimage
    except Exception:
        display_img = pil_img if pil_img.mode == "RGBA" else pil_img.convert("RGBA")
        data = display_img.tobytes("raw", "RGBA")
        return QImage(
            data,
            display_img.size[0],
            display_img.size[1],
            QImage.Format.Format_RGBA8888
        ).copy()


class Superseded(Exception):
    """Raised from a read callback to abort a decode that nobody is waiting for any more."""


def decode_preview(image_path, on_start=None, on_progress=None, is_stale=None):
    """1/8-size RGB PIL image of a big JPEG, or None if the file doesn't qualify
    (not a JPEG, below PREVIEW_MIN_PIXELS, or draft decoding not possible).
    The size test only looks at the header, so it costs next to nothing."""
    from PIL import Image

    def tick(pct):
        if is_stale is not None and is_stale():
            raise Superseded()
        if on_progress is not None:
            on_progress(pct)

    with open(image_path, "rb") as f:
        pf = ProgressFile(f, os.path.getsize(image_path), tick)
        with Image.open(pf) as img:
            if img.format != "JPEG" or img.width * img.height < PREVIEW_MIN_PIXELS:
                return None
            if is_stale is not None and is_stale():
                raise Superseded()
            original_size = img.size
            img.draft("RGB", (max(1, img.width // PREVIEW_DENOMINATOR),
                              max(1, img.height // PREVIEW_DENOMINATOR)))
            if img.size == original_size:
                return None   # not possible for this file: not worth a second full decode
            if on_start is not None:
                on_start()
            pf.counting = True
            img.load()
            small = img.copy()
    return small if small.mode == "RGB" else small.convert("RGB")


_preview_pool = None


def preview_pool():
    global _preview_pool
    if _preview_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _preview_pool = ThreadPoolExecutor(max_workers=PREVIEW_THREADS, thread_name_prefix="preview")
    return _preview_pool


_render_pool = None


def render_pool():
    global _render_pool
    if _render_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        _render_pool = ThreadPoolExecutor(max_workers=RENDER_THREADS, thread_name_prefix="render")
    return _render_pool


def shutdown_render_pool():
    for pool in (_render_pool, _preview_pool):
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


def render_region_rgba(pil_img, box, out_w, out_h):
    """Smooth-scale the (float) source box of pil_img to out_w x out_h, as RGBA bytes.
    Runs in pool threads; Pillow does the heavy lifting without holding the GIL."""
    from PIL import Image

    resample = getattr(Image, "Resampling", Image).BILINEAR
    tile = pil_img.resize((out_w, out_h), resample, box=box, reducing_gap=2.0)
    if tile.mode != "RGBA":
        tile = tile.convert("RGBA")
    return tile.tobytes("raw", "RGBA")


_extensions = None


def register_optional_plugins():
    """HEIC / HEIF, JPEG XL and similar are not part of Pillow itself. If their (optional)
    plugin packages are installed - pillow-heif, pillow-jxl-plugin - hook them in, so those
    files open like any other. Without them nothing changes."""
    from PIL import Image

    try:
        import pillow_heif
        pillow_heif.register_heif_opener()      # .heic .heif .hif
    except Exception:
        pass
    try:
        import pillow_jxl  # noqa: F401   (registers JPEG XL, .jxl, when imported)
    except Exception:
        pass
    if "AVIF" not in Image.OPEN:                # Pillow 11.3+ has AVIF built in
        try:
            import pillow_avif  # noqa: F401
        except Exception:
            pass


def supported_image_extensions():
    """Every file extension this Pillow can open, as a tuple of lower-case ".ext" strings.
    Pillow's plugins register themselves on Image.init(), so whatever formats this install
    supports (including optional ones) show up here automatically."""
    global _extensions
    if _extensions is None:
        from PIL import Image

        Image.init()
        register_optional_plugins()
        # formats that Pillow lists but can't show on their own
        skip = {
            "BUFR", "GRIB", "HDF5",   # stubs that need a handler supplied by the application
            "MPEG",                   # video, not pictures
        }
        if sys.platform != "win32":
            skip.add("WMF")           # Pillow renders WMF / EMF on Windows only
        if not (shutil.which("gs") or shutil.which("gswin64c") or shutil.which("gswin32c")):
            skip.add("EPS")           # EPS / PS need Ghostscript
        found = {
            ext.lower() for ext, fmt in Image.registered_extensions().items()
            if fmt in Image.OPEN and fmt not in skip
        }
        found.add(".mpo")             # multi-picture JPEG: opened through the JPEG plugin
        _extensions = tuple(sorted(found))
    return _extensions


def to_displayable(pil_img):
    """16-bit / 32-bit integer and float images (16-bit TIFF / PNG, FITS, PFM ...) as 8-bit
    grey. Pillow's own conversion just clips everything above 255, which turns them white."""
    mode = pil_img.mode
    try:
        if mode == "I" or mode.startswith("I;16"):
            hi = pil_img.getextrema()[1]
            divisor = 1 if hi <= 255 else 16 if hi <= 4095 else 256 if hi <= 65535 else hi / 255.0
            return pil_img.convert("I").point(lambda v: v * (1.0 / divisor)).convert("L")
        if mode == "F":
            lo, hi = pil_img.getextrema()
            scale = 255.0 / (hi - lo) if hi > lo else 1.0
            return pil_img.point(lambda v: (v - lo) * scale).convert("L")
        if mode == "RGBa":
            return pil_img.convert("RGBA")
        if mode == "La":
            return pil_img.convert("LA")
    except Exception:
        pass   # leave it as is; the usual conversion afterwards may still cope
    return pil_img


class MainImageWorker(QObject):
    finished = pyqtSignal(int, QImage, object, str)
    progress = pyqtSignal(int, int)   # request_id, percent
    preview_ready = pyqtSignal(int, QImage)   # request_id, small quick-look version
    preview_started = pyqtSignal(int)         # request_id: a preview is being decoded (orange bar)

    def __init__(self):
        super().__init__()
        # Written by the GUI thread, read here. If the user has already moved on,
        # a queued request is skipped instead of being decoded for nothing.
        self.latest_request_id = 0

    @pyqtSlot(int, str, float, bool)
    def process_image(self, request_id, image_path, rotation_angle, has_preview):
        if request_id != self.latest_request_id:
            return
        try:
            from PIL import Image, UnidentifiedImageError

            def stale():
                return request_id != self.latest_request_id

            # Rough share of the total time per stage (measured on a 40 MP JPEG):
            # decoding fills 0-55 %, the conversion steps below fill the rest.
            emit = self.progress.emit

            if not has_preview:
                try:
                    small = decode_preview(
                        image_path,
                        on_start=lambda: self.preview_started.emit(request_id),
                        on_progress=lambda pct: emit(request_id, pct),
                        is_stale=stale,
                    )
                    if small is not None:
                        if rotation_angle != 0:
                            small = small.rotate(-rotation_angle, expand=True)
                        if not stale():
                            self.preview_ready.emit(request_id, pil_to_qimage(small))
                except Superseded:
                    return
                except Exception:
                    pass   # the preview is only a bonus
                if stale():
                    return

            with open(image_path, "rb") as f:
                def decode_tick(pct):
                    if stale():
                        raise Superseded()   # user moved on: stop decoding right now
                    emit(request_id, pct * 55 // 100)

                pf = ProgressFile(f, os.path.getsize(image_path), decode_tick)
                with Image.open(pf) as img:
                    pf.counting = True
                    img.load()
                    pil_img = to_displayable(img.copy())
            emit(request_id, 65)

            if rotation_angle != 0:
                pil_img = pil_img.rotate(-rotation_angle, expand=True)
                emit(request_id, 72)

            emit(request_id, 82)

            qimage = pil_to_qimage(pil_img)
            emit(request_id, 93)

            self.finished.emit(request_id, qimage, pil_img, "")

        except Superseded:
            return
        except (UnidentifiedImageError, OSError, Exception) as err:
            self.finished.emit(request_id, QImage(), None, str(err))


class CacheWorker(QObject):
    cache_finished = pyqtSignal(str, QImage, object)
    cache_needs_room = pyqtSignal(str, object)   # path, bytes it needs: viewer frees lower-priority images
    wanted_paths = None   # set by the GUI thread: a load of anything outside it is abandoned

    def check_wanted(self, image_path):
        def check(_pct):
            wanted = self.wanted_paths
            if wanted is not None and image_path not in wanted:
                raise Superseded()
        return check

    @pyqtSlot(str, object, object)
    def preload_image(self, image_path, free_bytes, reclaimable_bytes):
        try:
            from PIL import Image

            with open(image_path, "rb") as f:
                pf = ProgressFile(f, os.path.getsize(image_path), self.check_wanted(image_path))
                with Image.open(pf) as img:
                    # only the header has been read so far: check the memory budget first
                    w, h = img.size
                    bands = len(img.getbands())
                    # RGBA QImage + PIL image, plus the second PIL copy that exists while decoding
                    need = w * h * (4 + 2 * bands)
                    if need > free_bytes:
                        if need <= free_bytes + reclaimable_bytes:
                            # fits if less important cached images are dropped first
                            self.cache_needs_room.emit(image_path, need)
                        else:
                            self.cache_finished.emit(image_path, QImage(), None)
                        return
                    pf.counting = True
                    img.load()
                    pil_img = to_displayable(img.copy())

            qimage = pil_to_qimage(pil_img)

            self.cache_finished.emit(image_path, qimage, pil_img)
        except Superseded:
            pass   # the user moved on; the next schedule_idle_cache() restarts preloading
        except Exception:
            pass


def sharpen_luma(img, unsharp, masking):
    """Unsharp-mask the luminance only, optionally restricted to edges.

    Sharpening R, G and B separately also boosts colour noise and fringes, which
    gives a crunchy, texture-like look. Working on Y (of YCbCr) keeps the colour
    channels and alpha untouched. `masking` (0-100) fades the effect out in flat
    areas, based on a slightly smoothed edge map of the luminance.
    """
    from PIL import Image, ImageFilter

    bands = img.getbands()
    alpha = img.getchannel("A") if "A" in bands else None
    gray_only = bands in (("L",), ("L", "A"))
    base = img.convert("L") if gray_only else img.convert("RGB")

    if gray_only:
        y = base
    else:
        y, cb, cr = base.convert("YCbCr").split()

    y_sharp = y.filter(unsharp)

    if masking > 0:
        # pre-blur so pixel noise isn't mistaken for an edge, then threshold the edge map
        lo = masking * 0.1
        hi = lo + 12
        lut = [min(255, max(0, int((v - lo) * 255 / (hi - lo)))) for v in range(256)]
        mask = (y.filter(ImageFilter.GaussianBlur(0.8)).filter(ImageFilter.FIND_EDGES)
                .point(lut).filter(ImageFilter.GaussianBlur(1.0)))
        y_sharp = Image.composite(y_sharp, y, mask)

    if gray_only:
        out = y_sharp
    else:
        out = Image.merge("YCbCr", (y_sharp, cb, cr)).convert("RGB")
    if alpha is not None:
        out.putalpha(alpha)
    return out


class SharpenWorker(QObject):
    finished = pyqtSignal(int, QImage, object, str)
    progress = pyqtSignal(int, int)   # request_id, percent

    @pyqtSlot(int, object, float, int, int)
    def process_sharpen(self, request_id, pil_img, radius, percent, threshold):
        try:
            from PIL import Image, ImageFilter

            src = pil_img
            if src.mode in ("P", "1"):
                src = src.convert("RGBA")

            # PIL's filter is a single opaque call with no progress callback, so the
            # image is sharpened in horizontal bands instead. Each band is cut with a
            # margin of extra rows so the blur at band edges sees the real neighbouring
            # pixels, which makes the stitched result match a one-shot sharpen.
            width, height = src.size
            unsharp = ImageFilter.UnsharpMask(radius=radius, percent=percent, threshold=threshold)
            margin = max(int(radius * 4) + 2, 8)   # also covers the edge-mask kernels
            masking = int(SHARPEN_MASKING)
            band_count = max(1, min(20, height // 64))
            sharpened = None

            for i in range(band_count):
                y0 = height * i // band_count
                y1 = height * (i + 1) // band_count
                top = max(0, y0 - margin)
                bottom = min(height, y1 + margin)
                band = sharpen_luma(src.crop((0, top, width, bottom)), unsharp, masking)
                band = band.crop((0, y0 - top, width, y0 - top + (y1 - y0)))
                if sharpened is None:
                    sharpened = Image.new(band.mode, src.size)
                sharpened.paste(band, (0, y0))
                # keep the last few % for the QImage conversion below
                self.progress.emit(request_id, (i + 1) * 95 // band_count)

            display_img = sharpened
            if display_img.mode != "RGBA":
                display_img = display_img.convert("RGBA")

            data = display_img.tobytes("raw", "RGBA")
            qimage = QImage(
                data,
                display_img.size[0],
                display_img.size[1],
                QImage.Format.Format_RGBA8888
            ).copy()

            self.finished.emit(request_id, qimage, sharpened, "")
        except Exception as err:
            self.finished.emit(request_id, QImage(), None, str(err))


class FolderScanWorker(QObject):
    # request_id, sorted image paths, index of the selected file (or 0 / -1), ok
    finished = pyqtSignal(int, object, int, bool)

    @pyqtSlot(int, str, str, object)
    def scan(self, request_id, folder_path, selected_file, extensions):
        try:
            all_files = os.listdir(folder_path)
        except OSError:
            self.finished.emit(request_id, [], -1, False)
            return

        if not extensions:
            extensions = supported_image_extensions()   # imports Pillow here, not on the GUI thread
        image_files = []
        for f in sorted(all_files):
            if f.lower().endswith(extensions):
                image_files.append(os.path.normpath(os.path.join(folder_path, f)))

        if selected_file in image_files:
            index = image_files.index(selected_file)
        else:
            index = 0 if image_files else -1

        self.finished.emit(request_id, image_files, index, True)


class HoverHintController(QObject):
    """Shows a button's description in the OSD after the cursor rests on it.

    One shared QTimer; the buttons report enter/leave through their own virtual
    methods, so no Python runs for unrelated events.
    """

    def __init__(self, viewer):
        super().__init__(viewer)
        self._viewer = viewer
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._show)
        self._target = None
        self._shown_text = None

    def hover(self, button):
        self._target = button
        self._timer.start(HINT_DELAY_MS)

    def cancel(self):
        self._timer.stop()
        self._target = None
        # only remove the OSD if it is still our hint, not a message from an action
        osd = self._viewer.osd_label
        if self._shown_text is not None and osd.isVisible() and osd.text() == self._shown_text:
            self._viewer.hide_osd()
        self._shown_text = None

    def _show(self):
        button = self._target
        if button is None or not button.isVisible():
            return
        self._shown_text = button.tip_text()
        self._viewer.show_osd(self._shown_text, duration=6000)


class TipButton(QPushButton):
    def __init__(self, text, tip, controller, parent=None):
        super().__init__(text, parent)
        self._tip = tip
        self._controller = controller

    def tip_text(self):
        return self._tip

    def set_tip(self, tip):
        self._tip = tip

    def enterEvent(self, event):
        self._controller.hover(self)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._controller.cancel()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        self._controller.cancel()
        super().mousePressEvent(event)

    def hideEvent(self, event):
        # the bar disappears in fullscreen without a leave event
        self._controller.cancel()
        super().hideEvent(event)


class PendingIndicator(QWidget):
    """Big orange circle with a white "!": an action was queued and will run once the
    image has finished loading."""

    SIZE = 170

    def __init__(self, parent):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        color = QColor(PREVIEW_COLOR)
        color.setAlpha(235)
        painter.setBrush(color)
        painter.drawEllipse(self.rect().adjusted(4, 4, -4, -4))
        painter.setPen(QColor("white"))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(self.SIZE * 0.7))
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "!")


class ImageCanvas(QLabel):
    tile_ready = pyqtSignal(object)   # (key, tx, ty, w, h, rgba bytes / None / False), from pool threads

    def __init__(self, parent=None):
        super().__init__(parent)
        self.selection_rect = QRect()
        self.is_selecting = False
        self.is_panning = False
        self.pan_start_pos = QPoint()
        self.selection_start_pos = QPoint()
        # zoomed-in view: the picture is not turned into a giant pixmap; only the
        # exposed part is scaled and painted straight from the source image
        self.virtual_image = None
        self.virtual_size = QSize()
        self.virtual_smooth = False
        self.tiles = {}        # cached smooth tiles, see paint_virtual
        self.tile_key = None
        self.tile_px = 512
        self.virtual_pil = None    # PIL twin of virtual_image: lets pool threads render tiles
        self.pending_tiles = set()
        self.bad_tiles = set()
        self.wanted = (0, 0, 0, 0)  # visible area (+1 tile) in picture coordinates
        self.tile_ready.connect(self.on_tile_ready)

    def set_virtual(self, image, size, smooth, pil=None):
        self.virtual_image = image
        self.virtual_size = size
        self.virtual_smooth = smooth
        self.virtual_pil = pil
        super().clear()   # drop any old pixmap / text
        self.update()

    def clear_virtual(self):
        self.virtual_image = None
        self.virtual_pil = None
        self.virtual_size = QSize()
        self.tiles = {}
        self.tile_key = None
        self.pending_tiles = set()
        self.bad_tiles = set()

    def clear(self):
        self.clear_virtual()
        super().clear()

    def displayed_size(self):
        """Size of the picture as currently shown (QSize(), if there is none)."""
        if self.virtual_image is not None:
            return self.virtual_size
        pixmap = self.pixmap()
        if pixmap and not pixmap.isNull():
            return pixmap.size()
        return QSize()

    def paint_virtual(self, exposed):
        vw, vh = self.virtual_size.width(), self.virtual_size.height()
        if vw <= 0 or vh <= 0:
            return
        ox = (self.width() - vw) // 2
        oy = (self.height() - vh) // 2
        target = exposed.intersected(QRect(ox, oy, vw, vh))
        if target.isEmpty():
            return

        image = self.virtual_image
        ratio_x = image.width() / vw
        ratio_y = image.height() / vh

        painter = QPainter(self)
        try:
            if not self.virtual_smooth or ratio_x <= 1.0:
                # Straight from the source image: cost depends only on the number
                # of pixels painted. The source->screen mapping is absolute, so
                # separately painted areas always line up. Bilinear when smooth
                # (that is exactly what upscaling needs), nearest while zooming.
                if self.virtual_smooth and ratio_x < 1.0:
                    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
                painter.drawImage(
                    QRectF(target),
                    image,
                    QRectF(
                        (target.left() - ox) * ratio_x,
                        (target.top() - oy) * ratio_y,
                        target.width() * ratio_x,
                        target.height() * ratio_y,
                    ),
                )
                return

            # Smooth downscale: high-quality scaling on fixed grid tiles that are
            # rendered once and cached, so a partial repaint (e.g. the crop rectangle
            # moving) just re-blits the same tiles and nothing underneath can shimmer.
            # With a PIL twin available the tiles are rendered by pool threads: until
            # a tile arrives, a quick nearest-neighbour version of it is shown, so the
            # GUI thread never waits for the scaler.
            key = (image.cacheKey(), vw, vh)
            if key != self.tile_key:
                self.tiles = {}
                self.tile_key = key
                self.pending_tiles = set()
                self.bad_tiles = set()
            tile_px = max(64, min(512, int(2048 / ratio_x)))
            self.tile_px = tile_px
            use_pool = self.virtual_pil is not None

            vis = self.visibleRegion().boundingRect()
            self.wanted = (vis.left() - ox - tile_px, vis.top() - oy - tile_px,
                           vis.right() - ox + tile_px, vis.bottom() - oy + tile_px)

            first_tx = (target.left() - ox) // tile_px
            last_tx = (target.right() - ox) // tile_px
            first_ty = (target.top() - oy) // tile_px
            last_ty = (target.bottom() - oy) // tile_px
            for ty in range(first_ty, last_ty + 1):
                for tx in range(first_tx, last_tx + 1):
                    x, y = ox + tx * tile_px, oy + ty * tile_px
                    tile = self.tiles.pop((tx, ty), None)
                    if tile is not None:
                        self.tiles[(tx, ty)] = tile     # most recently used goes last
                        painter.drawImage(x, y, tile)
                        continue
                    w = min(tile_px, vw - tx * tile_px)
                    h = min(tile_px, vh - ty * tile_px)
                    if use_pool:
                        painter.drawImage(
                            QRectF(x, y, w, h),
                            image,
                            QRectF(tx * tile_px * ratio_x, ty * tile_px * ratio_y,
                                   w * ratio_x, h * ratio_y),
                        )
                        self.request_tile(key, tx, ty, w, h, tile_px, ratio_x, ratio_y)
                    else:
                        tile = self.render_tile(image, vw, vh, tx, ty, tile_px, ratio_x, ratio_y)
                        self.store_tile(tx, ty, tile)
                        painter.drawImage(x, y, tile)
        finally:
            painter.end()

    def store_tile(self, tx, ty, tile):
        while len(self.tiles) >= 160:
            self.tiles.pop(next(iter(self.tiles)))
        self.tiles[(tx, ty)] = tile

    def request_tile(self, key, tx, ty, w, h, tile_px, ratio_x, ratio_y):
        job = (key, tx, ty)
        if job in self.pending_tiles or job in self.bad_tiles:
            return
        pil = self.virtual_pil
        x0, y0 = tx * tile_px, ty * tile_px
        box = (x0 * ratio_x, y0 * ratio_y,
               min(pil.width, (x0 + w) * ratio_x), min(pil.height, (y0 + h) * ratio_y))
        self.pending_tiles.add(job)
        render_pool().submit(self.tile_job, job, pil, box, w, h)

    def tile_job(self, job, pil, box, w, h):
        """Runs in a pool thread."""
        key, tx, ty = job
        result = None   # None = skipped (stale / scrolled out of view), False = failed
        try:
            x0, y0 = tx * self.tile_px, ty * self.tile_px
            l, t, r, b = self.wanted
            if key == self.tile_key and x0 < r and x0 + w > l and y0 < b and y0 + h > t:
                result = render_region_rgba(pil, box, w, h)
        except Exception:
            result = False
        try:
            self.tile_ready.emit((job, w, h, result))
        except RuntimeError:
            pass   # window already closed

    def on_tile_ready(self, payload):
        job, w, h, result = payload
        key, tx, ty = job
        self.pending_tiles.discard(job)
        if key != self.tile_key or self.virtual_image is None:
            return
        if result is False:
            self.bad_tiles.add(job)   # keep the quick version, don't retry forever
            return
        if result is None:
            l, t, r, b = self.wanted  # skipped earlier; if it is on screen now, repaint asks again
            if tx * self.tile_px < r and (tx * self.tile_px + w) > l \
                    and ty * self.tile_px < b and (ty * self.tile_px + h) > t:
                self.update()
            return
        self.store_tile(tx, ty, QImage(result, w, h, QImage.Format.Format_RGBA8888).copy())
        ox = (self.width() - self.virtual_size.width()) // 2
        oy = (self.height() - self.virtual_size.height()) // 2
        self.update(QRect(ox + tx * self.tile_px, oy + ty * self.tile_px, w, h))

    @staticmethod
    def render_tile(image, vw, vh, tx, ty, tile_px, ratio_x, ratio_y):
        x0, y0 = tx * tile_px, ty * tile_px
        w = min(tile_px, vw - x0)
        h = min(tile_px, vh - y0)
        sx1 = max(0, int(x0 * ratio_x))
        sy1 = max(0, int(y0 * ratio_y))
        sx2 = min(image.width(), max(sx1 + 1, math.ceil((x0 + w) * ratio_x)))
        sy2 = min(image.height(), max(sy1 + 1, math.ceil((y0 + h) * ratio_y)))
        tile = image.copy(QRect(sx1, sy1, sx2 - sx1, sy2 - sy1))
        if tile.width() != w or tile.height() != h:
            tile = tile.scaled(
                w, h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        return tile

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
            self.set_selection_rect(QRect(self.selection_start_pos, current_pos).normalized())

    def set_selection_rect(self, new_rect):
        # repaint only the area the rubber band touched, not the whole canvas
        old_rect = self.selection_rect
        self.selection_rect = new_rect
        self.update(old_rect.united(new_rect).adjusted(-3, -3, 3, 3))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        elif event.button() == Qt.MouseButton.RightButton:
            self.is_selecting = False
            current_pos = event.position().toPoint()
            self.set_selection_rect(QRect(self.selection_start_pos, current_pos).normalized())

    def paintEvent(self, event):
        if self.virtual_image is not None:
            self.paint_virtual(event.rect())
        else:
            super().paintEvent(event)
        if not self.selection_rect.isNull() and not self.displayed_size().isEmpty():
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
    request_processing = pyqtSignal(int, str, float, bool)   # id, path, rotation, a preview is already on screen
    preview_cached = pyqtSignal(object)   # (path, QImage / None skipped / False not previewable), from pool threads
    request_cache = pyqtSignal(str, object, object)   # path, free bytes, reclaimable bytes (object: can exceed a 32-bit int)
    fit_ready = pyqtSignal(object)         # (token, w, h, rgba bytes / None), from pool threads
    request_sharpen = pyqtSignal(int, object, float, int, int)
    request_scan = pyqtSignal(int, str, str, object)

    def __init__(self, initial_path=None):
        super().__init__()
        self.setWindowTitle("Image Viewer")
        self.resize(1100, 750)

        self.supported_extensions = None   # None: everything Pillow can open (supported_image_extensions)
        self.image_files = []
        self.current_index = -1
        
        self.current_pil_image = None
        self.base_image = None
        self.original_save_kwargs = {}

        self.image_cache = {}
        self.preload_queue = deque()
        self.cache_order = []   # cache priority around the current image, most important first
        
        self.slideshow_history = []
        self.slideshow_history_pos = -1

        self.rotation_angle = 0
        self.zoom_factor = 1.0
        self.is_custom_zoom = False

        self.slideshow_interval_ms = SLIDESHOW_INTERVAL_MS
        self.slideshow_random = True   # False = sequential order

        self.request_counter = 0
        self.scan_counter = 0
        self.fit_token = 0   # bumped on every redraw; lets background smoothing drop stale results
        self.loading_image = False       # the current image is still being loaded / decoded
        self.pending_actions = deque()   # (function, label) queued while loading, run in order afterwards
        self.draining = False
        self.preview_image = None   # quick 1/8-size look at the image that is still loading
        self.preview_cache = {}     # path -> 1/8-size QImage, built ahead of time (see PREVIEW_CACHE_*)
        self.preview_pending = set()
        self.preview_none = set()   # paths that have no preview (small / not a JPEG)
        self.preview_wanted = set() # read by the pool threads: previews outside it are not worth decoding
        self.preview_cached.connect(self.on_preview_cached)
        self.fit_ready.connect(self.on_fit_ready)

        self.load_timer = QTimer(self)
        self.load_timer.setSingleShot(True)
        self.load_timer.timeout.connect(self.trigger_worker)

        self.idle_cache_timer = QTimer(self)
        self.idle_cache_timer.setSingleShot(True)
        self.idle_cache_timer.timeout.connect(self.process_next_idle_cache)

        self.slideshow_timer = QTimer(self)
        self.slideshow_timer.setInterval(self.slideshow_interval_ms)
        self.slideshow_timer.timeout.connect(self.slideshow_tick)

        self.smooth_zoom_timer = QTimer(self)
        self.smooth_zoom_timer.setSingleShot(True)
        self.smooth_zoom_timer.timeout.connect(lambda: self.update_image_display(smooth=True))

        # wheel zoom steps are collapsed into one render per ~16 ms, so a burst of
        # wheel events on a huge image can't queue up several full-size rescales
        self.wheel_render_timer = QTimer(self)
        self.wheel_render_timer.setSingleShot(True)
        self.wheel_render_timer.timeout.connect(self.apply_wheel_zoom)
        self.wheel_anchor = None   # (cursor_pos, content_x, content_y, rendered_zoom)

        self.osd_timer = QTimer(self)
        self.osd_timer.setSingleShot(True)
        self.osd_timer.timeout.connect(self.hide_osd)

        # sharpening runs in a worker thread that reports real progress
        self.sharpen_busy = False
        self.sharpen_request_id = 0
        self.sharpen_source = None

        self._was_fullscreen = False

        self.init_ui()
        self.apply_initial_theme()

        # Save initial path for deferred post-show loading
        self._initial_path = initial_path
        self.threads_initialized = False

        QApplication.instance().installEventFilter(self)

        # Everything below runs after the window has been shown and painted once:
        # complete_startup() is kicked off from __main__.
        self._startup_started = False

    def complete_startup(self):
        # step 1: toolbar buttons + full stylesheet (the window is already visible)
        if self._startup_started:
            return
        self._startup_started = True
        self.build_controls()
        self.apply_dark_theme()
        # step 2 in a separate event-loop pass so the finished toolbar can paint first
        QTimer.singleShot(0, self.start_workers_and_load)

    def start_workers_and_load(self):
        # step 2: worker threads, then the folder scan / first image
        if not self.threads_initialized:
            self.setup_worker_threads()
            self.threads_initialized = True
        if self._initial_path:
            path = self._initial_path
            self._initial_path = None
            self.load_from_path(path)

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowStateChange:
            is_now_fullscreen = self.isFullScreen()
            if is_now_fullscreen != self._was_fullscreen:
                self._was_fullscreen = is_now_fullscreen
                # get_stylesheet() ignores its `fullscreen` argument, so re-applying it
                # here only forced a full re-polish of every widget for no visual change.
                if is_now_fullscreen:
                    self.controls_wrapper.hide()
                else:
                    self.controls_wrapper.show()
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
        self.main_worker.progress.connect(self.on_load_progress)
        self.main_worker.preview_ready.connect(self.on_preview_ready)
        self.main_worker.preview_started.connect(self.on_preview_started)
        self.main_worker_thread.start()

        self.cache_worker_thread = QThread()
        self.cache_worker = CacheWorker()
        self.cache_worker.moveToThread(self.cache_worker_thread)
        self.request_cache.connect(self.cache_worker.preload_image)
        self.cache_worker.cache_finished.connect(self.on_cache_processed)
        self.cache_worker.cache_needs_room.connect(self.on_cache_needs_room)
        self.cache_worker_thread.start()

        self.sharpen_worker_thread = QThread()
        self.sharpen_worker = SharpenWorker()
        self.sharpen_worker.moveToThread(self.sharpen_worker_thread)
        self.request_sharpen.connect(self.sharpen_worker.process_sharpen)
        self.sharpen_worker.finished.connect(self.on_sharpen_processed)
        self.sharpen_worker.progress.connect(self.on_sharpen_progress)
        self.sharpen_worker_thread.start()

        self.scan_worker_thread = QThread()
        self.scan_worker = FolderScanWorker()
        self.scan_worker.moveToThread(self.scan_worker_thread)
        self.request_scan.connect(self.scan_worker.scan)
        self.scan_worker.finished.connect(self.on_scan_finished)
        self.scan_worker_thread.start()

    def closeEvent(self, event):
        shutdown_render_pool()
        if self.threads_initialized:
            self.main_worker_thread.quit()
            self.cache_worker_thread.quit()
            self.sharpen_worker_thread.quit()
            self.scan_worker_thread.quit()
            self.main_worker_thread.wait()
            self.cache_worker_thread.wait()
            self.sharpen_worker_thread.wait()
            self.scan_worker_thread.wait()
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

        # The strip is created empty (and keeps its final height) so the window can be
        # shown immediately; the buttons themselves are built in build_controls().
        self.controls_layout = controls_layout
        self.controls_wrapper.setMinimumHeight(BTN_HEIGHT)
        self.buttons = []

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

        self.pending_indicator = PendingIndicator(self.central_widget)

        self.progress_bar = QProgressBar(self.central_widget)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.hide()

    def build_controls(self):
        controls_layout = self.controls_layout

        self.tips = HoverHintController(self)
        self.buttons = []

        self.btn_open = TipButton(BTN_TEXT_OPEN, "Open image", self.tips)
        self.btn_open.clicked.connect(self.open_file_dialog)
        self.buttons.append(self.btn_open)

        self.btn_prev = TipButton(BTN_TEXT_PREV, "Previous image  (←)", self.tips)
        self.btn_prev.setAutoRepeat(True)
        self.btn_prev.setAutoRepeatDelay(300)
        self.btn_prev.setAutoRepeatInterval(100)
        self.btn_prev.clicked.connect(self.show_previous_image)
        self.btn_prev.setEnabled(False)
        self.buttons.append(self.btn_prev)

        self.btn_next = TipButton(BTN_TEXT_NEXT, "Next image  (→)", self.tips)
        self.btn_next.setAutoRepeat(True)
        self.btn_next.setAutoRepeatDelay(300)
        self.btn_next.setAutoRepeatInterval(100)
        self.btn_next.clicked.connect(self.show_next_image)
        self.btn_next.setEnabled(False)
        self.buttons.append(self.btn_next)

        self.btn_rotate_left = TipButton(BTN_TEXT_ROTATE_LEFT, "Rotate left  (L)", self.tips)
        self.btn_rotate_left.clicked.connect(self.rotate_left)
        self.btn_rotate_left.setEnabled(False)
        self.buttons.append(self.btn_rotate_left)

        self.btn_rotate_right = TipButton(BTN_TEXT_ROTATE_RIGHT, "Rotate right  (R)", self.tips)
        self.btn_rotate_right.clicked.connect(self.rotate_right)
        self.btn_rotate_right.setEnabled(False)
        self.buttons.append(self.btn_rotate_right)

        self.btn_sharpen = TipButton(BTN_TEXT_SHARPEN, "Sharpen  (Shift+S)", self.tips)
        self.btn_sharpen.clicked.connect(self.sharpen_image)
        self.btn_sharpen.setEnabled(False)
        self.buttons.append(self.btn_sharpen)

        self.btn_crop = TipButton(BTN_TEXT_CROP, "Crop to selection  (C)  ·  right-drag to select", self.tips)
        self.btn_crop.clicked.connect(self.crop_selection)
        self.btn_crop.setEnabled(False)
        self.buttons.append(self.btn_crop)

        self.btn_reset_crop = TipButton(BTN_TEXT_RESET_CROP, "Reset / discard changes  (Space)", self.tips)
        self.btn_reset_crop.clicked.connect(self.reset_crop_or_selection)
        self.btn_reset_crop.setEnabled(False)
        self.buttons.append(self.btn_reset_crop)

        self.btn_save = TipButton(BTN_TEXT_SAVE, "Save over original  (S)", self.tips)
        self.btn_save.clicked.connect(self.save_image)
        self.btn_save.setEnabled(False)
        self.buttons.append(self.btn_save)
        
        self.btn_fullscreen = TipButton(BTN_TEXT_FULLSCREEN, "Fullscreen  (Enter / middle-click)", self.tips)
        self.btn_fullscreen.clicked.connect(self.toggle_fullscreen)
        self.buttons.append(self.btn_fullscreen)

        self.btn_slideshow = TipButton(BTN_TEXT_SLIDESHOW, "Slideshow", self.tips)
        self.btn_slideshow.clicked.connect(self.toggle_slideshow)
        self.btn_slideshow.setEnabled(False)
        self.btn_slideshow.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn_slideshow.customContextMenuRequested.connect(self.show_slideshow_menu)
        self.buttons.append(self.btn_slideshow)
        self.refresh_slideshow_tip()

        self.btn_info = TipButton(BTN_TEXT_INFO, "Image info  (I)", self.tips)
        self.btn_info.clicked.connect(self.show_image_info)
        self.btn_info.setEnabled(False)
        self.buttons.append(self.btn_info)

        self.btn_minify = TipButton(BTN_TEXT_MINIFY, "Shrink to 1920 px on the long edge  (M)", self.tips)
        self.btn_minify.clicked.connect(self.minify_image)
        self.btn_minify.setEnabled(False)
        self.buttons.append(self.btn_minify)

        self.btn_exit = TipButton(BTN_TEXT_EXIT, "Quit", self.tips)
        self.btn_exit.clicked.connect(self.close)
        self.buttons.append(self.btn_exit)

        for btn in self.buttons:
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setFixedWidth(BTN_WIDTH)
            btn.setFixedHeight(BTN_HEIGHT)
            controls_layout.addWidget(btn)

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
                background-color: {bg_color};
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
            QMenu {{
                background-color: #1e1e1e;
                color: #e0e0e0;
                border: 1px solid #3a3a3a;
                padding: 4px 0px;
            }}
            QMenu::item {{
                background-color: transparent;
                padding: 6px 24px 6px 14px;
            }}
            QMenu::item:selected {{
                background-color: #005999;
                color: #ffffff;
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

    def apply_initial_theme(self):
        # just enough to paint a black window with the dark toolbar strip immediately;
        # the full stylesheet is applied in complete_startup once the buttons exist
        self.setStyleSheet(
            "QWidget { background-color: #000000; color: #e0e0e0; }"
            "QWidget#controlsWidget { background-color: #181818; }"
        )

    def eventFilter(self, source, event):
        if event.type() == QEvent.Type.MouseButtonPress:
            if event.button() == Qt.MouseButton.MiddleButton:
                self.toggle_fullscreen()
                return True
        elif event.type() == QEvent.Type.KeyPress:
            # let menus and modal dialogs (file dialog etc.) have their own keys
            if QApplication.activePopupWidget() is not None or QApplication.activeModalWidget() is not None:
                return False
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
                mods = event.modifiers()
                if mods & Qt.KeyboardModifier.ControlModifier:
                    pass  # Ctrl+S intentionally does nothing
                elif mods & Qt.KeyboardModifier.ShiftModifier:
                    if not self.slideshow_timer.isActive():
                        self.sharpen_image()
                else:
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

    # ------------------------------------------------------------------ queued actions
    def defer_action(self, func, label):
        """While the full-size image is still loading (or earlier queued actions are waiting)
        an action can't run yet: queue it, show the orange "!", and let it run, in order,
        once the image is ready. Returns True if the action was queued."""
        if self.draining or not (self.loading_image or self.pending_actions):
            return False
        self.pending_actions.append((func, label))
        self.update_pending_indicator()
        self.show_osd(f"Queued: {label}", duration=1000)
        return True

    def update_pending_indicator(self):
        if self.pending_actions:
            self.reposition_pending_indicator()
            self.pending_indicator.show()
            self.pending_indicator.raise_()
        else:
            self.pending_indicator.hide()

    def reposition_pending_indicator(self):
        geom = self.scroll_area.geometry()
        self.pending_indicator.move(
            geom.x() + (geom.width() - self.pending_indicator.width()) // 2,
            geom.y() + (geom.height() - self.pending_indicator.height()) // 2,
        )

    def cancel_pending_actions(self):
        self.pending_actions.clear()
        self.update_pending_indicator()

    def run_pending_actions(self):
        """Run queued actions one by one. An action that starts more asynchronous work
        (rotate reloads the file, sharpen runs in a thread) pauses the queue until that
        finishes; on_image_processed / on_sharpen_processed resume it."""
        if self.draining:
            return
        self.draining = True
        try:
            while self.pending_actions and not (self.loading_image or self.sharpen_busy):
                func, _label = self.pending_actions.popleft()
                self.update_pending_indicator()
                func()
        finally:
            self.draining = False
        self.update_pending_indicator()

    # ------------------------------------------------------------------ slideshow
    def reset_slideshow_history(self):
        self.slideshow_history = [self.current_index]
        self.slideshow_history_pos = 0

    def extend_slideshow_ahead(self, count):
        """Decide the next `count` slideshow pictures in advance. The order is fixed once
        chosen, so what gets cached ahead is exactly what will be shown (and going back /
        forward with the arrow keys replays the same list)."""
        total = len(self.image_files)
        if total == 0:
            return
        history = self.slideshow_history
        if not (0 <= self.slideshow_history_pos < len(history)) \
                or history[self.slideshow_history_pos] != self.current_index:
            self.reset_slideshow_history()
            history = self.slideshow_history
        while len(history) - 1 - self.slideshow_history_pos < count:
            last = history[-1]
            if total == 1:
                following = 0
            elif not self.slideshow_random:
                following = (last + 1) % total
            else:
                import random

                following = random.randrange(total - 1)
                if following >= last:
                    following += 1
            history.append(following)
        if self.slideshow_history_pos > 400:      # don't let a very long session grow forever
            cut = self.slideshow_history_pos - 200
            del history[:cut]
            self.slideshow_history_pos -= cut

    def slideshow_tick(self):
        # never move on before the current picture is loaded: the display time is counted
        # from the moment it is on screen (on_image_processed restarts the timer then)
        if self.loading_image:
            return
        self.show_random_image()

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
            self.cancel_pending_actions()
            self.reset_slideshow_history()

            self.slideshow_timer.start()
            self.btn_slideshow.setStyleSheet("background-color: #005999; color: #ffffff;")
            self.show_osd("Slideshow Started", duration=1000)
            self.update_button_states()
            self.show_random_image()

    @staticmethod
    def format_interval(ms):
        if ms >= 60000 and ms % 60000 == 0:
            return f"{ms // 60000} min"
        return f"{ms / 1000:g} s"

    def refresh_slideshow_tip(self):
        mode = "random" if self.slideshow_random else "sequential"
        interval = self.format_interval(self.slideshow_interval_ms).replace(" ", "")
        self.btn_slideshow.set_tip(
            f"Slideshow, {interval} per image, M:{mode}, right click to change"
        )

    def show_slideshow_menu(self, _pos):
        self.tips.cancel()
        menu = QMenu(self.btn_slideshow)
        for is_random, name in ((True, "Random"), (False, "Sequential")):
            marker = "✓  " if is_random == self.slideshow_random else "     "
            action = menu.addAction(marker + name)
            action.triggered.connect(lambda _checked=False, v=is_random: self.set_slideshow_mode(v))
        menu.addSeparator()
        values = sorted(set(SLIDESHOW_PRESETS_MS) | {self.slideshow_interval_ms})
        for ms in values:
            label = self.format_interval(ms)
            if ms == SLIDESHOW_INTERVAL_MS:
                label += "  (default)"
            marker = "✓  " if ms == self.slideshow_interval_ms else "     "
            action = menu.addAction(marker + label)
            action.triggered.connect(lambda _checked=False, v=ms: self.set_slideshow_interval(v))

        # open upwards from the button, because the bar sits at the bottom of the window
        menu.ensurePolished()
        origin = self.btn_slideshow.mapToGlobal(QPoint(0, 0))
        menu.exec(QPoint(origin.x(), origin.y() - menu.sizeHint().height()))

    def set_slideshow_mode(self, is_random):
        self.slideshow_random = is_random
        if self.slideshow_timer.isActive():
            # the pictures already decided ahead follow the old mode: decide them again
            del self.slideshow_history[self.slideshow_history_pos + 1:]
            self.schedule_idle_cache()
        self.refresh_slideshow_tip()
        self.show_osd(f"Slideshow mode: {'random' if is_random else 'sequential'}", duration=1000)

    def set_slideshow_interval(self, ms):
        self.slideshow_interval_ms = ms
        self.slideshow_timer.setInterval(ms)   # on a running timer this restarts the countdown
        self.refresh_slideshow_tip()
        self.show_osd(f"Slideshow interval: {self.format_interval(ms)}", duration=1000)

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
            self.btn_sharpen.setEnabled(True)
            self.btn_crop.setEnabled(True)
            self.btn_reset_crop.setEnabled(True)
            self.btn_save.setEnabled(True)
            self.btn_minify.setEnabled(True)
        else:
            self.btn_rotate_left.setEnabled(False)
            self.btn_rotate_right.setEnabled(False)
            self.btn_sharpen.setEnabled(False)
            self.btn_crop.setEnabled(False)
            self.btn_reset_crop.setEnabled(False)
            self.btn_save.setEnabled(False)
            self.btn_minify.setEnabled(False)

    def show_image_info(self):
        if not self.image_files or not (0 <= self.current_index < len(self.image_files)):
            return
        
        image_path = self.image_files[self.current_index]
        try:
            from PIL import Image

            file_size_bytes = os.path.getsize(image_path)
            file_size_mb = file_size_bytes / (1024 * 1024)
            
            if self.current_pil_image:
                width, height = self.current_pil_image.size
            else:
                with Image.open(image_path) as img:
                    width, height = img.size

            info_text = f"{self.position_text()}\nResolution: {width} × {height} px  |  Size: {file_size_mb:.2f} MB"
            self.show_osd(info_text, duration=3000)
        except Exception as e:
            self.show_osd(f"Could not load image info:\n{str(e)}", duration=2000)

    def minify_image(self):
        if self.slideshow_timer.isActive():
            return
        if self.defer_action(self.minify_image, "minify"):
            return
        if self.current_pil_image is None:
            return

        from PIL import Image

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

        self.base_image = qimage
        self.update_image_display(smooth=True)
        self.show_osd("image Minified to 1080p", duration=1500)

    def sharpen_image(self):
        if self.slideshow_timer.isActive():
            return
        if self.defer_action(self.sharpen_image, "sharpen"):
            return
        if self.current_pil_image is None or self.sharpen_busy:
            return

        # a new id means any pending load/sharpen result for an older state is ignored
        self.request_counter += 1
        self.sharpen_request_id = self.request_counter
        self.sharpen_source = self.current_pil_image
        self.sharpen_busy = True
        self.image_label.clear_selection()

        self.set_progress_orange(False)
        self.progress_bar.setValue(0)
        self.reposition_progress_bar()
        self.progress_bar.show()
        self.progress_bar.raise_()

        self.request_sharpen.emit(
            self.sharpen_request_id,
            self.sharpen_source,
            float(SHARPEN_RADIUS),
            int(SHARPEN_PERCENT),
            int(SHARPEN_THRESHOLD),
        )

    @pyqtSlot(int, int)
    def on_sharpen_progress(self, request_id, percent):
        # ignore progress from a sharpen the user has since navigated / rotated away from
        if not self.sharpen_busy or request_id != self.request_counter:
            return
        self.progress_bar.setValue(percent)

    @pyqtSlot(int, QImage, object, str)
    def on_sharpen_processed(self, request_id, qimage, pil_img, error_msg):
        self.sharpen_busy = False
        if self.pending_actions:
            QTimer.singleShot(0, self.run_pending_actions)   # runs after this handler is done
        source = self.sharpen_source
        self.sharpen_source = None

        # something else (navigation, rotate, reset, save) took over: it owns the bar now
        if request_id != self.request_counter:
            return

        self.progress_bar.setValue(100)
        QTimer.singleShot(150, self.hide_progress_bar)

        if error_msg:
            self.show_osd(f"Could not sharpen:\n{error_msg}", duration=2000)
            return

        # a crop / minify happened while sharpening: don't overwrite it
        if self.current_pil_image is not source:
            return

        self.current_pil_image = pil_img
        self.rotation_angle = 0
        self.base_image = qimage
        self.update_image_display(smooth=True)
        self.show_osd("Sharpened in memory (Click Save to write)", duration=1500)

    def show_random_image(self):
        """Next slideshow picture (random or sequential, decided ahead of time)."""
        if not self.image_files:
            return
        self.extend_slideshow_ahead(1)
        self.slideshow_history_pos += 1
        self.current_index = self.slideshow_history[self.slideshow_history_pos]
        self.trigger_navigation()

    def open_file_dialog(self):
        if self.slideshow_timer.isActive():
            self.toggle_slideshow()
            
        # start in the folder of the current image (empty = let the dialog decide)
        start_dir = ""
        if 0 <= self.current_index < len(self.image_files):
            start_dir = os.path.dirname(self.image_files[self.current_index])

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Image File",
            start_dir,
            "Image Files (" + " ".join("*" + ext for ext in supported_image_extensions()) + ")"
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
        # the directory listing + sort runs in a worker thread so a huge or slow
        # (network) folder can't freeze the window; the result lands in on_scan_finished
        self.scan_counter += 1
        self.request_scan.emit(self.scan_counter, folder_path, selected_file, self.supported_extensions)

    @pyqtSlot(int, object, int, bool)
    def on_scan_finished(self, request_id, image_files, index, ok):
        if request_id != self.scan_counter or not ok:
            return

        self.image_files = image_files
        self.image_cache.clear()
        self.clear_previews()
        self.cancel_pending_actions()
        self.current_index = index
        self.load_current_image()

    def load_current_image(self):
        if 0 <= self.current_index < len(self.image_files):
            self.rotation_angle = 0
            self.zoom_factor = 1.0
            self.is_custom_zoom = False
            self.image_label.clear_selection()
            self.base_image = None
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

    def position_text(self):
        """"7 / 81 - name.jpg": shown briefly when a picture is opened, and on top of the info."""
        filename = os.path.basename(self.image_files[self.current_index])
        return f"{self.current_index + 1} / {len(self.image_files)} - {filename}"

    def trigger_navigation(self):
        if self.current_index < 0 or self.current_index >= len(self.image_files):
            return
        self.show_osd(self.position_text())
        
        self.zoom_factor = 1.0
        self.is_custom_zoom = False
        self.rotation_angle = 0
        self.image_label.clear_selection()
        self.base_image = None
        self.current_pil_image = None
        self.original_save_kwargs = {}

        self.update_button_states()

        # 75 ms debounce protects the decoder when skipping through uncached files;
        # an already cached image doesn't need to wait for it.
        path = self.image_files[self.current_index]
        self.loading_image = True
        self.cancel_pending_actions()   # queued actions belonged to the picture we just left
        self.load_timer.start(0 if path in self.image_cache else 75)

        # Not fully cached but a preview is: show it right now instead of waiting for the
        # debounce and the decode, so holding an arrow key flicks through at full speed.
        self.preview_image = None
        preview = self.preview_cache.get(path)
        if preview is not None and path not in self.image_cache:
            self.preview_image = preview
            self.update_image_display()

        # the scope moved: previews / loads that fell out of it are no longer worth finishing
        if self.threads_initialized:
            self.cache_worker.wanted_paths = set(self.window_paths([1, -1, 2, 3, 4, 5, 6, -2, -3]))
        self.schedule_previews()

    def window_paths(self, offsets):
        """Paths at the given offsets from the current image, in that order, no duplicates.
        During a slideshow the offsets are positions in the slideshow list (what really
        comes next / came before), otherwise positions in the folder."""
        total = len(self.image_files)
        current = self.image_files[self.current_index]
        if self.slideshow_timer.isActive():
            self.extend_slideshow_ahead(max([o for o in offsets if o > 0] or [0]))
            history, pos = self.slideshow_history, self.slideshow_history_pos
            indices = [history[pos + o] for o in offsets if 0 <= pos + o < len(history)]
        else:
            indices = [(self.current_index + o) % total for o in offsets]
        paths = []
        for idx in indices:
            path = self.image_files[idx]
            if path != current and path not in paths:
                paths.append(path)
        return paths

    def clear_previews(self):
        self.preview_cache.clear()
        self.preview_none.clear()
        self.preview_wanted = set()

    def forget_preview(self, path):
        self.preview_cache.pop(path, None)
        self.preview_none.discard(path)

    def schedule_previews(self):
        """Keep tiny 1/8-size previews of the surrounding pictures ready: own threads, own
        (small) memory, so they keep up even when the full-size cache is memory-starved."""
        if not self.image_files or not 0 <= self.current_index < len(self.image_files):
            return
        offsets = [1, -1] + list(range(2, PREVIEW_CACHE_AHEAD + 1)) \
            + [-k for k in range(2, PREVIEW_CACHE_BEHIND + 1)]
        ordered = self.window_paths(offsets)
        self.preview_wanted = set(ordered)
        for path in list(self.preview_cache):
            if path not in self.preview_wanted:
                del self.preview_cache[path]
        for path in ordered:
            if path in self.preview_cache or path in self.preview_pending or path in self.preview_none:
                continue
            self.preview_pending.add(path)
            preview_pool().submit(self.preview_job, path)

    def preview_job(self, path):
        """Runs in a preview pool thread."""
        result = None   # None = skipped (no longer wanted), False = nothing to preview
        try:
            if path in self.preview_wanted:
                small = decode_preview(path, is_stale=lambda: path not in self.preview_wanted)
                result = pil_to_qimage(small) if small is not None else False
        except Superseded:
            result = None
        except Exception:
            result = False
        try:
            self.preview_cached.emit((path, result))
        except RuntimeError:
            pass   # window already closed

    def on_preview_cached(self, payload):
        path, result = payload
        self.preview_pending.discard(path)
        if result is False:
            self.preview_none.add(path)
        elif result is not None and path in self.preview_wanted:
            self.preview_cache[path] = result

    def schedule_idle_cache(self):
        self.idle_cache_timer.stop()
        if not self.image_files or self.current_index < 0:
            return

        # Priority: the next image, then the previous one, then further ahead, and only
        # then more of the ones behind. When memory is tight, the tail of this list is
        # what gets dropped first (or never loaded), so going forward always stays cached.
        ordered = self.window_paths([1, -1, 2, 3, 4, 5, 6, -2, -3])
        self.cache_order = ordered
        target_paths = set(ordered)
        self.cache_worker.wanted_paths = target_paths
        self.schedule_previews()

        cached_keys = list(self.image_cache.keys())
        for path in cached_keys:
            if path not in target_paths and path != self.image_files[self.current_index]:
                del self.image_cache[path]
        self.enforce_cache_budget()

        self.preload_queue.clear()
        for path in ordered:
            if path not in self.image_cache and path != self.image_files[self.current_index]:
                self.preload_queue.append(path)

        if self.preload_queue:
            self.idle_cache_timer.start(150)

    @staticmethod
    def image_bytes(qimage, pil_img):
        total = qimage.sizeInBytes() if qimage is not None else 0
        if pil_img is not None:
            total += pil_img.width * pil_img.height * len(pil_img.getbands())
        return total

    def memory_in_use(self):
        """Bytes held by cached images plus the one on screen (counted once)."""
        total = sum(self.image_bytes(q, p) for q, p in self.image_cache.values())
        if self.base_image is not None and not self.base_image.isNull():
            current = self.image_files[self.current_index] if 0 <= self.current_index < len(self.image_files) else None
            entry = self.image_cache.get(current)
            # after an edit the cache holds the original and the screen an edited copy: both count
            if entry is None or entry[0] is not self.base_image:
                total += self.image_bytes(self.base_image, self.current_pil_image)
        return total

    def cache_rank(self, path):
        """Lower = more important. Images outside the cache window rank last."""
        try:
            return self.cache_order.index(path)
        except ValueError:
            return len(self.cache_order) + 1

    def evict_least_important(self, keep_path=None, until=None):
        """Drop cached images, least important first, until until() is true."""
        current = self.image_files[self.current_index] if 0 <= self.current_index < len(self.image_files) else None
        for path in sorted(self.image_cache, key=self.cache_rank, reverse=True):
            if until():
                return
            if path == current or path == keep_path:
                continue
            if keep_path is not None and self.cache_rank(path) <= self.cache_rank(keep_path):
                continue   # never drop something more important than what is being loaded
            del self.image_cache[path]

    def enforce_cache_budget(self):
        if self.memory_in_use() > CACHE_MEMORY_LIMIT_BYTES:
            self.evict_least_important(until=lambda: self.memory_in_use() <= CACHE_MEMORY_LIMIT_BYTES)

    def process_next_idle_cache(self):
        if self.preload_queue:
            path = self.preload_queue.popleft()
            free = CACHE_MEMORY_LIMIT_BYTES - self.memory_in_use()
            rank = self.cache_rank(path)
            current = self.image_files[self.current_index] if 0 <= self.current_index < len(self.image_files) else None
            reclaimable = sum(
                self.image_bytes(q, p) for cached, (q, p) in self.image_cache.items()
                if cached != current and self.cache_rank(cached) > rank
            )
            self.request_cache.emit(path, free, reclaimable)

    @pyqtSlot(str, object)
    def on_cache_needs_room(self, image_path, need):
        """The next image fits only if less important cached images go: drop them, retry."""
        self.evict_least_important(
            keep_path=image_path,
            until=lambda: need <= CACHE_MEMORY_LIMIT_BYTES - self.memory_in_use()
        )
        if need <= CACHE_MEMORY_LIMIT_BYTES - self.memory_in_use():
            self.preload_queue.appendleft(image_path)
            self.idle_cache_timer.start(0)
        else:
            self.preload_queue.clear()   # still doesn't fit: stop caching

    @pyqtSlot(str, QImage, object)
    def on_cache_processed(self, image_path, qimage, pil_img):
        if qimage and not qimage.isNull() and pil_img:
            self.image_cache[image_path] = (qimage, pil_img)
            self.enforce_cache_budget()
        else:
            # the next image would not fit in the memory budget: stop caching
            self.preload_queue.clear()
        if self.preload_queue:
            self.idle_cache_timer.start(50)

    def trigger_worker(self):
        if 0 <= self.current_index < len(self.image_files):
            image_path = self.image_files[self.current_index]
            
            if self.rotation_angle == 0 and image_path in self.image_cache:
                qimage, pil_img = self.image_cache[image_path]
                self.request_counter += 1
                self.main_worker.latest_request_id = self.request_counter
                # no pil_img.copy(): every edit (crop/minify/rotate) builds a new image
                # and never mutates this one, so sharing it with the cache is safe
                self.on_image_processed(self.request_counter, qimage, pil_img, "")
                return

            self.request_counter += 1
            self.main_worker.latest_request_id = self.request_counter
            
            # a preview that is already on screen (from the preview cache) means the bar is
            # blue from the start; otherwise the worker turns it orange if it makes one
            self.set_progress_orange(False)
            self.loading_image = True
            self.progress_bar.setValue(0)
            self.reposition_progress_bar()
            self.progress_bar.show()
            self.progress_bar.raise_()
            
            self.request_processing.emit(
                self.request_counter,
                image_path,
                float(self.rotation_angle),
                self.preview_image is not None
            )

    @pyqtSlot(int, int)
    def on_load_progress(self, request_id, percent):
        # ignore progress from a load the user has already moved on from
        if request_id != self.request_counter:
            return
        self.progress_bar.setValue(percent)

    def hide_progress_bar(self):
        # a queued action (sharpen, rotate...) may have started its own job since this was
        # scheduled: then the bar belongs to that job and must stay
        if self.sharpen_busy or self.loading_image:
            return
        self.progress_bar.hide()

    def set_progress_orange(self, orange):
        self.progress_bar.setStyleSheet(
            f"QProgressBar::chunk {{ background-color: {PREVIEW_COLOR}; }}" if orange else ""
        )

    @pyqtSlot(int)
    def on_preview_started(self, request_id):
        if request_id != self.request_counter:
            return
        self.set_progress_orange(True)   # decoding the preview itself: orange
        self.progress_bar.setValue(0)

    @pyqtSlot(int, QImage)
    def on_preview_ready(self, request_id, preview):
        if request_id != self.request_counter or preview.isNull():
            return
        self.set_progress_orange(False)  # preview is on screen, the real image loads now: blue
        self.progress_bar.setValue(0)
        # Only the label shows it: base_image / current_pil_image stay as they are, so no
        # edit can ever touch the low-res preview.
        self.preview_image = preview
        self.update_image_display()

    @pyqtSlot(int, QImage, object, str)
    def on_image_processed(self, request_id, qimage, pil_img, error_msg):
        if request_id != self.request_counter:
            return
        self.preview_image = None
        self.set_progress_orange(False)
        self.loading_image = False

        self.progress_bar.setValue(100)
        QTimer.singleShot(150, self.hide_progress_bar)

        # slideshow: the display time counts from now, when the picture is really on screen
        if self.slideshow_timer.isActive():
            self.slideshow_timer.start()

        image_path = self.image_files[self.current_index]

        if error_msg:
            self.cancel_pending_actions()
            self.current_pil_image = None
            self.base_image = None
            self.image_label.clear()
            self.image_label.setText(f"Corrupted or invalid image file:\n{os.path.basename(image_path)}\n{error_msg}")
            self.update_image_display()
            
            self.update_button_states()

            self.setWindowTitle(f"Image Viewer - [Corrupted] {os.path.basename(image_path)} ({self.current_index + 1}/{len(self.image_files)})")
        else:
            self.current_pil_image = pil_img
            self.base_image = qimage
            if self.rotation_angle == 0 and image_path not in self.image_cache:
                # keep the pristine original: crop / sharpen / minify replace the picture on
                # screen but never this entry, so Reset (Space) can show it again instantly
                self.image_cache[image_path] = (qimage, pil_img)
            self.setWindowTitle(f"Image Viewer - {os.path.basename(image_path)} ({self.current_index + 1}/{len(self.image_files)})")
            
            self.update_button_states()
            
            self.update_image_display(smooth=True)
            self.schedule_idle_cache()
            # actions the user asked for while this was loading now run, in order
            if self.pending_actions:
                QTimer.singleShot(0, self.run_pending_actions)

    def update_image_display(self, smooth=True):
        self.fit_token += 1   # any background smoothing started earlier is now outdated
        if self.preview_image is not None:
            # full image still decoding: fit the quick preview into the window (it is small)
            viewport_size = self.scroll_area.viewport().size()
            self.image_label.resize(viewport_size)
            self.image_label.clear_virtual()
            self.image_label.setPixmap(QPixmap.fromImage(self.preview_image.scaled(
                viewport_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation
            )))
            return
        if not self.base_image or self.base_image.isNull():
            # placeholder / error text: let the label fill the viewport so the
            # centered text is never clipped to a stale, too-small label size
            self.image_label.clear_virtual()
            self.image_label.resize(self.scroll_area.viewport().size())
            return

        # base_image is a QImage: scaling it directly avoids converting the full-size
        # picture into a QPixmap (a big copy on the GUI thread) on every image change.
        transform_mode = Qt.TransformationMode.SmoothTransformation if smooth else Qt.TransformationMode.FastTransformation
        viewport_size = self.scroll_area.viewport().size()

        pil = self.current_pil_image
        if pil is not None and (pil.mode not in RENDER_MODES
                                or pil.size != (self.base_image.width(), self.base_image.height())):
            pil = None

        if not self.is_custom_zoom:
            # huge pictures: show a quick nearest-neighbour version right away and let
            # pool threads produce the smooth one in the background
            background_smooth = (
                smooth and pil is not None
                and self.base_image.width() * self.base_image.height() >= ASYNC_SMOOTH_MIN_PIXELS
            )
            scaled_pixmap = self.base_image.scaled(
                viewport_size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation if background_smooth else transform_mode
            )
            self.image_label.resize(viewport_size)
            self.image_label.clear_virtual()
            self.image_label.setPixmap(QPixmap.fromImage(scaled_pixmap))
            if background_smooth:
                threading.Thread(
                    target=self.fit_job,
                    args=(self.fit_token, pil, scaled_pixmap.width(), scaled_pixmap.height()),
                    daemon=True,
                ).start()
        else:
            target_w = int(self.base_image.width() * self.zoom_factor)
            target_h = int(self.base_image.height() * self.zoom_factor)
            if target_w <= 0 or target_h <= 0:
                target_w, target_h = self.base_image.width(), self.base_image.height()

            # zoomed view: no full-size scaled copy / pixmap, the canvas paints just
            # the exposed part from base_image (see ImageCanvas.paint_virtual)
            label_w = max(viewport_size.width(), target_w)
            label_h = max(viewport_size.height(), target_h)
            self.image_label.resize(label_w, label_h)
            self.image_label.set_virtual(self.base_image, QSize(target_w, target_h), smooth, pil)

    def fit_job(self, token, pil, out_w, out_h):
        """Smooth fit-to-window version of a huge image, split into horizontal bands that
        render in parallel in the pool. Runs in its own short-lived thread (it only waits)."""
        data = None
        try:
            bands = max(1, min(RENDER_THREADS, out_h))
            rows = [out_h * i // bands for i in range(bands + 1)]
            ratio_y = pil.height / out_h

            def render_band(i):
                if token != self.fit_token:
                    return None   # the user has moved on
                return render_region_rgba(
                    pil, (0, rows[i] * ratio_y, pil.width, min(pil.height, rows[i + 1] * ratio_y)),
                    out_w, rows[i + 1] - rows[i])

            parts = [f.result() for f in [render_pool().submit(render_band, i) for i in range(bands)]]
            if all(part is not None for part in parts):
                data = b"".join(parts)
        except Exception:
            data = None
        try:
            self.fit_ready.emit((token, out_w, out_h, data))
        except RuntimeError:
            pass   # window already closed

    def on_fit_ready(self, payload):
        token, w, h, data = payload
        if data is None or token != self.fit_token or self.is_custom_zoom:
            return
        self.image_label.setPixmap(QPixmap.fromImage(QImage(data, w, h, QImage.Format.Format_RGBA8888)))

    def crop_selection(self):
        if self.slideshow_timer.isActive():
            return
        if not self.draining and (self.loading_image or self.pending_actions):
            # remember the selection as fractions of the picture, so it still fits once
            # the full-size image has replaced the preview
            size = self.image_label.displayed_size()
            sel = self.image_label.selection_rect
            if size.isEmpty() or sel.isEmpty():
                return
            ox = (self.image_label.width() - size.width()) // 2
            oy = (self.image_label.height() - size.height()) // 2
            fractions = (
                (sel.left() - ox) / size.width(), (sel.top() - oy) / size.height(),
                (sel.right() + 1 - ox) / size.width(), (sel.bottom() + 1 - oy) / size.height(),
            )
            self.image_label.clear_selection()
            self.defer_action(lambda: self.crop_from_fractions(fractions), "crop")
            return
        if self.current_pil_image is None or self.image_label.selection_rect.isEmpty():
            return

        pixmap_size = self.image_label.displayed_size()
        if pixmap_size.width() == 0 or pixmap_size.height() == 0:
            return

        label_size = self.image_label.size()

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

        self.base_image = qimage
        self.update_image_display(smooth=True)
        self.show_osd("Cropped in memory (Click Save to write)", duration=1500)

    def crop_from_fractions(self, fractions):
        size = self.image_label.displayed_size()
        if size.isEmpty():
            return
        ox = (self.image_label.width() - size.width()) // 2
        oy = (self.image_label.height() - size.height()) // 2
        x1, y1, x2, y2 = fractions
        self.image_label.selection_rect = QRect(
            int(ox + x1 * size.width()), int(oy + y1 * size.height()),
            max(1, int((x2 - x1) * size.width())), max(1, int((y2 - y1) * size.height())),
        )
        self.crop_selection()

    def reset_crop_or_selection(self):
        if self.slideshow_timer.isActive():
            self.toggle_slideshow()
            return
        if self.defer_action(self.reset_crop_or_selection, "reset"):
            return
            
        if 0 <= self.current_index < len(self.image_files):
            self.image_label.clear_selection()
            self.rotation_angle = 0
            self.is_custom_zoom = False
            self.zoom_factor = 1.0
            # the original is still in the cache (see on_image_processed), so this is instant;
            # without it the file is simply decoded again
            self.load_current_image()
            self.show_osd("Reset", duration=1000)

    def rotate_left(self):
        if self.slideshow_timer.isActive():
            return
        if self.defer_action(self.rotate_left, "rotate left"):
            return
        if self.current_pil_image:
            self.rotation_angle = (self.rotation_angle - 90) % 360
            self.image_label.clear_selection()
            self.trigger_worker()

    def rotate_right(self):
        if self.slideshow_timer.isActive():
            return
        if self.defer_action(self.rotate_right, "rotate right"):
            return
        if self.current_pil_image:
            self.rotation_angle = (self.rotation_angle + 90) % 360
            self.image_label.clear_selection()
            self.trigger_worker()

    def save_image(self):
        if self.slideshow_timer.isActive():
            self.toggle_slideshow()
        if self.defer_action(self.save_image, "save"):
            return
            
        if self.current_pil_image is None or self.current_index < 0:
            return

        current_path = self.image_files[self.current_index]
        
        no_alpha_formats = (".jpg", ".jpeg", ".bmp")
        save_img = self.current_pil_image
        if current_path.lower().endswith(no_alpha_formats):
            if save_img.mode in ("RGBA", "P"):
                save_img = save_img.convert("RGB")

        try:
            self.base_image = None
            
            ext = os.path.splitext(current_path)[1]
            temp_path = current_path + ".tmp_save" + ext
            
            save_kwargs = dict(self.original_save_kwargs)

            save_img.save(temp_path, **save_kwargs)
            
            if os.path.exists(current_path):
                os.remove(current_path)
            os.rename(temp_path, current_path)

            if current_path in self.image_cache:
                del self.image_cache[current_path]
            self.forget_preview(current_path)

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
        
        self.cancel_pending_actions()   # whatever was queued was meant for this picture
        try:
            self.base_image = None
            self.current_pil_image = None
            
            if image_path in self.image_cache:
                del self.image_cache[image_path]
            self.forget_preview(image_path)

            import subprocess

            abs_path = os.path.abspath(image_path)
            result = subprocess.run(["gio", "trash", abs_path], capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or "gio trash failed")

            self.show_osd("Moved to trash", duration=1200)

            del self.image_files[self.current_index]

            if not self.image_files:
                if self.slideshow_timer.isActive():
                    self.toggle_slideshow()
                self.loading_image = False
                self.current_index = -1
                self.image_label.clear()
                self.image_label.setText("Open an image to start")
                self.update_image_display()
                self.setWindowTitle("Image Viewer")
                self.update_button_states()
            else:
                if self.current_index >= len(self.image_files):
                    self.current_index = len(self.image_files) - 1
                if self.slideshow_timer.isActive():
                    self.reset_slideshow_history()   # the indices in the old list have shifted
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
            self.show_random_image()
        else:
            self.current_index = (self.current_index + 1) % len(self.image_files)
            self.trigger_navigation()

    def handle_wheel_zoom(self, event):
        if not self.base_image or self.base_image.isNull():
            return

        angle_delta = event.angleDelta().y()
        if angle_delta == 0:
            return

        old_zoom = self.zoom_factor
        if not self.is_custom_zoom:
            self.is_custom_zoom = True
            current_pixmap = self.image_label.pixmap()
            if current_pixmap and self.base_image.width() > 0:
                old_zoom = current_pixmap.width() / self.base_image.width()
            else:
                old_zoom = 1.0
            self.zoom_factor = old_zoom

        viewport_size = self.scroll_area.viewport().size()
        fit_scale_w = viewport_size.width() / float(self.base_image.width())
        fit_scale_h = viewport_size.height() / float(self.base_image.height())
        fit_to_screen_zoom = min(fit_scale_w, fit_scale_h)

        max_allowed_zoom = max(1.0, fit_to_screen_zoom)

        if angle_delta > 0:
            new_zoom = self.zoom_factor * 1.15
        else:
            new_zoom = self.zoom_factor / 1.15

        new_zoom = max(0.05, min(max_allowed_zoom, new_zoom))

        # already at the zoom limit: nothing to re-render
        if abs(new_zoom - self.zoom_factor) < 1e-9 and self.wheel_anchor is None:
            return
        self.zoom_factor = new_zoom

        if self.wheel_anchor is None:
            # remember what is under the cursor, in the coordinates of what is
            # currently rendered; later steps in the same burst reuse this anchor
            cursor_pos = event.position().toPoint()
            h_bar = self.scroll_area.horizontalScrollBar()
            v_bar = self.scroll_area.verticalScrollBar()
            self.wheel_anchor = (
                cursor_pos,
                h_bar.value() + cursor_pos.x(),
                v_bar.value() + cursor_pos.y(),
                old_zoom,
            )

        self.smooth_zoom_timer.stop()
        if not self.wheel_render_timer.isActive():
            self.wheel_render_timer.start(16)

    def apply_wheel_zoom(self):
        anchor = self.wheel_anchor
        self.wheel_anchor = None
        if anchor is None or not self.is_custom_zoom or not self.base_image or self.base_image.isNull():
            return
        cursor_pos, content_x, content_y, rendered_zoom = anchor

        h_bar = self.scroll_area.horizontalScrollBar()
        v_bar = self.scroll_area.verticalScrollBar()

        ratio = self.zoom_factor / rendered_zoom

        self.update_image_display(smooth=False)

        h_bar.setValue(int(content_x * ratio - cursor_pos.x()))
        v_bar.setValue(int(content_y * ratio - cursor_pos.y()))

        self.smooth_zoom_timer.start(150)

    def reset_to_fit_screen(self):
        if self.base_image:
            self.is_custom_zoom = False
            self.zoom_factor = 1.0
            self.update_image_display(smooth=True)
            self.image_label.update()

    def reset_to_actual_size_or_fit(self):
        if self.defer_action(self.reset_to_actual_size_or_fit, "1:1 / fit"):
            return
        if not self.base_image or self.base_image.isNull():
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
        self.reposition_pending_indicator()
        if not self.is_custom_zoom:
            self.update_image_display(smooth=False)
            self.smooth_zoom_timer.start(120)


if __name__ == "__main__":
    # Use the desktop portal's native file picker (GTK / GNOME / KDE) instead of Qt's
    # built-in dialog. Must be set before QApplication exists. An already-set
    # QT_QPA_PLATFORMTHEME (e.g. kde, qt6ct) is left alone; falls back to Qt's dialog
    # if no portal is running.
    os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    
    initial_path = sys.argv[1] if len(sys.argv) > 1 else None
    
    viewer = ImageViewer(initial_path)
    viewer.show()
    app.processEvents()   # paint the bare window right away
    QTimer.singleShot(0, viewer.complete_startup)
    sys.exit(app.exec())
