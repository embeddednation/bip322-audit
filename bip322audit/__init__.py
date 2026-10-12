"""bip322-audit: proof of control of a wallet's coins at a point in time.

Everything that talks to a node lives here (through ``bitcoin-cli``); the
``bip322core`` package stays pure and is used as a library:

* ``snapshot``  stamp block, funded addresses, message, one BIP-322 PSBT per address
* ``prove``     the same for given addresses, whether or not they hold coins yet
* ``finalize``  collect the signed PSBTs, finalize, write the proofs file
* ``verify``    BIP-322 verdicts, stamp check, UTXO checks, totals, report
* ``holdings``  what outputs or addresses hold now, and at a block

Everything is kept in one tree, ``~/bip322/<wallet>/snapshots/<date>-<height>/``
(``datadir``), and the proofs files under it are what ``--skip-proven`` reads (``proofs``).
"""

from ._version import __version__

__all__ = ["TOOL", "__version__"]

TOOL = f"bip322-audit {__version__}"
