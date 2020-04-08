# Automatically generated by pb2py
# fmt: off
from .. import protobuf as p

if __debug__:
    try:
        from typing import Dict, List  # noqa: F401
        from typing_extensions import Literal  # noqa: F401
    except ImportError:
        pass


class MoneroLiveRefreshStepRequest(p.MessageType):
    MESSAGE_WIRE_TYPE = 554

    def __init__(
        self,
        *,
        out_key: bytes = None,
        recv_deriv: bytes = None,
        real_out_idx: int = None,
        sub_addr_major: int = None,
        sub_addr_minor: int = None,
    ) -> None:
        self.out_key = out_key
        self.recv_deriv = recv_deriv
        self.real_out_idx = real_out_idx
        self.sub_addr_major = sub_addr_major
        self.sub_addr_minor = sub_addr_minor

    @classmethod
    def get_fields(cls) -> Dict:
        return {
            1: ('out_key', p.BytesType, 0),
            2: ('recv_deriv', p.BytesType, 0),
            3: ('real_out_idx', p.UVarintType, 0),
            4: ('sub_addr_major', p.UVarintType, 0),
            5: ('sub_addr_minor', p.UVarintType, 0),
        }
