from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
import sys

import hvac
import pytest
from requests import RequestException
from tenacity import retry_if_exception_type, Retrying, stop_after_attempt

from konfetti import Konfig, vault
from konfetti.exceptions import (
    InvalidSecretOverrideError,
    KonfettiError,
    MissingError,
    SecretKeyMissing,
    VaultBackendMissing,
)
from konfetti.utils import NOT_SET
from konfetti.vault import VaultBackend
from konfetti.vault.core import VaultVariable

pytestmark = [pytest.mark.usefixtures("env", "vault_data")]


@pytest.mark.parametrize(
    "option, expected",
    (
        ("SECRET", "value"),
        ("WHOLE_SECRET", {"SECRET": "value", "IS_SECRET": True, "DECIMAL": "1.3"}),
        ("NESTED_SECRET", "what?"),
        ("DECIMAL", Decimal("1.3")),
    ),
)
def test_vault_access(config, option, expected):
    assert getattr(config, option) == expected


def test_missing_key(config):
    """The given key could be absent in vault data."""
    with pytest.raises(
        SecretKeyMissing, match="Path `path/to` exists in Vault but does not contain given key path - `ANOTHER_SECRET`"
    ):
        assert config.ANOTHER_SECRET == "value"


def test_missing_variable(config, vault_prefix):
    with pytest.raises(
        MissingError, match="Option `{}/something/missing` is not present in Vault".format(vault_prefix)
    ):
        config.NOT_IN_VAULT


def test_missing_vault_backend():
    config = Konfig()
    with pytest.raises(
        VaultBackendMissing,
        match="Vault backend is not configured. "
        "Please specify `vault_backend` option in your `Konfig` initialization",
    ):
        config.SECRET


@pytest.mark.parametrize("path", ("/path/to/", "/path/to", "path/to/", "path/to"))
def test_get_secret(path, config):
    assert config.get_secret(path) == {"SECRET": "value", "IS_SECRET": True, "DECIMAL": "1.3"}


@pytest.mark.parametrize(
    "transform",
    (
        lambda x: x.center(len(x) + 2, "/"),  # /path/
        lambda x: x.rjust(len(x) + 1, "/"),  # /path
        lambda x: x.ljust(len(x) + 1, "/"),  # path/
    ),
)
def test_get_secret_with_prefix(vault_prefix, transform):
    """Trailing and leading slashes don't matter."""
    config = Konfig(vault_backend=VaultBackend(transform(vault_prefix), try_env_first=False))
    assert config.get_secret("/path/to") == {"SECRET": "value", "IS_SECRET": True, "DECIMAL": "1.3"}


@pytest.mark.parametrize("action", (lambda c: c.get_secret("path/to"), lambda c: c.SECRET))
def test_disable_secrets(config, monkeypatch, action):
    monkeypatch.setenv("KONFETTI_DISABLE_SECRETS", "1")
    with pytest.raises(
        RuntimeError,
        match="Access to vault is disabled. Unset `KONFETTI_DISABLE_SECRETS` environment variable to enable it.",
    ):
        action(config)


@pytest.mark.parametrize(
    "variables", (("VAULT_ADDR",), ("VAULT_TOKEN", "VAULT_USERNAME"), ("VAULT_TOKEN", "VAULT_PASSWORD"))
)
def test_get_secret_without_vault_credentials(config, monkeypatch, variables):
    monkeypatch.setenv("VAULT_USERNAME", "test_user")
    monkeypatch.setenv("VAULT_PASSWORD", "test_password")
    for env_var in variables:
        monkeypatch.delenv(env_var)
    with pytest.raises(MissingError, match="""Can't access secret `/path/to` due"""):
        config.get_secret("/path/to")


@pytest.mark.parametrize("path, keys, expected", (("/path/to/", ["SECRET"], "PATH__TO"), ("path/to", [], "PATH__TO")))
def test_override_variable_name(path, keys, expected):
    variable = VaultVariable(path)
    for key in keys:
        variable = variable[key]
    assert variable.override_variable_name == expected


def test_path_not_string():
    if sys.version_info[0] == 2:
        message = "'path' must be <type 'str'>"
    else:
        message = "'path' must be <class 'str'>"
    with pytest.raises(TypeError, match=message):
        vault(1)


@pytest.mark.parametrize(
    "prefix, path, expected", ((NOT_SET, "path/to", "path/to"), ("secret/team", "path/to", "secret/team/path/to"))
)
def test_prefixes(prefix, path, expected):
    backend = VaultBackend(prefix)
    assert backend._get_full_path(path) == expected


def test_get_secret_file(config):
    file = config.SECRET_FILE
    assert isinstance(file, BytesIO)
    assert file.read() == b"content\nanother_line"


def test_override_secret(config, monkeypatch):
    monkeypatch.setenv("PATH__TO", '{"foo": "bar"}')
    assert config.get_secret("path/to") == {"foo": "bar"}
    assert config.get_secret("path/to")["foo"] == "bar"


def test_override_config_secret(config, monkeypatch):
    monkeypatch.setenv("PATH__TO", '{"SECRET": "bar"}')
    assert config.WHOLE_SECRET == {"SECRET": "bar"}
    assert config.SECRET == "bar"


@pytest.mark.parametrize("data", ("[1, 2]", "[invalid]"))
@pytest.mark.parametrize("action", (lambda config: config.get_secret("path/to"), lambda config: config.SECRET))
def test_override_invalid(config, monkeypatch, data, action):
    monkeypatch.setenv("PATH__TO", data)
    with pytest.raises(InvalidSecretOverrideError, match="`PATH__TO` variable should be a JSON-encoded dictionary"):
        action(config)


def test_default_config(config):
    assert config.DEFAULT == "default"


def test_override_with_default(config, monkeypatch):
    monkeypatch.setenv("PATH__TO", '{"DEFAULT": "non-default"}')
    assert config.DEFAULT == "non-default"


def test_disable_defaults(config, monkeypatch):
    monkeypatch.setenv("VAULT_DISABLE_DEFAULTS", "True")
    with pytest.raises(SecretKeyMissing):
        config.DEFAULT


@pytest.fixture
def config_with_cached_vault(vault_prefix):
    return Konfig(vault_backend=VaultBackend(vault_prefix, cache_ttl=1))


SECRET_DATA = {"DECIMAL": "1.3", "IS_SECRET": True, "SECRET": "value"}


def test_cold_cache(config_with_cached_vault, vault_prefix):
    # Cache is empty, data is taken from vault
    assert not config_with_cached_vault.vault_backend.cache._data
    assert config_with_cached_vault.get_secret("/path/to") == SECRET_DATA
    # Response is cached
    data = config_with_cached_vault.vault_backend.cache._data
    # Straight comparison for dicts fails on Python 2.7 :(
    assert list(data) == [vault_prefix + "/path/to"]
    assert data[vault_prefix + "/path/to"]["data"] == SECRET_DATA


def test_warm_cache(config_with_cached_vault, vault_prefix, mocker):
    test_cold_cache(config_with_cached_vault, vault_prefix)
    vault = mocker.patch("hvac.Client.read")
    # Cache is warmed and contains the secret
    assert config_with_cached_vault.get_secret("/path/to") == SECRET_DATA
    assert config_with_cached_vault.vault_backend.cache[vault_prefix + "/path/to"] == SECRET_DATA

    assert not vault.called


@pytest.mark.freeze_time
def test_no_recaching(config_with_cached_vault, mocker, freezer):
    assert config_with_cached_vault.get_secret("/path/to") == SECRET_DATA
    freezer.tick(0.5)
    vault = mocker.patch("hvac.Client.read")
    assert config_with_cached_vault.get_secret("/path/to") == SECRET_DATA
    assert not vault.called
    freezer.tick(0.6)
    assert config_with_cached_vault.get_secret("/path/to")
    assert vault.called


def skip_if_python(version):
    return pytest.mark.skipif(sys.version_info[0] == version, reason="Doesnt work on Python {}".format(version))


@pytest.mark.parametrize(
    "ttl, exc_type, message",
    (
        pytest.param(10 ** 20, ValueError, r"'cache_ttl' should be in range \(0, 999999999\]", marks=skip_if_python(2)),
        pytest.param(10 ** 20, TypeError, r".*must be.*", marks=skip_if_python(3)),
        ('"', TypeError, r".*must be.*"),
    ),
)
def test_ttl(ttl, exc_type, message):
    with pytest.raises(exc_type, match=message):
        VaultBackend("path/to", cache_ttl=ttl)


def test_cast_decimal_warning(config):
    with pytest.warns(RuntimeWarning, match="Float to Decimal conversion detected, please use string or integer."):
        config.FLOAT_DECIMAL


def test_cast_date(config):
    assert config.DATE == date(year=2019, month=1, day=25)


def test_cast_datetime(config):
    assert config.DATETIME == datetime(year=2019, month=1, day=25, hour=14, minute=35, second=5)


def test_retry(config, mocker):
    mocker.patch("requests.adapters.HTTPAdapter.send", side_effect=RequestException)
    m = mocker.patch.object(config.vault_backend, "_call", wraps=config.vault_backend._call)
    with pytest.raises(RequestException):
        config.SECRET
    assert m.called is True
    assert m.call_count == 3


def test_retry_object(vault_prefix, mocker):
    config = Konfig(
        vault_backend=VaultBackend(
            vault_prefix,
            retry=Retrying(retry=retry_if_exception_type(KonfettiError), reraise=True, stop=stop_after_attempt(2)),
        )
    )
    mocker.patch("requests.adapters.HTTPAdapter.send", side_effect=KonfettiError)
    m = mocker.patch.object(config.vault_backend, "_call", wraps=config.vault_backend._call)
    with pytest.raises(KonfettiError):
        config.SECRET
    assert m.called is True
    assert m.call_count == 2


@pytest.mark.parametrize("token", ("token_removed", "token_expired"))
def test_userpass(config, monkeypatch, token):
    if token == "token_removed":
        monkeypatch.delenv("VAULT_TOKEN")
    else:
        monkeypatch.setenv("VAULT_TOKEN", token)
    monkeypatch.setenv("VAULT_USERNAME", "test_user")
    monkeypatch.setenv("VAULT_PASSWORD", "test_password")
    assert config.SECRET == "value"


def test_invalid_token(config, monkeypatch):
    monkeypatch.setenv("VAULT_TOKEN", "invalid")
    with pytest.raises(hvac.exceptions.Forbidden):
        config.SECRET


def test_userpass_cache(config_with_cached_vault, vault_prefix, mocker, monkeypatch):
    monkeypatch.delenv("VAULT_TOKEN")
    monkeypatch.setenv("VAULT_USERNAME", "test_user")
    monkeypatch.setenv("VAULT_PASSWORD", "test_password")
    test_cold_cache(config_with_cached_vault, vault_prefix)
    vault = mocker.patch("hvac.Client.read")
    # Cache is warmed and contains the secret
    assert config_with_cached_vault.get_secret("/path/to") == SECRET_DATA
    assert config_with_cached_vault.vault_backend.cache[vault_prefix + "/path/to"] == SECRET_DATA

    assert not vault.called


def test_userpass_token_cache(config, monkeypatch, mocker):
    monkeypatch.delenv("VAULT_TOKEN")
    monkeypatch.setenv("VAULT_USERNAME", "test_user")
    monkeypatch.setenv("VAULT_PASSWORD", "test_password")
    # First time access / auth
    assert config.vault_backend._token is NOT_SET
    assert config.SECRET == "value"
    assert config.vault_backend._token is not NOT_SET
    # Second time access / no auth
    auth = mocker.patch("hvac.Client.auth_userpass")
    first_token = config.vault_backend._token
    assert config.IS_SECRET is True
    assert auth.called is False
    assert config.vault_backend._token == first_token


def test_vault_var_reusage(vault_prefix, vault_addr, vault_token):
    variable = vault("path/to")

    class Test:
        VAULT_ADDR = vault_addr
        VAULT_TOKEN = vault_token
        SECRET = variable["SECRET"]
        IS_SECRET = variable["IS_SECRET"]

    config = Konfig.from_object(Test, vault_backend=VaultBackend(vault_prefix))
    assert config.asdict() == {
        "SECRET": "value",
        "IS_SECRET": True,
        "VAULT_ADDR": vault_addr,
        "VAULT_TOKEN": vault_token,
    }
