"""The RLUSD/XRP live venue (M8), TESTNET ONLY: test funds with no value, never mainnet.

This stage contains no way to sign for mainnet. The testnet hosts and issuer are constants
in `guards`, not settings, so no config can widen them. Only `signer` signs or submits,
and tests/test_xrpl_no_signing.py fails if anything else does.
"""

LABEL = "TESTNET: no real funds"
