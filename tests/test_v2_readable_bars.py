"""Regression for verifier's exact black-paper `invisible` theme receipt."""
from io import BytesIO

import pytest
from PIL import Image, ImageColor

from v2 import fields, render


@pytest.fixture(autouse=True)
def isolated_themes(monkeypatch):
    # The profile-scoped asset view is read-only; mutate only a per-test snapshot.
    monkeypatch.setattr(fields, 'THEMES', dict(fields.THEMES.items()))


def _public(verdict='unverified'):
    return {'kind': 'task', 'completion': verdict,
            'criteria': [{'required': True, 'label': 'Criterion 1', 'verdict': verdict}]}


def _dark():
    return {**fields.THEMES['thermal-classic'], 'paper': '#000000', 'ink': '#ffffff',
            'mono': True, 'green_bar': True,
            **{'ink_' + verdict: '#ffffff' for verdict in fields.VERDICT_WORDS}}


def _image(lines, theme):
    output = BytesIO()
    render._draw(lines, theme, output)
    output.seek(0)
    return Image.open(output).convert('RGB')


def _hex(pixel):
    return '#%02x%02x%02x' % pixel


def test_every_builtin_and_dark_theme_has_readable_band_pixel_rows(monkeypatch):
    monkeypatch.setitem(fields.THEMES, 'invisible', _dark())
    for name in fields.theme_names():
        theme = fields.load_theme(name)
        # Cover both parities for every semantic ink and ordinary/muted text.
        lines = [render.Line('ordinary'), render.Line('note', kind='note')]
        for verdict in fields.VERDICT_WORDS:
            lines.extend([render.Line(fields.verdict_word(verdict), kind='verdict', verdict=verdict)] * 2)
        image = _image(lines, theme)
        paper = ImageColor.getrgb(theme['paper'])
        bands = set()
        for y in range(render.TOP - 8, render.TOP - 8 + len(lines) * render.LINE_HEIGHT):
            background = image.getpixel((37, y))  # paper margin, not text/perforations/inverse
            if background != paper:
                bands.add(background)
                for ink in {theme['ink'], theme['ink_muted'],
                            *(fields.verdict_ink(theme, v) for v in fields.VERDICT_WORDS)}:
                    assert fields.contrast_ratio(ink, _hex(background)) >= 4.5, (name, y, ink)
        if name == 'dot-matrix':
            assert bands, 'safe light-paper bands must still render'
        if name == 'invisible':
            assert not bands, 'dark paper must not carry green-bar stripes'


def test_zero_contradicted_receipt_has_no_inverse_rectangle(monkeypatch):
    monkeypatch.setitem(fields.THEMES, 'invisible', _dark())
    theme = fields.load_theme('invisible')
    lines = render.receipt_lines(_public(), theme='invisible')
    image = _image(lines, theme)
    assert any(line.text == 'CONTRADICTED ✗: 0' for line in lines)
    # This column lies inside inverse rectangles but outside every glyph.
    assert all(image.getpixel((render.LEFT - 7, y)) == (0, 0, 0)
               for y in range(render.TOP - 8, render.TOP + len(lines) * render.LINE_HEIGHT))


@pytest.mark.parametrize('kind', ['task', 'report'])
@pytest.mark.parametrize('count', [0, 1, 12])
def test_count_metadata_controls_inverse_for_task_and_report(kind, count):
    theme = fields.load_theme('minimal-mono')
    if kind == 'task':
        public = _public('contradicted')
        public['criteria'] = public['criteria'] * count + _public()['criteria']
        lines = render._counts_section(public, theme)
    else:
        public = {'groups': [{'done': {'contradicted': count}}]}
        lines = render._report_lines(public, [], theme)
    contradicted = [line for line in lines if line.verdict == 'contradicted']
    assert contradicted
    for line in contradicted:
        assert line.count == (count if kind == 'task' or line.text.startswith('TASK COMPLETION:') else 0)
        image = _image([line], theme)
        # The report fixture only populates done, not required/tools.
        assert line.count is not None
        expected_inverse = line.count > 0
        expected = ImageColor.getrgb(theme['ink'] if expected_inverse else theme['paper'])
        assert image.getpixel((render.LEFT - 7, render.TOP)) == expected


def test_contradicted_claim_still_has_inverse_video():
    theme = fields.load_theme('minimal-mono')
    line = render.Line('  CONTRADICTED ✗', kind='verdict', verdict='contradicted')
    image = _image([line], theme)
    assert image.getpixel((render.LEFT - 7, render.TOP)) == ImageColor.getrgb(theme['ink'])


def test_bar_default_is_six_percent_toward_ink_and_explicit_is_supported(monkeypatch):
    raw = dict(fields.THEMES['dot-matrix'])
    monkeypatch.setitem(fields.THEMES, 'custom-bar', raw)
    theme = fields.load_theme('custom-bar')
    paper, ink = ImageColor.getrgb(theme['paper']), ImageColor.getrgb(theme['ink'])
    expected = tuple(round(p * .94 + i * .06) for p, i in zip(paper, ink))
    assert ImageColor.getrgb(theme['bar']) == expected
    raw['bar'] = '#eeeeee'
    theme = fields.load_theme('custom-bar')
    image = _image([render.Line('first'), render.Line('second')], theme)
    assert image.getpixel((37, render.TOP + render.LINE_HEIGHT)) == (238, 238, 238)


@pytest.mark.parametrize('bar', ['#000000', '#55604f'])
def test_unsafe_bar_is_skipped_not_recoloured(monkeypatch, bar):
    monkeypatch.setitem(fields.THEMES, 'unsafe-bar', {**fields.THEMES['dot-matrix'], 'bar': bar})
    theme = fields.load_theme('unsafe-bar')
    image = _image([render.Line('first'), render.Line('second')], theme)
    assert image.getpixel((37, render.TOP + render.LINE_HEIGHT)) == ImageColor.getrgb(theme['paper'])


@pytest.mark.parametrize('bar', [None, 42, '#fff', '#eeeeee\n', 'mint'])
def test_malformed_bar_is_rejected(monkeypatch, bar):
    monkeypatch.setitem(fields.THEMES, 'bad-bar', {**fields.THEMES['dot-matrix'], 'bar': bar})
    with pytest.raises(fields.ThemeError, match='bar'):
        fields.load_theme('bad-bar')


@pytest.mark.parametrize('kind', ['task', 'report'])
def test_count_survives_ascii_folding_and_narrow_wrapping(monkeypatch, kind):
    monkeypatch.setitem(fields.THEMES, 'ascii-mono',
                        {**fields.THEMES['minimal-mono'], 'glyph_set': 'ascii'})
    from v2.config import parse_config
    config = parse_config({'columns': 24})
    public = _public() if kind == 'task' else {'kind': 'report', 'groups': [{'done': {}}]}
    lines = render.receipt_lines(public, theme='ascii-mono', config=config)
    counts = [line for line in lines if line.verdict == 'contradicted']
    assert counts and all(line.count == 0 for line in counts)
    if kind == 'report':
        assert len(counts) > 3  # each of three count rows wrapped at 24 columns
    image = _image(counts, fields.load_theme('ascii-mono'))
    expected = ImageColor.getrgb(fields.load_theme('ascii-mono')['paper'])
    assert all(image.getpixel((render.LEFT - 7, y)) == expected
               for y in range(render.TOP - 8, render.TOP + len(counts) * render.LINE_HEIGHT))


@pytest.mark.parametrize('bar', [None, '#eeeeee', '#000000'])
def test_dark_paper_skips_bands_even_with_explicit_override(monkeypatch, bar):
    raw = _dark()
    if bar is not None:
        raw['bar'] = bar
    monkeypatch.setitem(fields.THEMES, 'dark-bar', raw)
    image = _image([render.Line('a'), render.Line('b')], fields.load_theme('dark-bar'))
    assert image.getpixel((37, render.TOP + render.LINE_HEIGHT)) == (0, 0, 0)


def test_bar_contrast_includes_muted_barcode_fallback(monkeypatch):
    monkeypatch.setitem(fields.THEMES, 'muted-barcode',
                        {**fields.THEMES['dot-matrix'], 'ink_muted': '#eeeeee'})
    theme = fields.load_theme('muted-barcode')
    lines = [render.Line('header'),
             render.Line('long payload', kind='barcode', payload='https://example.test/' * 50)]
    image = _image(lines, theme)
    assert image.getpixel((37, render.TOP + render.LINE_HEIGHT)) == ImageColor.getrgb(theme['paper'])


def test_theme_only_changes_appearance_not_verdict_mapping(monkeypatch):
    raw = dict(fields.THEMES['dot-matrix'])
    monkeypatch.setitem(fields.THEMES, 'alternate-bar', {**raw, 'bar': '#eeeeee'})
    public = _public('contradicted')
    original = render.receipt_lines(public, theme='dot-matrix')
    alternate = render.receipt_lines(public, theme='alternate-bar')
    assert original == alternate
    for verdict in fields.VERDICT_WORDS:
        assert fields.verdict_ink(fields.load_theme('dot-matrix'), verdict) == fields.verdict_ink(
            fields.load_theme('alternate-bar'), verdict)
