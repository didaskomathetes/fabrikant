"""`fabrikant serve` port resolution: --port > FABRIKANT_PORT > default."""

import pytest

from fabrikant import config


def test_explicit_port_wins():
    assert config.serve_port(9000, {"FABRIKANT_PORT": "8399"}) == 9000


def test_env_port_used_when_no_flag():
    assert config.serve_port(None, {"FABRIKANT_PORT": " 8399 "}) == 8399


@pytest.mark.parametrize("env", [{}, {"FABRIKANT_PORT": ""}, {"FABRIKANT_PORT": "  "}])
def test_default_port(env):
    assert config.serve_port(None, env) == config.DEFAULT_PORT


@pytest.mark.parametrize("raw", ["abc", "0", "70000", "-1", "80.5"])
def test_invalid_env_port_is_rejected(raw):
    with pytest.raises(ValueError, match="FABRIKANT_PORT"):
        config.serve_port(None, {"FABRIKANT_PORT": raw})
