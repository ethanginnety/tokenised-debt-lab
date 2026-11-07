"""The RLUSD/XRP live venue (M8): testnet (test funds), and mainnet (real funds, plan approved 7 Nov 2026).

Each network's hosts and issuer are constants in `guards`, not settings, so no config can widen
them, and the two allowlists share nothing. Only `signer` (testnet) and `mainnet_signer`
(mainnet: offers and cancels only, the account's regular key only) sign or submit, and
tests/test_xrpl_no_signing.py fails if anything else does. Every part of a running bot must be
on one network (`bot.py`); its database and the monitor's record which.
"""

LABEL = "TESTNET: no real funds"
