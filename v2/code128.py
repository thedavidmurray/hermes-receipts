"""Code 128 bar patterns, transcribed from the specification table.

The 107 symbol patterns below are the ISO/IEC 15417 patterns as published in the
Code 128 table on Wikipedia ("Code 128", Bar code widths section). Each entry is
the 11-module pattern for that symbol value; ``STOP_PATTERN`` appends the final
2-module bar so the stop symbol is the standard 13-module pattern.

Imported rather than typed by hand: the table was parsed straight out of the
fetched source, and every row is checked (pattern == the run-length expansion of
the widths column, 11 modules per symbol).
"""
from __future__ import annotations

#: Symbols 0..106: 103 = Start A, 104 = Start B, 105 = Start C, 106 = Stop.
PATTERNS: tuple[str, ...] = (
    '11011001100',  # 0 widths 212222
    '11001101100',  # 1 widths 222122
    '11001100110',  # 2 widths 222221
    '10010011000',  # 3 widths 121223
    '10010001100',  # 4 widths 121322
    '10001001100',  # 5 widths 131222
    '10011001000',  # 6 widths 122213
    '10011000100',  # 7 widths 122312
    '10001100100',  # 8 widths 132212
    '11001001000',  # 9 widths 221213
    '11001000100',  # 10 widths 221312
    '11000100100',  # 11 widths 231212
    '10110011100',  # 12 widths 112232
    '10011011100',  # 13 widths 122132
    '10011001110',  # 14 widths 122231
    '10111001100',  # 15 widths 113222
    '10011101100',  # 16 widths 123122
    '10011100110',  # 17 widths 123221
    '11001110010',  # 18 widths 223211
    '11001011100',  # 19 widths 221132
    '11001001110',  # 20 widths 221231
    '11011100100',  # 21 widths 213212
    '11001110100',  # 22 widths 223112
    '11101101110',  # 23 widths 312131
    '11101001100',  # 24 widths 311222
    '11100101100',  # 25 widths 321122
    '11100100110',  # 26 widths 321221
    '11101100100',  # 27 widths 312212
    '11100110100',  # 28 widths 322112
    '11100110010',  # 29 widths 322211
    '11011011000',  # 30 widths 212123
    '11011000110',  # 31 widths 212321
    '11000110110',  # 32 widths 232121
    '10100011000',  # 33 widths 111323
    '10001011000',  # 34 widths 131123
    '10001000110',  # 35 widths 131321
    '10110001000',  # 36 widths 112313
    '10001101000',  # 37 widths 132113
    '10001100010',  # 38 widths 132311
    '11010001000',  # 39 widths 211313
    '11000101000',  # 40 widths 231113
    '11000100010',  # 41 widths 231311
    '10110111000',  # 42 widths 112133
    '10110001110',  # 43 widths 112331
    '10001101110',  # 44 widths 132131
    '10111011000',  # 45 widths 113123
    '10111000110',  # 46 widths 113321
    '10001110110',  # 47 widths 133121
    '11101110110',  # 48 widths 313121
    '11010001110',  # 49 widths 211331
    '11000101110',  # 50 widths 231131
    '11011101000',  # 51 widths 213113
    '11011100010',  # 52 widths 213311
    '11011101110',  # 53 widths 213131
    '11101011000',  # 54 widths 311123
    '11101000110',  # 55 widths 311321
    '11100010110',  # 56 widths 331121
    '11101101000',  # 57 widths 312113
    '11101100010',  # 58 widths 312311
    '11100011010',  # 59 widths 332111
    '11101111010',  # 60 widths 314111
    '11001000010',  # 61 widths 221411
    '11110001010',  # 62 widths 431111
    '10100110000',  # 63 widths 111224
    '10100001100',  # 64 widths 111422
    '10010110000',  # 65 widths 121124
    '10010000110',  # 66 widths 121421
    '10000101100',  # 67 widths 141122
    '10000100110',  # 68 widths 141221
    '10110010000',  # 69 widths 112214
    '10110000100',  # 70 widths 112412
    '10011010000',  # 71 widths 122114
    '10011000010',  # 72 widths 122411
    '10000110100',  # 73 widths 142112
    '10000110010',  # 74 widths 142211
    '11000010010',  # 75 widths 241211
    '11001010000',  # 76 widths 221114
    '11110111010',  # 77 widths 413111
    '11000010100',  # 78 widths 241112
    '10001111010',  # 79 widths 134111
    '10100111100',  # 80 widths 111242
    '10010111100',  # 81 widths 121142
    '10010011110',  # 82 widths 121241
    '10111100100',  # 83 widths 114212
    '10011110100',  # 84 widths 124112
    '10011110010',  # 85 widths 124211
    '11110100100',  # 86 widths 411212
    '11110010100',  # 87 widths 421112
    '11110010010',  # 88 widths 421211
    '11011011110',  # 89 widths 212141
    '11011110110',  # 90 widths 214121
    '11110110110',  # 91 widths 412121
    '10101111000',  # 92 widths 111143
    '10100011110',  # 93 widths 111341
    '10001011110',  # 94 widths 131141
    '10111101000',  # 95 widths 114113
    '10111100010',  # 96 widths 114311
    '11110101000',  # 97 widths 411113
    '11110100010',  # 98 widths 411311
    '10111011110',  # 99 widths 113141
    '10111101110',  # 100 widths 114131
    '11101011110',  # 101 widths 311141
    '11110101110',  # 102 widths 411131
    '11010000100',  # 103 widths 211412
    '11010010000',  # 104 widths 211214
    '11010011100',  # 105 widths 211232
    '11000111010',  # 106 widths 233111
)
#: The stop symbol is the only 13-module pattern: 106 plus a final 2-module bar.
STOP_PATTERN: str = PATTERNS[106] + '11'
#: Value of the Start B symbol (ASCII 32..127, one module value per character).
START_B = 104
#: Quiet zone either side of the symbol, in modules (10x is the standard minimum).
QUIET_MODULES = 10


def symbol_values(payload: str) -> list[int]:
    """Code Set B symbol values for *payload*, without start/check/stop.

    Code Set B covers ASCII 32..127 with ``value = ord(char) - 32``. A character
    outside that range (or an empty payload) raises ``ValueError``: nothing here
    silently transliterates a payload, because a barcode that scans as different
    text than it claims is worse than no barcode.
    """
    if not isinstance(payload, str) or not payload:
        raise ValueError('barcode payload must be a non-empty string')
    values = []
    for char in payload:
        code = ord(char)
        if not 32 <= code <= 127:
            raise ValueError(f'barcode payload may only hold printable ASCII; got {char!r}')
        values.append(code - 32)
    return values


def checksum(values: list[int], start: int = START_B) -> int:
    """Weighted modulo-103 check symbol: start + sum(value x weight) mod 103."""
    total = start
    for weight, value in enumerate(values, 1):
        total += weight * value
    return total % 103


def symbol_sequence(payload: str) -> list[int]:
    """Full symbol list: Start B, data, check symbol, stop."""
    values = symbol_values(payload)
    return [START_B, *values, checksum(values), 106]


def modules(payload: str, quiet: bool = True) -> str:
    """The module string for *payload*: 1 is a bar, 0 a space."""
    bits = ''.join(PATTERNS[value] for value in symbol_sequence(payload)[:-1])
    bits += STOP_PATTERN
    if quiet:
        bits = '0' * QUIET_MODULES + bits + '0' * QUIET_MODULES
    return bits


def bars(payload: str, quiet: bool = True) -> list[tuple[int, int]]:
    """``(start, width)`` runs of bars, in modules, left to right."""
    runs: list[tuple[int, int]] = []
    position = 0
    for char in modules(payload, quiet=quiet):
        if char == '1':
            if runs and runs[-1][0] + runs[-1][1] == position:
                runs[-1] = (runs[-1][0], runs[-1][1] + 1)
            else:
                runs.append((position, 1))
        position += 1
    return runs


def decode(bits: str, quiet: bool = True) -> str:
    """Decode a module string back to its payload (used to test the encoder)."""
    if quiet:
        if not bits.startswith('0' * QUIET_MODULES) or not bits.endswith('0' * QUIET_MODULES):
            raise ValueError('missing quiet zone')
        bits = bits[QUIET_MODULES:len(bits) - QUIET_MODULES]
    if not bits.endswith(STOP_PATTERN):
        raise ValueError('missing stop pattern')
    body = bits[:len(bits) - len(STOP_PATTERN)]
    if len(body) % 11:
        raise ValueError('symbols must be 11 modules')
    values = []
    for index in range(0, len(body), 11):
        symbol = body[index:index + 11]
        if symbol not in PATTERNS:
            raise ValueError(f'unknown symbol {symbol!r}')
        values.append(PATTERNS.index(symbol))
    if len(values) < 2 or values[0] != START_B:
        raise ValueError('code set B start required')
    payload = ''.join(chr(value + 32) for value in values[1:-1])
    if checksum([ord(char) - 32 for char in payload]) != values[-1]:
        raise ValueError('check symbol mismatch')
    return payload
