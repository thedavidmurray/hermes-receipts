"""Thermal paper renderer; data-only rendering, no remote fonts or printer access."""
from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

PAPER = '#f4efdf'
INK = '#222620'
GREEN = '#127847'
RED = '#a73532'
MUTED = '#666557'
WIDTH = 580


def _font(size: int, bold: bool = False):
    candidates = (["/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"])
    for name in candidates:
        if Path(name).is_file():
            return ImageFont.truetype(name, size=size, index=1 if bold and name.endswith('.ttc') else 0)
    return ImageFont.load_default()


def render_text(receipt: dict) -> str:
    claims = receipt['claims']
    verified = sum(c['verdict'] == '✓' for c in claims)
    lines = ['HERMES AGENT', '*** RECEIPT ***', f"session {receipt['session_id'][:28]}", f"turn    #{receipt['turn_id']}", '-' * 38]
    for row in receipt['rows'][:80]:
        duration = '—' if row.get('duration_ms') is None else f"{row['duration_ms'] / 1000:.1f}s"
        lines.append(f"{row['tool'][:16]:16} {row.get('label', '')[:15]:15} {duration}")
        if row['status'] != 'ok':
            lines.append(f"   {row['status']}{' exit ' + str(row['exit_code']) if row.get('exit_code') is not None else ''}")
    if len(receipt['rows']) > 80:
        lines.append(f"... {len(receipt['rows']) - 80} more tool calls")
    lines += ['-' * 38, 'CLAIMS']
    for claim in claims:
        lines.append(f"{claim['verdict']} {claim['text'][:55]}")
        lines.append(f"    {claim['evidence'][:55]}")
    lines += ['-' * 38, f"TOKENS      {receipt['tokens'] if receipt['tokens'] is not None else 'unavailable (turn)'}",
              f"EST. COST  {'$' + format(receipt['cost_usd'], '.4f') if receipt['cost_usd'] is not None else 'unavailable (turn)'}",
              f"SESSION TOKENS  {receipt.get('session_tokens') if receipt.get('session_tokens') is not None else 'unavailable'}",
              f"SESSION COST    {'$' + format(receipt['session_cost_usd'], '.4f') if receipt.get('session_cost_usd') is not None else 'unavailable'}",
              f"WALL TIME   {receipt['wall_seconds']}s", f"VERIFIED    {verified} / {len(claims)}",
              '-' * 38, 'ledger sha256 (join both lines)', receipt['hash'][:32], receipt['hash'][32:],
              f"hermes receipt verify {receipt['hash']}",
              '', 'THANK YOU FOR YOUR HONEST AGENT']
    return '\n'.join(lines)


def render_png(receipt: dict, path: Path) -> Path:
    """Save a narrow high-resolution PNG, colored evidence and a torn paper edge."""
    lines = render_text(receipt).splitlines()
    line_height = 29
    top = 40
    paper_height = max(520, top + line_height * len(lines) + 52)
    image = Image.new('RGB', (WIDTH + 72, paper_height + 40), '#272727')
    draw = ImageDraw.Draw(image)
    draw.rectangle((36, 14, WIDTH + 35, paper_height), fill=PAPER)
    for x in range(36, WIDTH + 36, 20):
        draw.polygon(((x, paper_height), (x + 10, paper_height + 13), (x + 20, paper_height)), fill=PAPER)
    normal, bold, header = _font(18), _font(19, True), _font(31, True)
    y = top
    for index, line in enumerate(lines):
        if index == 0:
            draw.text((WIDTH // 2 + 36, y), line, font=header, fill=INK, anchor='mt')
            y += 14
        elif index == 1 or line == 'CLAIMS':
            draw.text((WIDTH // 2 + 36, y), line, font=bold, fill=INK, anchor='mt')
        elif line.startswith(('✓ ', '✗ ')):
            draw.text((59, y), line[:57], font=normal, fill=GREEN if line.startswith('✓') else RED)
        elif line.startswith('not observed '):
            draw.text((59, y), line[:57], font=normal, fill=MUTED)
        elif line.startswith('hermes receipt verify'):
            draw.text((59, y), 'hermes receipt verify <joined hash>', font=normal, fill=MUTED)
        elif line.startswith('THANK YOU'):
            draw.text((WIDTH // 2 + 36, y), line, font=bold, fill=INK, anchor='mt')
        else:
            draw.text((59, y), line[:57], font=normal, fill=INK)
        y += line_height
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format='PNG', optimize=True)
    return path
