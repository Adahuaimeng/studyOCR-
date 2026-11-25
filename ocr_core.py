from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from hashlib import file_digest
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import BinaryIO

from PIL import Image, ImageOps

try:
    from pillow_heif import register_heif_opener
except ImportError:
    pass
else:
    register_heif_opener()

@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2

    def crop(self, image: Image.Image) -> Image.Image:
        return image.crop((self.left, self.top, self.right, self.bottom))

    def intersection(self, other: "Rect") -> "Rect | None":
        overlap = Rect(
            max(self.left, other.left), max(self.top, other.top),
            min(self.right, other.right), min(self.bottom, other.bottom),
        )
        return overlap if overlap.width > 0 and overlap.height > 0 else None

    def translated(self, x: int, y: int) -> "Rect":
        return Rect(self.left + x, self.top + y, self.right + x, self.bottom + y)

    def relative_to(self, origin: "Rect") -> "Rect":
        return self.translated(-origin.left, -origin.top)



class TextKind(StrEnum):
    PRINTED = "printed"
    HANDWRITTEN = "handwritten"
    REVIEW = "review"
    OMIT = "omit"

    @property
    def label(self):
        return {
            TextKind.PRINTED: "印刷体", TextKind.HANDWRITTEN: "手写体",
            TextKind.REVIEW: "待确认", TextKind.OMIT: "不导出",
        }[self]


@dataclass(frozen=True)
class TextLine:
    bounds: Rect
    text: str
    confidence: float
    kind: TextKind = TextKind.REVIEW
    role: str | None = None
    review_order: int | None = None


@dataclass(frozen=True)
class Scan:
    image: Image.Image
    area: Rect
    lines: tuple[TextLine, ...]
    seconds: float

    @cached_property
    def illustrations(self) -> tuple[Rect, ...]:
        from illustrations import detect_illustrations

        return detect_illustrations(self.image, self.lines)


def open_image(path: Path | BinaryIO, *, max_pixels: int | None = None) -> Image.Image:
    with Image.open(path) as source:
        if max_pixels is not None and source.width * source.height > max_pixels:
            raise ValueError("图片像素超过接收上限")
        image = ImageOps.exif_transpose(source).convert("RGBA")
    # Use white for transparent areas in the preview and both export formats.
    background = Image.new("RGBA", image.size, "white")
    background.alpha_composite(image)
    return background.convert("RGB")


class LocalOcr:
    @cached_property
    def direction_model_key(self):
        from offline import MODELS, model_directory

        try:
            directory = model_directory()
            values = [version("rapidocr"), version("onnxruntime")]
            for name in MODELS[:2]:
                with (directory / name).open("rb") as file:
                    values.append(file_digest(file, "sha256").hexdigest())
        except (OSError, PackageNotFoundError, ValueError):
            return None
        return ":".join(values)

    @cached_property
    def engine(self):
        return self.build_engine(handwriting=False)

    @cached_property
    def handwriting_engine(self):
        return self.build_engine(handwriting=True)

    @staticmethod
    def build_engine(handwriting):
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        return RapidOCR(params={
            "Det.engine_type": EngineType.ONNXRUNTIME,
            "Det.ocr_version": OCRVersion.PPOCRV5,
            "Det.lang_type": LangDet.CH,
            "Det.model_type": ModelType.MOBILE,
            "Det.limit_side_len": 1600,
            "Det.limit_type": "max",
            "Rec.engine_type": EngineType.ONNXRUNTIME,
            "Rec.ocr_version": OCRVersion.PPOCRV5,
            "Rec.lang_type": LangRec.CH,
            "Rec.model_type": ModelType.SERVER if handwriting else ModelType.MOBILE,
            "Global.text_score": 0.3,
            "Global.max_side_len": 1600,
            "EngineConfig.onnxruntime.enable_cpu_mem_arena": True,
            "EngineConfig.onnxruntime.intra_op_num_threads": 4,
            "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        })

    def recognize(self, image: Image.Image, area: Rect | None = None,
                  *, handwriting: bool = False, mixed: bool = False) -> Scan:
        import numpy as np

        area = area or Rect(0, 0, *image.size)
        selected = area.crop(image)
        started = perf_counter()
        engine = self.handwriting_engine if handwriting else self.engine
        output = engine(
            np.asarray(selected)[:, :, ::-1].copy(), use_cls=False,
            return_word_box=mixed, return_single_char_box=mixed,
        )
        lines = []
        if mixed:
            from ink_style import Glyph, classify

            gray = np.asarray(selected.convert("L"))

        if output.boxes is not None and output.txts is not None:
            extent = Rect(0, 0, *selected.size)
            def rectangle(points):
                box = np.asarray(points)
                return Rect(
                    int(box[:, 0].min()), int(box[:, 1].min()),
                    int(box[:, 0].max()) + 1, int(box[:, 1].max()) + 1,
                ).intersection(extent)

            boxes = [rectangle(box) for box in output.boxes]
            typical_height = median(
                [b.height for b in boxes if b and b.width > b.height * 4] or [1]
            )
            for index, (bounds, text, score) in enumerate(
                zip(boxes, output.txts, output.scores, strict=True)
            ):
                if bounds is None or not text.strip():
                    continue
                kind = TextKind.HANDWRITTEN if handwriting and not mixed else TextKind.REVIEW
                line = TextLine(bounds, text.strip(), float(score), kind)
                if not mixed or not output.word_results:
                    lines.append(line)
                    continue
                glyphs = []
                for value, confidence, points in output.word_results[index]:
                    area_bounds = rectangle(points)
                    if area_bounds:
                        glyphs.append(Glyph(value, float(confidence), area_bounds))
                lines.extend(classify(gray, line, glyphs, typical_height))
        return Scan(selected, area, tuple(lines), perf_counter() - started)
