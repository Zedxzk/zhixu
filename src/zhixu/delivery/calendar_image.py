"""Deterministic PNG rendering for monthly calendars and daily agendas.

Drawn at twice the delivered size and resampled down, so corners, dots and text
edges are smooth. Only dates and aggregate counts enter an image; titles stay in
the accompanying card, which also keeps the renderer free of CJK typography.
"""

from __future__ import annotations

from calendar import month_name
from datetime import date, timedelta
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from zhixu.channels import CalendarPreview, DailyAgendaPreview

# Everything is laid out at this scale and resampled down once at the end.
_SUPERSAMPLE = 2

_WIDTH, _HEIGHT = 1120, 880

_BACKGROUND = (17, 24, 39)
_SURFACE = (31, 41, 55)
_SURFACE_MUTED = (24, 32, 45)
_HAIRLINE = (55, 65, 81)
_PRIMARY = (243, 244, 246)
_MUTED = (148, 163, 184)
_FAINT = (100, 116, 139)
_ACCENT = (59, 130, 246)
_BUSY = (251, 146, 60)
_AGENDA = (59, 130, 246)
_REMINDER = (251, 146, 60)
_ANNIVERSARY = (167, 139, 250)

_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


# Installed separately by the host; the bundled face is Latin-only. Titles are
# simply left out when none of these is present, so a missing font degrades the
# image instead of failing a briefing.
_CJK_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
)


@lru_cache(maxsize=1)
def _cjk_font_path() -> str | None:
    for candidate in _CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def cjk_titles_available() -> bool:
    """Whether titles can be drawn; preflight reports this to the operator."""

    return _cjk_font_path() is not None


@lru_cache(maxsize=32)
def _font(size: int) -> ImageFont.FreeTypeFont:
    """The scalable font Pillow bundles; no font file has to be shipped."""

    return ImageFont.load_default(size=size * _SUPERSAMPLE)


@lru_cache(maxsize=32)
def _title_font(size: int) -> ImageFont.FreeTypeFont | None:
    path = _cjk_font_path()
    if path is None:
        return None
    return ImageFont.truetype(path, size * _SUPERSAMPLE)


# The bundled face covers Latin only. Typographic punctuation and any CJK would
# come out as replacement boxes, so fold what we can and drop the rest.
_ASCII_FOLDING = str.maketrans(
    {
        "–": "-",
        "—": "-",
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "…": "...",
        " ": " ",
    }
)


def _drawable(value: str) -> str:
    folded = value.translate(_ASCII_FOLDING)
    return "".join(character for character in folded if character.isascii())


class _Sheet:
    """A supersampled drawing surface that flattens to a PNG."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        self._image = Image.new(
            "RGB",
            (width * _SUPERSAMPLE, height * _SUPERSAMPLE),
            _BACKGROUND,
        )
        self.draw = ImageDraw.Draw(self._image)

    def _box(self, x: float, y: float, width: float, height: float) -> tuple[float, ...]:
        left, top = x * _SUPERSAMPLE, y * _SUPERSAMPLE
        return (left, top, left + width * _SUPERSAMPLE - 1, top + height * _SUPERSAMPLE - 1)

    def panel(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        *,
        radius: float,
        fill: tuple[int, int, int] | None,
        outline: tuple[int, int, int] | None = None,
        outline_width: int = 1,
    ) -> None:
        self.draw.rounded_rectangle(
            self._box(x, y, width, height),
            radius=radius * _SUPERSAMPLE,
            fill=fill,
            outline=outline,
            width=outline_width * _SUPERSAMPLE,
        )

    def rule(self, x: float, y: float, width: float, colour: tuple[int, int, int]) -> None:
        self.panel(x, y, width, 1, radius=0, fill=colour)

    def dot(self, x: float, y: float, radius: float, colour: tuple[int, int, int]) -> None:
        centre_x, centre_y = x * _SUPERSAMPLE, y * _SUPERSAMPLE
        span = radius * _SUPERSAMPLE
        self.draw.ellipse(
            (centre_x - span, centre_y - span, centre_x + span, centre_y + span),
            fill=colour,
        )

    def text(
        self,
        x: float,
        y: float,
        value: str,
        size: int,
        colour: tuple[int, int, int],
        *,
        anchor: str = "la",
    ) -> None:
        self.draw.text(
            (x * _SUPERSAMPLE, y * _SUPERSAMPLE),
            _drawable(value),
            font=_font(size),
            fill=colour,
            anchor=anchor,
        )

    def title_text(
        self,
        x: float,
        y: float,
        value: str,
        size: int,
        colour: tuple[int, int, int],
        *,
        max_width: float,
    ) -> bool:
        """Draw a possibly-CJK title, or report that no font could render it."""

        font = _title_font(size)
        if font is None or not value:
            return False
        limit = max_width * _SUPERSAMPLE
        text = value
        while text and self.draw.textlength(text, font=font) > limit:
            text = text[:-1]
        if not text:
            return False
        if text != value:
            text = text[:-1] + "…"
        self.draw.text(
            (x * _SUPERSAMPLE, y * _SUPERSAMPLE),
            text,
            font=font,
            fill=colour,
            anchor="la",
        )
        return True

    def png(self) -> bytes:
        flattened = self._image.resize((self.width, self.height), Image.LANCZOS)
        buffer = BytesIO()
        flattened.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def _calendar_grid(year: int, month: int) -> tuple[date, ...]:
    """Return a stable six-week grid, including adjacent-month dates."""

    first_day = date(year, month, 1)
    grid_start = first_day - timedelta(days=first_day.weekday())
    return tuple(grid_start + timedelta(days=offset) for offset in range(42))


def render_calendar_png(preview: CalendarPreview) -> bytes:
    """Render a fixed six-week grid; only dates and counts enter the image."""

    sheet = _Sheet(_WIDTH, _HEIGHT)

    sheet.text(64, 52, month_name[preview.month].upper(), 40, _PRIMARY)
    sheet.text(64, 108, str(preview.year), 22, _FAINT)
    sheet.rule(64, 152, _WIDTH - 128, _HAIRLINE)

    margin, gap = 64, 10
    cell_width = (_WIDTH - margin * 2 - gap * 6) / 7
    cell_height = 92
    header_y = 176
    grid_y = 212

    for index, label in enumerate(_WEEKDAYS):
        centre = margin + index * (cell_width + gap) + cell_width / 2
        colour = _FAINT if index >= 5 else _MUTED
        sheet.text(centre, header_y, label, 17, colour, anchor="ma")

    busy_counts = dict(preview.busy_day_counts)
    for offset, cell_date in enumerate(_calendar_grid(preview.year, preview.month)):
        row, column = divmod(offset, 7)
        x = margin + column * (cell_width + gap)
        y = grid_y + row * (cell_height + gap)
        in_month = (
            cell_date.year == preview.year and cell_date.month == preview.month
        )
        is_today = in_month and cell_date.day == preview.today_day

        if is_today:
            sheet.panel(x, y, cell_width, cell_height, radius=14, fill=_ACCENT)
            number_colour = (255, 255, 255)
        else:
            sheet.panel(
                x,
                y,
                cell_width,
                cell_height,
                radius=14,
                fill=_SURFACE if in_month else _SURFACE_MUTED,
                outline=_HAIRLINE if in_month else None,
            )
            number_colour = _PRIMARY if in_month else _FAINT

        sheet.text(x + 16, y + 14, str(cell_date.day), 26, number_colour)

        count = busy_counts.get(cell_date.day) if in_month else None
        if count:
            dot_colour = (255, 255, 255) if is_today else _BUSY
            for dot_index in range(min(count, 3)):
                sheet.dot(x + 24 + dot_index * 16, y + cell_height - 22, 4.5, dot_colour)

    legend_y = grid_y + 6 * (cell_height + gap) + 20
    sheet.dot(margin + 6, legend_y, 5, _BUSY)
    sheet.text(margin + 22, legend_y - 10, "SCHEDULED", 16, _MUTED)
    if preview.today_day is not None:
        sheet.panel(margin + 176, legend_y - 8, 16, 16, radius=5, fill=_ACCENT)
        sheet.text(margin + 202, legend_y - 10, "TODAY", 16, _MUTED)
    return sheet.png()


def render_daily_agenda_png(preview: DailyAgendaPreview) -> bytes:
    """Render a title-free timeline; private titles remain in the card."""

    visible = preview.entries[:7]
    anniversary_rows = 1 if preview.anniversary_day_numbers else 0
    content_height = 190 + anniversary_rows * 80 + max(len(visible), 1) * 76
    sheet = _Sheet(_WIDTH, min(_HEIGHT, content_height + 40))
    stamp = date(preview.year, preview.month, preview.day)

    sheet.text(64, 52, stamp.strftime("%d %B").upper().lstrip("0"), 40, _PRIMARY)
    sheet.text(64, 108, stamp.strftime("%A %Y").upper(), 22, _FAINT)
    sheet.rule(64, 152, _WIDTH - 128, _HAIRLINE)

    y = 190.0
    if preview.anniversary_day_numbers:
        values = "   ".join(
            f"DAY {value}" for value in preview.anniversary_day_numbers[:3]
        )
        sheet.panel(64, y, _WIDTH - 128, 64, radius=14, fill=_SURFACE)
        sheet.panel(64, y, 6, 64, radius=3, fill=_ANNIVERSARY)
        sheet.text(96, y + 20, values, 22, _ANNIVERSARY)
        y += 80

    if not visible:
        sheet.text(_WIDTH / 2, y + 16, "NOTHING SCHEDULED", 26, _FAINT, anchor="ma")
    for index, (start, end, kind, title) in enumerate(visible, start=1):
        colour = _AGENDA if kind == "agenda" else _REMINDER
        sheet.panel(64, y, _WIDTH - 128, 64, radius=14, fill=_SURFACE)
        sheet.panel(64, y, 6, 64, radius=3, fill=colour)
        sheet.text(
            96,
            y + 20,
            f"{start // 60:02d}:{start % 60:02d} - {end // 60:02d}:{end % 60:02d}",
            22,
            _PRIMARY,
        )
        label = "EVENT" if kind == "agenda" else "REMINDER"
        label_width = 130
        drawn = sheet.title_text(
            288,
            y + 20,
            title,
            22,
            _PRIMARY,
            max_width=_WIDTH - 288 - 96 - label_width,
        )
        if not drawn:
            # No CJK font installed: fall back to the numbered label alone.
            sheet.text(_WIDTH - 96, y + 20, f"{label} {index}", 18, colour, anchor="ra")
        else:
            sheet.text(_WIDTH - 96, y + 22, label, 16, colour, anchor="ra")
        y += 76
    return sheet.png()
