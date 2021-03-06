# Automatically generated by pb2py
# fmt: off
from .. import protobuf as p

if __debug__:
    try:
        from typing import Dict, List  # noqa: F401
        from typing_extensions import Literal  # noqa: F401
    except ImportError:
        pass


class MoneroGetTxKeyAck(p.MessageType):
    MESSAGE_WIRE_TYPE = 551

    def __init__(
        self,
        salt: bytes = None,
        tx_keys: bytes = None,
        tx_derivations: bytes = None,
    ) -> None:
        self.salt = salt
        self.tx_keys = tx_keys
        self.tx_derivations = tx_derivations

    @classmethod
    def get_fields(cls) -> Dict:
        return {
            1: ('salt', p.BytesType, 0),
            2: ('tx_keys', p.BytesType, 0),
            3: ('tx_derivations', p.BytesType, 0),
        }
