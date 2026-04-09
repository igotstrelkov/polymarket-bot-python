"""
Authentication and CLOB client construction.

Level 1 — EIP-712 typed signing: private key derives API credentials via
create_or_derive_api_creds(). One-time per wallet.

Level 2 — HMAC-SHA256: all subsequent API requests signed with derived
credentials. Private key is NOT used or stored after credential derivation.
"""

from typing import NamedTuple

from config.settings import Settings

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon PoS


class ApiCreds(NamedTuple):
    """Derived CLOB API credentials. Never contains the private key."""
    api_key: str
    secret: str
    passphrase: str


async def derive_credentials(private_key: str, host: str, chain_id: int) -> ApiCreds:
    """Level 1 EIP-712 auth: derive CLOB API credentials from private key.

    The private key is used only for this one-time derivation call and is
    NOT stored in the returned object or anywhere else after this function
    returns. All subsequent API calls use Level 2 HMAC-SHA256 with the
    returned ApiCreds.
    """
    # Lazy import so unit tests can mock py_clob_client via sys.modules
    from py_clob_client.client import ClobClient  # type: ignore[import]

    tmp_client = ClobClient(host=host, key=private_key, chain_id=chain_id)
    sdk_creds = tmp_client.create_or_derive_api_creds()

    return ApiCreds(
        api_key=sdk_creds.api_key,
        secret=sdk_creds.api_secret,
        passphrase=sdk_creds.api_passphrase,
    )


def build_clob_client(settings: Settings, creds: ApiCreds):
    """Build an authenticated ClobClient for Level 2 (HMAC-SHA256) requests.

    Signature type 2 (Gnosis Safe via Builder Relayer) when USE_RELAYER=True.
    Signature type 0 (EOA, direct EIP-712) when USE_RELAYER=False.
    """
    # Lazy import so unit tests can mock py_clob_client via sys.modules
    from py_clob_client.client import ClobClient  # type: ignore[import]
    from py_clob_client.clob_types import ApiCreds as SdkCreds  # type: ignore[import]

    signature_type = 2 if settings.USE_RELAYER else 0
    sdk_creds = SdkCreds(
        api_key=creds.api_key,
        api_secret=creds.secret,
        api_passphrase=creds.passphrase,
    )
    return ClobClient(
        host=CLOB_HOST,
        chain_id=CHAIN_ID,
        creds=sdk_creds,
        signature_type=signature_type,
    )
