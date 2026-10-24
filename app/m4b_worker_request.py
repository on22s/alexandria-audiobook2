"""Strict, bounded data boundary for the isolated M4B remux worker."""
import json
from pydantic import Field, StrictInt, StrictStr, model_validator

from audio_encoder_identity import FFmpegRuntimeIdentity
from json_metadata import get_unique_json_object
from m4b_mux_profile import get_m4b_mux_profile
from m4b_cover import MAX_COVER_BYTES
from pcm_worker_contract import PCMWorkerMastering, MAX_PCM_BYTES, Filename, WorkerFileIdentity, PCMInputShape, get_pcm_output_shape

MAX_REQUEST_BYTES = 65536
MAX_METADATA_BYTES = 8 * 1024**2


class M4BInputShape(PCMInputShape):
    cover_name: Filename | None = None

    @model_validator(mode='after')
    def validate_cover_name(self):
        get_pcm_output_shape('m4b',{key:getattr(self,key) for key in PCMInputShape.model_fields},MAX_PCM_BYTES)
        if self.raw_name == self.cover_name:
            raise ValueError('Invalid M4B worker PCM shape')
        return self


class M4BWorkerRequest(M4BInputShape, PCMWorkerMastering):
    schema_version: StrictInt = Field(ge=1, le=1)
    raw: WorkerFileIdentity
    metadata: WorkerFileIdentity
    cover: WorkerFileIdentity | None = None
    encoder: FFmpegRuntimeIdentity
    output_limit: StrictInt = Field(ge=1, le=MAX_PCM_BYTES)
    mux_profile: dict[StrictStr, StrictStr]

    @model_validator(mode='after')
    def validate_contract(self):
        expected = self.frames*self.channels*self.sample_width
        if (self.raw.version[2] != expected
                or not 1 <= self.metadata.version[2] <= MAX_METADATA_BYTES
                or (self.cover_name is None) != (self.cover is None)
                or self.mux_profile != get_m4b_mux_profile()):
            raise ValueError('Invalid M4B worker geometry, files or mux profile')
        if self.cover is not None and not 1 <= self.cover.version[2] <= MAX_COVER_BYTES:
            raise ValueError('Invalid M4B worker cover size')
        return self


def get_m4b_worker_request(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_REQUEST_BYTES:
        raise ValueError('M4B worker request exceeds its allowance')
    value = json.loads(raw, object_pairs_hook=get_unique_json_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite worker request')))
    return M4BWorkerRequest.model_validate(value)
