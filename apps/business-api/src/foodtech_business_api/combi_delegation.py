"""Minimal BFF delegation: transport-authenticated metadata, never browser tokens."""
from dataclasses import asdict,dataclass
import json
import math

from foodtech_contracts.combi_history_witness import canonical_uuid
from .subject_identity import valid_subject

CONTENT_TYPE='application/vnd.foodtech.combi-invocation+json'
MAX_BYTES=2048


def _unique(pairs):
    result={}
    for key,value in pairs:
        if key in result: raise ValueError('duplicate delegation field')
        result[key]=value
    return result


@dataclass(frozen=True,repr=False)
class CombiInvocationDelegation:
    subject: str
    epoch: int
    execution_id: str
    session_expires: int
    schema: str = 'combi-invocation-delegation-v1'

    def __post_init__(self):
        if not valid_subject(self.subject) or type(self.epoch) is not int or not 0<=self.epoch<2**63 or \
                not canonical_uuid(self.execution_id) or type(self.session_expires) is not int or not 0<self.session_expires<2**63 or \
                self.schema!='combi-invocation-delegation-v1': raise ValueError('invalid combi delegation')

    def __repr__(self): return 'CombiInvocationDelegation()'

    def require_live(self,now):
        if type(now) not in (int,float) or not 0<now<self.session_expires or not math.isfinite(now):
            raise ValueError('combi delegation expired')

    def encode_for_broker(self):
        return json.dumps(asdict(self),separators=(',',':'),ensure_ascii=True).encode('ascii')

    @classmethod
    def decode_authenticated_bff(cls,raw):
        # Caller must authenticate the dedicated BFF TLS peer BEFORE this parser.
        try:
            if type(raw) is not bytes or not 0<len(raw)<=MAX_BYTES: raise ValueError
            def constant(_): raise ValueError
            value=json.loads(raw,object_pairs_hook=_unique,parse_constant=constant)
            if type(value) is not dict or set(value)!={'subject','epoch','execution_id','session_expires','schema'}: raise ValueError
            return cls(**value)
        except (ValueError,TypeError,UnicodeError,RecursionError):
            raise ValueError('combi delegation unavailable') from None
