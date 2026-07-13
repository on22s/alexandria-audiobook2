"""Shared config.json models and shape-safe loading boundary."""

import logging
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from utils import safe_load_json


logger = logging.getLogger(__name__)

_DICT_SECTIONS = ("llm", "tts")
_OPTIONAL_DICT_SECTIONS = ("prompts", "generation", "llm_local", "llm_remote")


def load_app_config(path: str) -> dict:
    """Return a fresh, dictionary-shaped app config without writing repairs."""
    loaded = safe_load_json(path, default={})
    config = dict(loaded)
    for section in _DICT_SECTIONS:
        if section in config and not isinstance(config[section], dict):
            logger.warning("Invalid '%s' section in config, ignoring it", section)
            del config[section]
    for section in _OPTIONAL_DICT_SECTIONS:
        if config.get(section) is not None and not isinstance(config[section], dict):
            logger.warning("Invalid '%s' section in config, ignoring it", section)
            config[section] = None
    return config


class LLMConfig(BaseModel):
    base_url: str
    api_key: str
    model_name: str


class TTSConfig(BaseModel):
    mode: Literal["local", "external"] = "local"
    url: str = "http://127.0.0.1:7860"
    device: str = "auto"
    language: str = "English"
    parallel_workers: int = Field(default=2, ge=1)
    batch_seed: Optional[int] = None
    compile_codec: bool = False
    sub_batch_enabled: bool = True
    sub_batch_min_size: int = Field(default=4, ge=1)
    sub_batch_ratio: float = Field(default=5.0, ge=1)
    sub_batch_max_items: int = Field(default=0, ge=0)
    batch_group_by_type: bool = False
    pause_between_speakers_ms: int = Field(default=500, ge=0)
    pause_same_speaker_ms: int = Field(default=250, ge=0)


class GenerationConfig(BaseModel):
    chunk_size: int = Field(default=3000, ge=500)
    max_tokens: int = Field(default=4096, ge=256)
    temperature: float = Field(default=0.6, ge=0, le=2)
    top_p: float = Field(default=0.8, ge=0, le=1)
    top_k: int = Field(default=0, ge=0, le=200)
    min_p: float = Field(default=0, ge=0, le=1)
    presence_penalty: float = Field(default=0.0, ge=-2, le=2)
    banned_tokens: List[str] = Field(default_factory=list)
    merge_narrators: bool = False


class PromptConfig(BaseModel):
    system_prompt: Optional[str] = None
    user_prompt: Optional[str] = None
    review_system_prompt: Optional[str] = None
    review_user_prompt: Optional[str] = None
    persona_system_prompt: Optional[str] = None
    persona_user_prompt: Optional[str] = None
    persona_advanced_prompt: Optional[str] = None


class AppConfig(BaseModel):
    llm: LLMConfig
    llm_mode: Literal["local", "remote"] = "local"
    llm_local: Optional[LLMConfig] = None
    llm_remote: Optional[LLMConfig] = None
    llm_remote_ssh: Optional[str] = None
    tts: TTSConfig
    prompts: Optional[PromptConfig] = None
    generation: Optional[GenerationConfig] = None
