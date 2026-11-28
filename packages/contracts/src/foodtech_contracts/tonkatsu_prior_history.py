"""Private fryer ordering witness carrying two server-derived stream names."""
import re
from dataclasses import dataclass, field

from .prior_history_witness import PriorHistoryWitness
from .profile100 import stream_binding_key

_SOURCE = "synthetic-rest"
_DEVICE = "mockprofile-100-a-001"
_BATCH = re.compile(r"batch-a-[A-Za-z0-9-]{1,72}")


def fryer_stream(binding):
    if (
        type(binding) is not dict or set(binding) != {"source_id", "device_id", "batch_id"}
        or binding["source_id"] != _SOURCE or binding["device_id"] != _DEVICE
        or type(binding["batch_id"]) is not str or not _BATCH.fullmatch(binding["batch_id"])
    ):
        raise ValueError("explicit synthetic fryer source required")
    return stream_binding_key(**binding)


@dataclass(frozen=True)
class TonkatsuPriorHistoryWitness:
    history: PriorHistoryWitness = field(repr=False)
    streams: tuple[str, str] = field(repr=False)

    def __post_init__(self):
        if (
            type(self.history) is not PriorHistoryWitness or type(self.streams) is not tuple
            or len(self.streams) != 2 or self.streams[0] == self.streams[1]
        ):
            raise ValueError("invalid private fryer history observation")
        for stream in self.streams:
            if type(stream) is not str or stream != fryer_stream({
                "source_id": _SOURCE, "device_id": _DEVICE,
                "batch_id": stream.rsplit(":", 1)[-1],
            }):
                raise ValueError("invalid private fryer stream")
