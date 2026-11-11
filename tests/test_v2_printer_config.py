"""Lane: the user customizes the printer (receipts.yaml) — never the verdict.

Covers the config schema and its key-naming errors, every section toggle, both paper
widths, a custom field template, a rejected malicious theme, the immutability of the
verdict block, the printer options, and that the full kit prints the whole run.
"""
import argparse
import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('receipt_printer_package', ROOT / '__init__.py',
                                              submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)

from receipt_printer_package.v2 import fields
from receipt_printer_package.v2.config import (EXAMPLE_CONFIG, PAPERS, ConfigError, init_config,
                                               load_config, parse_config)
from receipt_printer_package.v2.contracts import claim_key
from receipt_printer_package.v2.evidence import observe_artifact
from receipt_printer_package.v2.public import to_public_receipt
from receipt_printer_package.v2.render import public_text, render_task_png
from receipt_printer_package.v2.tasks import judge_task

SCOPE = {'source_id': 'cfg-source', 'profile': 'kilo', 'session_id': 'local-abc',
         'user_message_id': '1', 'final_message_id': '2'}
TARGET = 'a' * 64


@pytest.fixture(autouse=True)
def _isolated_config_dir(tmp_path, monkeypatch):
    """Every test gets its own config dir, so no user file leaks in and no test leaks out."""
    monkeypatch.setenv('HERMES_RECEIPTS_CONFIG_DIR', str(tmp_path / 'cfg'))
    fields.reload()
    yield
    monkeypatch.delenv('HERMES_RECEIPTS_CONFIG_DIR', raising=False)
    fields.reload()


def _public(tmp_path, metrics=None):
    """A real, honestly-judged public receipt: one genuinely supported criterion."""
    artifact = tmp_path / 'artifact.bin'
    artifact.write_bytes(b'the printer config lane artifact')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    now = time.time()
    requirements = [{'requirement_id': 'artifact', 'public_label': 'Artifact bytes match',
                     'required': True, 'evidence_predicate': 'artifact_digest',
                     'target_key': TARGET, 'artifact_version': digest}]
    claim = {'claim_id': claim_key(SCOPE, 'done', 0, 4, 'deterministic-2.0'), 'scope': SCOPE,
             'kind': 'done', 'task_key': 'printer-lane', 'target_key': TARGET}
    evidence = observe_artifact(artifact, digest, SCOPE, TARGET, digest, observed_at=now)
    private = judge_task(requirements, [evidence], (now - 60, now + 60), claim=claim)
    return to_public_receipt(private, {'artifact': 'Artifact bytes match'}, metrics=metrics)


def _lines_with(text, prefix):
    return [line for line in text.splitlines() if line.startswith(prefix)]


# ── the config schema names the bad key ────────────────────────────────────
def test_unknown_top_level_key_is_named():
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'schema_version': 2, 'pappr': '58mm'}, 'receipts.yaml')
    assert 'receipts.yaml' in str(excinfo.value) and 'pappr' in str(excinfo.value)


def test_unknown_paper_preset_is_named_with_alternatives():
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'paper': '99mm'}, 'receipts.yaml')
    message = str(excinfo.value)
    assert 'paper' in message and '99mm' in message and '58mm' in message


def test_custom_columns_require_custom_paper_and_are_bounded():
    assert parse_config({'paper': 'custom', 'columns': 40}).columns == 40
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'paper': '58mm', 'columns': 40}, 'receipts.yaml')
    assert 'columns' in str(excinfo.value)
    with pytest.raises(ConfigError):
        parse_config({'paper': 'custom', 'columns': 1000})
    with pytest.raises(ConfigError):
        parse_config({'paper': 'custom'})  # columns is required for a custom paper


def test_unknown_schema_version_and_kit_and_theme_are_named():
    with pytest.raises(ConfigError):
        parse_config({'schema_version': 1})
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'kit': 'nope'})
    assert 'kit' in str(excinfo.value)
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'theme': 'nope'})
    assert 'theme' in str(excinfo.value)


# ── custom fine-print fields ────────────────────────────────────────────────
def test_extra_field_needs_exactly_one_of_template_or_value():
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'extra_fields': [{'key': 'a', 'label': 'A', 'template': '{turns}', 'value': 'x'}]})
    assert 'exactly one' in str(excinfo.value)
    with pytest.raises(ConfigError):
        parse_config({'extra_fields': [{'key': 'a', 'label': 'A'}]})


def test_extra_field_rejects_unknown_placeholder_and_bad_spec():
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'extra_fields': [{'key': 'a', 'label': 'A', 'template': '{nope}'}]})
    assert 'unknown placeholder' in str(excinfo.value)
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'extra_fields': [{'key': 'a', 'label': 'A', 'template': '{model:.2f}'}]})
    assert 'format spec' in str(excinfo.value)


def test_duplicate_extra_field_keys_rejected():
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'extra_fields': [{'key': 'a', 'label': 'A', 'value': 'x'},
                                       {'key': 'a', 'label': 'B', 'value': 'y'}]})
    assert 'duplicate' in str(excinfo.value)


def test_custom_field_template_renders_and_missing_prints_na(tmp_path):
    public = _public(tmp_path, metrics={'tokens_total': 12345, 'cost_usd': 0.1234, 'turns': 3})
    config = parse_config({'extra_fields': [
        {'key': 'spend', 'label': 'SPEND', 'template': '{tokens_total:,.0f} tok - {cost_usd:.4f}'},
        {'key': 'terms', 'label': 'TERMS', 'value': 'No refunds on agent promises.'},
        {'key': 'gpu', 'label': 'GPU HOURS', 'template': '{files_touched} files'},
    ]})
    text = public_text(public, config=config)
    assert 'SPEND: 12,345 tok - 0.1234' in text
    assert 'TERMS: No refunds on agent promises.' in text
    # files_touched is not in the metrics, so the field prints n/a rather than a guess.
    assert 'GPU HOURS: n/a' in text


def test_numeric_field_name_takes_a_spec_and_bare_name_uses_the_field():
    public = {'kind': 'task', 'completion': 'supported', 'criteria': [],
              'metrics': {'cost_usd': 0.5, 'tool_calls': 9}}
    config = parse_config({'extra_fields': [
        {'key': 's', 'label': 'SPEND', 'template': '{cost:.2f} and {cost}'},
        {'key': 't', 'label': 'T', 'template': '{tool_call_count} calls'},
    ]})
    text = public_text(public, config=config)
    assert 'SPEND: 0.50 and $0.5000' in text
    assert 'T: 9 calls' in text


# ── every section toggle ────────────────────────────────────────────────────
def test_every_section_toggles_off_and_on(tmp_path):
    public = _public(tmp_path, metrics={'turns': 2})
    baseline = public_text(public)
    assert 'DECLARED CLAIMS' in baseline and 'EVIDENCE CHECKED' in baseline

    off = public_text(public, config=parse_config({'sections': {
        'header': False, 'claims': False, 'evidence': False, 'fine_print': False, 'footer': False}}))
    for marker in ('FINE PRINT', 'DECLARED CLAIMS', 'EVIDENCE CHECKED', 'MURRAY'):
        assert marker not in off
    # The verdict *and* the criteria always print, even when every optional section is off.
    assert 'COMPLETION: SUPPORTED' in off and 'Artifact bytes match' in off

    only_evidence = public_text(public, config=parse_config({'sections': {
        'claims': False, 'evidence': True}}))
    assert 'EVIDENCE CHECKED' in only_evidence and 'DECLARED CLAIMS' not in only_evidence


def test_mandatory_sections_cannot_be_switched_off_or_dropped_from_order(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'sections': {'verdict': False}})
    assert 'mandatory' in str(excinfo.value)
    with pytest.raises(ConfigError):
        parse_config({'sections': {'criteria': False}})
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'order': ['header', 'claims', 'fine_print']})
    assert "order" in str(excinfo.value)


def test_section_order_is_respected(tmp_path):
    public = _public(tmp_path, metrics={'turns': 2})
    config = parse_config({'sections': {'claims': False, 'evidence': False, 'fine_print': False},
                           'order': ['verdict', 'criteria', 'header', 'footer']})
    text = public_text(public, config=config)
    assert text.index('COMPLETION:') < text.index('REQUIRED CRITERIA') < text.index('Artifact bytes match')
    assert 'EVIDENCE CHECKED' not in text and 'FINE PRINT' not in text
    # An order that leaves an enabled section out is refused: nothing prints silently less.
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'order': ['header', 'verdict', 'criteria', 'footer']})
    assert 'missing from the order' in str(excinfo.value)
    assert 'claims' in str(excinfo.value)


# ── paper widths ────────────────────────────────────────────────────────────
def _field_lines(text, columns):
    return [line for line in text.splitlines() if line.startswith('LONG:') or
            (line.startswith('  ') and not line.startswith('  SUPPORTED') and line.strip())]


def test_both_paper_widths_wrap_the_same_field_differently(tmp_path):
    public = _public(tmp_path, metrics={'turns': 2})
    value = 'word ' * 20
    config58 = parse_config({'paper': '58mm', 'sections': {'claims': False, 'evidence': False},
                             'extra_fields': [{'key': 'long', 'label': 'LONG', 'value': value.strip()}]})
    config80 = config58.with_paper('80mm')
    text58 = public_text(public, config=config58)
    text80 = public_text(public, config=config80)
    assert config58.columns == 32 and config80.columns == 48
    lines58 = [line for line in text58.splitlines() if line.startswith(('LONG:', '  '))]
    lines80 = [line for line in text80.splitlines() if line.startswith(('LONG:', '  '))]
    assert len(lines58) > len(lines80)
    assert all(len(line) <= 32 for line in lines58)
    assert all(len(line) <= 48 for line in lines80)


def test_custom_column_count_wraps_and_resizes_the_png(tmp_path):
    public = _public(tmp_path, metrics={'turns': 2})
    narrow = parse_config({'paper': 'custom', 'columns': 24})
    assert narrow.columns == 24
    png58 = render_task_png(public, tmp_path / 'a.png', config=parse_config({'paper': '58mm'}))
    png80 = render_task_png(public, tmp_path / 'b.png', config=parse_config({'paper': '80mm'}))
    with Image.open(png58) as a, Image.open(png80) as b:
        assert a.width < b.width
    longest = max(len(line) for line in public_text(public, config=narrow).splitlines()
                  if not line.startswith('---'))
    assert longest <= 24


def test_paper_presets_match_the_documented_column_counts():
    assert PAPERS['58mm']['columns'] == 32 and PAPERS['80mm']['columns'] == 48
    assert PAPERS['a4']['columns'] == 80 and PAPERS['letter']['columns'] == 80
    assert PAPERS['custom']['columns'] is None


# ── malicious themes ────────────────────────────────────────────────────────
def _write_theme(tmp_path, name, payload):
    themes = Path(fields.config_dir()) / 'themes'
    themes.mkdir(parents=True, exist_ok=True)
    (themes / f'{name}.json').write_text(json.dumps({'name': name, **payload}))
    fields.reload()
    return name


def test_theme_that_hides_a_verdict_is_rejected(tmp_path):
    name = _write_theme(tmp_path, 'sneaky', {'hide_verdict': True, 'paper': '#ffffff', 'ink': '#000000'})
    with pytest.raises(fields.ThemeError) as excinfo:
        fields.load_theme(name)
    assert 'hide_verdict' in str(excinfo.value)
    name = _write_theme(tmp_path, 'sneaky2', {'remap_supported_to': 'unverified'})
    with pytest.raises(fields.ThemeError) as excinfo:
        fields.load_theme(name)
    assert 'remap_supported_to' in str(excinfo.value)


def test_theme_with_low_verdict_contrast_is_rejected(tmp_path):
    name = _write_theme(tmp_path, 'washed', {'paper': '#ffffff', 'ink': '#000000',
                                             'ink_supported': '#f8f8f8'})
    with pytest.raises(fields.ThemeError) as excinfo:
        fields.load_theme(name)
    assert 'contrast' in str(excinfo.value).lower() or 'below' in str(excinfo.value)


def test_theme_unknown_key_and_bounds_are_rejected(tmp_path):
    name = _write_theme(tmp_path, 'typo', {'paper_colour': '#ffffff'})
    with pytest.raises(fields.ThemeError) as excinfo:
        fields.load_theme(name)
    assert 'paper_colour' in str(excinfo.value)
    name = _write_theme(tmp_path, 'tiny', {'font_size': 2})
    with pytest.raises(fields.ThemeError):
        fields.load_theme(name)


def test_user_theme_is_selectable_from_the_config_dir(tmp_path):
    name = _write_theme(tmp_path, 'shop-hand', {'paper': '#fbf7ef', 'ink': '#282620',
                                                'edge_style': 'perforated', 'font': 'dot',
                                                'font_size': 17, 'line_spacing': 1.1})
    theme = fields.load_theme(name)
    assert theme['perforation'] == 'perforated' and theme['font_size'] == 17
    assert name in fields.theme_names()


# ── the verdict block is not configurable ───────────────────────────────────
def test_no_config_can_remove_or_fake_the_verdict(tmp_path):
    public = _public(tmp_path, metrics={'turns': 1})
    # A user label may not impersonate a verdict line.
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'extra_fields': [{'key': 'v', 'label': 'COMPLETION', 'value': 'SUPPORTED'}]})
    assert 'verdict' in str(excinfo.value).lower()
    with pytest.raises(ConfigError):
        parse_config({'header': {'shop_name': 'SUPPORTED RECEIPTS'}})
    with pytest.raises(ConfigError):
        parse_config({'footer': {'thanks': 'CONTRADICTED, COME AGAIN'}})
    # The verdict line count and text are identical with and without a config.
    plain = [line for line in public_text(public).splitlines() if line.startswith('COMPLETION:')]
    styled = [line for line in public_text(public, config=parse_config(
        {'header': {'shop_name': 'MURRAY & SONS'}, 'footer': {'thanks': 'THANK YOU'},
         'extra_fields': [{'key': 't', 'label': 'T', 'value': 'x'}], 'paper': '58mm'})).splitlines()
        if line.startswith('COMPLETION:')]
    assert plain == styled == [f'COMPLETION: SUPPORTED {fields.VERDICT_GLYPHS["supported"]}']


def test_a_config_cannot_downgrade_or_upgrade_a_contradicted_verdict(tmp_path):
    artifact = tmp_path / 'a.bin'
    artifact.write_bytes(b'bytes')
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    now = time.time()
    requirements = [{'requirement_id': 'r', 'public_label': 'Artifact bytes match', 'required': True,
                     'evidence_predicate': 'artifact_digest', 'target_key': TARGET,
                     'artifact_version': digest}]
    claim = {'claim_id': claim_key(SCOPE, 'done', 0, 4, 'deterministic-2.0'), 'scope': SCOPE,
             'kind': 'done', 'task_key': 't', 'target_key': TARGET}
    # No evidence observed -> unverified, and no config can dress it up as supported.
    public = to_public_receipt(judge_task(requirements, [], (now - 60, now + 60), claim=claim), {})
    config = parse_config({'theme': 'thermal-classic', 'header': {'shop_name': 'SHOP'},
                           'sections': {'claims': False}})
    assert 'COMPLETION: UNVERIFIED' in public_text(public, config=config)


# ── printer options ─────────────────────────────────────────────────────────
def test_printer_options_are_parsed_and_bounded():
    options = parse_config({'printer': {'cut': False, 'feed_lines': 3, 'density': 8, 'copies': 2}}).printer
    assert options == {'cut': False, 'feed_lines': 3, 'density': 8, 'copies': 2}
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'printer': {'density': 99}})
    assert 'printer.density' in str(excinfo.value)
    with pytest.raises(ConfigError):
        parse_config({'printer': {'copies': 0}})
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'printer': {'colour': True}})
    assert 'printer.colour' in str(excinfo.value)


def test_density_command_bytes_and_bounds():
    from receipt_printer_package.printer import density_command
    assert density_command(8) == b'\x12\x23\x83'
    assert density_command(0, break_time=0) == b'\x12\x23\x00'
    with pytest.raises(ValueError):
        density_command(16)
    with pytest.raises(ValueError):
        density_command(True)
    with pytest.raises(ValueError):
        density_command(4, break_time=9)


def test_print_receipt_validates_before_touching_the_device():
    from receipt_printer_package.printer import print_receipt
    with pytest.raises(ValueError):
        print_receipt('body', 0x1, 0x2, copies=0)
    with pytest.raises(ValueError):
        print_receipt('body', 0x1, 0x2, feed_lines=99)
    with pytest.raises(ValueError):
        print_receipt('body', 0x1, 0x2, density=42)


# ── branding: logo and barcode ──────────────────────────────────────────────
def test_barcode_hash_and_url_payloads():
    public = {'receipt_hash': 'sha256-0123456789abcdef0123'}
    assert parse_config({'barcode': 'hash'}).barcode_payload(public) == 'sha256-0123456789ab'
    url = parse_config({'barcode': 'url',
                        'barcode_url': 'https://example.invalid/r/{hash_prefix}'}).barcode_payload(public)
    assert url == 'https://example.invalid/r/sha256-0123456789ab'
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'barcode': 'url'})
    assert 'barcode_url' in str(excinfo.value)


def test_qr_is_offered_only_when_a_provider_is_installed():
    from receipt_printer_package.v2 import branding
    provider = branding.qr_provider()
    if provider is None:
        with pytest.raises(ConfigError) as excinfo:
            parse_config({'barcode': 'qr'})
        assert 'qr' in str(excinfo.value).lower()
        assert branding.qr_matrix('x') is None  # never fabricate a matrix
    else:  # pragma: no cover - only on a host that already has qrcode/segno
        assert parse_config({'barcode': 'qr'}).barcode == 'qr'
        assert branding.qr_matrix('hello')


def test_code128_is_pure_python_and_geometry_fits():
    from receipt_printer_package.v2 import branding
    modules = branding.barcode_modules('sha256-0123456789ab')
    assert set(modules) <= {'0', '1'} and len(modules) > 20
    fit = branding.barcode_geometry('sha256-0123456789ab', 400)
    assert fit['fits'] and fit['width_px'] <= 400
    assert branding.barcode_geometry('sha256-0123456789ab', 5)['fits'] is False


def test_logo_is_dithered_to_one_bit(tmp_path):
    from receipt_printer_package.v2 import branding
    logo = tmp_path / 'logo.png'
    Image.new('RGB', (200, 120), '#204020').save(logo)
    mask = branding.logo_mask(logo, 300, 96)
    assert mask.mode == 'L' and max(mask.size) <= 300
    dither = branding.dithered_logo(logo, 300, 96)
    assert dither.mode == '1'
    with pytest.raises(branding.BrandingError):
        branding.dithered_logo(tmp_path / 'missing.png', 100, 100)


def test_config_logo_must_exist(tmp_path):
    with pytest.raises(ConfigError) as excinfo:
        parse_config({'logo': str(tmp_path / 'nope.png')})
    assert 'logo' in str(excinfo.value)


def test_config_logo_resolves_next_to_the_config_file(tmp_path):
    Image.new('RGB', (80, 40), '#204020').save(tmp_path / 'logo.png')
    (tmp_path / 'receipts.yaml').write_text('logo: logo.png\n')
    config = load_config(tmp_path / 'receipts.yaml')
    assert config.logo == str(tmp_path / 'logo.png')


def test_barcode_line_prints_in_png_and_text(tmp_path):
    public = _public(tmp_path, metrics={'turns': 1})
    config = parse_config({'paper': '58mm', 'barcode': 'hash'})
    text = public_text(public, config=config)
    assert f'VERIFY: {public["receipt_hash"][:19]}' in text
    render_task_png(public, tmp_path / 'bar.png', config=config)
    assert (tmp_path / 'bar.png').stat().st_size > 0


# ── load_config / init ──────────────────────────────────────────────────────
def test_init_writes_the_commented_example_and_refuses_to_clobber(tmp_path):
    target = init_config(tmp_path / 'receipts.yaml')
    assert target.read_text() == EXAMPLE_CONFIG
    assert 'printer:' in target.read_text() and 'paper:' in target.read_text()
    with pytest.raises(ConfigError):
        init_config(target)
    init_config(target, force=True)  # --force overwrites


def test_load_config_from_file_and_from_the_config_dir(tmp_path):
    explicit = tmp_path / 'mine.yaml'
    explicit.write_text('paper: 80mm\nheader:\n  shop_name: "CUSTOM SHOP"\n')
    config = load_config(explicit)
    assert config.columns == 48 and config.header['shop_name'] == 'CUSTOM SHOP'
    assert config.source == 'mine.yaml'

    default = Path(fields.config_dir()) / 'receipts.yaml'
    default.parent.mkdir(parents=True, exist_ok=True)
    default.write_text('paper: 58mm\n')
    assert load_config().columns == 32
    # A CLI paper override wins over the file.
    assert load_config(paper='80mm').columns == 48
    with pytest.raises(ConfigError):
        load_config(tmp_path / 'absent.yaml')


def test_load_config_returns_none_without_a_file():
    assert load_config(use_default=True) is None
    # A paper flag alone still yields a config (no file needed).
    assert load_config(paper='58mm', use_default=True).columns == 32


def test_cli_config_init_and_show(tmp_path, capsys):
    parser = argparse.ArgumentParser(prog='hermes receipt')
    plugin._cli_setup(parser)
    dest = tmp_path / 'receipts.yaml'
    args = parser.parse_args(['config', '--init', '--path', str(dest)])
    assert plugin._cli_handler(args) == 0
    assert dest.is_file()
    capsys.readouterr()

    dest.write_text('paper: 58mm\nheader:\n  shop_name: "MURRAY & SONS"\n')
    args = parser.parse_args(['config', '--show', '--path', str(dest)])
    assert plugin._cli_handler(args) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown['columns'] == 32 and shown['header']['shop_name'] == 'MURRAY & SONS'
    assert shown['sections']['verdict'] is True


def test_cli_options_still_lists_kits_and_themes(capsys):
    parser = argparse.ArgumentParser(prog='hermes receipt')
    plugin._cli_setup(parser)
    args = parser.parse_args(['options'])
    assert plugin._cli_handler(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert 'full' in payload['kits'] and 'thermal-classic' in payload['themes']


# ── the full kit prints the whole run ───────────────────────────────────────
def test_full_kit_prints_every_run_fact_pseudonymised(tmp_path):
    metrics = {'profile': 'kilo', 'agent': 'kilo', 'model': 'deepseek/deepseek-v4.1-flash',
               'provider': 'custom:runtime-nous', 'turns': 30, 'tool_calls': 46,
               'tokens_in': 78036, 'tokens_out': 18745, 'tokens_total': 96781,
               'cost_usd': 0.026165505, 'duration_seconds': 363.7, 'session': 'local-abc'}
    public = _public(tmp_path, metrics=metrics)
    text = public_text(public, kit='full')
    for label in ('PROFILE:', 'AGENT:', 'MODEL:', 'PROVIDER:', 'TURNS:', 'TOOL CALLS:',
                  'TOKENS IN:', 'TOKENS OUT:', 'TOKENS TOTAL:', 'COST (EST.):', 'DURATION:'):
        assert label in text, label
    assert 'custom:runtime-nous' in text and 'deepseek/deepseek-v4.1-flash' in text
    assert 'TOKENS TOTAL: 96,781' in text and 'COST (EST.): $0.0262' in text
    assert 'TURNS: 30' in text and 'TOOL CALLS: 46' in text
    assert 'DURATION:' in text and '363' in text
    # Raw run identifiers never print: they are pseudonymised before they enter the receipt.
    assert 'local-abc' not in text
    assert 'sha256-' in text
