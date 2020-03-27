import gc
from micropython import const

from trezor import utils
from trezor.crypto import base58, bip32, cashaddr, der
from trezor.crypto.curve import secp256k1
from trezor.crypto.hashlib import sha256
from trezor.messages import FailureType, InputScriptType, OutputScriptType
from trezor.messages.SignTx import SignTx
from trezor.messages.TxInputType import TxInputType
from trezor.messages.TxOutputBinType import TxOutputBinType
from trezor.messages.TxOutputType import TxOutputType
from trezor.messages.TxRequest import TxRequest
from trezor.messages.TxRequestDetailsType import TxRequestDetailsType
from trezor.messages.TxRequestSerializedType import TxRequestSerializedType

from apps.common import address_type, coininfo, seed
from apps.wallet.sign_tx import (
    addresses,
    helpers,
    multisig,
    progress,
    scripts,
    segwit_bip143,
    tx_weight,
    writers,
)

if not utils.BITCOIN_ONLY:
    from apps.wallet.sign_tx import zcash

# the number of bip32 levels used in a wallet (chain and address)
_BIP32_WALLET_DEPTH = const(2)

# the chain id used for change
_BIP32_CHANGE_CHAIN = const(1)

# the maximum allowed change address.  this should be large enough for normal
# use and still allow to quickly brute-force the correct bip32 path
_BIP32_MAX_LAST_ELEMENT = const(1000000)


class SigningError(ValueError):
    pass


# Transaction signing
# ===
# see https://github.com/trezor/trezor-mcu/blob/master/firmware/signing.c#L84
# for pseudo code overview
# ===


class Bitcoin:
    async def signer(
        self, tx: SignTx, keychain: seed.Keychain, coin: coininfo.CoinInfo
    ):
        self.initialize(tx, keychain, coin)

        progress.init(self.tx.inputs_count, self.tx.outputs_count)

        # Phase 1
        # - check inputs, previous transactions, and outputs
        # - ask for confirmations
        # - check fee
        await self.phase1()

        # Phase 2
        # - sign inputs
        # - check that nothing changed
        await self.phase2()

    def initialize(self, tx: SignTx, keychain: seed.Keychain, coin: coininfo.CoinInfo):
        self.coin = coin
        self.tx = helpers.sanitize_sign_tx(tx, self.coin)
        self.keychain = keychain

        self.multisig_fp = (
            multisig.MultisigFingerprint()
        )  # control checksum of multisig inputs
        self.wallet_path = []  # common prefix of input paths
        self.bip143_in = 0  # sum of segwit input amounts
        self.segwit = {}  # dict of booleans stating if input is segwit
        self.total_in = 0  # sum of input amounts
        self.total_out = 0  # sum of output amounts
        self.change_out = 0  # change output amount

        self.tx_req = TxRequest()
        self.tx_req.details = TxRequestDetailsType()

        # h_first is used to make sure the inputs and outputs streamed in Phase 1
        # are the same as in Phase 2 when signing legacy inputs.  it is thus not required to fully hash the
        # tx, as the SignTx info is streamed only once
        self.h_first = utils.HashWriter(sha256())  # not a real tx hash

        self.init_hash143()

    def init_hash143(self):
        if not utils.BITCOIN_ONLY and self.coin.overwintered:
            if self.tx.version == 3:
                branch_id = self.tx.branch_id or 0x5BA81B19  # Overwinter
                self.hash143 = zcash.Zip143(branch_id)  # ZIP-0143 transaction hashing
            elif self.tx.version == 4:
                branch_id = self.tx.branch_id or 0x76B809BB  # Sapling
                self.hash143 = zcash.Zip243(branch_id)  # ZIP-0243 transaction hashing
            else:
                raise SigningError(
                    FailureType.DataError,
                    "Unsupported version for overwintered transaction",
                )
        else:
            self.hash143 = segwit_bip143.Bip143()  # BIP-0143 transaction hashing

    async def phase1(self):
        weight = tx_weight.TxWeightCalculator(
            self.tx.inputs_count, self.tx.outputs_count
        )

        # compute sum of input amounts (total_in)
        # add inputs to hash143 and h_first
        for i in range(self.tx.inputs_count):
            # STAGE_REQUEST_1_INPUT
            progress.advance()
            txi = await helpers.request_tx_input(self.tx_req, i, self.coin)
            weight.add_input(txi)
            await self.phase1_process_input(i, txi)

        txo_bin = TxOutputBinType()
        for i in range(self.tx.outputs_count):
            # STAGE_REQUEST_3_OUTPUT
            txo = await helpers.request_tx_output(self.tx_req, i, self.coin)
            txo_bin.amount = txo.amount
            txo_bin.script_pubkey = self.output_derive_script(txo)
            weight.add_output(txo_bin.script_pubkey)
            await self.phase1_confirm_output(i, txo, txo_bin)

        fee = self.total_in - self.total_out

        if not utils.BITCOIN_ONLY and self.coin.negative_fee:
            pass  # bypass check for negative fee coins, required for reward TX
        else:
            if fee < 0:
                raise SigningError(FailureType.NotEnoughFunds, "Not enough funds")

        # fee > (coin.maxfee per byte * tx size)
        if fee > (self.coin.maxfee_kb / 1000) * (weight.get_total() / 4):
            if not await helpers.confirm_feeoverthreshold(fee, self.coin):
                raise SigningError(FailureType.ActionCancelled, "Signing cancelled")

        if self.tx.lock_time > 0:
            if not await helpers.confirm_nondefault_locktime(self.tx.lock_time):
                raise SigningError(FailureType.ActionCancelled, "Locktime cancelled")

        if not await helpers.confirm_total(
            self.total_in - self.change_out, fee, self.coin
        ):
            raise SigningError(FailureType.ActionCancelled, "Total cancelled")

    async def phase1_process_input(self, i: int, txi: TxInputType):
        self.wallet_path = input_extract_wallet_path(txi, self.wallet_path)
        writers.write_tx_input_check(self.h_first, txi)
        self.hash143.add_prevouts(txi)  # all inputs are included (non-segwit as well)
        self.hash143.add_sequence(txi)

        if not addresses.validate_full_path(txi.address_n, self.coin, txi.script_type):
            await helpers.confirm_foreign_address(txi.address_n)

        if txi.multisig:
            self.multisig_fp.add(txi.multisig)
        else:
            self.multisig_fp.mismatch = True

        if txi.script_type in (
            InputScriptType.SPENDWITNESS,
            InputScriptType.SPENDP2SHWITNESS,
        ):
            if not self.coin.segwit:
                raise SigningError(
                    FailureType.DataError, "Segwit not enabled on this coin"
                )
            if not txi.amount:
                raise SigningError(FailureType.DataError, "Segwit input without amount")
            self.segwit[i] = True
            self.bip143_in += txi.amount
            self.total_in += txi.amount
        elif txi.script_type in (
            InputScriptType.SPENDADDRESS,
            InputScriptType.SPENDMULTISIG,
        ):
            if not utils.BITCOIN_ONLY and (
                self.coin.force_bip143 or self.coin.overwintered
            ):
                if not txi.amount:
                    raise SigningError(
                        FailureType.DataError, "Expected input with amount"
                    )
                self.segwit[i] = False
                self.bip143_in += txi.amount
                self.total_in += txi.amount
            else:
                self.segwit[i] = False
                self.total_in += await self.get_prevtx_output_value(
                    txi.prev_hash, txi.prev_index
                )
        else:
            raise SigningError(FailureType.DataError, "Wrong input script type")

    async def phase1_confirm_output(
        self, i: int, txo: TxOutputType, txo_bin: TxOutputBinType
    ):
        if self.change_out == 0 and self.output_is_change(txo):
            # output is change and does not need confirmation
            self.change_out = txo.amount
        elif not await helpers.confirm_output(txo, self.coin):
            raise SigningError(FailureType.ActionCancelled, "Output cancelled")

        writers.write_tx_output(self.h_first, txo_bin)
        self.hash143.add_output(txo_bin)
        self.total_out += txo_bin.amount

    async def phase2(self):
        self.tx_req.serialized = None

        # Serialize inputs and sign non-segwit inputs.
        for i in range(self.tx.inputs_count):
            progress.advance()
            if self.segwit[i]:
                await self.phase2_serialize_segwit_input(i)
            elif not utils.BITCOIN_ONLY and (
                self.coin.force_bip143 or self.coin.overwintered
            ):
                await self.phase2_sign_bip143_input(i)
            else:
                await self.phase2_sign_legacy_input(i)

        # Serialize outputs.
        tx_ser = TxRequestSerializedType()
        for i in range(self.tx.outputs_count):
            # STAGE_REQUEST_5_OUTPUT
            progress.advance()
            tx_ser.serialized_tx = await self.phase2_serialize_output(i)
            self.tx_req.serialized = tx_ser

        # Sign segwit inputs.
        any_segwit = True in self.segwit.values()
        for i in range(self.tx.inputs_count):
            progress.advance()
            if self.segwit[i]:
                # STAGE_REQUEST_SEGWIT_WITNESS
                witness, signature = await self.phase2_sign_segwit_input(i)
                tx_ser.serialized_tx = witness
                tx_ser.signature_index = i
                tx_ser.signature = signature
            elif any_segwit:
                tx_ser.serialized_tx += bytearray(
                    1
                )  # empty witness for non-segwit inputs
                tx_ser.signature_index = None
                tx_ser.signature = None

            self.tx_req.serialized = tx_ser

        writers.write_uint32(tx_ser.serialized_tx, self.tx.lock_time)

        if not utils.BITCOIN_ONLY and self.coin.overwintered:
            if self.tx.version == 3:
                writers.write_uint32(
                    tx_ser.serialized_tx, self.tx.expiry
                )  # expiryHeight
                writers.write_varint(tx_ser.serialized_tx, 0)  # nJoinSplit
            elif self.tx.version == 4:
                writers.write_uint32(
                    tx_ser.serialized_tx, self.tx.expiry
                )  # expiryHeight
                writers.write_uint64(tx_ser.serialized_tx, 0)  # valueBalance
                writers.write_varint(tx_ser.serialized_tx, 0)  # nShieldedSpend
                writers.write_varint(tx_ser.serialized_tx, 0)  # nShieldedOutput
                writers.write_varint(tx_ser.serialized_tx, 0)  # nJoinSplit
            else:
                raise SigningError(
                    FailureType.DataError,
                    "Unsupported version for overwintered transaction",
                )

        await helpers.request_tx_finish(self.tx_req)

    async def phase2_serialize_segwit_input(self, i_sign):
        # STAGE_REQUEST_SEGWIT_INPUT
        txi_sign = await helpers.request_tx_input(self.tx_req, i_sign, self.coin)

        if not input_is_segwit(txi_sign):
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )
        input_check_wallet_path(txi_sign, self.wallet_path)
        # NOTE: No need to check the multisig fingerprint, because we won't be signing
        # the script here. Signatures are produced in STAGE_REQUEST_SEGWIT_WITNESS.

        key_sign = self.keychain.derive(txi_sign.address_n, self.coin.curve_name)
        key_sign_pub = key_sign.public_key()
        txi_sign.script_sig = self.input_derive_script(txi_sign, key_sign_pub)

        w_txi = writers.empty_bytearray(
            7 + len(txi_sign.prev_hash) + 4 + len(txi_sign.script_sig) + 4
        )
        if i_sign == 0:  # serializing first input => prepend headers
            self.write_tx_header(w_txi)
        writers.write_tx_input(w_txi, txi_sign)
        self.tx_req.serialized = TxRequestSerializedType(serialized_tx=w_txi)

    async def phase2_sign_segwit_input(self, i):
        txi = await helpers.request_tx_input(self.tx_req, i, self.coin)

        input_check_wallet_path(txi, self.wallet_path)
        input_check_multisig_fingerprint(txi, self.multisig_fp)

        if not input_is_segwit(txi) or txi.amount > self.bip143_in:
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )
        self.bip143_in -= txi.amount

        key_sign = self.keychain.derive(txi.address_n, self.coin.curve_name)
        key_sign_pub = key_sign.public_key()
        hash143_hash = self.hash143.preimage_hash(
            self.coin,
            self.tx,
            txi,
            addresses.ecdsa_hash_pubkey(key_sign_pub, self.coin),
            self.get_hash_type(),
        )

        signature = ecdsa_sign(key_sign, hash143_hash)
        if txi.multisig:
            # find out place of our signature based on the pubkey
            signature_index = multisig.multisig_pubkey_index(txi.multisig, key_sign_pub)
            witness = scripts.witness_p2wsh(
                txi.multisig, signature, signature_index, self.get_hash_type()
            )
        else:
            witness = scripts.witness_p2wpkh(
                signature, key_sign_pub, self.get_hash_type()
            )

        return witness, signature

    async def phase2_sign_bip143_input(self, i_sign):
        # STAGE_REQUEST_SEGWIT_INPUT
        txi_sign = await helpers.request_tx_input(self.tx_req, i_sign, self.coin)
        input_check_wallet_path(txi_sign, self.wallet_path)
        input_check_multisig_fingerprint(txi_sign, self.multisig_fp)

        is_bip143 = (
            txi_sign.script_type == InputScriptType.SPENDADDRESS
            or txi_sign.script_type == InputScriptType.SPENDMULTISIG
        )
        if not is_bip143 or txi_sign.amount > self.bip143_in:
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )
        self.bip143_in -= txi_sign.amount

        key_sign = self.keychain.derive(txi_sign.address_n, self.coin.curve_name)
        key_sign_pub = key_sign.public_key()
        self.hash143_hash = self.hash143.preimage_hash(
            self.coin,
            self.tx,
            txi_sign,
            addresses.ecdsa_hash_pubkey(key_sign_pub, self.coin),
            self.get_hash_type(),
        )

        # if multisig, check if signing with a key that is included in multisig
        if txi_sign.multisig:
            multisig.multisig_pubkey_index(txi_sign.multisig, key_sign_pub)

        signature = ecdsa_sign(key_sign, self.hash143_hash)

        # serialize input with correct signature
        gc.collect()
        txi_sign.script_sig = self.input_derive_script(
            txi_sign, key_sign_pub, signature
        )
        w_txi_sign = writers.empty_bytearray(
            5 + len(txi_sign.prev_hash) + 4 + len(txi_sign.script_sig) + 4
        )
        if i_sign == 0:  # serializing first input => prepend headers
            self.write_tx_header(w_txi_sign)
        writers.write_tx_input(w_txi_sign, txi_sign)
        self.tx_req.serialized = TxRequestSerializedType(i_sign, signature, w_txi_sign)

    async def phase2_sign_legacy_input(self, i_sign):
        # hash of what we are signing with this input
        h_sign = utils.HashWriter(sha256())
        # same as h_first, checked before signing the digest
        h_second = utils.HashWriter(sha256())

        writers.write_uint32(h_sign, self.tx.version)  # nVersion
        if not utils.BITCOIN_ONLY and self.coin.timestamp:
            writers.write_uint32(h_sign, self.tx.timestamp)

        writers.write_varint(h_sign, self.tx.inputs_count)

        for i in range(self.tx.inputs_count):
            # STAGE_REQUEST_4_INPUT
            txi = await helpers.request_tx_input(self.tx_req, i, self.coin)
            input_check_wallet_path(txi, self.wallet_path)
            writers.write_tx_input_check(h_second, txi)
            if i == i_sign:
                txi_sign = txi
                input_check_multisig_fingerprint(txi_sign, self.multisig_fp)
                key_sign = self.keychain.derive(txi.address_n, self.coin.curve_name)
                key_sign_pub = key_sign.public_key()
                # for the signing process the script_sig is equal
                # to the previous tx's scriptPubKey (P2PKH) or a redeem script (P2SH)
                if txi_sign.script_type == InputScriptType.SPENDMULTISIG:
                    txi_sign.script_sig = scripts.output_script_multisig(
                        multisig.multisig_get_pubkeys(txi_sign.multisig),
                        txi_sign.multisig.m,
                    )
                elif txi_sign.script_type == InputScriptType.SPENDADDRESS:
                    txi_sign.script_sig = scripts.output_script_p2pkh(
                        addresses.ecdsa_hash_pubkey(key_sign_pub, self.coin)
                    )
                else:
                    raise SigningError(
                        FailureType.ProcessError, "Unknown transaction type"
                    )
            else:
                txi.script_sig = bytes()
            writers.write_tx_input(h_sign, txi)

        writers.write_varint(h_sign, self.tx.outputs_count)

        txo_bin = TxOutputBinType()
        for i in range(self.tx.outputs_count):
            # STAGE_REQUEST_4_OUTPUT
            txo = await helpers.request_tx_output(self.tx_req, i, self.coin)
            txo_bin.amount = txo.amount
            txo_bin.script_pubkey = self.output_derive_script(txo)
            writers.write_tx_output(h_second, txo_bin)
            writers.write_tx_output(h_sign, txo_bin)

        writers.write_uint32(h_sign, self.tx.lock_time)
        writers.write_uint32(h_sign, self.get_hash_type())

        # check the control digests
        if writers.get_tx_hash(self.h_first, False) != writers.get_tx_hash(h_second):
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )

        # if multisig, check if signing with a key that is included in multisig
        if txi_sign.multisig:
            multisig.multisig_pubkey_index(txi_sign.multisig, key_sign_pub)

        # compute the signature from the tx digest
        signature = ecdsa_sign(
            key_sign, writers.get_tx_hash(h_sign, double=self.coin.sign_hash_double)
        )

        # serialize input wittx_reqh correct signature
        gc.collect()
        txi_sign.script_sig = self.input_derive_script(
            txi_sign, key_sign_pub, signature
        )
        w_txi_sign = writers.empty_bytearray(
            5 + len(txi_sign.prev_hash) + 4 + len(txi_sign.script_sig) + 4
        )
        if i_sign == 0:  # serializing first input => prepend headers
            self.write_tx_header(w_txi_sign)
        writers.write_tx_input(w_txi_sign, txi_sign)
        self.tx_req.serialized = TxRequestSerializedType(i_sign, signature, w_txi_sign)

    async def phase2_serialize_output(self, i: int):
        txo = await helpers.request_tx_output(self.tx_req, i, self.coin)
        txo_bin = TxOutputBinType()
        txo_bin.amount = txo.amount
        txo_bin.script_pubkey = self.output_derive_script(txo)

        # serialize output
        w_txo_bin = writers.empty_bytearray(5 + 8 + 5 + len(txo_bin.script_pubkey) + 4)
        if i == 0:  # serializing first output => prepend outputs count
            writers.write_varint(w_txo_bin, self.tx.outputs_count)
        writers.write_tx_output(w_txo_bin, txo_bin)

        return w_txo_bin

    async def get_prevtx_output_value(self, prev_hash: bytes, prev_index: int) -> int:
        total_out = 0  # sum of output amounts

        # STAGE_REQUEST_2_PREV_META
        tx = await helpers.request_tx_meta(self.tx_req, self.coin, prev_hash)

        if tx.outputs_cnt <= prev_index:
            raise SigningError(
                FailureType.ProcessError, "Not enough outputs in previous transaction."
            )

        txh = utils.HashWriter(sha256())

        if not utils.BITCOIN_ONLY and self.coin.overwintered:
            writers.write_uint32(
                txh, tx.version | zcash.OVERWINTERED
            )  # nVersion | fOverwintered
            writers.write_uint32(txh, tx.version_group_id)  # nVersionGroupId
        else:
            writers.write_uint32(txh, tx.version)  # nVersion
            if not utils.BITCOIN_ONLY and self.coin.timestamp:
                writers.write_uint32(txh, tx.timestamp)

        writers.write_varint(txh, tx.inputs_cnt)

        for i in range(tx.inputs_cnt):
            # STAGE_REQUEST_2_PREV_INPUT
            txi = await helpers.request_tx_input(self.tx_req, i, self.coin, prev_hash)
            writers.write_tx_input(txh, txi)

        writers.write_varint(txh, tx.outputs_cnt)

        for o in range(tx.outputs_cnt):
            # STAGE_REQUEST_2_PREV_OUTPUT
            txo_bin = await helpers.request_tx_output(
                self.tx_req, o, self.coin, prev_hash
            )
            writers.write_tx_output(txh, txo_bin)
            if o == prev_index:
                total_out += txo_bin.amount

        writers.write_uint32(txh, tx.lock_time)

        if not utils.BITCOIN_ONLY and self.coin.extra_data:
            ofs = 0
            while ofs < tx.extra_data_len:
                size = min(1024, tx.extra_data_len - ofs)
                data = await helpers.request_tx_extra_data(
                    self.tx_req, ofs, size, prev_hash
                )
                writers.write_bytes_unchecked(txh, data)
                ofs += len(data)

        if (
            writers.get_tx_hash(txh, double=self.coin.sign_hash_double, reverse=True)
            != prev_hash
        ):
            raise SigningError(
                FailureType.ProcessError, "Encountered invalid prev_hash"
            )

        return total_out

    # TX Helpers
    # ===

    def get_hash_type(self) -> int:
        SIGHASH_FORKID = const(0x40)
        SIGHASH_ALL = const(0x01)
        hashtype = SIGHASH_ALL
        if self.coin.fork_id is not None:
            hashtype |= (self.coin.fork_id << 8) | SIGHASH_FORKID
        return hashtype

    def write_tx_header(self, w: writers.Writer) -> None:
        if not utils.BITCOIN_ONLY and self.coin.overwintered:
            # nVersion | fOverwintered
            writers.write_uint32(w, self.tx.version | zcash.OVERWINTERED)
            writers.write_uint32(w, self.tx.version_group_id)  # nVersionGroupId
        else:
            writers.write_uint32(w, self.tx.version)  # nVersion
            if not utils.BITCOIN_ONLY and self.coin.timestamp:
                writers.write_uint32(w, self.tx.timestamp)
        if True in self.segwit.values():
            writers.write_varint(w, 0x00)  # segwit witness marker
            writers.write_varint(w, 0x01)  # segwit witness flag
        writers.write_varint(w, self.tx.inputs_count)

    # TX Outputs
    # ===

    def output_derive_script(self, o: TxOutputType) -> bytes:
        if o.script_type == OutputScriptType.PAYTOOPRETURN:
            return scripts.output_script_paytoopreturn(o.op_return_data)

        if o.address_n:
            # change output
            o.address = self.get_address_for_change(o)

        if self.coin.bech32_prefix and o.address.startswith(self.coin.bech32_prefix):
            # p2wpkh or p2wsh
            witprog = addresses.decode_bech32_address(
                self.coin.bech32_prefix, o.address
            )
            return scripts.output_script_native_p2wpkh_or_p2wsh(witprog)

        if (
            not utils.BITCOIN_ONLY
            and self.coin.cashaddr_prefix is not None
            and o.address.startswith(self.coin.cashaddr_prefix + ":")
        ):
            prefix, addr = o.address.split(":")
            version, data = cashaddr.decode(prefix, addr)
            if version == cashaddr.ADDRESS_TYPE_P2KH:
                version = self.coin.address_type
            elif version == cashaddr.ADDRESS_TYPE_P2SH:
                version = self.coin.address_type_p2sh
            else:
                raise SigningError("Unknown cashaddr address type")
            raw_address = bytes([version]) + data
        else:
            try:
                raw_address = base58.decode_check(o.address, self.coin.b58_hash)
            except ValueError:
                raise SigningError(FailureType.DataError, "Invalid address")

        if address_type.check(self.coin.address_type, raw_address):
            # p2pkh
            pubkeyhash = address_type.strip(self.coin.address_type, raw_address)
            script = scripts.output_script_p2pkh(pubkeyhash)
            return script

        elif address_type.check(self.coin.address_type_p2sh, raw_address):
            # p2sh
            scripthash = address_type.strip(self.coin.address_type_p2sh, raw_address)
            script = scripts.output_script_p2sh(scripthash)
            return script

        raise SigningError(FailureType.DataError, "Invalid address type")

    def get_address_for_change(self, o: TxOutputType):
        try:
            input_script_type = helpers.CHANGE_OUTPUT_TO_INPUT_SCRIPT_TYPES[
                o.script_type
            ]
        except KeyError:
            raise SigningError(FailureType.DataError, "Invalid script type")
        node = self.keychain.derive(o.address_n, self.coin.curve_name)
        return addresses.get_address(input_script_type, self.coin, node, o.multisig)

    def output_is_change(self, o: TxOutputType) -> bool:
        if o.script_type not in helpers.CHANGE_OUTPUT_SCRIPT_TYPES:
            return False
        if o.multisig and not self.multisig_fp.matches(o.multisig):
            return False
        return (
            self.wallet_path is not None
            and self.wallet_path == o.address_n[:-_BIP32_WALLET_DEPTH]
            and o.address_n[-2] <= _BIP32_CHANGE_CHAIN
            and o.address_n[-1] <= _BIP32_MAX_LAST_ELEMENT
        )

    # Tx Inputs
    # ===

    def input_derive_script(
        self, i: TxInputType, pubkey: bytes, signature: bytes = None
    ) -> bytes:
        if i.script_type == InputScriptType.SPENDADDRESS:
            # p2pkh or p2sh
            return scripts.input_script_p2pkh_or_p2sh(
                pubkey, signature, self.get_hash_type()
            )

        if i.script_type == InputScriptType.SPENDP2SHWITNESS:
            # p2wpkh or p2wsh using p2sh

            if i.multisig:
                # p2wsh in p2sh
                pubkeys = multisig.multisig_get_pubkeys(i.multisig)
                witness_script_hasher = utils.HashWriter(sha256())
                scripts.output_script_multisig(
                    pubkeys, i.multisig.m, witness_script_hasher
                )
                witness_script_hash = witness_script_hasher.get_digest()
                return scripts.input_script_p2wsh_in_p2sh(witness_script_hash)

            # p2wpkh in p2sh
            return scripts.input_script_p2wpkh_in_p2sh(
                addresses.ecdsa_hash_pubkey(pubkey, self.coin)
            )
        elif i.script_type == InputScriptType.SPENDWITNESS:
            # native p2wpkh or p2wsh
            return scripts.input_script_native_p2wpkh_or_p2wsh()
        elif i.script_type == InputScriptType.SPENDMULTISIG:
            # p2sh multisig
            signature_index = multisig.multisig_pubkey_index(i.multisig, pubkey)
            return scripts.input_script_multisig(
                i.multisig, signature, signature_index, self.get_hash_type(), self.coin
            )
        else:
            raise SigningError(FailureType.ProcessError, "Invalid script type")


def input_is_segwit(i: TxInputType) -> bool:
    return (
        i.script_type == InputScriptType.SPENDWITNESS
        or i.script_type == InputScriptType.SPENDP2SHWITNESS
    )


def input_extract_wallet_path(txi: TxInputType, wallet_path: list) -> list:
    if wallet_path is None:
        return None  # there was a mismatch in previous inputs
    address_n = txi.address_n[:-_BIP32_WALLET_DEPTH]
    if not address_n:
        return None  # input path is too short
    if not wallet_path:
        return address_n  # this is the first input
    if wallet_path == address_n:
        return address_n  # paths match
    return None  # paths don't match


def input_check_wallet_path(txi: TxInputType, wallet_path: list) -> list:
    if wallet_path is None:
        return  # there was a mismatch in Phase 1, ignore it now
    address_n = txi.address_n[:-_BIP32_WALLET_DEPTH]
    if wallet_path != address_n:
        raise SigningError(
            FailureType.ProcessError, "Transaction has changed during signing"
        )


def input_check_multisig_fingerprint(
    txi: TxInputType, multisig_fp: multisig.MultisigFingerprint
) -> None:
    if multisig_fp.mismatch is False:
        # All inputs in Phase 1 had matching multisig fingerprints, allowing a multisig change-output.
        if not txi.multisig or not multisig_fp.matches(txi.multisig):
            # This input no longer has a matching multisig fingerprint.
            raise SigningError(
                FailureType.ProcessError, "Transaction has changed during signing"
            )


def ecdsa_sign(node: bip32.HDNode, digest: bytes) -> bytes:
    sig = secp256k1.sign(node.private_key(), digest)
    sigder = der.encode_seq((sig[1:33], sig[33:65]))
    return sigder
