"""Regression tests for the findings of the review before the first release (one test per finding)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from test_audit import T0, FakeCli, _signed_bundle, fake_hash

import bip322audit.cli as audit_cli
from bip322audit.audit import AuditError, finalize_bundle, format_report, verify_proofs
from bip322audit.holdings import holdings
from bip322audit.ledger import find_proofs, proven_addresses
from bip322audit.rpc import BitcoinCli, RpcError
from bip322audit.snapshot import take_snapshot, wallet_from_node, write_bundle
from bip322audit.stamp import Stamp, check_stamp, compose_message, iso_utc, parse_stamp

ROOT = Path(__file__).resolve().parent.parent
ROW_KEYS = {"txid", "vout", "amount_sat", "height", "blockhash", "status", "verified", "contradiction"}


@pytest.fixture(scope="module")
def funded(wallet):
    a0, a1 = wallet.derive(0).address, wallet.derive(1, 1).address
    return {a0: [(50_000_000, 990), (25_000_000, 993)], a1: [(10_000_000, 980)]}


@pytest.fixture(scope="module")
def signed_template(tmp_path_factory, wallet, funded, signer_expressions):
    """One signed bundle for the module (the snapshot searches 1000 indexes for the node's foreign coins: seconds)."""
    return _signed_bundle(tmp_path_factory.mktemp("template"), wallet, funded, signer_expressions)


@pytest.fixture
def bundle(tmp_path, signed_template):
    return Path(shutil.copytree(signed_template, tmp_path / "bundle"))


@pytest.fixture
def document(bundle):
    return finalize_bundle(bundle)


def _copy(document):
    return json.loads(json.dumps(document))


def _first(document):
    """The proof and the output of the 0.5 BTC coin, wherever the document's ordering put it."""
    for proof in document["proofs"]:
        for utxo in proof["utxos"]:
            if utxo["amount_sat"] == 50_000_000:
                return proof, utxo
    raise AssertionError("no such output")


def _row(report, utxo):
    return next(u for p in report["proofs"] for u in p["utxos"] if (u["txid"], u["vout"]) == (utxo["txid"], utxo["vout"]))


def test_verify_report_rows_come_from_the_node_not_the_document(wallet, funded, document):
    """Finding 1: a key planted in proofs.json must not surface as a finding."""
    doc = _copy(document)
    _, utxo = _first(doc)
    utxo.update({"unspent_at_snapshot": True, "spent_height": 1150, "verified": True, "note": "trust me", "node_amount_sat": 1})
    node = FakeCli(wallet, funded, tip=1200, spent={(utxo["txid"], utxo["vout"]): 990})  # spent before the stamp, nothing recorded
    report = verify_proofs(doc, node, engines=["btclib"])
    row = _row(report, utxo)
    assert not row["verified"] and "unspent_at_snapshot" not in row and "spent_height" not in row and row.get("note") != "trust me"
    assert (
        report["result"] == "incomplete"
        and "RESULT: OK" not in format_report(report)
        and "unspent at snapshot" not in format_report(report)
    )
    offline = verify_proofs(doc, None, engines=["btclib"])
    assert set(_row(offline, utxo)) == ROW_KEYS and not _row(offline, utxo)["verified"] and "[ok]" not in format_report(offline)
    clean = verify_proofs(document, FakeCli(wallet, funded, tip=1200), engines=["btclib"])
    allowed = ROW_KEYS | {"node_amount_sat", "node_address", "created_height", "confirmations"}
    assert all(set(u) <= allowed for p in clean["proofs"] for u in p["utxos"])


def test_spent_output_without_a_checked_spend_is_not_verified(tmp_path, wallet, funded, document, monkeypatch, capsys):
    """Finding 2: spent, and nothing shows when: not [ok], not in the held total, and the result is INCOMPLETE (exit 3)."""
    _, utxo = _first(document)
    outpoint = (utxo["txid"], utxo["vout"])
    node = FakeCli(wallet, funded, tip=1200, spent={outpoint: 990})
    report = verify_proofs(document, node, engines=["btclib"])
    row = _row(report, utxo)
    assert row["status"] == "spent_time_unknown" and not row["verified"] and not row["contradiction"] and row["existed_at_snapshot"]
    assert report["result"] == "incomplete" and not report["ok"]  # "ok" is strictly the verdict; "result" tells incomplete from failed
    assert report["summary"]["utxos_verified_at_snapshot"] == "2/3" and report["summary"]["utxos_unexplained"] == 1
    assert report["totals"]["held_at_stamp_sat"] == 35_000_000 and report["totals"]["claimed_sat"] == 85_000_000
    text = format_report(report)
    assert "RESULT: INCOMPLETE" in text and "[??]" in text and "RESULT: OK" not in text and "RESULT: FAILED" not in text
    # the command line: exit 3, neither 0 (ok) nor 1 (failed)
    (tmp_path / "proofs.json").write_text(json.dumps(document))
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: node)
    assert audit_cli.main(["verify", str(tmp_path / "proofs.json")]) == 3
    assert "RESULT: INCOMPLETE" in capsys.readouterr().out
    # a recorded spend that does not spend the output is a problem of the document: FAILED
    later = FakeCli(wallet, funded, tip=1200, spent={outpoint: 1150})
    other = later.spend_txid(*outpoint)
    wrong = dict(document, spends={f"{outpoint[0]}:{outpoint[1]}": {"spent_by": other, "blockhash": fake_hash(1150), "height": 1150}})
    # the node's transaction of that name spends something else
    monkeypatch.setattr(later, "_spends", lambda: [(other, 1150, ("ab" * 32, 0))])
    report = verify_proofs(wrong, later, engines=["btclib"])
    assert report["result"] == "failed" and any("does not spend" in p for p in report["document_problems"])
    assert not _row(report, utxo)["verified"]
    # a recorded spend the node cannot fetch: unknown, not verified
    gone = dict(document, spends={f"{outpoint[0]}:{outpoint[1]}": {"spent_by": "cd" * 32, "blockhash": fake_hash(1150), "height": 1150}})
    report = verify_proofs(gone, FakeCli(wallet, funded, tip=1200, spent={outpoint: 1150}), engines=["btclib"])
    assert report["result"] == "incomplete" and _row(report, utxo)["status"] == "spent_time_unknown"


def test_verify_totals_duplicates_and_empty_documents(wallet, funded, document):
    """Finding 3: totals come from the listed outputs; duplicates and an empty document fail."""
    node = FakeCli(wallet, funded, tip=1200)
    doc = _copy(document)
    doc["proofs"][0]["total_sat"] = 10**12
    report = verify_proofs(doc, node, engines=["btclib"])
    assert report["totals"]["claimed_sat"] == 85_000_000 and report["result"] == "failed"
    assert any("total_sat" in p for p in report["document_problems"]) and "10000.1" not in format_report(report)
    doc = _copy(document)
    doc["total_sat"] = 1
    assert any("total_sat" in p for p in verify_proofs(doc, node, engines=["btclib"])["document_problems"])
    doc = _copy(document)
    proof, utxo = _first(doc)
    proof["utxos"].append(dict(utxo))
    proof["total_sat"] += utxo["amount_sat"]
    doc["proofs"].append(_copy(proof))
    report = verify_proofs(doc, node, engines=["btclib"])
    assert report["result"] == "failed" and report["totals"]["verified_unspent_sat"] == 85_000_000
    assert report["totals"]["claimed_sat"] == 85_000_000 and report["summary"]["utxos_verified_at_snapshot"] == "3/3"
    problems = " | ".join(report["document_problems"])
    assert "listed more than once" in problems and "more than one proof" in problems
    for mode in (node, None):
        report = verify_proofs(dict(document, proofs=[], total_sat=0), mode, engines=["btclib"])
        assert report["result"] == "failed" and not report["ok"] and "RESULT: FAILED" in format_report(report)


class MovingTip(FakeCli):
    """A block arrives right after the first read of the tip (or at the first call of ``on``)."""

    def __init__(self, *args, on="getblockchaininfo", **kwargs):
        super().__init__(*args, **kwargs)
        self.on, self.moved = on, False

    def call(self, method, *params):
        if method == self.on and not self.moved and self.on != "getblockchaininfo":
            self.moved = True
            self.tip_height += 1
        result = super().call(method, *params)
        if method == self.on == "getblockchaininfo" and not self.moved and len([c for c in self.calls if c[0] == method]) >= 2:
            self.moved = True
            self.tip_height += 1
        return result


def test_verify_and_holdings_survive_a_tip_that_moves(wallet, funded, document):
    """Finding 4: heights are derived from the block gettxout answered at, not from a tip read earlier."""
    node = MovingTip(wallet, funded, tip=1200, on="gettxout")
    report = verify_proofs(document, node, engines=["btclib"])
    assert (
        report["result"] == "ok" and report["summary"]["contradictions"] == 0 and report["summary"]["utxos_verified_at_snapshot"] == "3/3"
    )
    assert sorted(u["created_height"] for p in report["proofs"] for u in p["utxos"]) == [980, 990, 993]
    node = MovingTip(wallet, funded, tip=1000, on="gettxout")
    outs = [f"{t}:{n}" for _, t, n, _, _ in node._utxos()]  # noqa: SLF001
    assert sorted(o["height"] for o in holdings(node, outs)["outputs"]) == [980, 990, 993]


def test_snapshot_retries_when_the_tip_moves(wallet):
    """Finding 5: the coins, their heights and block hashes belong to one tip."""
    a0, a1 = wallet.derive(0).address, wallet.derive(1, 1).address
    coins = {a0: [(50_000_000, 990), (25_000_000, 996)], a1: [(10_000_000, 980)]}
    for source in ("listunspent", "scantxoutset"):
        node = MovingTip(wallet, coins, tip=1000, on=source, rpcwallet=source == "listunspent")
        lines = []
        snapshot, _ = take_snapshot(node, wallet, "x", max_index=20, progress=lines.append)
        assert node.moved and snapshot.tip_height == 1001 and snapshot.stamp.height == 995 and any("tip moved" in x for x in lines)
        listed = sorted((u["height"], u["blockhash"]) for a in snapshot.addresses for u in a["utxos"])
        assert listed == [(980, fake_hash(980)), (990, fake_hash(990))]  # true heights; the output at 996 is after the stamp
        assert parse_stamp(snapshot.message) == snapshot.stamp

    class Restless(FakeCli):
        def call(self, method, *params):
            if method == "listunspent":
                self.tip_height += 1
            return super().call(method, *params)

    with pytest.raises(RpcError, match="tip kept moving"):
        take_snapshot(Restless(wallet, coins), wallet, "x", max_index=20)


def test_finalize_with_node_wallet_named_empty_string(bundle, tmp_path, wallet, funded, signer_expressions, monkeypatch, capsys):
    """Finding 6: Core's default wallet is named "": that is a wallet, not "no wallet"."""
    directory = bundle
    snap = json.loads((directory / "snapshot.json").read_text())
    snap["node_wallet"] = ""
    (directory / "snapshot.json").write_text(json.dumps(snap))
    fake = FakeCli(wallet, funded, tip=1200)
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: fake)
    assert audit_cli.main(["finalize", str(directory)]) == 0
    assert "no node wallet known" not in capsys.readouterr().err and "-rpcwallet=" in fake.argv
    assert json.loads((directory / "proofs.json").read_text())["spends"] == {}
    assert audit_cli._opt(audit_cli.build_parser().parse_args(["-w", "", "stamp"]), "wallet") == ""  # noqa: SLF001


def test_finalize_offline_keeps_recorded_spends(bundle, tmp_path, wallet, funded, signer_expressions, monkeypatch, capsys):
    """Finding 7: a later --offline run must not erase the spends an online run recorded."""
    directory = bundle
    _, txid, vout, _, _ = FakeCli(wallet, funded)._utxos()[0]  # noqa: SLF001
    fake = FakeCli(wallet, funded, tip=1200, spent={(txid, vout): 1150})
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: fake)
    assert audit_cli.main(["finalize", str(directory)]) == 0
    before = json.loads((directory / "proofs.json").read_text())
    assert list(before["spends"]) == [f"{txid}:{vout}"]
    capsys.readouterr()
    assert audit_cli.main(["finalize", str(directory), "--offline"]) == 0
    after = json.loads((directory / "proofs.json").read_text())
    assert after["spends"] == before["spends"] and after["spends_utc"] == before["spends_utc"]
    assert "kept" in capsys.readouterr().err


def test_holdings_accepts_upper_case_bech32(wallet, funded):
    """Finding 8: bech32 may be written in upper case (QR codes); the node answers in lower case."""
    a0 = wallet.derive(0).address
    node = FakeCli(wallet, funded, tip=1000)
    result = holdings(node, [a0.upper()])
    assert result["total_sat"] == 75_000_000 and {o["address"] for o in result["outputs"]} == {a0}
    assert holdings(node, [a0.upper(), a0])["total_sat"] == 75_000_000


def test_bad_input_gives_one_line_errors_not_tracebacks(tmp_path, wallet, funded, document, monkeypatch, capsys):
    """Finding 9: template placeholders, document shape and a chatty --cli command end in `error: ...` and exit 2."""
    fake = FakeCli(wallet, funded, tip=1000)
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: fake)
    for template in ("Audit {year}", "Audit {0}", "Audit {date.__class__}", "Audit {}"):
        assert audit_cli.main(["-w", "watch", "snapshot", "--max-index", "20", "--text", template, "-o", str(tmp_path / "b")]) == 2
        err = capsys.readouterr().err
        assert err.startswith("error: ") and "placeholder" in err and len(err.strip().splitlines()) == 1
    path = tmp_path / "proofs.json"
    shapes = [
        [],
        {"proofs": {}},
        {**document, "proofs": [{"address": "x"}]},
        {k: v for k, v in document.items() if k != "message_hex"} | {"message": 5},
    ]
    broken = _copy(document)
    del _first(broken)[1]["height"]
    untyped = _copy(document)
    _first(untyped)[1]["amount_sat"] = "50000000"
    for shape in [*shapes, broken, untyped, dict(document, spends=[1])]:
        path.write_text(json.dumps(shape))
        for extra in ([], ["--offline"]):
            assert audit_cli.main(["verify", str(path), *extra]) == 2
            err = capsys.readouterr().err
            assert err.startswith("error: ") and len(err.strip().splitlines()) == 1
        with pytest.raises(AuditError):
            verify_proofs(shape, None, engines=["btclib"])
    path.write_text('{"proofs": [')
    assert audit_cli.main(["verify", str(path), "--offline"]) == 2 and "not valid JSON" in capsys.readouterr().err
    old = _copy(document)  # documents written before proofs carried total_sat still verify
    for proof in old["proofs"]:
        del proof["total_sat"]
    assert verify_proofs(old, FakeCli(wallet, funded, tip=1200), engines=["btclib"])["ok"]
    # a wrapper that prints a banner before the JSON
    stub = tmp_path / "chatty cli"
    stub.write_text('#!/bin/sh\necho "Connecting to the node..."\necho \'{"chain": "main", "blocks": 1, "bestblockhash": "00"}\'\n')
    stub.chmod(0o755)
    with pytest.raises(RpcError, match="not JSON"):
        BitcoinCli([str(stub)]).chain()
    with pytest.raises(RpcError, match="block hash"):
        BitcoinCli([str(stub)]).block_hash(5)
    monkeypatch.undo()
    assert audit_cli.main(["--cli", f"'{stub}'", "stamp"]) == 2
    assert capsys.readouterr().err.startswith("error: getblockchaininfo: ")


def test_recorded_spend_in_a_block_that_left_the_main_chain(wallet, funded, document):
    """Finding 10: a stale spending block says nothing about when the output was spent; it is not a contradiction."""
    _, utxo = _first(document)
    outpoint = (utxo["txid"], utxo["vout"])

    class Reorged(FakeCli):
        stale = fake_hash(1150)

        def call(self, method, *params):
            result = super().call(method, *params)
            if method == "getblockheader" and params[0] == self.stale:
                result["confirmations"] = -1
            return result

    node = Reorged(wallet, funded, tip=1200, spent={outpoint: 1150})
    key = f"{outpoint[0]}:{outpoint[1]}"
    doc = dict(document, spends={key: {"spent_by": node.spend_txid(*outpoint), "blockhash": fake_hash(1150), "height": 1150}})
    report = verify_proofs(doc, node, engines=["btclib"])
    row = _row(report, utxo)
    assert row["status"] == "spent_time_unknown" and not row["contradiction"] and "main chain" in row["note"]
    assert report["result"] == "incomplete" and report["summary"]["contradictions"] == 0
    # the block that created a spent output must be in the main chain as well
    Reorged.stale = utxo["blockhash"]
    row = _row(verify_proofs(doc, Reorged(wallet, funded, tip=1200, spent={outpoint: 1150}), engines=["btclib"]), utxo)
    assert row["status"] == "spent_or_unknown" and not row["verified"] and "main chain" in row["note"]


def test_bundle_names_do_not_collide_and_force_is_limited(tmp_path, wallet, funded, signer_expressions, monkeypatch, capsys):
    """Finding 11: two runs in one block get two directories; --force never writes over signatures or proofs."""
    fake = FakeCli(wallet, funded, tip=1000)
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: fake)
    monkeypatch.chdir(tmp_path)
    assert audit_cli.main(["-w", "watch", "snapshot", "--max-index", "20"]) == 0
    first = Path(capsys.readouterr().out.strip())
    assert audit_cli.main(["-w", "watch", "prove", "--max-index", "20", wallet.derive(0).address]) == 0
    second = Path(capsys.readouterr().out.strip())
    assert first != second and second.name == first.name + "-2" and len(list((second / "to_sign").iterdir())) == 1
    # --force rewrites an unsigned bundle and drops the PSBTs of the earlier run
    assert audit_cli.main(["-w", "watch", "prove", "--max-index", "20", wallet.derive(0).address, "-o", str(first), "--force"]) == 0
    assert [p.name for p in (first / "to_sign").iterdir()] == ["to_sign-01.psbt"]
    capsys.readouterr()
    (first / "signed" / "to_sign-01-part.psbt").write_text("x")
    assert audit_cli.main(["-w", "watch", "snapshot", "--max-index", "20", "-o", str(first), "--force"]) == 2
    assert "signed" in capsys.readouterr().err
    (first / "signed" / "to_sign-01-part.psbt").unlink()
    (first / "proofs.json").write_text("{}")
    assert audit_cli.main(["-w", "watch", "snapshot", "--max-index", "20", "-o", str(first), "--force"]) == 2
    assert "proofs.json" in capsys.readouterr().err and (first / "proofs.json").read_text() == "{}"


def test_snapshot_says_what_it_left_out(tmp_path, wallet, funded, monkeypatch, capsys):
    """Finding 12: coins of other descriptors in the node wallet are counted and named, and so is the chosen descriptor."""

    class Mixed(FakeCli):
        def call(self, method, *params):
            result = super().call(method, *params)
            if method == "listdescriptors":
                result["descriptors"] += [
                    {"desc": "tr([00000000/86h/0h/0h]xpub6Bogus/0/*)#aaaaaaaa"},
                    {"desc": "pkh(xpub6Bogus/0/*)#bbbbbbbb"},
                ]
            return result

    snapshot, _ = take_snapshot(FakeCli(wallet, funded), wallet, "x", max_index=20)
    other = snapshot.left_out["other_descriptors"]
    assert (other["outputs"], other["amount_sat"], other["amount_btc"]) == (2, 200_000_000, "2.00000000") and "descriptor" in other["why"]
    lines = []
    assert wallet_from_node(Mixed(wallet, funded), progress=lines.append).to_descriptor() == wallet.to_descriptor()
    assert len(lines) == 1 and "not covered" in lines[0] and "tr" in lines[0] and "pkh" in lines[0] and "xpub" not in lines[0]
    # the command prints it (stderr stays one JSON summary) and snapshot.json records it
    node = Mixed(wallet, funded)
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: node)
    assert audit_cli.main(["-w", "watch", "snapshot", "--max-index", "20", "-o", str(tmp_path / "b")]) == 0
    summary = json.loads(capsys.readouterr().err)
    assert summary["descriptor"] == "wsh(sortedmulti) 2 of 3" and "pkh, tr" in summary["notes"][0]
    assert summary["left_out"]["other_descriptors"]["outputs"] == 2 and summary["left_out"]["other_descriptors"]["why"]
    assert json.loads((tmp_path / "b" / "snapshot.json").read_text())["left_out"] == summary["left_out"]
    later = FakeCli(wallet, {**funded, wallet.derive(3).address: [(7_000_000, 990)]})
    skipped, _ = take_snapshot(later, wallet, "x", max_index=20, skip_addresses=set(funded))
    assert skipped.left_out["proven_earlier"]["outputs"] == 3 and skipped.left_out["proven_earlier"]["amount_sat"] == 85_000_000


PROOFS_KEYS = {
    "tool",
    "chain",
    "stamp",
    "message",
    "message_hex",
    "policy",
    "finalized_utc",
    "proofs",
    "total_sat",
    "total_btc",
    "spends",
    "spends_utc",
}


def test_proofs_document_is_built_from_a_whitelist(bundle, tmp_path, wallet, funded, signer_expressions, monkeypatch):
    """Finding 13: only named fields reach proofs.json, proofs are in address order, and only smp proofs are written."""
    directory = bundle
    snap = json.loads((directory / "snapshot.json").read_text())
    snap["a_field_added_later"] = "m/48h/0h"
    snap["addresses"][0]["origin"] = "ea34d476"
    snap["addresses"][0]["utxos"][0]["desc"] = "wsh(...)"
    (directory / "snapshot.json").write_text(json.dumps(snap))
    document = finalize_bundle(directory, cli=FakeCli(wallet, funded, tip=1200))
    assert set(document) == PROOFS_KEYS
    assert all(set(p) == {"address", "utxos", "total_sat", "signature", "variant"} for p in document["proofs"])
    assert all(set(u) == {"txid", "vout", "amount_sat", "height", "blockhash"} for p in document["proofs"] for u in p["utxos"])
    addresses = [p["address"] for p in document["proofs"]]
    assert addresses == sorted(addresses) and addresses != [a["address"] for a in snap["addresses"]]  # not derivation order
    report = verify_proofs(document, FakeCli(wallet, funded, tip=1200), engines=["btclib"])
    for text in (json.dumps(document), json.dumps(report, default=str), format_report(report)):
        hits = [
            w
            for w in ("ea34d476", "48h/", "xpub", "to_sign-", ".psbt", "watch", "descriptor", "branch", "listunspent", "a_field")
            if w in text
        ]
        assert not hits, hits
    import bip322audit.audit as audit_module

    monkeypatch.setattr(audit_module, "signature_from_psbt", lambda psbt: "pof" + "A" * 40)
    with pytest.raises(AuditError, match="smp"):
        finalize_bundle(directory)


def test_finalize_names_files_and_lists_ignored_ones(bundle, tmp_path, wallet, funded, signer_expressions, monkeypatch, capsys):
    """Finding 16: a bad PSBT is reported with its address and files; files finalize did not use are listed."""
    directory = bundle
    (directory / "signed" / "notes.txt").write_text("hello")
    (directory / "signed" / "garbage.psbt").write_text("not a psbt")
    (directory / "signed" / "sub").mkdir()
    notes = []
    finalize_bundle(directory, notes=notes)
    assert sorted(n.split(":")[0] for n in notes) == ["ignored signed/garbage.psbt", "ignored signed/notes.txt", "ignored signed/sub"]
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: FakeCli(wallet, funded))
    assert audit_cli.main(["finalize", str(directory), "--offline"]) == 0
    assert "ignored signed/garbage.psbt" in capsys.readouterr().err
    from bip322core.core import BIP322Error

    import bip322audit.audit as audit_module

    def refuse(psbts):
        raise BIP322Error("input 0: witness_script differs between the PSBTs being combined")

    monkeypatch.setattr(audit_module, "combine_psbts", refuse)
    with pytest.raises(AuditError) as caught:
        finalize_bundle(directory)
    message = str(caught.value)
    assert all(a in message for a in funded) and "to_sign-01-cc0-part.psbt" in message and "witness_script differs" in message


def test_cli_quoting_and_timeout(wallet, funded, monkeypatch, capsys):
    """Finding 17: --cli is split like a shell line (documented), and --timeout reaches the subprocess."""
    assert BitcoinCli('"/opt/my node/bitcoin-cli" -signet').argv == ["/opt/my node/bitcoin-cli", "-signet"]
    with pytest.raises(RpcError, match="quote"):
        BitcoinCli("/nonexistent dir/bitcoin-cli").call("getblockchaininfo")
    fake = FakeCli(wallet, funded)
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: fake)
    assert audit_cli.main(["--timeout", "5", "stamp"]) == 0 and fake.timeout == 5.0
    assert audit_cli.main(["stamp", "--timeout", "7.5"]) == 0 and fake.timeout == 7.5
    capsys.readouterr()
    with pytest.raises(SystemExit):
        audit_cli.main(["help", "snapshot"]) if False else audit_cli.build_parser().parse_args(["--help"])
    assert "quote" in capsys.readouterr().out
    slow = BitcoinCli(["sleep", "5"], timeout=0.2)
    with pytest.raises(RpcError, match="timed out after 0.2s.*--timeout"):
        slow.call("1")


def test_testnet4_and_network_errors(wallet, funded):
    """Finding 19: testnet4 is a test network; no error points at an option that does not exist."""
    from bip322core.wallet import Wallet

    from bip322audit.rpc import network_of

    assert network_of("testnet4") == "test" and network_of("main") == "main" and network_of("signet") == "signet"
    with pytest.raises(RpcError, match="unknown chain"):
        network_of("mars")
    with pytest.raises(RpcError) as caught:
        take_snapshot(FakeCli(wallet, funded, chain="test"), wallet, "x", max_index=20)
    assert "--network" not in str(caught.value) and "chain" in str(caught.value)
    test_wallet = Wallet.from_descriptor(wallet.to_descriptor(), network="test")
    coins = {test_wallet.derive(0).address: [(50_000_000, 990)]}
    node = FakeCli(test_wallet, coins, chain="testnet4")
    snapshot, _ = take_snapshot(node, test_wallet, "x", max_index=20)
    assert snapshot.chain == "testnet4" and snapshot.total_sat == 50_000_000
    assert wallet_from_node(node).network == "test"
    assert holdings(FakeCli(test_wallet, coins, chain="testnet4", rpcwallet=False), list(coins))["total_sat"] == 50_000_000


def test_stamp_hardening(wallet, funded):
    """Finding 20: ASCII digits only, no second stamp line, only the documented placeholders, and a syncing node says so."""
    stamp = Stamp(994, fake_hash(994), iso_utc(T0 + 994 * 600))
    assert parse_stamp(stamp.line()) == stamp
    assert parse_stamp(stamp.line().replace("994", "٩٩٤", 1)) is None  # Arabic-Indic digits
    assert parse_stamp(stamp.line().replace("2023", "٢٠٢٣")) is None
    for template in (f"x\n{Stamp(1, fake_hash(1), stamp.time).line()}", "block: 5", "a\n  Block: later"):
        with pytest.raises(ValueError, match="block:"):
            compose_message(template, stamp)
    assert compose_message("{date} {time} {height} {hash} a } b { c", stamp) == (
        f"{stamp.time[:10]} {stamp.time} 994 {stamp.hash} a }} b {{ c\n{stamp.line()}"
    )
    for template in ("{date.__class__}", "{year}", "{0}", "{}", "{date!r}", "{height:>10}"):
        with pytest.raises(ValueError, match="placeholder"):
            compose_message(template, stamp)

    class Syncing(FakeCli):
        def call(self, method, *params):
            result = super().call(method, *params)
            if method == "getblockchaininfo":
                result.update({"initialblockdownload": True, "headers": 5000})
            return result

    late = Stamp(2000, "ab" * 32, stamp.time)
    assert "still syncing" in check_stamp(Syncing(wallet, funded, tip=1000), late)["error"]
    assert "unknown to this node" in check_stamp(FakeCli(wallet, funded, tip=1000), late)["error"]


def test_writes_are_atomic_and_ledger_reports_skipped_files(bundle, tmp_path, wallet, funded, signer_expressions, monkeypatch, capsys):
    """Finding 22: a failed write leaves the old file; a truncated proofs.json and a mistyped --skip-proven are reported."""
    import bip322audit.snapshot as snapshot_module

    directory = bundle
    fake = FakeCli(wallet, funded, tip=1200)
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: fake)
    assert audit_cli.main(["finalize", str(directory), "--offline"]) == 0
    good = (directory / "proofs.json").read_text()
    report_path = tmp_path / "report.json"
    report_path.write_text("OLD")
    snap_before = (directory / "snapshot.json").read_text()

    def fail(src, dst):
        raise OSError(28, "No space left on device", str(dst))

    with monkeypatch.context() as m:
        m.setattr(snapshot_module.os, "replace", fail)
        assert audit_cli.main(["finalize", str(directory), "--offline"]) == 2
        assert audit_cli.main(["verify", str(directory), "--report", str(report_path)]) == 2
        snapshot, psbts = take_snapshot(FakeCli(wallet, funded), wallet, "x", max_index=20)
        with pytest.raises(OSError):
            write_bundle(directory, snapshot, psbts)
    assert (directory / "proofs.json").read_text() == good and report_path.read_text() == "OLD"
    assert (directory / "snapshot.json").read_text() == snap_before
    assert not [p.name for p in directory.iterdir() if p.name.endswith(".tmp")] and not list(tmp_path.glob(".*.tmp"))
    capsys.readouterr()
    # the ledger
    ledger = tmp_path / "ledger"
    (ledger / "a").mkdir(parents=True)
    (ledger / "a" / "proofs.json").write_text(good)
    (ledger / "b").mkdir()
    (ledger / "b" / "proofs.json").write_text(good[: len(good) // 2])
    skipped = []
    assert len(find_proofs([ledger], skipped=skipped)) == 1 and proven_addresses([ledger], skipped=skipped) == set(funded)
    assert [p for p, _ in skipped] == [ledger / "b" / "proofs.json"] * 2
    later = FakeCli(wallet, {**funded, wallet.derive(3).address: [(7_000_000, 1010)]}, tip=1020)
    monkeypatch.setattr(audit_cli, "BitcoinCli", lambda command: later)
    assert audit_cli.main(["-w", "watch", "snapshot", "--max-index", "20", "--skip-proven", str(ledger), "-o", str(tmp_path / "n1")]) == 0
    err = capsys.readouterr().err
    assert "warning" in err and str(ledger / "b" / "proofs.json") in err
    monkeypatch.chdir(tmp_path)
    assert audit_cli.main(["-w", "watch", "snapshot", "--max-index", "20", "--skip-proven", str(tmp_path / "ledgre")]) == 2
    assert "does not exist" in capsys.readouterr().err and not list(tmp_path.glob("snapshot-*"))


def test_walkthrough_never_removes_a_given_directory(tmp_path):
    """Finding 21: the example script refuses an existing non-empty directory instead of deleting it."""
    keep = tmp_path / "precious"
    keep.mkdir()
    (keep / "file.txt").write_text("keep me")
    script = (ROOT / "examples" / "audit_walkthrough.sh").read_text()
    assert 'rm -rf "$WORK"' not in script and "rm -rf $WORK" not in script
    proc = subprocess.run(["bash", str(ROOT / "examples" / "audit_walkthrough.sh"), str(keep)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and "not empty" in proc.stderr and (keep / "file.txt").read_text() == "keep me"


def test_docs_name_every_command_module_and_exit_code():
    """Finding 24: README and DESIGN describe what the code has."""
    readme, design = (ROOT / "README.md").read_text(), (ROOT / "docs" / "DESIGN.md").read_text()
    for module in sorted(p.name for p in (ROOT / "bip322audit").glob("*.py") if not p.name.startswith("_")):
        assert f"bip322audit/{module}" in readme, module
    for word in ("prove", "holdings", "ledger", "INCOMPLETE", "Exit codes"):
        assert word in readme and word in design, word
    assert design.count("never imports") == 1  # the paragraph that said it twice
    assert "prove" in audit_cli.__doc__ and "holdings" in audit_cli.__doc__
