"""YAML backend compatibility, including Hermes's no-PyYAML runtime."""
import builtins
import sys
import types
import pytest

from v2.config import ConfigError, _read_mapping, load_config


@pytest.mark.parametrize('document', ['paper: [\n', 'paper: 58mm\npaper: 80mm\n'])
def test_hermes_yaml_errors_are_config_errors(tmp_path, document):
    pytest.importorskip('hermes_yaml')
    path = tmp_path / 'receipts.yaml'
    path.write_text(document, encoding='utf-8')
    with pytest.raises(ConfigError, match=r'^receipts.yaml: invalid YAML: .+'):
        load_config(path)


def test_hermes_yaml_is_preferred_and_uses_yaml_11(tmp_path, monkeypatch):
    pytest.importorskip('hermes_yaml')
    real_import = builtins.__import__

    def without_pyyaml(name, *args, **kwargs):
        if name == 'yaml':
            raise AssertionError('Hermes must not need PyYAML')
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_pyyaml)
    path = tmp_path / 'receipts.yaml'
    path.write_text('enabled: y\ndisabled: n\n', encoding='utf-8')
    assert _read_mapping(path) == {'enabled': True, 'disabled': False}


def test_yaml_without_either_backend_has_json_guidance(tmp_path, monkeypatch):
    real_import = builtins.__import__

    def without_yaml(name, *args, **kwargs):
        if name in ('hermes_yaml', 'yaml'):
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_yaml)
    path = tmp_path / 'receipts.yaml'
    path.write_text('paper: 58mm\n', encoding='utf-8')
    with pytest.raises(ConfigError, match=r'receipts.yaml: reading YAML needs .*receipts.json'):
        load_config(path)
    json_path = tmp_path / 'receipts.json'
    json_path.write_text('{"paper": "58mm"}', encoding='utf-8')
    assert load_config(json_path) is not None


@pytest.mark.parametrize('invalid', [False, True])
def test_standalone_pyyaml_fallback(tmp_path, monkeypatch, invalid):
    """A backend double checks dispatch/error handling, not clean-runtime support."""
    real_import = builtins.__import__
    calls = []

    class YAMLError(Exception):
        problem = 'bad standalone YAML'

    def safe_load(text):
        calls.append(text)
        if invalid:
            raise YAMLError()
        return {'paper': '58mm'}

    monkeypatch.setitem(sys.modules, 'yaml', types.SimpleNamespace(
        safe_load=safe_load, YAMLError=YAMLError))

    def without_hermes(name, *args, **kwargs):
        if name == 'hermes_yaml':
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, '__import__', without_hermes)
    path = tmp_path / 'receipts.yaml'
    path.write_text('paper: 58mm\n', encoding='utf-8')
    if invalid:
        with pytest.raises(ConfigError, match='receipts.yaml: invalid YAML: bad standalone YAML'):
            load_config(path)
    else:
        assert load_config(path) is not None
    assert calls == ['paper: 58mm\n']
