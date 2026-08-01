from datetime import date

from zhixu.delivery.calendar_image import _calendar_grid


def test_calendar_grid_fills_leading_and_trailing_adjacent_month_days() -> None:
    grid = _calendar_grid(2026, 8)

    assert len(grid) == 42
    assert grid[:7] == tuple(date(2026, 7, day) for day in range(27, 32)) + (
        date(2026, 8, 1),
        date(2026, 8, 2),
    )
    assert grid[-7:] == (date(2026, 8, 31),) + tuple(
        date(2026, 9, day) for day in range(1, 7)
    )


def test_calendar_grid_fills_across_a_year_boundary() -> None:
    grid = _calendar_grid(2026, 12)

    assert grid[0] == date(2026, 11, 30)
    assert grid[-1] == date(2027, 1, 10)
