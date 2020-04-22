from trezor.crypto.hashlib import sha256
from trezor.messages import FailureType, InputScriptType
from trezor.messages.SignTx import SignTx
from trezor.messages.TxInputType import TxInputType
from trezor.messages.TxOutputBinType import TxOutputBinType
from trezor.utils import HashWriter, ensure

from apps.common.coininfo import CoinInfo
from apps.wallet.sign_tx.multisig import multisig_get_pubkeys
from apps.wallet.sign_tx.scripts import output_script_multisig, output_script_p2pkh
from apps.wallet.sign_tx.writers import (
    TX_HASH_SIZE,
    get_tx_hash,
    write_bytes_fixed,
    write_bytes_prefixed,
    write_bytes_reversed,
    write_tx_output,
    write_uint32,
    write_uint64,
)


class Bip143Error(ValueError):
    pass


class Bip143:
    def __init__(self) -> None:
        self.h_prevouts = HashWriter(sha256())
        self.h_sequence = HashWriter(sha256())
        self.h_outputs = HashWriter(sha256())

    def add_input(self, txi: TxInputType) -> None:
        write_bytes_reversed(self.h_prevouts, txi.prev_hash, TX_HASH_SIZE)
        write_uint32(self.h_prevouts, txi.prev_index)
        write_uint32(self.h_sequence, txi.sequence)

    def add_output_count(self, tx: SignTx) -> None:
        pass

    def add_output(self, txo_bin: TxOutputBinType) -> None:
        write_tx_output(self.h_outputs, txo_bin)

    def add_locktime_expiry(self, tx: SignTx) -> None:
        pass

    def get_prevouts_hash(self, coin: CoinInfo) -> bytes:
        return get_tx_hash(self.h_prevouts, double=coin.sign_hash_double)

    def get_sequence_hash(self, coin: CoinInfo) -> bytes:
        return get_tx_hash(self.h_sequence, double=coin.sign_hash_double)

    def get_outputs_hash(self, coin: CoinInfo) -> bytes:
        return get_tx_hash(self.h_outputs, double=coin.sign_hash_double)

    def get_prefix_hash(self) -> bytes:
        pass

    def preimage_hash(
        self,
        coin: CoinInfo,
        tx: SignTx,
        txi: TxInputType,
        pubkeyhash: bytes,
        sighash: int,
    ) -> bytes:
        h_preimage = HashWriter(sha256())

        ensure(not coin.overwintered)

        write_uint32(h_preimage, tx.version)  # nVersion
        # hashPrevouts
        write_bytes_fixed(h_preimage, self.get_prevouts_hash(coin), TX_HASH_SIZE)
        # hashSequence
        write_bytes_fixed(h_preimage, self.get_sequence_hash(coin), TX_HASH_SIZE)

        write_bytes_reversed(h_preimage, txi.prev_hash, TX_HASH_SIZE)  # outpoint
        write_uint32(h_preimage, txi.prev_index)  # outpoint

        script_code = derive_script_code(txi, pubkeyhash)  # scriptCode
        write_bytes_prefixed(h_preimage, script_code)

        write_uint64(h_preimage, txi.amount)  # amount
        write_uint32(h_preimage, txi.sequence)  # nSequence
        # hashOutputs
        write_bytes_fixed(h_preimage, self.get_outputs_hash(coin), TX_HASH_SIZE)
        write_uint32(h_preimage, tx.lock_time)  # nLockTime
        write_uint32(h_preimage, sighash)  # nHashType

        return get_tx_hash(h_preimage, double=coin.sign_hash_double)


# see https://github.com/bitcoin/bips/blob/master/bip-0143.mediawiki#specification
# item 5 for details
def derive_script_code(txi: TxInputType, pubkeyhash: bytes) -> bytearray:

    if txi.multisig:
        return output_script_multisig(
            multisig_get_pubkeys(txi.multisig), txi.multisig.m
        )

    p2pkh = (
        txi.script_type == InputScriptType.SPENDWITNESS
        or txi.script_type == InputScriptType.SPENDP2SHWITNESS
        or txi.script_type == InputScriptType.SPENDADDRESS
    )
    if p2pkh:
        # for p2wpkh in p2sh or native p2wpkh
        # the scriptCode is a classic p2pkh
        return output_script_p2pkh(pubkeyhash)

    else:
        raise Bip143Error(
            FailureType.DataError, "Unknown input script type for bip143 script code",
        )
