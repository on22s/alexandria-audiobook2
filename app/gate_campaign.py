"""Durable membership and completion evidence for provenance re-gating."""
import argparse
import hashlib
import json
from pathlib import Path
import uuid
import sys

from experiments.verify_adapter_identity import get_completed_identity_gate
from utils import atomic_json_write


JOURNAL_NAME = "regate_provenance_campaign.json"


def get_campaign_document(path):
    document = json.loads(Path(path).read_bytes())
    if (not isinstance(document, dict) or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or not isinstance(document.get("campaign_id"), str)
            or not document["campaign_id"]
            or document.get("status") not in ("running", "complete")
            or not isinstance(document.get("members"), dict)
            or not document["members"]):
        raise ValueError("invalid re-gate campaign")
    for name, row in document["members"].items():
        if (not isinstance(name, str) or not name or Path(name).name != name
                or name in (".", "..") or not isinstance(row, dict)
                or any(not isinstance(row.get(key), str) or not row[key]
                       for key in ("adapter", "dataset"))):
            raise ValueError("invalid re-gate campaign member")
    return document


def save_campaign_start(queue_path, journal_path):
    members = {}
    for line in Path(queue_path).read_text().splitlines():
        if line.startswith("#"):
            continue
        name, adapter, dataset = line.split("\t")
        if name in members or not name or Path(name).name != name or name in (".", ".."):
            raise ValueError("invalid or duplicate campaign member")
        members[name] = {"adapter": adapter, "dataset": dataset}
    if not members:
        raise ValueError("cannot start an empty re-gate campaign")
    document = {"schema_version": 1, "campaign_id": uuid.uuid4().hex,
                "status": "running", "members": members}
    atomic_json_write(document, str(journal_path))


def get_measured_campaign_member(journal_path, name, row, lines):
    path = Path(journal_path).parent / f"gate_promote__{name}.json"
    before = path.read_bytes()
    document = get_completed_identity_gate(path, row["adapter"], row["dataset"], lines)
    provenance = document.get("provenance")
    if not isinstance(provenance, dict) or not provenance or "error" in provenance:
        raise ValueError("gate lacks successful provenance")
    if before != path.read_bytes():
        raise ValueError("gate changed during campaign validation")
    return document, hashlib.sha256(before).hexdigest()


def save_campaign_result(journal_path, name, rc, lines=6):
    if type(rc) is not int:
        raise ValueError("worker status must be an integer")
    document = get_campaign_document(journal_path)
    if document["status"] != "running" or name not in document["members"]:
        raise ValueError("campaign is not running for this member")
    row = dict(document["members"][name], rc=rc)
    if rc in (0, 3):
        gate, digest = get_measured_campaign_member(journal_path, name, row, lines)
        if rc != (0 if gate["passed"] else 3):
            raise ValueError("worker status disagrees with measured verdict")
        row["sha256"] = digest
    document["members"][name] = row
    atomic_json_write(document, str(journal_path))


def save_campaign_completion(journal_path, lines=6):
    document = get_campaign_document(journal_path)
    for name, row in document["members"].items():
        if type(row.get("rc")) is not int or row["rc"] not in (0, 3):
            raise ValueError(f"campaign member {name} was not measured")
        gate, digest = get_measured_campaign_member(journal_path, name, row, lines)
        if row.get("sha256") != digest or row["rc"] != (0 if gate["passed"] else 3):
            raise ValueError(f"campaign member {name} changed")
    atomic_json_write(dict(document, status="complete"), str(journal_path))


def get_campaign_gate_evidence_snapshot(path):
    """Return a validated gate and SHA-256 of the exact bytes parsed.

    Read a gate only if its campaign's entire published set is intact.

    Historical and independently produced gates retain their existing policy
    when no journal exists or when they are outside its declared membership.
    An unreadable journal cannot establish that exception and refuses.
    """
    path = Path(path)
    journal = path.parent / JOURNAL_NAME
    with open(path, "rb") as handle:
        contents = handle.read()
    if journal.exists() and path.name.startswith("gate_promote__"):
        before = journal.read_bytes()
        campaign = get_campaign_document(journal)
        name = path.name[len("gate_promote__"):-len(".json")]
        if name in campaign["members"]:
            if campaign["status"] != "complete":
                raise ValueError("re-gate campaign is incomplete")
            for member, row in campaign["members"].items():
                if type(row.get("rc")) is not int or row["rc"] not in (0, 3):
                    raise ValueError("re-gate campaign has an unmeasured member")
                data = (path.parent / f"gate_promote__{member}.json").read_bytes()
                if row.get("sha256") != hashlib.sha256(data).hexdigest():
                    raise ValueError("re-gate campaign evidence changed")
            if before != journal.read_bytes() or contents != path.read_bytes():
                raise ValueError("re-gate campaign changed during admission")
    gate = json.loads(contents)
    if not isinstance(gate, dict):
        raise ValueError("gate evidence must be a JSON object")
    return gate, hashlib.sha256(contents).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("queue")
    start.add_argument("journal")
    result = commands.add_parser("result")
    result.add_argument("journal")
    result.add_argument("name")
    result.add_argument("rc", type=int)
    finish = commands.add_parser("complete")
    finish.add_argument("journal")
    args = parser.parse_args()
    try:
        if args.command == "start":
            save_campaign_start(args.queue, args.journal)
        elif args.command == "result":
            save_campaign_result(args.journal, args.name, args.rc)
        else:
            save_campaign_completion(args.journal)
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"REFUSING re-gate campaign: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())


def get_campaign_gate_evidence(path):
    """Return the gate from the same validated evidence snapshot."""
    return get_campaign_gate_evidence_snapshot(path)[0]
