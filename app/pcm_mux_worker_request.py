"""Strict boundary for the isolated native MP3/WAV encoder/remux worker."""
import json
from typing import Literal
from pydantic import Field, StrictInt, StrictStr, model_validator
from audio_encoder_identity import FFmpegRuntimeIdentity
from json_metadata import get_unique_json_object
from pcm_worker_contract import PCMWorkerMastering, PCMInputShape, WorkerFileIdentity, MAX_PCM_BYTES, get_pcm_output_shape
from pcm_mux_profile import get_pcm_mux_profile

MAX_REQUEST_BYTES = 65536


class PCMMuxWorkerRequest(PCMInputShape, PCMWorkerMastering):
    schema_version: StrictInt = Field(ge=1, le=1)
    format: Literal['mp3', 'wav']
    raw: WorkerFileIdentity
    encoder: FFmpegRuntimeIdentity
    output_limit: StrictInt = Field(ge=1, le=MAX_PCM_BYTES)
    mux_profile: dict[StrictStr, StrictStr]
    # A fixed bitrate is part of this new bounded path's explicit contract.
    # WAV shares the request envelope; this field has no PCM codec effect.
    bitrate: Literal['128k', '192k'] = '192k'

    @model_validator(mode='after')
    def validate_contract(self):
        get_pcm_output_shape(self.format,{key:getattr(self,key) for key in PCMInputShape.model_fields},self.output_limit)
        if (self.raw.version[2] != self.frames*self.channels*self.sample_width
                or self.mux_profile != get_pcm_mux_profile()):
            raise ValueError('Invalid PCM worker geometry or mux profile')
        return self


def get_pcm_mux_worker_request(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_REQUEST_BYTES:
        raise ValueError('PCM worker request exceeds its allowance')
    value = json.loads(raw, object_pairs_hook=get_unique_json_object,
        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite worker request')))
    return PCMMuxWorkerRequest.model_validate(value)
