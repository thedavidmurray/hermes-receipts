"""Fields registry, kits, themes and the verdict-visibility invariant.

These are the tests the 2026-11-10 review asks for: every kit, a user kit, missing
data as ``n/a``, the theme invariant (a theme can never hide or soften a verdict),
and the verdict colour/glyph map being correct in *every* theme.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from v2 import fields
from v2.fields import (ASCII_GLYPHS, FIELDS, FIELDS_BY_NAME, KNOWN_FIELDS, MANDATORY_SECTIONS,
                       MIN_VERDICT_CONTRAST, OPTIONAL_SECTIONS, VERDICT_GLYPHS, VERDICT_WORDS,
                       KitError, ThemeError, auto_kit_fields, kit_fields, kit_names, kit_sections,
                       load_theme, public_metrics, render_field, field_view, theme_names,
                       validate_fields, validate_sections, verdict_ink, worst_verdict)
from v2.public import to_public_receipt, to_public_report, verify_receipt_hash
from v2.render import (LEFT, LINE_HEIGHT, TOP, Line, public_text, receipt_lines,
                       render_report_png, render_task_png, _line_color)

TARGET = 'a' * 64
VERSION = 'b' * 64
EVIDENCE = 'c' * 64


def _private(verdicts=('unverified',), *, required=True, completion=None, labelled=True):
    """A judge-shaped private receipt with one criterion per requested verdict."""
    requirements, findings, labels = [], {}, {}
    for index, verdict in enumerate(verdicts, 1):
        rid = f'r{index}'
        predicate = ('artifact_digest', 'remote_commit', 'tests_pass')[index % 3]
        requirements.append({'requirement_id': rid, 'public_label': f'/private/{rid}',
                             'required': required, 'evidence_predicate': predicate,
                             'target_key': TARGET, 'artifact_version': VERSION})
        findings[rid] = {'verdict': verdict,
                         'reason_code': ('required_outcome_readback' if verdict == 'supported'
                                         else 'no_applicable_readback'),
                         'evidence_ids': [EVIDENCE] if verdict == 'supported' else []}
        labels[rid] = ('Artifact bytes match', 'Commit present in remote',
                       'Tests pass for this version')[index % 3] if labelled else f'/private/{rid}'
    if completion is None:
        completion = 'supported' if verdicts and all(v == 'supported' for v in verdicts) else 'unverified'
    return {'task_key': 'canary-task', 'requirements': requirements, 'criterion_findings': findings,
            'completion': {'verdict': completion}, 'observation_start': 1e9, 'observation_end': 1e9 + 60,
            '_labels': labels}


def _public(verdicts=('unverified',), **kwargs):
    private = _private(verdicts, **{k: v for k, v in kwargs.items() if k in ('required', 'completion')})
    style = {k: kwargs[k] for k in ('kit', 'theme', 'metrics') if k in kwargs}
    return to_public_receipt(private, private['_labels'], **style)


def _verdict_lines(lines):
    return [line for line in lines if line.kind == 'verdict']


# ── registry ───────────────────────────────────────────────────────────────
def test_registry_declares_every_documented_field_with_a_data_source():
    documented = {'tokens_in', 'tokens_out', 'tokens_total', 'cost', 'tool_call_count', 'top_tools',
                  'duration', 'model', 'provider', 'profile', 'turns', 'files_touched',
                  'claims_detected', 'verdict_per_claim', 'evidence_locators', 'task_session_id',
                  'timestamp', 'session', 'agent', 'detector_version', 'matcher_version',
                  'plugin_version', 'git_sha', 'receipt_hash', 'artifact_version', 'verdict'}
    assert documented <= set(KNOWN_FIELDS)
    assert len(FIELDS) == len(FIELDS_BY_NAME) == len(KNOWN_FIELDS)  # names are unique
    for field in FIELDS:
        assert field.source and isinstance(field.source, str), field
        assert field.label and field.section
    # Every field's declared source is documentation a reader can act on.
    assert FIELDS_BY_NAME['tokens_in'].source == 'metrics.tokens_in'
    assert FIELDS_BY_NAME['verdict'].source == 'public.completion'


def test_missing_data_prints_n_a_and_never_a_guess():
    view = field_view(_public())
    empty = ('tokens_in', 'tokens_out', 'tokens_total', 'cost', 'tool_call_count', 'top_tools',
             'duration', 'model', 'provider', 'profile', 'turns', 'files_touched', 'session',
             'agent', 'cost_scope')
    for name in empty:
        assert render_field(name, view) == 'n/a', name
    # A field that exists but has no data is n/a too (agent is never invented).
    assert render_field('claims_detected', view) == '1 declared'  # derived from the receipt itself
    with pytest.raises(KitError):
        render_field('not_a_field', view)
    with pytest.raises(KitError):
        validate_fields(['tokens_in', 'nope'])


def test_metrics_present_in_the_receipt_are_rendered_not_na():
    metrics = {'tokens_in': 1200, 'tokens_out': 300, 'cost_usd': 0.5, 'tool_calls': 7,
               'duration_seconds': 12.5, 'model': 'gpt-test', 'provider': 'test-provider',
               'turns': 3, 'files_touched': 2, 'top_tools': [['terminal', 5]], 'session': 'local-deadbeef'}
    public = _public(metrics=metrics)
    text = public_text(public, kit='full')
    assert 'TOKENS IN: 1,200' in text and 'COST (EST.): $0.5000' in text
    assert 'DURATION: 12.5s' in text and 'TOP TOOLS: terminal x5' in text
    assert 'MODEL: gpt-test' in text
    # The old hardcoded "unavailable" line is gone; real data fills the field.
    assert 'unavailable for this task' not in text


# ── kits ───────────────────────────────────────────────────────────────────
def test_every_builtin_kit_keeps_the_verdict_block():
    assert set(kit_names()) >= {'minimal', 'verify', 'cost', 'ops', 'full', 'auto'}
    for name in kit_names():
        sections = kit_sections(name)
        assert set(MANDATORY_SECTIONS) <= set(sections), name
        assert set(sections) - set(MANDATORY_SECTIONS) <= set(OPTIONAL_SECTIONS), name
        assert all(field in FIELDS_BY_NAME for field in kit_fields(name)), name
        lines = receipt_lines(_public(kit=name), kit=name)
        assert any(line.text.startswith('COMPLETION:') for line in lines), name
        assert any(line.text.startswith('REQ 1:') for line in lines), name
    with pytest.raises(KitError):
        kit_sections('no-such-kit')
    with pytest.raises(KitError):
        kit_fields('no-such-kit')
    with pytest.raises(KitError):
        validate_sections(['evidence', 'hide_verdict'])
    with pytest.raises(KitError):
        validate_sections(['verdict'])  # mandatory: cannot be listed or removed


def test_kits_select_different_content():
    public = _public(('supported', 'unverified'), metrics={'tokens_total': 10})
    minimal = public_text(public, kit='minimal')
    verify = public_text(public, kit='verify')
    cost = public_text(public, kit='cost')
    assert 'EVIDENCE CHECKED' not in minimal and 'EVIDENCE CHECKED' in verify
    assert 'TOKENS TOTAL: 10' in cost and 'TOKENS TOTAL' not in verify
    # The minimal kit prints the verdict block plus exactly one field.
    assert 'TASK/SESSION ID' in minimal
    assert 'CLAIMS DETECTED' not in minimal and 'TOKENS' not in minimal
    # Every kit still prints both criterion verdicts and the completion verdict.
    for text in (minimal, verify, cost):
        assert text.count('UNVERIFIED') >= 1 and 'REQ 1:' in text and 'COMPLETION:' in text


def test_user_kit_is_selectable_by_name_from_the_config_dir(tmp_path):
    config = tmp_path / 'config'
    (config / 'kits').mkdir(parents=True)
    (config / 'kits' / 'spend.json').write_text(json.dumps({
        'name': 'spend', 'schema_version': 2, 'description': 'user kit',
        'sections': [], 'fields': ['tokens_total', 'cost']}))
    (config / 'themes').mkdir()
    (config / 'themes' / 'blue.json').write_text(json.dumps({
        'name': 'blue', 'schema_version': 2, 'mono': False, 'perforation': 'none',
        'paper': '#ffffff', 'ink': '#000000', 'ink_supported': '#0b5c33',
        'ink_contradicted': '#8c1d18', 'ink_unverified': '#7a5200',
        'ink_not_evaluable': '#444444', 'ink_muted': '#555555'}))
    fields.reload(config)
    try:
        assert 'spend' in kit_names() and 'blue' in theme_names()
        assert kit_sections('spend') == MANDATORY_SECTIONS
        public = _public(kit='spend', theme='blue', metrics={'tokens_total': 42})
        text = public_text(public, kit='spend', theme='blue')
        assert 'TOKENS TOTAL: 42' in text and 'EVIDENCE CHECKED' not in text
        png = render_task_png(public, tmp_path / 'user.png', kit='spend', theme='blue')
        with Image.open(png) as image:
            assert image.width == 652
    finally:
        fields.reload()  # drop the user dir again for the rest of the suite


def test_user_kit_cannot_ask_to_drop_the_verdict():
    with pytest.raises(KitError):
        validate_sections(['hide_verdict'])
    with pytest.raises(KitError):
        validate_fields(['verdict', 'nope'])


# ── themes ─────────────────────────────────────────────────────────────────
def test_theme_rejects_any_key_that_hides_or_softens_a_verdict(tmp_path):
    config = tmp_path / 'config'
    (config / 'themes').mkdir(parents=True)
    # Exactly the first-pass payload the review flagged: it must now be rejected.
    (config / 'themes' / 'sneaky.json').write_text(json.dumps({
        'name': 'sneaky', 'schema_version': 2, 'mono': False, 'hide_unsupported': True,
        'soften_contradicted': True, 'ink_supported': '#00ff00', 'ink_contradicted': '#ffffff',
        'ink_unverified': '#bbbbbb', 'ink_not_evaluable': '#666666', 'paper': '#121212',
        'ink': '#e0e0e0', 'perforation': 'none'}))
    fields.reload(config)
    try:
        with pytest.raises(ThemeError):
            load_theme('sneaky')
        assert 'sneaky' in theme_names()  # discoverable, but never renderable
        with pytest.raises(ThemeError):
            public_text(_public(), theme='sneaky')
    finally:
        fields.reload()
    for forbidden in sorted(fields.THEME_FORBIDDEN_KEYS):
        assert forbidden.startswith(('hide', 'soften', 'suppress', 'mute', 'verdict', 'glyph', 'relabel'))
    with pytest.raises(ThemeError):
        load_theme('no-such-theme')


def test_theme_rejects_a_second_ink_in_a_mono_theme_and_low_contrast_ink(tmp_path):
    config = tmp_path / 'config'
    (config / 'themes').mkdir(parents=True)
    (config / 'themes' / 'smuggler.json').write_text(json.dumps({
        'name': 'smuggler', 'mono': True, 'paper': '#ffffff', 'ink': '#111111',
        'ink_supported': '#00ff00', 'ink_contradicted': '#111111', 'ink_unverified': '#111111',
        'ink_not_evaluable': '#111111', 'ink_muted': '#555555'}))
    (config / 'themes' / 'washed.json').write_text(json.dumps({
        'name': 'washed', 'mono': False, 'paper': '#f4efdf', 'ink': '#222620',
        'ink_supported': '#eeee55', 'ink_contradicted': '#a73532', 'ink_unverified': '#7a5200',
        'ink_not_evaluable': '#666557', 'ink_muted': '#666557'}))
    fields.reload(config)
    try:
        with pytest.raises(ThemeError):
            load_theme('smuggler')
        with pytest.raises(ThemeError):
            load_theme('washed')
    finally:
        fields.reload()


def test_every_theme_renders_each_verdict_with_its_own_glyph_word_and_colour():
    public = _public(('supported', 'contradicted', 'unverified'), completion='contradicted')
    for name in theme_names():
        theme = load_theme(name)
        table = ASCII_GLYPHS if theme['glyph_set'] == 'ascii' else VERDICT_GLYPHS
        lines = receipt_lines(public, kit='verify', theme=name)
        by_verdict = {}
        for line in _verdict_lines(lines):
            by_verdict.setdefault(line.verdict, []).append(line)
        assert set(by_verdict) == {'supported', 'contradicted', 'unverified', 'not_evaluable'}, name
        for verdict, group in by_verdict.items():
            assert any(f'{VERDICT_WORDS[verdict]} {table[verdict]}' in line.text for line in group), name
        # Distinct ink per verdict, outside a declared monochrome theme.
        inks = {verdict: verdict_ink(theme, verdict) for verdict in VERDICT_WORDS}
        if theme['mono']:
            assert len(set(inks.values())) == 1, name
            assert all(fields.contrast_ratio(ink, theme['paper']) >= MIN_VERDICT_CONTRAST
                       for ink in inks.values()), name
        else:
            assert len(set(inks.values())) == 4, name


def test_unsupported_text_never_renders_green():
    """Colour comes from the verdict enum, never from a substring of the text."""
    for name in theme_names():
        theme = load_theme(name)
        supported = verdict_ink(theme, 'supported')
        for text in ('UNSUPPORTED', 'NOT SUPPORTED', 'SUPPORTED BUT UNVERIFIED',
                     'THIS LINE MENTIONS SUPPORTED', 'CONTRADICTED'):
            plain = Line(text, kind='plain')
            if theme['mono']:
                # A monochrome theme carries no green at all, so no text can look supported.
                assert supported == theme['ink'] == _line_color(plain, theme), (name, text)
            else:
                assert _line_color(plain, theme) != supported, (name, text)
        bad = Line(f'{VERDICT_WORDS["unverified"]} {VERDICT_GLYPHS["unverified"]}',
                   kind='verdict', verdict='unverified')
        if not theme['mono']:
            assert _line_color(bad, theme) != supported, name
    # The word "unsupported" inside narration is not a verdict and gets normal ink.
    public = _public(('unverified',))
    assert 'remote ref not found in this window' in ' '.join(public_text(public, kit='full').split())


def test_completion_is_green_only_when_every_required_criterion_is_supported():
    def completion_line(public):
        return next(line for line in receipt_lines(public, kit='verify')
                    if line.text.startswith('COMPLETION:'))

    all_supported = _public(('supported', 'supported'))
    assert completion_line(all_supported).verdict == 'supported'

    # Completion claimed supported while a required criterion is unverified.
    downgraded = dict(all_supported)
    downgraded['criteria'] = [dict(all_supported['criteria'][0]),
                             {**all_supported['criteria'][1], 'verdict': 'unverified'}]
    assert completion_line(downgraded).verdict == 'unverified'

    # The worst required verdict wins; a weaker criterion cannot upgrade a claim.
    contradicted = dict(all_supported)
    contradicted['criteria'] = [{**all_supported['criteria'][0], 'verdict': 'contradicted'},
                               all_supported['criteria'][1]]
    assert completion_line(contradicted).verdict == 'contradicted'

    no_upgrade = dict(all_supported)
    no_upgrade['completion'] = 'unverified'
    assert completion_line(no_upgrade).verdict == 'unverified'
    assert worst_verdict(['supported', 'supported', 'unverified']) == 'unverified'
    assert worst_verdict([]) == 'unverified'
    # Optional criteria never move the completion verdict.
    optional_only = _public(('contradicted',), required=False)
    assert completion_line(optional_only).verdict == 'unverified'


def test_public_serializer_refuses_a_supported_completion_without_bound_required_evidence():
    private = _private(('supported', 'unverified'), completion='supported')
    public = to_public_receipt(private, private['_labels'])
    assert public['completion'] == 'unverified'


def test_monochrome_theme_renders_contradicted_as_inverse_video(tmp_path):
    public = _public(('contradicted',), completion='contradicted')
    lines = receipt_lines(public, kit='verify', theme='minimal-mono')
    index = next(i for i, line in enumerate(lines)
                 if line.kind == 'verdict' and line.verdict == 'contradicted')
    mono = render_task_png(public, tmp_path / 'mono.png', kit='verify', theme='minimal-mono')
    colour = render_task_png(public, tmp_path / 'colour.png', kit='verify', theme='thermal-classic')
    band = (50, TOP + index * LINE_HEIGHT - 4, 615, TOP + index * LINE_HEIGHT + LINE_HEIGHT - 6)

    def dark_pixels(path):
        with Image.open(path) as image:
            raw = image.convert('RGB').crop(band).tobytes()
        return sum(1 for offset in range(0, len(raw), 3) if raw[offset] < 60)

    assert dark_pixels(mono) > 400, 'a contradicted verdict must be an inverse-video bar in a mono theme'
    assert dark_pixels(colour) < 400, 'a colour theme must not fill the row'


# ── rendering ──────────────────────────────────────────────────────────────
def test_every_kit_and_theme_renders_a_real_png_without_private_ids(tmp_path):
    private = _private(('unverified',))
    public = to_public_receipt(private, private['_labels'], metrics={'session': 'local-' + 'd' * 64,
                                                                    'profile': 'canary-profile'})
    digests = {}
    for kit in ('minimal', 'verify', 'cost', 'ops', 'full'):
        for theme in theme_names():
            path = render_task_png(public, tmp_path / f'{kit}-{theme}.png', kit=kit, theme=theme)
            assert path.is_file() and path.stat().st_size > 2000
            with Image.open(path) as image:
                image.load()
                assert image.width == 652 and not image.info
            raw = path.read_bytes().decode('latin1')
            for canary in ('canary-profile', 'local-dddd', '/private/', 'CANARY'):
                assert canary not in raw
            digests[(kit, theme)] = path.read_bytes()
    # The theme really is applied: two themes of the same kit differ in pixels.
    assert digests[('full', 'thermal-classic')] != digests[('full', 'dot-matrix')]
    assert digests[('full', 'ascii')] != digests[('full', 'bold-branded')]


def test_ascii_theme_is_seven_bit_and_unicode_theme_is_not():
    public = _public(('unverified',))
    assert public_text(public, kit='verify', theme='ascii').isascii()
    assert not public_text(public, kit='verify', theme='thermal-classic').isascii()


def test_report_counts_are_coloured_per_category(tmp_path):
    report = to_public_report(
        [{'profile': 'canary', 'action': {'supported': 3, 'contradicted': 1, 'unverified': 2},
          'task_completion': {'supported': 1}, 'done': {'unverified': 1},
          'unknown_model_claims': 1, 'unknown_source_claims': 0, 'tasks': 1}],
        [{'source_id': 's', 'availability': 'available', 'coverage_reason': 'complete'}],
        start=0, end=60, group_by=['profile'])
    for name in theme_names():
        lines = receipt_lines(report, theme=name)
        verdicts = {line.verdict for line in _verdict_lines(lines)}
        assert verdicts == {'supported', 'contradicted', 'unverified', 'not_evaluable'}, name
        assert any('CONTRADICTED' in line.text and '(1)' in line.text for line in lines), name
    text = public_text(report, theme='ascii')
    assert 'NOT A LIE RATE' in text and 'ACTION CLAIMS' in text
    png = render_report_png(report, tmp_path / 'report.png', theme='dot-matrix')
    with Image.open(png) as image:
        assert image.width == 652 and not image.info


def test_receipt_hash_is_verifiable_and_content_bound():
    public = _public(('supported',))
    assert verify_receipt_hash(public)
    tampered = dict(public)
    tampered['completion'] = 'contradicted'
    assert not verify_receipt_hash(tampered)
    assert public['receipt_hash'].startswith('sha256-')


def test_public_metrics_pseudonymise_run_identifiers():
    metrics = public_metrics({'session': 'local-' + 'e' * 64, 'profile': 'canary-profile',
                              'agent': 'canary-profile', 'model': 'm', 'tokens_in': 5})
    for key in ('session', 'profile', 'agent'):
        assert metrics[key].startswith('sha256-') and len(metrics[key]) == 19
    assert 'canary' not in json.dumps(metrics)
    assert metrics['model'] == 'm' and metrics['tokens_in'] == 5
    assert public_metrics({'tokens_in': -5, 'cost_usd': float('nan'), 'session': ''}) == {}


def test_auto_kit_consumes_the_reviewed_rules_file_and_drops_v1_only_fields():
    assert auto_kit_fields({}) == ['task_session_id']
    heavy = auto_kit_fields({'cost_heavy': True})
    assert {'tokens_in', 'tokens_out', 'tokens_total', 'cost'} <= set(heavy)
    ops = auto_kit_fields({'session_type': 'ops'})
    assert {'tool_call_count', 'top_tools', 'duration'} <= set(ops)
    # v1 rule fields with no v2 equivalent are dropped, never invented.
    assert not {'sources', 'artifacts', 'errors', 'push_check'} & set(auto_kit_fields(
        {'session_type': 'research'}))
    assert 'verdict' not in auto_kit_fields({'tests_run': True})
