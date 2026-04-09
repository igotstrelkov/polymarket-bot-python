"""
Unit tests for auth/credentials.py.

py_clob_client is mocked via sys.modules so these tests run without the SDK
installed. The mock is installed at module level so lazy imports inside
derive_credentials() and build_clob_client() resolve to the mock.
"""

import sys
from unittest.mock import MagicMock

import pytest

# ── Inject mock SDK into sys.modules before any test runs ────────────────────
# auth/credentials.py uses lazy `from py_clob_client.* import ...` so these
# mocks must be present before the functions execute, not just before import.

_mock_clob_cls = MagicMock(name="ClobClient")
_mock_sdk_creds_cls = MagicMock(name="SdkCreds")

_mock_clob_module = MagicMock()
_mock_clob_module.ClobClient = _mock_clob_cls

_mock_clob_types_module = MagicMock()
_mock_clob_types_module.ApiCreds = _mock_sdk_creds_cls

sys.modules["py_clob_client"] = MagicMock()
sys.modules["py_clob_client.client"] = _mock_clob_module
sys.modules["py_clob_client.clob_types"] = _mock_clob_types_module

# Now safe to import the module under test
from auth.credentials import (  # noqa: E402
    CHAIN_ID,
    CLOB_HOST,
    ApiCreds,
    build_clob_client,
    derive_credentials,
)
from config.settings import Settings  # noqa: E402


def make_settings(**overrides) -> Settings:
    defaults = dict(
        PRIVATE_KEY="0x" + "a" * 64,
        POLYGON_RPC_URL="https://polygon-rpc.example.com",
        BUILDER_API_KEY="key",
        BUILDER_SECRET="secret",
        BUILDER_PASSPHRASE="passphrase",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _reset_clob_mock():
    """Reset call history on the shared mock between tests."""
    _mock_clob_cls.reset_mock()
    _mock_sdk_creds_cls.reset_mock()


# ── derive_credentials ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_derive_credentials_returns_api_creds_named_tuple():
    _reset_clob_mock()
    private_key = "0x" + "b" * 64

    mock_instance = MagicMock()
    mock_instance.create_or_derive_api_creds.return_value = MagicMock(
        api_key="ak", api_secret="as", api_passphrase="ap"
    )
    _mock_clob_cls.return_value = mock_instance

    creds = await derive_credentials(private_key=private_key, host=CLOB_HOST, chain_id=CHAIN_ID)

    assert isinstance(creds, ApiCreds)
    assert creds.api_key == "ak"
    assert creds.secret == "as"
    assert creds.passphrase == "ap"


@pytest.mark.asyncio
async def test_derive_credentials_does_not_store_private_key():
    _reset_clob_mock()
    private_key = "0x" + "c" * 64

    mock_instance = MagicMock()
    mock_instance.create_or_derive_api_creds.return_value = MagicMock(
        api_key="k2", api_secret="s2", api_passphrase="p2"
    )
    _mock_clob_cls.return_value = mock_instance

    creds = await derive_credentials(private_key=private_key, host=CLOB_HOST, chain_id=CHAIN_ID)

    # Private key must not appear in any credential field
    for value in creds:
        assert value != private_key, f"Private key leaked into credential field: {value}"


@pytest.mark.asyncio
async def test_derive_credentials_constructs_temporary_client_with_private_key():
    """Private key is passed only to the temporary derivation client."""
    _reset_clob_mock()
    private_key = "0x" + "d" * 64

    mock_instance = MagicMock()
    mock_instance.create_or_derive_api_creds.return_value = MagicMock(
        api_key="x", api_secret="y", api_passphrase="z"
    )
    _mock_clob_cls.return_value = mock_instance

    await derive_credentials(private_key=private_key, host=CLOB_HOST, chain_id=CHAIN_ID)

    _mock_clob_cls.assert_called_once_with(
        host=CLOB_HOST, key=private_key, chain_id=CHAIN_ID
    )


# ── build_clob_client ─────────────────────────────────────────────────────────

def test_build_clob_client_uses_signature_type_2_when_relayer_enabled():
    _reset_clob_mock()
    settings = make_settings(USE_RELAYER=True)
    creds = ApiCreds(api_key="k", secret="s", passphrase="p")

    build_clob_client(settings, creds)

    _, kwargs = _mock_clob_cls.call_args
    assert kwargs.get("signature_type") == 2


def test_build_clob_client_uses_signature_type_0_when_relayer_disabled():
    _reset_clob_mock()
    settings = make_settings(USE_RELAYER=False)
    creds = ApiCreds(api_key="k", secret="s", passphrase="p")

    build_clob_client(settings, creds)

    _, kwargs = _mock_clob_cls.call_args
    assert kwargs.get("signature_type") == 0


def test_build_clob_client_uses_clob_host_and_polygon_chain_id():
    _reset_clob_mock()
    settings = make_settings(USE_RELAYER=True)
    creds = ApiCreds(api_key="k", secret="s", passphrase="p")

    build_clob_client(settings, creds)

    _, kwargs = _mock_clob_cls.call_args
    assert kwargs.get("host") == CLOB_HOST
    assert kwargs.get("chain_id") == CHAIN_ID


def test_build_clob_client_maps_api_creds_to_sdk_format():
    _reset_clob_mock()
    settings = make_settings(USE_RELAYER=True)
    creds = ApiCreds(api_key="mykey", secret="mysecret", passphrase="mypass")

    build_clob_client(settings, creds)

    _mock_sdk_creds_cls.assert_called_once_with(
        api_key="mykey",
        api_secret="mysecret",
        api_passphrase="mypass",
    )
