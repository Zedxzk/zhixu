"""Dependency-free, deterministic PNG rendering for monthly calendars."""

from __future__ import annotations

import binascii
import struct
import zlib
from datetime import date, timedelta

from zhixu.channels import CalendarPreview, DailyAgendaPreview

_FONT = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
}


class _Canvas:
    def __init__(self, width: int, height: int, color: tuple[int, int, int]) -> None:
        self.width = width
        self.height = height
        self.pixels = bytearray(color * (width * height))

    def rect(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        color: tuple[int, int, int],
    ) -> None:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + width), min(self.height, y + height)
        row = bytes(color) * max(0, x1 - x0)
        for current_y in range(y0, y1):
            start = (current_y * self.width + x0) * 3
            self.pixels[start : start + len(row)] = row

    def circle(
        self,
        center_x: int,
        center_y: int,
        radius: int,
        color: tuple[int, int, int],
    ) -> None:
        radius_squared = radius * radius
        for y in range(center_y - radius, center_y + radius + 1):
            distance = radius_squared - (y - center_y) ** 2
            if distance < 0:
                continue
            half_width = int(distance**0.5)
            self.rect(center_x - half_width, y, half_width * 2 + 1, 1, color)

    def text_width(self, value: str, scale: int) -> int:
        return max(0, len(value) * 6 * scale - scale)

    def text(
        self,
        x: int,
        y: int,
        value: str,
        scale: int,
        color: tuple[int, int, int],
    ) -> None:
        cursor = x
        for character in value.upper():
            glyph = _FONT.get(character)
            if glyph is not None:
                for row_index, row in enumerate(glyph):
                    for column_index, pixel in enumerate(row):
                        if pixel == "1":
                            self.rect(
                                cursor + column_index * scale,
                                y + row_index * scale,
                                scale,
                                scale,
                                color,
                            )
            cursor += 6 * scale

    def png(self) -> bytes:
        scanlines = bytearray()
        row_size = self.width * 3
        for y in range(self.height):
            scanlines.append(0)
            start = y * row_size
            scanlines.extend(self.pixels[start : start + row_size])
        signature = b"\x89PNG\r\n\x1a\n"
        header = struct.pack(">IIBBBBB", self.width, self.height, 8, 2, 0, 0, 0)
        return signature + _chunk(b"IHDR", header) + _chunk(
            b"IDAT", zlib.compress(bytes(scanlines), level=9)
        ) + _chunk(b"IEND", b"")


def _chunk(kind: bytes, data: bytes) -> bytes:
    checksum = binascii.crc32(kind + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)


def _calendar_grid(year: int, month: int) -> tuple[date, ...]:
    """Return a stable six-week grid, including adjacent-month dates."""

    first_day = date(year, month, 1)
    grid_start = first_day - timedelta(days=first_day.weekday())
    return tuple(grid_start + timedelta(days=offset) for offset in range(42))


def render_calendar_png(preview: CalendarPreview) -> bytes:
    """Render a fixed-grid PNG; only dates and aggregate counts enter the image."""

    width, height = 1120, 820
    background = (15, 23, 42)
    panel = (30, 41, 59)
    adjacent_panel = (23, 32, 48)
    border = (51, 65, 85)
    primary = (241, 245, 249)
    muted = (148, 163, 184)
    today = (37, 99, 235)
    busy = (244, 114, 86)
    canvas = _Canvas(width, height, background)

    title = f"{preview.year:04d}-{preview.month:02d}"
    canvas.text(62, 48, title, 8, primary)
    canvas.rect(62, 120, 996, 2, border)

    weekdays = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
    cell_width, cell_height, gap = 136, 88, 7
    grid_x, grid_y = 61, 205
    for index, label in enumerate(weekdays):
        x = grid_x + index * (cell_width + gap)
        label_width = canvas.text_width(label, 3)
        canvas.text(x + (cell_width - label_width) // 2, 151, label, 3, muted)

    busy_counts = dict(preview.busy_day_counts)
    grid = _calendar_grid(preview.year, preview.month)
    for row_index in range(6):
        for column_index in range(7):
            cell_date = grid[row_index * 7 + column_index]
            day = cell_date.day
            in_month = (
                cell_date.year == preview.year and cell_date.month == preview.month
            )
            x = grid_x + column_index * (cell_width + gap)
            y = grid_y + row_index * (cell_height + gap)
            fill = panel if in_month else adjacent_panel
            if in_month and day == preview.today_day:
                fill = today
            canvas.rect(x, y, cell_width, cell_height, border)
            canvas.rect(x + 2, y + 2, cell_width - 4, cell_height - 4, fill)
            day_value = str(day)
            day_color = primary if in_month else muted
            canvas.text(x + 15, y + 15, day_value, 5, day_color)
            count = busy_counts.get(day) if in_month else None
            if count is not None:
                canvas.circle(x + cell_width - 25, y + cell_height - 24, 14, busy)
                count_value = str(min(count, 99))
                count_width = canvas.text_width(count_value, 2)
                canvas.text(
                    x + cell_width - 25 - count_width // 2,
                    y + cell_height - 31,
                    count_value,
                    2,
                    primary,
                )

    canvas.circle(72, 786, 9, busy)
    canvas.text(93, 772, "BUSY", 3, muted)
    if preview.today_day is not None:
        canvas.rect(265, 776, 20, 20, today)
        canvas.text(300, 772, "TODAY", 3, muted)
    return canvas.png()


def render_daily_agenda_png(preview: DailyAgendaPreview) -> bytes:
    """Render a title-free timeline; private titles remain in the accompanying card."""

    width, height = 1120, 820
    background = (15, 23, 42)
    panel = (30, 41, 59)
    border = (51, 65, 85)
    primary = (241, 245, 249)
    muted = (148, 163, 184)
    agenda_color = (37, 99, 235)
    reminder_color = (244, 114, 86)
    canvas = _Canvas(width, height, background)
    canvas.text(62, 45, f"{preview.year:04d}-{preview.month:02d}-{preview.day:02d}", 7, primary)
    canvas.text(62, 112, "TODAY SCHEDULE", 3, muted)
    canvas.rect(62, 156, 996, 2, border)

    y = 186
    if preview.anniversary_day_numbers:
        values = " ".join(f"DAY {value}" for value in preview.anniversary_day_numbers[:3])
        canvas.rect(62, y, 996, 64, border)
        canvas.rect(64, y + 2, 992, 60, (51, 45, 83))
        canvas.text(88, y + 18, values, 3, (216, 180, 254))
        y += 82

    visible = preview.entries[:7]
    if not visible:
        canvas.text(62, y + 80, "NO EVENTS", 5, muted)
    for index, (start, end, kind) in enumerate(visible, start=1):
        start_hour, start_minute = divmod(start, 60)
        end_hour, end_minute = divmod(end, 60)
        color = agenda_color if kind == "agenda" else reminder_color
        canvas.rect(62, y, 996, 64, border)
        canvas.rect(64, y + 2, 12, 60, color)
        canvas.rect(76, y + 2, 980, 60, panel)
        canvas.text(
            100,
            y + 17,
            f"{start_hour:02d}-{start_minute:02d}  {end_hour:02d}-{end_minute:02d}",
            3,
            primary,
        )
        label = "EVENT" if kind == "agenda" else "REMINDER"
        label_value = f"{label} {index}"
        label_width = canvas.text_width(label_value, 3)
        canvas.text(1030 - label_width, y + 17, label_value, 3, color)
        y += 76
    if len(preview.entries) > len(visible):
        canvas.text(62, 772, f"PLUS {len(preview.entries) - len(visible)}", 3, muted)
    return canvas.png()
