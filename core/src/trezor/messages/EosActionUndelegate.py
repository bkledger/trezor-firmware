# Automatically generated by pb2py
# fmt: off
import protobuf as p

from .EosAsset import EosAsset

if __debug__:
    try:
        from typing import Dict, List  # noqa: F401
        from typing_extensions import Literal  # noqa: F401
    except ImportError:
        pass


class EosActionUndelegate(p.MessageType):

    def __init__(
        self,
        *,
        sender: int = None,
        receiver: int = None,
        net_quantity: EosAsset = None,
        cpu_quantity: EosAsset = None,
    ) -> None:
        self.sender = sender
        self.receiver = receiver
        self.net_quantity = net_quantity
        self.cpu_quantity = cpu_quantity

    @classmethod
    def get_fields(cls) -> Dict:
        return {
            1: ('sender', p.UVarintType, 0),
            2: ('receiver', p.UVarintType, 0),
            3: ('net_quantity', EosAsset, 0),
            4: ('cpu_quantity', EosAsset, 0),
        }
