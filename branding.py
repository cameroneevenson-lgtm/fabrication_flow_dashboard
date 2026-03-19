from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap, QTransform


def _clamp8(value: float) -> int:
    return max(0, min(255, int(round(value))))


def _boost_logo_tile(tile: QPixmap, *, saturation: float, contrast: float) -> QPixmap:
    image = tile.toImage().convertToFormat(QImage.Format_ARGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            alpha = color.alpha()
            if alpha <= 0:
                continue
            red = float(color.red())
            green = float(color.green())
            blue = float(color.blue())
            gray = 0.299 * red + 0.587 * green + 0.114 * blue
            red = (gray + (red - gray) * saturation - 128.0) * contrast + 128.0
            green = (gray + (green - gray) * saturation - 128.0) * contrast + 128.0
            blue = (gray + (blue - gray) * saturation - 128.0) * contrast + 128.0
            image.setPixelColor(x, y, QColor(_clamp8(red), _clamp8(green), _clamp8(blue), alpha))
    return QPixmap.fromImage(image)


def _trim_transparent_padding(pixmap: QPixmap) -> QPixmap:
    if pixmap.isNull():
        return QPixmap()
    image = pixmap.toImage().convertToFormat(QImage.Format_ARGB32)
    left = image.width()
    top = image.height()
    right = -1
    bottom = -1
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() <= 0:
                continue
            left = min(left, x)
            top = min(top, y)
            right = max(right, x)
            bottom = max(bottom, y)
    if right < left or bottom < top:
        return pixmap
    return pixmap.copy(left, top, right - left + 1, bottom - top + 1)


def make_tiled_banner_pixmap(
    logo_path: Path | None,
    *,
    height_px: int,
    width_px: int,
    opacity: float = 0.90,
    saturation: float = 1.55,
    contrast: float = 1.30,
) -> QPixmap:
    pixmap = QPixmap(str(logo_path)) if logo_path else QPixmap()
    if pixmap.isNull():
        return QPixmap()
    tile = pixmap.scaledToHeight(max(1, height_px), Qt.SmoothTransformation)
    if tile.isNull():
        return QPixmap()
    tile = _boost_logo_tile(tile, saturation=saturation, contrast=contrast)
    output_width = max(tile.width(), int(width_px))
    output = QPixmap(output_width, max(1, height_px))
    output.fill(QColor("#000000"))
    painter = QPainter(output)
    painter.setOpacity(max(0.05, min(1.0, float(opacity))))
    x = 0
    while x < output_width:
        painter.drawPixmap(x, 0, tile)
        x += max(1, tile.width())
    painter.end()
    return output


def make_angled_logo_texture_pixmap(
    logo_path: Path | None,
    *,
    tile_width_px: int,
    tile_height_px: int,
    stripe_height_px: int = 54,
    opacity: float = 0.22,
    background_color: QColor | str | None = None,
) -> QPixmap:
    pixmap = QPixmap(str(logo_path)) if logo_path else QPixmap()
    if pixmap.isNull():
        return QPixmap()
    base_tile = pixmap.scaledToHeight(max(8, int(stripe_height_px)), Qt.SmoothTransformation)
    if base_tile.isNull():
        return QPixmap()
    base_tile = _boost_logo_tile(base_tile, saturation=1.60, contrast=1.32)

    tile_45 = _trim_transparent_padding(
        base_tile.transformed(QTransform().rotate(45.0), Qt.SmoothTransformation)
    )
    tile_225 = _trim_transparent_padding(
        base_tile.transformed(QTransform().rotate(225.0), Qt.SmoothTransformation)
    )
    if tile_45.isNull():
        tile_45 = base_tile
    if tile_225.isNull():
        tile_225 = base_tile

    output = QPixmap(max(64, int(tile_width_px)), max(64, int(tile_height_px)))
    if background_color is None:
        output.fill(Qt.transparent)
    else:
        output.fill(QColor(background_color))

    painter = QPainter(output)
    painter.setOpacity(max(0.05, min(1.0, float(opacity))))
    step_x = max(12, int(max(tile_45.width(), tile_225.width()) * 0.40))
    step_y = max(18, int(max(tile_45.height(), tile_225.height()) * 0.56))
    row = 0
    y = -step_y
    while y < output.height() + step_y:
        x = -(step_x * 2) + (step_x if row % 2 else 0)
        alternate = bool(row % 2)
        while x < output.width() + step_x:
            tile = tile_45 if not alternate else tile_225
            painter.drawPixmap(x, y, tile)
            x += step_x
            alternate = not alternate
        y += step_y
        row += 1
    painter.end()
    return output
