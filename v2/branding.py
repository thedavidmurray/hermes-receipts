"""Branding: a dithered 1-bit logo and an optional bar/QR code.

Two honest halves:

* **logo** – any image the user drops in is converted to greyscale, scaled to fit
  the paper and dithered to 1-bit (Floyd-Steinberg) so a thermal or dot-matrix head
  prints it as dots. Failures are reported, never silently skipped.
* **code** – a Code 128 barcode (:mod:`v2.code128`) is always available because it is
  pure Python. A **QR** code is only offered when a pure-python QR dependency
  (``qrcode`` or ``segno``) is already importable; :func:`qr_provider` reports which,
  and :func:`qr_matrix` refuses rather than inventing a matrix. On a host without
  one, ``barcode: qr`` is rejected by config validation with that reason.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from . import code128

#: QR providers we accept, in preference order. Both are pure Python.
QR_PROVIDERS = ('qrcode', 'segno')


class BrandingError(ValueError):
    """A logo or barcode could not be produced."""


def qr_provider() -> str | None:
    """Name of the installed pure-python QR provider, or ``None``."""
    import importlib.util
    for name in QR_PROVIDERS:
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except (ImportError, ValueError):  # pragma: no cover - broken install
            continue
    return None


def qr_matrix(text: str) -> list[list[int]] | None:
    """A QR module matrix, or ``None`` when no provider is installed.

    ``None`` is the only "unavailable" signal: nothing here fabricates a matrix.
    """
    provider = qr_provider()
    if provider is None or not text:
        return None
    if provider == 'qrcode':
        import qrcode  # pragma: no cover - needs the optional dependency
        symbol = qrcode.QRCode(border=1)
        symbol.add_data(text)
        symbol.make(fit=True)
        return [[1 if module else 0 for module in row] for row in symbol.get_matrix()]
    import segno  # pragma: no cover - needs the optional dependency
    symbol = segno.make(text)
    return [[int(module) for module in row] for row in symbol.matrix]


@lru_cache(maxsize=16)
def _logo_bits(path: str, max_width: int, max_height: int) -> Image.Image:
    """Load, scale and dither a logo to 1-bit ink; cached per (path, box)."""
    if max_width < 1 or max_height < 1:
        raise BrandingError('logo box must be positive')
    try:
        with Image.open(path) as source:
            source.load()
            grey = ImageOps.grayscale(source.convert('RGBA').convert('RGB'))
    except (OSError, ValueError) as exc:
        raise BrandingError(f'logo unreadable: {type(exc).__name__}') from exc
    width, height = grey.size
    if not width or not height:
        raise BrandingError('logo has no pixels')
    scale = min(max_width / width, max_height / height, 1.0)
    target = (max(1, int(width * scale)), max(1, int(height * scale)))
    if target != grey.size:
        grey = grey.resize(target, Image.Resampling.LANCZOS)
    # Threshold + Floyd-Steinberg: 1 bit per pixel, which is all the head has.
    return grey.convert('1', dither=Image.Dither.FLOYDSTEINBERG)


def dithered_logo(path: str | Path, max_width: int, max_height: int) -> Image.Image:
    """The logo as a 1-bit image (mode ``1``): ink is black."""
    return _logo_bits(str(Path(path).expanduser()), int(max_width), int(max_height)).copy()


def logo_mask(path: str | Path, max_width: int, max_height: int) -> Image.Image:
    """An ``L`` mask with 255 where the logo prints ink and 0 where paper shows."""
    bits = dithered_logo(path, max_width, max_height)
    return bits.convert('L').point(lambda value: 255 - value)


def barcode_modules(payload: str) -> str:
    """The Code 128 module string for *payload* (1 = bar)."""
    return code128.modules(payload)


def barcode_bars(payload: str) -> list[tuple[int, int]]:
    """``(start, width)`` bar runs in modules for *payload*."""
    return code128.bars(payload)


def barcode_module_count(payload: str) -> int:
    return len(barcode_modules(payload))


def fit_module_width(payload: str, available_px: int) -> int:
    """Widest integer module width that fits *available_px*, or 0 when even 1 px is too wide."""
    modules = barcode_module_count(payload)
    if modules <= 0 or available_px < modules:
        return 0
    return max(1, available_px // modules)


def barcode_geometry(payload: str, available_px: int) -> dict[str, Any]:
    """Everything the renderer needs to draw the barcode, or ``{'fits': False}``."""
    modules = barcode_module_count(payload)
    module_px = fit_module_width(payload, available_px)
    if not module_px:
        return {'fits': False, 'modules': modules, 'module_px': 0,
                'reason': f'{modules} modules do not fit {available_px} px'}
    return {'fits': True, 'modules': modules, 'module_px': module_px,
            'width_px': modules * module_px, 'bars': barcode_bars(payload),
            'payload': payload}
