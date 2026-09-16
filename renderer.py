"""基于 Pillow 的无课教室渲染器（替代 HTML + Playwright 文转图）。

渲染效果对齐旧的 HTML 模板：玻璃拟态卡片 + 仿 Excel 表格，

    楼栋名称 | 楼层数 | 一大节 | 二大节 | 三大节 | 四大节 | 五大节 | 六大节
    逸夫楼   |  1层   | 教室   | 教室   |   —    | 教室   | 教室   | 教室

其中「楼栋名称」列纵向合并该楼栋的所有楼层，没有空教室的格子画一个破折号。

实现约定：

* 几何尺寸一律沿用旧模板的 CSS 数值（卡片圆角 23、表头高 66、正文字号 17……），
  统一以「CSS 像素」书写；绘制时乘 ``scale`` 超采样，最后用 LANCZOS 缩回，
  以此获得接近浏览器的抗锯齿边缘。
* 布局全部在整数 CSS 像素空间算完，绘制只做整数倍放大，避免表格线发虚。
* Pillow 在 RGBA 图上绘制是「覆盖」而不是「混合」，所以所有半透明色块和
  文字都先画在独立图层上再 ``alpha_composite``，保证叠色与浏览器一致。
* 字体只在插件数据目录的 ``fonts/`` 子目录里找（见 :func:`render_schedule`
  的 ``font_dir`` 参数），不做系统字体探测：把中文 ttf/ttc/otf 丢进去即可。
  目录里没有字体文件时抛 :class:`RenderError`，由调用方降级成文本输出。

本模块不依赖 AstrBot，可以脱离框架单独调用。
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

__all__ = ["RenderError", "SLOT_LABELS", "render_schedule"]


# ===========================================================================
# 一、可调常量（数值基本沿用旧 HTML 模板的 CSS）
# ===========================================================================

SCALE = 2  # 超采样倍率，等价于浏览器的 devicePixelRatio
SLOT_COUNT = 6
# fmt: off
SLOT_LABELS: tuple[str, ...] = ("一大节", "二大节", "三大节", "四大节", "五大节", "六大节")
DASH = "—"
# fmt: on

# 页面
PAGE_PAD_X = 42
PAGE_PAD_TOP = 34
PAGE_PAD_BOTTOM = 38

# 标题卡片
HERO_MIN_H = 154
HERO_PAD_X = 32
HERO_PAD_Y = 28
HERO_RADIUS = 28
HERO_GAP = 22  # 标题卡片与第一栋楼之间的距离
HERO_MARK_W = 130
HERO_MARK_H = 96
HERO_MARK_RADIUS = 22
HERO_MARK_TILT = 2.0  # 右上角小方块旋转角度（度）
HERO_MARK_GAP = 7  # 「无课」与「ROOMS」的间距

# 楼栋卡片
CARD_PAD = 12
CARD_RADIUS = 23
CARD_GAP = 20

# 表格
TABLE_RADIUS = 16
COL_BUILDING = 132
COL_FLOOR = 82
COL_SLOT_MIN = 160
COL_SLOT_MAX = 260  # 教室号很长时允许把大节列加宽到 260
HEADER_H = 66
ROW_MIN_H = 56  # 单行楼层的最小行高
CELL_PAD_X = 10
CELL_PAD_Y = 12
FLOOR_PAD_X = 7
ICON_SIZE = 38
ICON_GAP = 9  # 楼栋图标与楼栋名之间的间距
NAME_PAD_X = 10

# 字号（CSS 像素）
FONT_EYEBROW = 14
FONT_TITLE = 36
FONT_TITLE_MIN = 22
FONT_META = 15
FONT_HEADER = 18
FONT_BUILDING = 21
FONT_BUILDING_MIN = 15
FONT_FLOOR = 17
FONT_FLOOR_MIN = 12
FONT_ROOM = 17
FONT_DASH = 18
FONT_MARK = 25
FONT_MARK_SUB = 11
FONT_FOOTER = 12

# 行高倍率
LH_EYEBROW = 1.2
LH_TITLE = 1.18
LH_META = 1.35
LH_BUILDING = 1.25
LH_FLOOR = 1.3
LH_ROOM = 1.45
LH_MARK = 1.0
LH_MARK_SUB = 1.2
LH_FOOTER = 1.4

# 字距（CSS 像素）
TRACK_EYEBROW = 2.4
TRACK_MARK_SUB = 2.0

EYEBROW = "IBEIKE · CLASSROOM STATUS"
MARK_MAIN = "无课"
MARK_SUB = "ROOMS"
FOOTER_LEFT = "数据来源：iBeiKe 教务公开接口"
FOOTER_RIGHT = "astrbot_plugin_chai"


def _rgba(color: str, alpha: float = 1.0) -> tuple[int, int, int, int]:
    """把 ``#rrggbb`` / ``#rgb`` 与透明度转成 Pillow 用的 RGBA 元组。"""
    text = str(color).lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError("非法颜色: %r" % (color,))
    ratio = max(0.0, min(1.0, float(alpha)))
    return (
        int(text[0:2], 16),
        int(text[2:4], 16),
        int(text[4:6], 16),
        int(round(ratio * 255)),
    )


def _line_height(css_size: float, ratio: float) -> int:
    """按 CSS 的 line-height 倍率算出行高（CSS 像素，至少 1）。"""
    return max(1, int(round(css_size * ratio)))


# 背景：linear-gradient(135deg, #dcecff 0%, #edf6ff 46%, #e9e1ff 100%) + 两个径向光斑
# fmt: off
BG_STOPS: tuple[tuple[float, str], ...] = ((0.0, "#dcecff"), (0.46, "#edf6ff"), (1.0, "#e9e1ff"))
# (圆心 x 比例, 圆心 y 比例, 颜色, 透明度, 实心结束位置, 淡出结束位置)
BG_BLOBS: tuple[tuple[float, float, str, float, float, float], ...] = (
    (0.08, 0.05, "#ffffff", 0.95, 0.07, 0.22),
    (0.95, 0.10, "#b2e0ff", 0.62, 0.00, 0.25),
)
# fmt: on
BACKDROP_BLUR = 18  # backdrop-filter: blur(18px)
BACKDROP_SHRINK = 4  # 模糊背景按 1/4 分辨率保存，省内存

# 配色（对应旧模板的 CSS）
C_EYEBROW = "#7085aa"
C_TITLE = "#263a60"
C_META = "#73819b"

C_HERO_FILL = _rgba("#ffffff", 0.60)
C_HERO_BORDER = _rgba("#ffffff", 0.78)
C_HERO_SHADOW = _rgba("#5b709b", 0.16)

C_MARK_FROM = _rgba("#76b7ff", 0.82)
C_MARK_TO = _rgba("#a189f6", 0.72)
C_MARK_SHADOW = _rgba("#657dbe", 0.22)

C_CARD_FILL = _rgba("#ffffff", 0.48)
C_CARD_BORDER = _rgba("#ffffff", 0.74)
C_CARD_SHADOW = _rgba("#4c6592", 0.13)
C_CARD_INNER_HL = _rgba("#ffffff", 0.85)

C_TABLE_FILL = _rgba("#ffffff", 0.54)
C_TABLE_BORDER = _rgba("#9db1cf", 0.42)
C_GRID_LINE = _rgba("#a4b5cf", 0.38)
C_GRID_LINE_SOFT = _rgba("#a4b5cf", 0.32)

C_HEAD_BUILDING_FROM = _rgba("#74b3f1", 0.96)
C_HEAD_BUILDING_TO = _rgba("#758be0", 0.90)
C_HEAD_BUILDING_TEXT = (255, 255, 255, 255)
C_HEAD_FLOOR_TEXT = "#9a6c24"
C_HEAD_FLOOR_FILL = _rgba("#ffe7a4", 0.72)
C_HEAD_SLOT_TEXT = "#a94f62"
C_HEAD_SLOT_FILL = _rgba("#ffd2db", 0.62)

C_NAME_TEXT = "#243a60"
C_NAME_FROM = _rgba("#a7e0fa", 0.76)
C_NAME_TO = _rgba("#b4d2f7", 0.55)
C_ICON_FILL = _rgba("#5d81c0", 0.55)
C_ICON_GLYPH = _rgba("#ffffff", 0.92)

C_FLOOR_TEXT = "#816a43"
C_FLOOR_FILL = _rgba("#fff7d6", 0.68)
C_ROOM_TEXT = "#314665"
C_ROOM_FILL = _rgba("#ffffff", 0.38)
C_ROOM_FILL_ALT = _rgba("#f6faff", 0.42)
C_ROOM_EMPTY_TEXT = _rgba("#aeb8c8", 0.85)
C_ROOM_EMPTY_FILL = _rgba("#eef3f9", 0.34)

C_FOOTER_TEXT = _rgba("#77849a", 1.00)
C_FOOTER_WEAK = _rgba("#77849a", 0.65)

WHITE = (255, 255, 255, 255)
TRANSPARENT = (0, 0, 0, 0)


class RenderError(RuntimeError):
    """可以预期的渲染失败（没有数据、缺少中文字体等），调用方可据此降级。"""


# ===========================================================================
# 二、字体
# ===========================================================================
# 只从插件数据目录的 fonts/ 子目录里取字体，不做系统字体探测、也不做字形检测：
# 放进去什么就用什么（所以要放中文字体，别放 Arial 这类纯拉丁字体）。

_FONT_SUFFIXES = (".ttf", ".ttc", ".otf", ".otc")
_MAX_TTC_FACES = 12  # .ttc/.otc 枚举字面时的上限
_BOLD_WORDS = ("bold", "heavy", "black", "semibold", "bd")
# .ttc/.otc 里含多个字面时，优先挑名字像简体的那个
_SC_FACE_WORDS = ("sc", "simplified", "gb", "yahei", "pingfang", "hei", "song")


@dataclass(frozen=True)
class _Face:
    """某个字体文件中的一个字面。"""

    path: str
    index: int
    family: str
    style: str

    @property
    def label(self) -> str:
        return ("%s %s" % (self.family, self.style)).strip()


def _font_files(font_dir: str | os.PathLike[str]) -> list[Path]:
    """列出字体目录里的字体文件（目录不存在时返回空列表，只读一层）。"""
    try:
        return sorted(
            path
            for path in Path(font_dir).iterdir()
            if path.is_file() and path.suffix.lower() in _FONT_SUFFIXES
        )
    except OSError:
        return []


def _pick_face_index(path: Path) -> int:
    """决定用字体文件里的第几个字面。

    ``.ttf``/``.otf`` 只有第 0 个；``.ttc``/``.otc`` 里往往同时装着
    JP/KR/SC/TC 多个字面，这里优先取名字像简体中文的那个，找不到就用第 0 个。
    """
    for index in range(_MAX_TTC_FACES):
        try:
            font = ImageFont.truetype(str(path), 16, index=index)
        except Exception:  # noqa: BLE001 - 枚举到头或文件损坏
            break
        try:
            family, style = font.getname()
        except Exception:  # noqa: BLE001
            continue
        text = ("%s %s" % (family, style)).lower()
        if any(word in text for word in _SC_FACE_WORDS):
            return index
    return 0


def _load_face(path: Path, index: int) -> _Face:
    """读一下字面名字（只为日志/排查时看得懂，读不到也不影响渲染）。"""
    family = style = ""
    try:
        family, style = ImageFont.truetype(str(path), 16, index=index).getname()
    except Exception:  # noqa: BLE001
        pass
    return _Face(path=str(path), index=index, family=str(family), style=str(style))


def _resolve_faces(font_dir: str | os.PathLike[str]) -> tuple[_Face, _Face | None]:
    """从字体目录里挑出常规字面与粗体字面。

    文件名里带 ``bold``/``heavy``/``black``/``semibold``/``bd`` 的当作粗体；
    只放了一个字体文件就用它，没有粗体时用描边模拟加粗。
    """
    files = _font_files(font_dir)
    if not files:
        raise RenderError(
            "字体目录里没有字体文件：%s\n"
            "请把任意中文 ttf/ttc/otf 放进该目录后重试"
            "（例如 msyh.ttc、SourceHanSansSC-Regular.otf、NotoSansSC-Regular.ttf）。"
            % font_dir
        )

    regular: _Face | None = None
    bold: _Face | None = None
    for path in files:
        is_bold = any(word in path.stem.lower() for word in _BOLD_WORDS)
        if is_bold:
            if bold is None:
                bold = _load_face(path, _pick_face_index(path))
        elif regular is None:
            regular = _load_face(path, _pick_face_index(path))

    if regular is None:  # 目录里只有粗体，那就拿它当常规字面用
        regular, bold = bold, None
    return regular, bold  # type: ignore[return-value]


@dataclass(frozen=True)
class _Metrics:
    """一个字号的字体度量，长度单位都是「画布像素」。"""

    font: ImageFont.FreeTypeFont
    size: int
    ascent: int
    descent: int
    line_box: int
    ink_shift: float
    stroke: int  # > 0 表示没有粗体字面，用描边模拟加粗

    def stroke_kwargs(self, fill: tuple[int, int, int, int]) -> dict[str, Any]:
        if not self.stroke:
            return {}
        return {"stroke_width": self.stroke, "stroke_fill": fill}


@dataclass
class _FontBook:
    """解析好的字体集合，按字号缓存 FreeType 字体对象与度量。"""

    regular: _Face
    bold: _Face | None
    scale: int
    _cache: dict[tuple[int, bool], _Metrics] = field(default_factory=dict, repr=False)

    @classmethod
    def resolve(
        cls, font_dir: str | os.PathLike[str] | None, scale: int
    ) -> "_FontBook":
        """从字体目录里取字体；目录为空或没指定时抛 :class:`RenderError`。"""
        if not font_dir:
            raise RenderError("没有指定字体目录")
        regular, bold = _resolve_faces(font_dir)
        return cls(regular=regular, bold=bold, scale=scale)

    def metrics(self, css_size: float, bold: bool = False) -> _Metrics:
        """取某个 CSS 字号下的字体度量。"""
        pixel_size = max(6, int(round(css_size * self.scale)))
        faux_bold = bool(bold) and self.bold is None
        key = (pixel_size, bool(bold))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        face = self.bold if (bold and self.bold is not None) else self.regular
        try:
            font = ImageFont.truetype(face.path, pixel_size, index=face.index)
        except Exception:  # noqa: BLE001 - 字体文件被删掉时退回常规字面
            font = ImageFont.truetype(
                self.regular.path, pixel_size, index=self.regular.index
            )

        ascent, descent = font.getmetrics()
        line_box = ascent + descent
        # 汉字的视觉中心比行盒几何中心略低，量出来做一次补偿
        try:
            ref = font.getbbox("国")
            ink_shift = (ref[1] + ref[3]) / 2.0 - line_box / 2.0
        except Exception:  # noqa: BLE001
            ink_shift = 0.0

        metrics = _Metrics(
            font=font,
            size=pixel_size,
            ascent=ascent,
            descent=descent,
            line_box=line_box,
            ink_shift=ink_shift,
            stroke=max(1, self.scale) if faux_bold else 0,
        )
        self._cache[key] = metrics
        return metrics


# ===========================================================================
# 三、文本折行与绘制（坐标单位统一为画布像素）
# ===========================================================================

_TOKEN_RE = re.compile(r"[^、,，;；/]+[、,，;；/]*")


def _clean_text(value: Any) -> str:
    """把任意值整理成单行文本。"""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return re.sub(r"\s{2,}", " ", text)


class _Measurer:
    """文字宽度测量工具，返回 CSS 像素（布局全程只用 CSS 像素）。"""

    def __init__(self, scale: int = 1) -> None:
        self.scale = max(1, int(scale))
        self._draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))

    def width(self, text: str, metrics: _Metrics) -> float:
        if not text:
            return 0.0
        return float(self._draw.textlength(text, font=metrics.font)) / self.scale


def _wrap_by_width(
    text: str, metrics: _Metrics, max_width: float, measurer: _Measurer
) -> list[str]:
    """按像素宽度折行。

    优先以「、」等分隔符为单位断行，避免把一个教室号拆成两行；只有单个词
    本身就超宽时才逐字符硬拆。
    """
    text = _clean_text(text)
    if not text:
        return []
    tokens = [token for token in _TOKEN_RE.findall(text) if token] or [text]

    lines: list[str] = []
    current = ""
    for token in tokens:
        if measurer.width(token, metrics) > max_width:
            if current:
                lines.append(current)
                current = ""
            chunks = _hard_split(token, metrics, max_width, measurer)
            lines.extend(chunks[:-1])
            current = chunks[-1] if chunks else ""
            continue
        if current and measurer.width(current + token, metrics) > max_width:
            lines.append(current)
            current = token
        else:
            current += token
    if current:
        lines.append(current)
    return lines


def _hard_split(
    token: str, metrics: _Metrics, max_width: float, measurer: _Measurer
) -> list[str]:
    """把一个过长的词按字符切开。"""
    chunks: list[str] = []
    current = ""
    for char in token:
        if current and measurer.width(current + char, metrics) > max_width:
            chunks.append(current)
            current = char
        else:
            current += char
    if current:
        chunks.append(current)
    return chunks


def _draw_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    metrics: _Metrics,
    fill: tuple[int, int, int, int],
    center_x: float,
    center_y: float,
    tracking: float = 0.0,
) -> None:
    """以 (center_x, center_y) 为中心画一行文本（坐标均为画布像素）。"""
    if not text:
        return
    y = center_y - metrics.line_box / 2.0 - metrics.ink_shift
    kwargs: dict[str, Any] = {"font": metrics.font, "fill": fill}
    kwargs.update(metrics.stroke_kwargs(fill))

    if not tracking:
        width = float(draw.textlength(text, font=metrics.font))
        draw.text((center_x - width / 2.0, y), text, **kwargs)
        return

    width = float(draw.textlength(text, font=metrics.font)) + tracking * (len(text) - 1)
    x = center_x - width / 2.0
    for char in text:
        draw.text((x, y), char, **kwargs)
        x += float(draw.textlength(char, font=metrics.font)) + tracking


def _draw_line_left(
    draw: ImageDraw.ImageDraw,
    text: str,
    metrics: _Metrics,
    fill: tuple[int, int, int, int],
    left_x: float,
    center_y: float,
    tracking: float = 0.0,
) -> None:
    """左对齐画一行文本，垂直居中于 center_y。"""
    if not text:
        return
    y = center_y - metrics.line_box / 2.0 - metrics.ink_shift
    kwargs: dict[str, Any] = {"font": metrics.font, "fill": fill}
    kwargs.update(metrics.stroke_kwargs(fill))
    if not tracking:
        draw.text((left_x, y), text, **kwargs)
        return
    x = left_x
    for char in text:
        draw.text((x, y), char, **kwargs)
        x += float(draw.textlength(char, font=metrics.font)) + tracking


def _draw_right(
    draw: ImageDraw.ImageDraw,
    text: str,
    metrics: _Metrics,
    fill: tuple[int, int, int, int],
    right_x: float,
    center_y: float,
) -> None:
    """右对齐画一行文本。"""
    if not text:
        return
    width = float(draw.textlength(text, font=metrics.font))
    _draw_line_left(draw, text, metrics, fill, right_x - width, center_y)


def _draw_paragraph(
    draw: ImageDraw.ImageDraw,
    lines: Sequence[str],
    metrics: _Metrics,
    fill: tuple[int, int, int, int],
    center_x: float,
    top: float,
    line_height: int,
) -> None:
    """多行文本整体水平居中，行盒自 top 起向下排列（坐标均为画布像素）。"""
    for index, line in enumerate(lines):
        _draw_line(
            draw, line, metrics, fill, center_x, top + (index + 0.5) * line_height
        )


# ===========================================================================
# 四、绘图基础件
# ===========================================================================


def _composite(
    canvas: Image.Image, layer: Image.Image, position: tuple[int, int]
) -> None:
    """把局部图层按 alpha 叠加到画布上，自动裁剪越界部分。"""
    x, y = position
    width, height = layer.size
    left, top = max(0, -x), max(0, -y)
    right, bottom = min(width, canvas.width - x), min(height, canvas.height - y)
    if right <= left or bottom <= top:
        return
    if (left, top, right, bottom) != (0, 0, width, height):
        layer = layer.crop((left, top, right, bottom))
    canvas.alpha_composite(layer, dest=(x + left, y + top))


def _fill_box(
    layer: Image.Image, box: tuple[int, int, int, int], fill: tuple[int, int, int, int]
) -> None:
    """在半透明图层上「叠」一块底色（Pillow 的 draw 是覆盖，这里要混合）。"""
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0 or fill[3] <= 0:
        return
    if fill[3] >= 255:
        ImageDraw.Draw(layer).rectangle(box, fill=fill)
        return
    patch = Image.new("RGBA", (x1 - x0, y1 - y0), fill)
    layer.alpha_composite(patch, dest=(x0, y0))


def _gradient(
    size: tuple[int, int],
    start: tuple[int, int, int, int],
    end: tuple[int, int, int, int],
) -> Image.Image:
    """用 2x2 源图双线性放大，模拟 ``linear-gradient(145deg)``。"""
    width, height = max(1, size[0]), max(1, size[1])
    source = Image.new("RGBA", (2, 2))
    middle = tuple(int(round((start[i] + end[i]) / 2.0)) for i in range(4))
    source.putpixel((0, 0), start)
    source.putpixel((1, 0), middle)
    source.putpixel((0, 1), middle)
    source.putpixel((1, 1), end)
    return source.resize((width, height), Image.BILINEAR)


def _gradient_vertical(
    size: tuple[int, int],
    start: tuple[int, int, int, int],
    end: tuple[int, int, int, int],
) -> Image.Image:
    """竖向渐变，模拟 ``linear-gradient(180deg)``。"""
    width, height = max(1, size[0]), max(1, size[1])
    source = Image.new("RGBA", (1, 2))
    source.putpixel((0, 0), start)
    source.putpixel((0, 1), end)
    return source.resize((width, height), Image.BILINEAR)


def _lerp(
    start: tuple[int, int, int], end: tuple[int, int, int], ratio: float
) -> tuple[int, int, int]:
    ratio = max(0.0, min(1.0, ratio))
    return (
        int(round(start[0] + (end[0] - start[0]) * ratio)),
        int(round(start[1] + (end[1] - start[1]) * ratio)),
        int(round(start[2] + (end[2] - start[2]) * ratio)),
    )


def _make_background(width: int, height: int) -> Image.Image:
    """背景：135° 三色渐变 + 两个径向光斑。"""
    grid = 128
    stops = [(_rgba(color)[:3], position) for position, color in BG_STOPS]
    pixels: list[tuple[int, int, int]] = []
    for y in range(grid):
        for x in range(grid):
            t = (x + y) / (2.0 * (grid - 1))
            color = stops[-1][0]
            for (start, begin), (end, finish) in zip(stops, stops[1:]):
                if t <= finish:
                    color = _lerp(start, end, (t - begin) / max(1e-6, finish - begin))
                    break
            pixels.append(color)
    gradient = Image.new("RGB", (grid, grid))
    gradient.putdata(pixels)
    canvas = gradient.resize((width, height), Image.BICUBIC).convert("RGBA")

    for cx_ratio, cy_ratio, color, alpha, solid, fade in BG_BLOBS:
        center_x, center_y = cx_ratio * width, cy_ratio * height
        farthest = max(
            math.hypot(center_x - x, center_y - y)
            for x in (0.0, float(width))
            for y in (0.0, float(height))
        )
        radius = max(1.0, farthest * fade)
        size = int(round(radius * 2))
        mask = _radial_mask(grid, solid / max(1e-6, fade), 1.0).resize(
            (size, size), Image.BICUBIC
        )
        color_rgba = _rgba(color)
        mask = mask.point(lambda value, a=color_rgba[3]: int(value * a / 255))
        blob = Image.new("RGBA", (size, size), color_rgba[:3] + (255,))
        blob.putalpha(mask)
        _composite(
            canvas, blob, (int(round(center_x - radius)), int(round(center_y - radius)))
        )
    return canvas


def _radial_mask(size: int, solid: float, fade: float) -> Image.Image:
    """径向渐变遮罩：到 solid 为止满值，到 fade 处衰减为 0（按半径比例）。"""
    solid = max(0.0, min(solid, 0.98))
    fade = max(solid + 1e-3, fade)
    half = (size - 1) / 2.0
    data: list[int] = []
    for y in range(size):
        for x in range(size):
            distance = math.hypot(x - half, y - half) / half
            if distance <= solid:
                data.append(255)
            elif distance >= fade:
                data.append(0)
            else:
                data.append(int(round(255 * (fade - distance) / (fade - solid))))
    mask = Image.new("L", (size, size))
    mask.putdata(data)
    return mask


@dataclass(frozen=True)
class _Backdrop:
    """缩小保存的模糊背景，用于模拟 backdrop-filter。"""

    image: Image.Image
    shrink: int

    def patch(self, box: tuple[int, int, int, int]) -> Image.Image:
        """裁出指定区域并放大回原尺寸（越界时自动夹到图片范围内）。"""
        x0, y0, x1, y1 = box
        shrink = self.shrink
        width, height = self.image.size
        left = min(max(0, x0 // shrink), width - 1)
        top = min(max(0, y0 // shrink), height - 1)
        right = min(max(left + 1, -(-x1 // shrink)), width)
        bottom = min(max(top + 1, -(-y1 // shrink)), height)
        return self.image.crop((left, top, right, bottom)).resize(
            (x1 - x0, y1 - y0), Image.BICUBIC
        )


class _Painter:
    """把 CSS 像素坐标换算成画布像素的一层薄封装。

    所有方法都通过局部图层做 alpha 混合，避免 Pillow 在 RGBA 图上的
    「覆盖式」绘制破坏半透明叠加。
    """

    def __init__(self, scale: int) -> None:
        self.scale = max(1, int(scale))

    # ------------------------------------------------------------- 换算
    def px(self, value: float) -> int:
        return int(round(value * self.scale))

    def box(self, box: Sequence[float]) -> tuple[int, int, int, int]:
        x0, y0, x1, y1 = box
        return (self.px(x0), self.px(y0), self.px(x1), self.px(y1))

    def size(self, width: float, height: float) -> tuple[int, int]:
        return (max(1, self.px(width)), max(1, self.px(height)))

    # ------------------------------------------------------------- 图元
    def line(
        self,
        canvas: Image.Image,
        start: Sequence[float],
        end: Sequence[float],
        fill: tuple[int, int, int, int],
        width: float = 1.0,
    ) -> None:
        if fill[3] <= 0:
            return
        pixel_width = max(1, self.px(width))
        x0, y0 = self.px(start[0]), self.px(start[1])
        x1, y1 = self.px(end[0]), self.px(end[1])
        pad = pixel_width + 1
        left, top = min(x0, x1) - pad, min(y0, y1) - pad
        layer = Image.new(
            "RGBA", (abs(x1 - x0) + 2 * pad, abs(y1 - y0) + 2 * pad), TRANSPARENT
        )
        ImageDraw.Draw(layer).line(
            (x0 - left, y0 - top, x1 - left, y1 - top), fill=fill, width=pixel_width
        )
        _composite(canvas, layer, (left, top))

    def rounded(
        self,
        canvas: Image.Image,
        box: Sequence[float],
        radius: float,
        fill: tuple[int, int, int, int] | None = None,
        outline: tuple[int, int, int, int] | None = None,
        width: float = 1.0,
    ) -> None:
        pixel_box = self.box(box)
        width_px = pixel_box[2] - pixel_box[0]
        height_px = pixel_box[3] - pixel_box[1]
        if width_px <= 0 or height_px <= 0 or (fill is None and outline is None):
            return
        pad = max(1, self.px(width)) + 1
        layer = Image.new(
            "RGBA", (width_px + 2 * pad, height_px + 2 * pad), TRANSPARENT
        )
        ImageDraw.Draw(layer).rounded_rectangle(
            (pad, pad, pad + width_px - 1, pad + height_px - 1),
            radius=self.px(radius),
            fill=fill,
            outline=outline,
            width=max(1, self.px(width)) if outline else 0,
        )
        _composite(canvas, layer, (pixel_box[0] - pad, pixel_box[1] - pad))

    def shadow(
        self,
        canvas: Image.Image,
        box: Sequence[float],
        radius: float,
        color: tuple[int, int, int, int],
        blur: float,
        offset: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        """模拟 CSS 的 box-shadow（blur 为模糊半径，高斯 sigma 取其一半）。"""
        if color[3] <= 0:
            return
        sigma = max(0.5, blur * self.scale / 2.0)
        pad = int(math.ceil(sigma * 3)) + 1
        width_px = self.px(box[2] - box[0])
        height_px = self.px(box[3] - box[1])
        if width_px <= 0 or height_px <= 0:
            return
        layer = Image.new(
            "RGBA", (width_px + 2 * pad, height_px + 2 * pad), TRANSPARENT
        )
        ImageDraw.Draw(layer).rounded_rectangle(
            (pad, pad, pad + width_px - 1, pad + height_px - 1),
            radius=self.px(radius),
            fill=color,
        )
        layer = layer.filter(ImageFilter.GaussianBlur(sigma))
        _composite(
            canvas,
            layer,
            (self.px(box[0] + offset[0]) - pad, self.px(box[1] + offset[1]) - pad),
        )

    def glass(
        self,
        canvas: Image.Image,
        backdrop: _Backdrop | None,
        box: Sequence[float],
        radius: float,
        fill: tuple[int, int, int, int],
        border: tuple[int, int, int, int] | None = None,
        inner_highlight: tuple[int, int, int, int] | None = None,
    ) -> None:
        """玻璃卡片：先贴一层背景模糊（等价 backdrop-filter），再叠半透明底色。"""
        pixel_box = self.box(box)
        width_px = pixel_box[2] - pixel_box[0]
        height_px = pixel_box[3] - pixel_box[1]
        if width_px <= 0 or height_px <= 0:
            return

        if backdrop is not None:
            patch = backdrop.patch(pixel_box)
            mask = Image.new("L", (width_px, height_px), 0)
            ImageDraw.Draw(mask).rounded_rectangle(
                (0, 0, width_px - 1, height_px - 1), radius=self.px(radius), fill=255
            )
            patch.putalpha(ImageChops.multiply(patch.getchannel("A"), mask))
            _composite(canvas, patch, (pixel_box[0], pixel_box[1]))

        self.rounded(canvas, box, radius, fill=fill)
        if border is not None:
            self.rounded(canvas, box, radius, outline=border, width=1.0)
        if inner_highlight is not None:
            inset = radius / 2.0
            self.line(
                canvas,
                (box[0] + inset, box[1] + 1),
                (box[2] - inset, box[1] + 1),
                inner_highlight,
                width=1.0,
            )


# ===========================================================================
# 五、布局计算（整数 CSS 像素）
# ===========================================================================


@dataclass
class _RowLayout:
    floor_lines: list[str] = field(default_factory=list)
    floor_size: float = FONT_FLOOR
    floor_line_height: int = _line_height(FONT_FLOOR, LH_FLOOR)
    cells: list[list[str]] = field(default_factory=list)
    height: int = ROW_MIN_H


@dataclass
class _BuildingLayout:
    name: str = ""
    name_lines: list[str] = field(default_factory=list)
    name_size: float = FONT_BUILDING
    name_line_height: int = _line_height(FONT_BUILDING, LH_BUILDING)
    rows: list[_RowLayout] = field(default_factory=list)
    header_h: int = HEADER_H
    body_h: int = 0

    @property
    def table_height(self) -> int:
        return self.header_h + self.body_h

    @property
    def name_group_height(self) -> int:
        return (
            ICON_SIZE + ICON_GAP + self.name_line_height * max(1, len(self.name_lines))
        )


@dataclass
class _PageLayout:
    width: int = 0
    height: int = 0
    table_width: int = 0
    content_width: int = 0  # 卡片外框宽度 = 表格宽 + 左右内边距
    column_widths: list[int] = field(default_factory=list)
    hero: tuple[int, int, int, int] = (0, 0, 0, 0)
    hero_content_top: int = 0
    eyebrow_height: int = _line_height(FONT_EYEBROW, LH_EYEBROW)
    title_lines: list[str] = field(default_factory=list)
    title_size: float = FONT_TITLE
    title_line_height: int = _line_height(FONT_TITLE, LH_TITLE)
    meta: str = ""
    meta_height: int = _line_height(FONT_META, LH_META)
    cards: list[tuple[int, int, int, int]] = field(default_factory=list)
    buildings: list[_BuildingLayout] = field(default_factory=list)
    footer_y: int = 0
    footer_line_height: int = _line_height(FONT_FOOTER, LH_FOOTER)


def _normalize_buildings(
    buildings: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    """整理成 ``[{"name": str, "rows": [{"floor": str, "cells": [str] * 6}]}]``。"""
    result: list[dict[str, Any]] = []
    for item in buildings or ():
        if not isinstance(item, Mapping):
            continue
        name = _clean_text(item.get("name")) or "未知楼栋"
        rows: list[dict[str, Any]] = []
        for raw_row in item.get("rows") or ():
            if not isinstance(raw_row, Mapping):
                continue
            cells = [_clean_text(value) for value in (raw_row.get("cells") or ())]
            cells = (cells + [""] * SLOT_COUNT)[:SLOT_COUNT]
            floor = _clean_text(raw_row.get("floor"))
            if not floor and not any(cells):
                continue
            rows.append({"floor": floor, "cells": cells})
        if rows:
            result.append({"name": name, "rows": rows})
    return result


def _pick_slot_width(
    buildings: Sequence[Mapping[str, Any]],
    book: _FontBook,
    measurer: _Measurer,
) -> int:
    """按最长的单个教室号决定大节列宽，保证教室号不会被拆行。"""
    metrics = book.metrics(FONT_ROOM)
    widest = 0.0
    for building in buildings:
        for row in building["rows"]:
            for cell in row["cells"]:
                for token in _TOKEN_RE.findall(cell) or ():
                    widest = max(
                        widest, measurer.width(token.rstrip("、,，;；/"), metrics)
                    )
    return int(min(COL_SLOT_MAX, max(COL_SLOT_MIN, math.ceil(widest + 2 * CELL_PAD_X))))


def _fit_text(
    text: str,
    book: _FontBook,
    measurer: _Measurer,
    max_width: float,
    sizes: Sequence[float],
    bold: bool,
    ratio: float,
    max_lines: int,
) -> tuple[list[str], float, int]:
    """从给定字号候选中挑一个能放下文本的字号。

    Returns:
        ``(折行后的文本, 使用的字号, 行高)``。
    """
    lines: list[str] = []
    size = sizes[-1] if sizes else FONT_ROOM
    line_height = _line_height(size, ratio)
    for candidate in sizes:
        metrics = book.metrics(candidate, bold=bold)
        wrapped = _wrap_by_width(text, metrics, max_width, measurer)
        lines, size = wrapped, candidate
        line_height = _line_height(candidate, ratio)
        if len(wrapped) <= max_lines:
            break
    return lines, size, line_height


def _build_layout(
    buildings: Sequence[Mapping[str, Any]],
    title: str,
    fetched_at: str,
    book: _FontBook,
    scale: int,
) -> _PageLayout:
    """算好整页布局：列宽、行高、各区块的绝对位置（全部为整数 CSS 像素）。"""
    measurer = _Measurer(scale)
    layout = _PageLayout()

    slot_width = _pick_slot_width(buildings, book, measurer)
    layout.column_widths = [COL_BUILDING, COL_FLOOR] + [slot_width] * SLOT_COUNT
    layout.table_width = int(sum(layout.column_widths))
    layout.content_width = layout.table_width + 2 * CARD_PAD
    layout.width = layout.content_width + 2 * PAGE_PAD_X

    room_metrics = book.metrics(FONT_ROOM)
    room_line_height = _line_height(FONT_ROOM, LH_ROOM)
    room_max_width = max(20.0, float(slot_width - 2 * CELL_PAD_X))
    name_max_width = max(30.0, float(COL_BUILDING - 2 * NAME_PAD_X))
    floor_max_width = max(20.0, float(COL_FLOOR - 2 * FLOOR_PAD_X))

    for building in buildings:
        item = _BuildingLayout(name=building["name"])
        item.name_lines, item.name_size, item.name_line_height = _fit_text(
            building["name"],
            book,
            measurer,
            name_max_width,
            (FONT_BUILDING, 19, 17, FONT_BUILDING_MIN),
            bold=True,
            ratio=LH_BUILDING,
            max_lines=4,
        )

        rows: list[_RowLayout] = []
        for raw_row in building["rows"]:
            row = _RowLayout()
            floor_text = "%s层" % raw_row["floor"] if raw_row["floor"] else ""
            row.floor_lines, row.floor_size, row.floor_line_height = _fit_text(
                floor_text,
                book,
                measurer,
                floor_max_width,
                (FONT_FLOOR, 15, 13, FONT_FLOOR_MIN),
                bold=True,
                ratio=LH_FLOOR,
                max_lines=1,
            )

            max_lines = 1
            for cell in raw_row["cells"]:
                lines = _wrap_by_width(cell, room_metrics, room_max_width, measurer)
                row.cells.append(lines)
                max_lines = max(max_lines, len(lines))
            row.height = max(ROW_MIN_H, max_lines * room_line_height + 2 * CELL_PAD_Y)
            rows.append(row)

        body_height = sum(row.height for row in rows)
        # 楼栋名（图标 + 文字）比楼层区域还高时，把多出来的高度摊到各行
        if rows and item.name_group_height + 16 > body_height:
            extra = int(
                math.ceil((item.name_group_height + 16 - body_height) / len(rows))
            )
            for row in rows:
                row.height += extra
            body_height = sum(row.height for row in rows)

        item.rows = rows
        item.body_h = body_height
        layout.buildings.append(item)

    # ---- 标题区 ----
    hero_text_width = layout.content_width - 2 * HERO_PAD_X - HERO_MARK_W - 24
    layout.title_lines, layout.title_size, layout.title_line_height = _fit_text(
        title,
        book,
        measurer,
        max(120.0, float(hero_text_width)),
        (FONT_TITLE, 32, 28, 26, FONT_TITLE_MIN),
        bold=True,
        ratio=LH_TITLE,
        max_lines=2,
    )
    layout.eyebrow_height = _line_height(FONT_EYEBROW, LH_EYEBROW)
    layout.meta_height = _line_height(FONT_META, LH_META)
    layout.meta = "%d 栋楼 · %d 个大节 · 数据更新于 %s" % (
        len(layout.buildings),
        SLOT_COUNT,
        fetched_at or "未知",
    )
    hero_content = (
        layout.eyebrow_height
        + 9
        + layout.title_line_height * max(1, len(layout.title_lines))
        + 11
        + layout.meta_height
    )
    hero_height = max(HERO_MIN_H, hero_content + 2 * HERO_PAD_Y)

    # ---- 纵向排布 ----
    y = PAGE_PAD_TOP
    layout.hero = (PAGE_PAD_X, y, PAGE_PAD_X + layout.content_width, y + hero_height)
    layout.hero_content_top = y + HERO_PAD_Y
    y += hero_height + HERO_GAP
    for item in layout.buildings:
        card_height = 2 * CARD_PAD + item.table_height
        layout.cards.append(
            (PAGE_PAD_X, y, PAGE_PAD_X + layout.content_width, y + card_height)
        )
        y += card_height + CARD_GAP

    layout.footer_y = y
    layout.footer_line_height = _line_height(FONT_FOOTER, LH_FOOTER)
    layout.height = y + 4 + layout.footer_line_height + PAGE_PAD_BOTTOM
    return layout


# ===========================================================================
# 六、页面绘制
# ===========================================================================


def _draw_hero(
    painter: _Painter,
    canvas: Image.Image,
    backdrop: _Backdrop,
    layout: _PageLayout,
    book: _FontBook,
) -> None:
    box = layout.hero
    painter.shadow(canvas, box, HERO_RADIUS, C_HERO_SHADOW, blur=48, offset=(0, 18))
    painter.glass(
        canvas,
        backdrop,
        box,
        HERO_RADIUS,
        fill=C_HERO_FILL,
        border=C_HERO_BORDER,
        inner_highlight=C_CARD_INNER_HL,
    )

    width_px = painter.px(box[2] - box[0])
    height_px = painter.px(box[3] - box[1])
    layer = Image.new("RGBA", (width_px, height_px), TRANSPARENT)
    draw = ImageDraw.Draw(layer)

    left = painter.px(HERO_PAD_X)
    cursor = painter.px(layout.hero_content_top - box[1])

    eyebrow = book.metrics(FONT_EYEBROW, bold=True)
    _draw_line_left(
        draw,
        EYEBROW,
        eyebrow,
        _rgba(C_EYEBROW),
        left,
        cursor + painter.px(layout.eyebrow_height) / 2.0,
        tracking=painter.px(TRACK_EYEBROW),
    )
    cursor += painter.px(layout.eyebrow_height + 9)

    title = book.metrics(layout.title_size, bold=True)
    for index, line in enumerate(layout.title_lines):
        _draw_line(
            draw,
            line,
            title,
            _rgba(C_TITLE),
            left + float(draw.textlength(line, font=title.font)) / 2.0,
            cursor + (index + 0.5) * painter.px(layout.title_line_height),
        )
    cursor += painter.px(layout.title_line_height) * max(
        1, len(layout.title_lines)
    ) + painter.px(11)

    meta = book.metrics(FONT_META)
    _draw_line_left(
        draw,
        layout.meta,
        meta,
        _rgba(C_META),
        left,
        cursor + painter.px(layout.meta_height) / 2.0,
    )
    _composite(canvas, layer, painter.box(box)[:2])

    _draw_hero_mark(painter, canvas, layout, book)


def _draw_hero_mark(
    painter: _Painter, canvas: Image.Image, layout: _PageLayout, book: _FontBook
) -> None:
    """右上角的「无课 ROOMS」小方块（带一点旋转）。"""
    hero = layout.hero
    mark_top = hero[1] + (hero[3] - hero[1] - HERO_MARK_H) // 2
    mark_box = (
        hero[2] - HERO_PAD_X - HERO_MARK_W,
        mark_top,
        hero[2] - HERO_PAD_X,
        mark_top + HERO_MARK_H,
    )
    painter.shadow(
        canvas, mark_box, HERO_MARK_RADIUS, C_MARK_SHADOW, blur=28, offset=(0, 12)
    )

    width_px, height_px = painter.size(HERO_MARK_W, HERO_MARK_H)
    shape = Image.new("RGBA", (width_px, height_px), TRANSPARENT)
    gradient = _gradient((width_px, height_px), C_MARK_FROM, C_MARK_TO)
    mask = Image.new("L", (width_px, height_px), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width_px - 1, height_px - 1),
        radius=painter.px(HERO_MARK_RADIUS),
        fill=255,
    )
    shape.paste(gradient, (0, 0), mask)

    main = book.metrics(FONT_MARK, bold=True)
    sub = book.metrics(FONT_MARK_SUB, bold=True)
    main_line = painter.px(_line_height(FONT_MARK, LH_MARK))
    sub_line = painter.px(_line_height(FONT_MARK_SUB, LH_MARK_SUB))
    gap = painter.px(HERO_MARK_GAP)
    top = (height_px - (main_line + gap + sub_line)) / 2.0

    draw = ImageDraw.Draw(shape)
    _draw_line(draw, MARK_MAIN, main, WHITE, width_px / 2.0, top + main_line / 2.0)
    _draw_line(
        draw,
        MARK_SUB,
        sub,
        (255, 255, 255, 222),
        width_px / 2.0,
        top + main_line + gap + sub_line / 2.0,
        tracking=painter.px(TRACK_MARK_SUB),
    )

    rotated = shape.rotate(
        HERO_MARK_TILT, resample=Image.BICUBIC, center=(width_px / 2.0, height_px / 2.0)
    )
    _composite(
        canvas,
        rotated,
        (
            int(round(painter.px(mark_box[0]) + (width_px - rotated.width) / 2.0)),
            int(round(painter.px(mark_box[1]) + (height_px - rotated.height) / 2.0)),
        ),
    )


def _draw_building(
    painter: _Painter,
    canvas: Image.Image,
    backdrop: _Backdrop,
    item: _BuildingLayout,
    card: Sequence[float],
    columns: Sequence[int],
    book: _FontBook,
) -> None:
    painter.shadow(canvas, card, CARD_RADIUS, C_CARD_SHADOW, blur=34, offset=(0, 12))
    painter.glass(
        canvas,
        backdrop,
        card,
        CARD_RADIUS,
        fill=C_CARD_FILL,
        border=C_CARD_BORDER,
        inner_highlight=C_CARD_INNER_HL,
    )
    table_box = (
        card[0] + CARD_PAD,
        card[1] + CARD_PAD,
        card[0] + CARD_PAD + sum(columns),
        card[1] + CARD_PAD + item.table_height,
    )
    _draw_table(painter, canvas, item, table_box, columns, book)


def _draw_table(
    painter: _Painter,
    canvas: Image.Image,
    item: _BuildingLayout,
    table_box: Sequence[float],
    columns: Sequence[int],
    book: _FontBook,
) -> None:
    """绘制一栋楼的表格：底色层 + 文字层，圆角裁切后贴到画布，最后画表格线。"""
    table_width = int(sum(columns))
    table_height = item.table_height
    base = Image.new("RGBA", painter.size(table_width, table_height), TRANSPARENT)
    text_layer = Image.new("RGBA", base.size, TRANSPARENT)
    draw = ImageDraw.Draw(text_layer)

    header_metrics = book.metrics(FONT_HEADER, bold=True)
    room_metrics = book.metrics(FONT_ROOM)
    dash_metrics = book.metrics(FONT_DASH)
    room_line_height = painter.px(_line_height(FONT_ROOM, LH_ROOM))
    floor_metrics: dict[float, _Metrics] = {}

    _fill_box(base, painter.box((0, 0, table_width, table_height)), C_TABLE_FILL)

    # ---------------------------------------------------------- 表头
    headers = ("楼栋名称", "楼层数") + tuple(SLOT_LABELS)
    header_colors = (C_HEAD_BUILDING_TEXT, _rgba(C_HEAD_FLOOR_TEXT)) + (
        _rgba(C_HEAD_SLOT_TEXT),
    ) * SLOT_COUNT
    x = 0
    for index, column_width in enumerate(columns):
        pixel_box = painter.box((x, 0, x + column_width, item.header_h))
        if index == 0:
            base.alpha_composite(
                _gradient(
                    (pixel_box[2] - pixel_box[0], pixel_box[3] - pixel_box[1]),
                    C_HEAD_BUILDING_FROM,
                    C_HEAD_BUILDING_TO,
                ),
                dest=(pixel_box[0], pixel_box[1]),
            )
        elif index == 1:
            _fill_box(base, pixel_box, C_HEAD_FLOOR_FILL)
        else:
            _fill_box(base, pixel_box, C_HEAD_SLOT_FILL)
        _draw_line(
            draw,
            headers[index],
            header_metrics,
            header_colors[index],
            painter.px(x + column_width / 2),
            painter.px(item.header_h / 2),
        )
        x += column_width

    # ---------------------------------------------------------- 正文
    row_tops: list[int] = []
    y = item.header_h
    for row in item.rows:
        row_tops.append(y)
        x = 0
        for column_index, column_width in enumerate(columns):
            pixel_box = painter.box((x, y, x + column_width, y + row.height))
            if column_index >= 2:
                cell = row.cells[column_index - 2]
                if not cell:
                    _fill_box(base, pixel_box, C_ROOM_EMPTY_FILL)
                elif (column_index - 2) % 2:
                    _fill_box(base, pixel_box, C_ROOM_FILL_ALT)
                else:
                    _fill_box(base, pixel_box, C_ROOM_FILL)
            elif column_index == 1:
                _fill_box(base, pixel_box, C_FLOOR_FILL)
            x += column_width

        if row.floor_lines:
            metrics = floor_metrics.get(row.floor_size)
            if metrics is None:
                metrics = book.metrics(row.floor_size, bold=True)
                floor_metrics[row.floor_size] = metrics
            line_height = painter.px(row.floor_line_height)
            top = (
                painter.px(y)
                + (painter.px(row.height) - line_height * len(row.floor_lines)) / 2.0
            )
            _draw_paragraph(
                draw,
                row.floor_lines,
                metrics,
                _rgba(C_FLOOR_TEXT),
                painter.px(columns[0] + columns[1] / 2),
                top,
                line_height,
            )

        x = columns[0] + columns[1]
        for slot_index, lines in enumerate(row.cells):
            column_width = columns[2 + slot_index]
            center_x = painter.px(x + column_width / 2)
            if lines:
                text_height = room_line_height * len(lines)
                top = painter.px(y) + (painter.px(row.height) - text_height) / 2.0
                _draw_paragraph(
                    draw,
                    lines,
                    room_metrics,
                    _rgba(C_ROOM_TEXT),
                    center_x,
                    top,
                    room_line_height,
                )
            else:
                _draw_line(
                    draw,
                    DASH,
                    dash_metrics,
                    C_ROOM_EMPTY_TEXT,
                    center_x,
                    painter.px(y + row.height / 2),
                )
            x += column_width
        y += row.height

    # ---------------------------------------------------------- 楼栋名（纵向合并）
    _draw_building_name(painter, base, text_layer, item, columns[0], book)

    base.alpha_composite(text_layer)

    width_px, height_px = base.size
    mask = Image.new("L", (width_px, height_px), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, width_px - 1, height_px - 1), radius=painter.px(TABLE_RADIUS), fill=255
    )
    base.putalpha(ImageChops.multiply(base.getchannel("A"), mask))
    _composite(canvas, base, painter.box(table_box)[:2])

    # ---------------------------------------------------------- 表格线
    left, top = float(table_box[0]), float(table_box[1])
    right, bottom = left + table_width, top + table_height
    x = left
    for column_width in columns[:-1]:
        x += column_width
        painter.line(canvas, (x, top), (x, bottom), C_GRID_LINE)
    painter.line(
        canvas, (left, top + item.header_h), (right, top + item.header_h), C_GRID_LINE
    )
    for row_top in row_tops[1:]:
        painter.line(
            canvas,
            (left + columns[0], top + row_top),
            (right, top + row_top),
            C_GRID_LINE_SOFT,
        )
    painter.rounded(
        canvas, (left, top, right, bottom), TABLE_RADIUS, outline=C_TABLE_BORDER
    )
    # 楼栋名整列与右侧第一列之间的分隔线
    painter.line(
        canvas, (left + columns[0], top), (left + columns[0], bottom), C_GRID_LINE
    )


def _draw_building_name(
    painter: _Painter,
    base: Image.Image,
    text_layer: Image.Image,
    item: _BuildingLayout,
    column_width: int,
    book: _FontBook,
) -> None:
    """绘制纵向合并的「楼栋名称」单元格：竖向渐变底 + 小房子图标 + 楼栋名。

    渐变底画在底色层，图标与文字画在文字层，避免文字的半透明边缘覆盖底色。
    """
    top = item.header_h
    height = item.body_h
    pixel_box = painter.box((0, top, column_width, top + height))
    base.alpha_composite(
        _gradient_vertical(
            (pixel_box[2] - pixel_box[0], pixel_box[3] - pixel_box[1]),
            C_NAME_FROM,
            C_NAME_TO,
        ),
        dest=(pixel_box[0], pixel_box[1]),
    )

    metrics = book.metrics(item.name_size, bold=True)
    line_height = painter.px(item.name_line_height)
    group_height = (
        painter.px(ICON_SIZE)
        + painter.px(ICON_GAP)
        + line_height * max(1, len(item.name_lines))
    )
    group_top = painter.px(top) + (painter.px(height) - group_height) / 2.0

    _composite(
        text_layer,
        _house_icon(painter),
        (
            int(
                round(
                    pixel_box[0]
                    + (pixel_box[2] - pixel_box[0] - painter.px(ICON_SIZE)) / 2.0
                )
            ),
            int(round(group_top)),
        ),
    )
    _draw_paragraph(
        ImageDraw.Draw(text_layer),
        item.name_lines,
        metrics,
        _rgba(C_NAME_TEXT),
        (pixel_box[0] + pixel_box[2]) / 2.0,
        group_top + painter.px(ICON_SIZE + ICON_GAP),
        line_height,
    )


def _house_icon(painter: _Painter) -> Image.Image:
    """画一个圆角方块 + 房子剪影，避免依赖 ⌂ 这类符号字形。"""
    size = painter.px(ICON_SIZE)
    icon = Image.new("RGBA", (size, size), TRANSPARENT)
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=painter.px(12), fill=C_ICON_FILL
    )
    unit = size / float(ICON_SIZE)
    draw.polygon(
        (
            (int(round(10.0 * unit)), int(round(8.0 * unit))),
            (int(round(32.5 * unit)), int(round(17.0 * unit))),
            (int(round(5.5 * unit)), int(round(17.0 * unit))),
        ),
        fill=C_ICON_GLYPH,
    )
    draw.rectangle(
        (
            int(round(9.0 * unit)),
            int(round(16.0 * unit)),
            int(round(29.0 * unit)),
            int(round(29.5 * unit)),
        ),
        fill=C_ICON_GLYPH,
    )
    # 门：在图标图层上挖空，露出下层的渐变色
    draw.rectangle(
        (
            int(round(16.4 * unit)),
            int(round(22.0 * unit)),
            int(round(21.6 * unit)),
            int(round(29.5 * unit)),
        ),
        fill=TRANSPARENT,
    )
    return icon


def _draw_footer(
    painter: _Painter, canvas: Image.Image, layout: _PageLayout, book: _FontBook
) -> None:
    metrics = book.metrics(FONT_FOOTER)
    width_px = painter.px(layout.width)
    height_px = painter.px(4 + layout.footer_line_height)
    layer = Image.new("RGBA", (width_px, height_px), TRANSPARENT)
    draw = ImageDraw.Draw(layer)
    center_y = painter.px(4 + layout.footer_line_height / 2)
    _draw_line_left(
        draw, FOOTER_LEFT, metrics, C_FOOTER_TEXT, painter.px(PAGE_PAD_X + 10), center_y
    )
    _draw_right(
        draw,
        FOOTER_RIGHT,
        metrics,
        C_FOOTER_WEAK,
        painter.px(PAGE_PAD_X + layout.content_width - 10),
        center_y,
    )
    _composite(canvas, layer, (0, painter.px(layout.footer_y)))


# ===========================================================================
# 七、对外接口
# ===========================================================================


def render_schedule(
    buildings: Sequence[Mapping[str, Any]],
    title: str,
    fetched_at: str,
    output_path: str | os.PathLike[str],
    *,
    font_dir: str | os.PathLike[str] | None = None,
    scale: int = SCALE,
    quality: int = 92,
) -> str:
    """把无课教室数据渲染成图片并保存，返回图片路径。

    Args:
        buildings: ``[{"name": 楼栋名, "rows": [{"floor": 楼层, "cells": [六列文本]}]}]``。
        title: 标题，例如 ``9月15日（周二） 无课教室``。
        fetched_at: 数据更新时间，显示在标题下方。
        output_path: 输出路径，后缀决定格式（``.jpg`` / ``.png``）。
        font_dir: 字体目录，渲染时从里面挑中文字体。
        scale: 超采样倍率，默认 2。
        quality: JPEG 质量，默认 92。

    Returns:
        字符串形式的输出路径。

    Raises:
        RenderError: 没有可渲染的数据，或字体目录里没有字体文件。
        OSError: 输出目录不可写或图片保存失败。
    """
    normalized = _normalize_buildings(buildings)
    if not normalized:
        raise RenderError("没有可渲染的楼栋数据")

    scale = max(1, int(scale))
    book = _FontBook.resolve(font_dir, scale)
    layout = _build_layout(
        normalized,
        _clean_text(title) or "无课教室",
        _clean_text(fetched_at),
        book,
        scale,
    )
    painter = _Painter(scale)

    canvas = _make_background(painter.px(layout.width), painter.px(layout.height))
    backdrop = _Backdrop(
        image=canvas.resize(
            (
                max(1, canvas.width // BACKDROP_SHRINK),
                max(1, canvas.height // BACKDROP_SHRINK),
            ),
            Image.BILINEAR,
        ).filter(
            ImageFilter.GaussianBlur(max(0.5, BACKDROP_BLUR * scale / BACKDROP_SHRINK))
        ),
        shrink=BACKDROP_SHRINK,
    )

    _draw_hero(painter, canvas, backdrop, layout, book)
    for item, card in zip(layout.buildings, layout.cards):
        _draw_building(
            painter, canvas, backdrop, item, card, layout.column_widths, book
        )
    _draw_footer(painter, canvas, layout, book)

    del backdrop
    final = canvas.convert("RGB").resize((layout.width, layout.height), Image.LANCZOS)

    path = Path(output_path)
    if str(path.parent) not in ("", ".") and not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix.lower()
    if suffix in (".jpg", ".jpeg"):
        final.save(
            path, format="JPEG", quality=int(quality), optimize=True, progressive=True
        )
    else:
        final.save(path, format="PNG", optimize=True)
    return str(path)
