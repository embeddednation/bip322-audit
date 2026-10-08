import pytest
from bip322core.dev.testing import key_expression, master_key
from bip322core.wallet import MultisigWallet
from embit.bip32 import HDKey


@pytest.fixture(scope="session")
def masters() -> list[HDKey]:
    """The three deterministic demo cosigners of bip322-core's test suite."""
    return [master_key(label) for label in "ABC"]


@pytest.fixture(scope="session")
def descriptor_text(masters) -> str:
    return "wsh(sortedmulti(2," + ",".join(key_expression(m) for m in masters) + "))"


@pytest.fixture(scope="session")
def wallet(descriptor_text) -> MultisigWallet:
    return MultisigWallet.from_descriptor(descriptor_text, network="main", name="test-2of3")


@pytest.fixture(scope="session")
def signer_expressions(masters) -> list[str]:
    """``[fp/48h/0h/0h/2h]xprv.../<0;1>/*`` for each cosigner (account-level private keys)."""
    return [key_expression(m, private=True) for m in masters]
