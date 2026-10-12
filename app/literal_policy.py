"""Portable opt-in narration provenance; speech verbalization is a later stage."""
import copy
import hashlib
import json
import re

from literal_fidelity import audit_literal_words, _words

SCRIPT_POLICY_KEY = "_literal_source_policy"
CHUNK_POLICY_KEY = "_literal_narration"
POLICY_VERSION = "literal_words_v1"
PROMPT_VERSION = "literal_prompts_v1"


def get_word_identity(texts):
    digest = hashlib.sha256()
    count = 0
    for text in texts:
        if not isinstance(text, str):
            raise ValueError("Literal narration text must be a string")
        for word in _words(text):
            encoded = word.encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
            count += 1
    if not count:
        raise ValueError("Literal narration has no verifiable words")
    return {"words": count, "sha256": digest.hexdigest()}


def get_literal_policy(entries):
    policies = [entry[SCRIPT_POLICY_KEY] for entry in entries
                if isinstance(entry, dict) and SCRIPT_POLICY_KEY in entry]
    if not policies:
        return None
    policy = policies[0]
    if (not isinstance(policy, dict) or policy.get("version") != POLICY_VERSION
            or policy.get("prompt_version") != PROMPT_VERSION
            or policy.get("boundary") != "preprocessed_source_v1"
            or any(other != policy for other in policies)):
        raise ValueError("Invalid or conflicting literal-source policy")
    identity = policy.get("source_words")
    if (not isinstance(identity, dict) or type(identity.get("words")) is not int
            or identity["words"] < 1
            or not isinstance(identity.get("sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity["sha256"])
            or not isinstance(policy.get("source_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", policy["source_sha256"])):
        raise ValueError("Invalid literal-source identity")
    if "input_sha256" in policy and (not isinstance(policy["input_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", policy["input_sha256"])):
        raise ValueError("Invalid literal input-file identity")
    if "strip_front_matter" in policy and type(policy["strip_front_matter"]) is not bool:
        raise ValueError("Invalid literal preprocessing choice")
    return copy.deepcopy(policy)


def ensure_literal_narration(entries, policy=None):
    policy = policy if policy is not None else get_literal_policy(entries)
    if policy is not None:
        identity = get_word_identity(entry.get("text") for entry in entries)
        if identity != policy["source_words"]:
            raise ValueError("Literal narration changed ordered source words; regenerate or review the source policy")
    return policy


def retain_literal_policy(entries, policy):
    if policy is None:
        return entries
    ensure_literal_narration(entries, policy)
    retained = [{key: copy.deepcopy(value) for key, value in entry.items()
                 if key != SCRIPT_POLICY_KEY} for entry in entries]
    retained[0][SCRIPT_POLICY_KEY] = copy.deepcopy(policy)
    return retained


def bind_literal_policy(source, entries, *, input_sha256=None, strip_front_matter=None):
    if not audit_literal_words(source, entries)["passed"]:
        raise ValueError("Literal annotation changed ordered source words")
    policy = {"version": POLICY_VERSION, "prompt_version": PROMPT_VERSION,
              "boundary": "preprocessed_source_v1",
              "source_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
              "source_words": get_word_identity([source]),
              "speech_normalization": "existing_speech_preparation_allowed"}
    if input_sha256 is not None:
        policy["input_sha256"] = input_sha256
    if strip_front_matter is not None:
        policy["strip_front_matter"] = strip_front_matter
    return retain_literal_policy(entries, policy)


def get_literal_policy_identity(policy):
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()


def bind_literal_chunks(chunks, policy):
    if policy is None:
        return chunks
    return [{**chunk, CHUNK_POLICY_KEY: {
        "version": POLICY_VERSION,
        "source_policy_sha256": get_literal_policy_identity(policy),
        "narration_words": get_word_identity([chunk["text"]]),
        "projection": "existing_project_speakable_entries_v1",
        "boundary": "before_speech_preparation"}} for chunk in chunks]


def ensure_literal_chunk(chunk, policy=None):
    marker = chunk.get(CHUNK_POLICY_KEY)
    if marker is None and policy is None:
        return
    if (not isinstance(marker, dict) or marker.get("version") != POLICY_VERSION
            or marker.get("boundary") != "before_speech_preparation"
            or marker.get("narration_words") != get_word_identity([chunk.get("text")])):
        raise ValueError("Literal narration chunk changed or lacks provenance; regenerate chunks")
    if policy is not None and marker.get("source_policy_sha256") != get_literal_policy_identity(policy):
        raise ValueError("Literal narration chunk belongs to a different source policy")
