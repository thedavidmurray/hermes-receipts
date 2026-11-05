"""Optional ESC/POS USB printer. No enumeration or side effects on import."""
from __future__ import annotations


def print_receipt(text: str, vendor: int, product: int) -> tuple[bool, str]:
    """The operator opts in with the device IDs; never guess which USB printer to use."""
    try:
        from escpos.printer import Usb
    except ImportError:
        return False, 'Printer support not installed. Install python-escpos>=3,<4 to opt in.'
    try:
        printer = Usb(vendor, product, timeout=1000)
        printer.text(text.replace('✓', 'OK').replace('✗', 'NO').replace('—', '-').replace('🧾', ''))
        printer.cut()
        printer.close()
    except Exception as exc:
        return False, f'USB printer unavailable: {type(exc).__name__}: {exc}'
    return True, 'Receipt printed.'
