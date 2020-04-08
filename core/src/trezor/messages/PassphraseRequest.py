# Automatically generated by pb2py
# fmt: off
import protobuf as p

if __debug__:
    try:
        from typing import Dict, List  # noqa: F401
        from typing_extensions import Literal  # noqa: F401
    except ImportError:
        pass


class PassphraseRequest(p.MessageType):
    MESSAGE_WIRE_TYPE = 41

    def __init__(
        self,
        *,
        _on_device: bool = None,
    ) -> None:
        self._on_device = _on_device

    @classmethod
    def get_fields(cls) -> Dict:
        return {
            1: ('_on_device', p.BoolType, 0),
        }
