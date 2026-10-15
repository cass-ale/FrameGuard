"""Small, platform-tolerant helpers for FrameGuard rendered UI contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
import re

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget


@dataclass(frozen=True)
class VisualSignature:
    width: int
    height: int
    luma_tiles: tuple[int, ...]
    chroma_tiles: tuple[int, ...]
    luma_span: int
    sampled_colors: int

    def to_dict(self) -> dict:
        value = asdict(self)
        value["luma_tiles"] = list(self.luma_tiles)
        value["chroma_tiles"] = list(self.chroma_tiles)
        return value


def settle(app: QApplication, rounds: int = 6) -> None:
    for _ in range(rounds):
        app.processEvents()


def capture_application_content(window: QWidget) -> QImage:
    """Capture app-owned content, excluding native title bar claims."""
    target = window.centralWidget() if hasattr(window, "centralWidget") else window
    image = target.grab().toImage().convertToFormat(QImage.Format_RGBA8888)
    if image.isNull():
        raise AssertionError("Qt returned a null application-content screenshot")
    return image


def visual_signature(image: QImage, columns: int = 16, rows: int = 12) -> VisualSignature:
    image = image.convertToFormat(QImage.Format_RGBA8888)
    luma_tiles = []
    chroma_tiles = []
    sampled = set()
    sampled_luma = []
    for row in range(rows):
        y0 = row * image.height() // rows
        y1 = max(y0 + 1, (row + 1) * image.height() // rows)
        for column in range(columns):
            x0 = column * image.width() // columns
            x1 = max(x0 + 1, (column + 1) * image.width() // columns)
            luma_total = chroma_total = count = 0
            # A bounded sample grid keeps the signature fast at 1920x1080 while
            # remaining insensitive to one-pixel rasterization differences.
            step_x = max(1, (x1 - x0) // 6)
            step_y = max(1, (y1 - y0) // 6)
            for y in range(y0, y1, step_y):
                for x in range(x0, x1, step_x):
                    color = image.pixelColor(x, y)
                    r, g, b = color.red(), color.green(), color.blue()
                    luma = round(0.2126 * r + 0.7152 * g + 0.0722 * b)
                    chroma = max(r, g, b) - min(r, g, b)
                    luma_total += luma
                    chroma_total += chroma
                    count += 1
                    sampled_luma.append(luma)
                    sampled.add((r // 16, g // 16, b // 16))
            luma_tiles.append(round(luma_total / max(1, count)))
            chroma_tiles.append(round(chroma_total / max(1, count)))
    return VisualSignature(
        width=image.width(),
        height=image.height(),
        luma_tiles=tuple(luma_tiles),
        chroma_tiles=tuple(chroma_tiles),
        luma_span=max(sampled_luma) - min(sampled_luma),
        sampled_colors=len(sampled),
    )


def signature_distance(first: VisualSignature, second: VisualSignature) -> float:
    if (first.width, first.height) != (second.width, second.height):
        return 1.0
    luma = sum(abs(a - b) for a, b in zip(first.luma_tiles, second.luma_tiles))
    chroma = sum(abs(a - b) for a, b in zip(first.chroma_tiles, second.chroma_tiles))
    denominator = max(1, 255 * (len(first.luma_tiles) + len(first.chroma_tiles)))
    return (luma + chroma) / denominator


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def difference_image(first: QImage, repeat: QImage) -> QImage:
    first = first.convertToFormat(QImage.Format_RGBA8888)
    repeat = repeat.convertToFormat(QImage.Format_RGBA8888)
    width = min(first.width(), repeat.width())
    height = min(first.height(), repeat.height())
    output = QImage(width, height, QImage.Format_RGBA8888)
    output.fill(QColor(0, 0, 0, 255))
    for y in range(height):
        for x in range(width):
            left = first.pixelColor(x, y)
            right = repeat.pixelColor(x, y)
            delta = max(
                abs(left.red() - right.red()),
                abs(left.green() - right.green()),
                abs(left.blue() - right.blue()),
            )
            output.setPixelColor(x, y, QColor(delta, 0, 0, 255))
    return output


def write_failure_artifacts(directory: Path, state: str, first: QImage,
                            repeat: QImage, first_signature: VisualSignature,
                            repeat_signature: VisualSignature) -> dict[str, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    stem = _safe_name(state)
    paths = {
        "first": directory / f"{stem}-first.png",
        "repeat": directory / f"{stem}-repeat.png",
        "diff": directory / f"{stem}-diff.png",
        "report": directory / f"{stem}-signatures.json",
    }
    first.save(str(paths["first"]), "PNG")
    repeat.save(str(paths["repeat"]), "PNG")
    difference_image(first, repeat).save(str(paths["diff"]), "PNG")
    paths["report"].write_text(
        json.dumps({
            "state": state,
            "first": first_signature.to_dict(),
            "repeat": repeat_signature.to_dict(),
            "distance": signature_distance(first_signature, repeat_signature),
        }, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return paths
