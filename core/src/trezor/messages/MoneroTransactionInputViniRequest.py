# Automatically generated by pb2py
# fmt: off
import protobuf as p

from .MoneroTransactionSourceEntry import MoneroTransactionSourceEntry

if __debug__:
    try:
        from typing import Dict, List  # noqa: F401
        from typing_extensions import Literal  # noqa: F401
    except ImportError:
        pass


class MoneroTransactionInputViniRequest(p.MessageType):
    MESSAGE_WIRE_TYPE = 507

    def __init__(
        self,
        *,
        src_entr: MoneroTransactionSourceEntry = None,
        vini: bytes = None,
        vini_hmac: bytes = None,
        pseudo_out: bytes = None,
        pseudo_out_hmac: bytes = None,
        orig_idx: int = None,
    ) -> None:
        self.src_entr = src_entr
        self.vini = vini
        self.vini_hmac = vini_hmac
        self.pseudo_out = pseudo_out
        self.pseudo_out_hmac = pseudo_out_hmac
        self.orig_idx = orig_idx

    @classmethod
    def get_fields(cls) -> Dict:
        return {
            1: ('src_entr', MoneroTransactionSourceEntry, 0),
            2: ('vini', p.BytesType, 0),
            3: ('vini_hmac', p.BytesType, 0),
            4: ('pseudo_out', p.BytesType, 0),
            5: ('pseudo_out_hmac', p.BytesType, 0),
            6: ('orig_idx', p.UVarintType, 0),
        }
