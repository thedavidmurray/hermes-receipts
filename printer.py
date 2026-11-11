"""Optional ESC/POS USB printer. No enumeration or side effects on import.

The operator opts in explicitly with the device IDs. Everything a receipt printer
does after the paper is loaded is configurable from ``receipts.yaml`` (``printer:``):
whether to cut, how many lines to feed, the print density and how many copies.

``density`` is sent as ``DC2 '#' n`` (``0x12 0x23 n``) with ``n = (density << 4) |
break_time`` — the ESC/POS "select print density" setting thermal printers use. The
receipt text, feed and cut go through python-escpos so nothing is re-implemented;
only the density command needs a raw write, because python-escpos has no API for it.
"""
from __future__ import annotations

#: DC2 '#' — ESC/POS "select print density" / "set print speed" pair.
DENSITY_COMMAND = b'\x12\x23'
#: Break-time bits (the common "speed level 1" pairing).
DEFAULT_BREAK_TIME = 3
MIN_DENSITY, MAX_DENSITY = 0, 15
MAX_FEED_LINES, MAX_COPIES = 10, 5


def density_command(density: int, break_time: int = DEFAULT_BREAK_TIME) -> bytes:
    """The raw ESC/POS bytes for a print density (0 lightest .. 15 darkest)."""
    if (isinstance(density, bool) or not isinstance(density, int)
            or not MIN_DENSITY <= density <= MAX_DENSITY):
        raise ValueError(f'printer density must be an integer between {MIN_DENSITY} and {MAX_DENSITY}')
    if not 0 <= break_time <= 3:
        raise ValueError('printer break time must be between 0 and 3')
    return DENSITY_COMMAND + bytes([(density << 4) | break_time])


def printer_text(text: str) -> str:
    """7-bit-safe paper text: the same substitution the printer path always used."""
    for glyph, replacement in (('\u2713', 'OK'), ('\u2717', 'NO'), ('\u2014', '-'), ('\u2013', '-'),
                               ('\U0001f9fe', '')):
        text = text.replace(glyph, replacement)
    return text


def print_receipt(text: str, vendor: int, product: int, *, cut: bool = True, feed_lines: int = 0,
                  density: int | None = None, copies: int = 1) -> tuple[bool, str]:
    """Print *text*; the operator opted in with the device IDs, so never guess a printer."""
    if not 1 <= copies <= MAX_COPIES:
        raise ValueError(f'printer copies must be between 1 and {MAX_COPIES}')
    if not 0 <= feed_lines <= MAX_FEED_LINES:
        raise ValueError(f'printer feed lines must be between 0 and {MAX_FEED_LINES}')
    if density is not None:
        density_command(density)  # validate before touching the device
    try:
        from escpos.printer import Usb
    except ImportError:
        return False, 'Printer support not installed. Install python-escpos>=3,<4 to opt in.'
    try:
        printer = Usb(vendor, product, timeout=1000)
        try:
            if density is not None:
                printer._raw(density_command(density))
            body = printer_text(text)
            for copy_index in range(copies):
                printer.text(body)
                if feed_lines:
                    printer.ln(feed_lines)
                if cut and copy_index == copies - 1:
                    printer.cut()
        finally:
            printer.close()
    except Exception as exc:
        return False, f'USB printer unavailable: {type(exc).__name__}: {exc}'
    return True, (f'Receipt printed ({copies} cop{"y" if copies == 1 else "ies"}, '
                  f'{"cut" if cut else "no cut"}, feed {feed_lines}, '
                  f'density {density if density is not None else "default"}).')
