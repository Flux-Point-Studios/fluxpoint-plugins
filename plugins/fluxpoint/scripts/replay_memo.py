"""Build replay inputs from completed Workflow calls, never from caller claims.

The Workflow journal is an internal interface. Unknown, missing or ambiguous
records cannot produce a replay entry. Old runs without compiler call receipts
remain resumable, but do not supply a node memo.
"""
import copy
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re


class ReplayError(ValueError):
    pass


def canonical(value):
    """Sorted UTF-8 JSON, shared with the compiler's JavaScript serializer."""
    def encode(v):
        if v is None or type(v) is bool or isinstance(v, str):
            return json.dumps(v, ensure_ascii=False, separators=(",", ":"))
        if type(v) in (int, float):
            if not math.isfinite(v):
                raise ReplayError("non-finite memo number")
            if v == 0:
                return "0"
            if isinstance(v, int) or v.is_integer():
                if abs(v) < 1e21:
                    return str(int(v))
            text = repr(v).lower()
            if "e" in text:
                mantissa, exponent = text.split("e")
                n = int(exponent)
                if -6 <= n < 21:
                    from decimal import Decimal
                    return format(Decimal(text), "f")
                return mantissa.removesuffix(".0") + "e" + ("+" if n >= 0 else "-") + str(abs(n))
            return text
        if isinstance(v, list):
            return "[" + ",".join(encode(x) for x in v) + "]"
        if isinstance(v, dict) and all(isinstance(k, str) for k in v):
            # JS sorts UTF-16 code units; Python normally sorts Unicode codepoints.
            keys = sorted(v, key=lambda k: k.encode("utf-16-be", "surrogatepass"))
            return "{" + ",".join(encode(k) + ":" + encode(v[k]) for k in keys) + "}"
        raise ReplayError("memo payload contains a non-JSON value")
    return encode(value)


def memo_key(payload):
    return hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()


def _component(value, name):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", value)
            or value.endswith(".") or value.split(".")[0].upper() in
            ({"CON", "PRN", "AUX", "NUL"} | {"COM" + str(i) for i in range(1, 10)}
             | {"LPT" + str(i) for i in range(1, 10)})):
        raise ReplayError("invalid " + name)
    return value


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReplayError("cannot read replay source " + str(path) + ": " + str(exc)) from exc


def _rows(path):
    try:
        result = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReplayError("cannot read journal/transcript " + str(path) + ": " + str(exc)) from exc
    if any(not isinstance(row, dict) for row in result):
        raise ReplayError("journal/transcript records must be objects")
    return result


def artifact(root, run_id):
    run_id = _component(run_id, "run id")
    base = (Path(root).resolve() / ".claude/fluxpoint/runs").resolve()
    path = base / (run_id + ".json")
    if path.resolve().parent != base:
        raise ReplayError("replay artifact escapes canonical run store")
    row = _json(path)
    if not isinstance(row, dict) or row.get("runId") != run_id or not isinstance(row.get("summary"), dict):
        raise ReplayError("source artifact has no matching run id and summary")
    return row


def _projects(projects):
    if projects is not None:
        return Path(projects)
    return Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))) / "projects"


def _run_dir(run_id, projects):
    import glob
    hits = {p.parent.resolve() for p in _projects(projects).glob(
        "**/subagents/workflows/" + glob.escape(_component(run_id, "run id")) + "/journal.jsonl")}
    if len(hits) != 1:
        raise ReplayError("source run needs exactly one journal: " + run_id)
    return hits.pop()


def _prompt(path):
    for row in _rows(path):
        msg = row.get("message", row)
        if not isinstance(msg, dict):
            continue
        if msg.get("role", row.get("type")) != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list) and all(isinstance(x, dict) and x.get("type") == "text"
                                             and isinstance(x.get("text"), str) for x in content):
            return "".join(x["text"] for x in content)
        raise ReplayError("source transcript first user message is not text")
    raise ReplayError("source transcript has no user prompt")


def _journal_calls(run_id, projects):
    import glob
    directory = _run_dir(run_id, projects)
    started, calls = {}, []
    for event in _rows(directory / "journal.jsonl"):
        kind = event.get("type")
        if "agentId" not in event:
            continue
        aid = _component(event["agentId"], "agent id")
        if kind == "started":
            if aid in started:
                raise ReplayError("duplicate started agent in source journal")
            call = {"agentId": aid, "start": event, "done": None}
            started[aid] = call
            calls.append(call)
        elif kind in ("result", "failed"):
            if aid not in started or started[aid]["done"] is not None:
                raise ReplayError("source journal terminal event has no unique start")
            started[aid]["done"] = event
        else:
            raise ReplayError("unknown source journal agent event: " + str(kind))
    for call in calls:
        aid = call["agentId"]
        hits = {p.resolve() for parent in (directory, directory.parent, directory.parent.parent)
                for p in parent.glob("agent-" + glob.escape(aid) + ".jsonl") if p.is_file()}
        if len(hits) != 1:
            raise ReplayError("source agent needs exactly one transcript: " + aid)
        path = hits.pop()
        call["prompt"] = _prompt(path)
        usage, counted, known = 0, set(), False
        for row in _rows(path):
            message = row.get("message")
            if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                continue
            identity = message.get("id")
            # Content-block rows may repeat one API message's usage. Without
            # its identity, a precise token total cannot be established.
            if not isinstance(identity, str):
                continue
            if identity in counted:
                continue
            counted.add(identity)
            known = True
            usage += sum(v for k, v in message["usage"].items() if k in (
                "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                         and type(v) is int and v >= 0)
        minutes = None
        try:
            def clock(row):
                value = row.get("timestamp", row.get("ts"))
                if type(value) in (int, float):
                    return float(value)
                return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            minutes = max(0, clock(call["done"]) - clock(call["start"])) / 60
        except (ValueError, TypeError, AttributeError):
            pass
        call["cost"] = {"minutes": minutes, "tokens": usage if known else None}
    return calls


def _result(event):
    if not isinstance(event, dict) or event.get("type") != "result":
        raise ReplayError("source agent did not complete")
    values = [event[k] for k in ("result", "value", "output") if k in event]
    if not values:
        raise ReplayError("source journal result has no returned value")
    values = [json.loads(v) if isinstance(v, str) else v for v in values]
    if any(canonical(v) != canonical(values[0]) for v in values[1:]):
        raise ReplayError("source journal result fields disagree")
    return values[0]


def _check_call(receipt, call):
    if not isinstance(receipt, dict) or not isinstance(receipt.get("prompt"), str) or not isinstance(receipt.get("opts"), dict):
        raise ReplayError("invalid compiler call receipt")
    if receipt["prompt"] != call["prompt"]:
        raise ReplayError("source transcript prompt differs from receipt")
    if canonical(receipt.get("result")) != canonical(_result(call["done"])):
        raise ReplayError("source journal result differs from receipt")
    start = call["start"]
    label = start.get("label", (start.get("opts") or {}).get("label"))
    if label is not None and label != receipt["opts"].get("label"):
        raise ReplayError("source journal label differs from receipt")
    options = start.get("opts", start.get("options"))
    if options is not None and canonical(options) != canonical(receipt["opts"]):
        raise ReplayError("source journal options differ from receipt")


def _validate_record(record, summary, launch, compiler):
    if not isinstance(record, dict) or record.get("mode") not in ("live", "memo", "prefix"):
        raise ReplayError("invalid memo receipt mode")
    payload = record.get("payload")
    required = {"campaign", "compiler", "launch", "id", "prompt", "opts", "tree", "ancestors"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise ReplayError("memo key payload fields differ from protocol")
    if (payload["campaign"] != summary.get("campaign") or payload["compiler"] != compiler
            or canonical(payload["launch"]) != canonical(launch)
            or payload["id"] != record.get("node") or payload["prompt"] != record.get("prompt")
            or not isinstance(payload["opts"], dict) or not isinstance(payload["ancestors"], list)
            or any(not isinstance(k, str) or not re.fullmatch(r"[0-9a-f]{64}", k) for k in payload["ancestors"])):
        raise ReplayError("memo payload identity mismatch")
    if record.get("key") != memo_key(payload):
        raise ReplayError("memo receipt SHA-256 mismatch")


def _source_calls(root, run_id, launch, compiler, projects, seen):
    if run_id in seen:
        raise ReplayError("cyclic replay provenance")
    seen = seen | {run_id}
    summary = artifact(root, run_id)["summary"]
    if canonical(summary.get("launch")) != canonical(launch):
        raise ReplayError("memo source belongs to another launch")
    receipts = summary.get("callRecords", [])
    records = summary.get("memoRecords", [])
    if not isinstance(receipts, list) or not isinstance(records, list):
        raise ReplayError("compiler call and memo receipts must be arrays")
    if not receipts and not records:
        return summary, [], []
    calls = _journal_calls(run_id, projects)
    unused = list(calls)
    validated = []
    for receipt in receipts:
        if not isinstance(receipt, dict):
            raise ReplayError("invalid compiler call receipt")
        source = receipt.get("source")
        if source:
            if not isinstance(source, dict):
                raise ReplayError("invalid replay call source")
            _, source_calls, _ = _source_calls(root, source.get("runId"), launch, compiler, projects, seen)
            matches = [c for c in source_calls if c["agentId"] == source.get("agentId")]
            if len(matches) != 1:
                raise ReplayError("replayed call source missing or ambiguous")
            original = matches[0]
            if any(canonical(receipt.get(k)) != canonical(original.get(k)) for k in ("prompt", "opts", "result")):
                raise ReplayError("replayed call differs from original call")
            validated.append(copy.deepcopy(original))
            continue
        matches = []
        for call in unused:
            try:
                _check_call(receipt, call)
                matches.append(call)
            except (ReplayError, ValueError):
                continue
        # Repeated identical prompts are paired in issuance order, as recorded
        # by both the journal's starts and compiler callRecords.
        if not matches:
            raise ReplayError("compiler call has no matching completed journal/transcript")
        call = matches[0]
        unused.remove(call)
        validated.append(dict(prompt=receipt["prompt"], opts=receipt["opts"], result=receipt.get("result"),
                              runId=run_id, agentId=call["agentId"], cost=call["cost"]))
    verified = []
    for record in records:
        _validate_record(record, summary, launch, compiler)
        if record.get("mode") == "memo":
            source = record.get("source") or {}
            _, _, previous = _source_calls(root, source.get("runId"), launch, compiler, projects, seen)
            candidates = [x for x in previous if x["key"] == record["key"] and x["agentId"] == source.get("agentId")]
        else:
            candidates = [c for c in validated if c["prompt"] == record["prompt"]
                          and canonical(c["opts"]) == canonical(record["payload"]["opts"])
                          and canonical(c["result"]) == canonical(record.get("result"))]
        if len(candidates) != 1:
            raise ReplayError("memo receipt has no unique corroborated agent call")
        origin = candidates[0]
        if canonical(origin.get("result")) != canonical(record.get("result")):
            raise ReplayError("memo result differs from source")
        entry = dict(key=record["key"], prompt=record["prompt"], result=record.get("result"),
                     payload=record["payload"], runId=origin["runId"], agentId=origin["agentId"],
                     cost=origin.get("cost", {"minutes": None, "tokens": None}))
        if record.get("verifiedReading") is not None:
            reading = record["verifiedReading"]
            # A builder's replay binds to an actual verifying gate's sentinel,
            # never a head claimed by either worker.
            verifier = record.get("verifiedByNode")
            declared = record.get("verifiedBy", [])
            if (not isinstance(declared, list) or verifier not in declared
                    or not any(r.get("node") == verifier and record["node"] in (
                        r.get("verifies") if isinstance(r.get("verifies"), list) else [r.get("verifies")])
                        and r.get("payload", {}).get("tree") == reading for r in records)):
                raise ReplayError("builder verified reading has no verifying gate receipt")
            entry["verifiedReading"] = reading
            entry["verifiedByNode"] = verifier
        verified.append(entry)
    return summary, validated, verified


def build_memo(root, run_id, launch, compiler_version, projects=None):
    """Return launcher-only memo and prefix inputs after independent verification."""
    _, calls, entries = _source_calls(root, run_id, launch, compiler_version, projects, set())
    memo = {}
    for entry in entries:
        if entry["key"] in memo and canonical(memo[entry["key"]]) != canonical(entry):
            raise ReplayError("conflicting source memo entries")
        memo[entry["key"]] = entry
    return {"_memo": memo, "_prefix": calls}


def _graph_nodes(graph):
    try:
        blocks = re.findall(r"```json\s+graph-ir\s*\n(.*?)\n```", Path(graph).read_text(encoding="utf-8"), re.S)
        if len(blocks) != 1:
            raise ReplayError("graph needs one graph-ir block for replay verification")
        nodes = json.loads(blocks[0])["nodes"]
        if not isinstance(nodes, list) or any(not isinstance(n, dict) for n in nodes):
            raise ReplayError("invalid replay graph nodes")
        return {n["id"]: n for n in nodes}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ReplayError("cannot freeze replay graph: " + str(exc)) from exc


def verify_replays(root, summary, projects=None, graph=None):
    """Recorder check: re-read every memo replay's original journal and transcript."""
    records = summary.get("memoRecords", [])
    if not isinstance(records, list):
        raise ReplayError("memoRecords must be an array")
    checked = []
    nodes = _graph_nodes(graph) if graph is not None and records else None
    for record in records:
        if not isinstance(record, dict):
            raise ReplayError("invalid memo record")
        if record.get("mode") != "memo":
            continue
        if nodes is not None:
            node = nodes.get(record.get("node")) or {}
            allowed = "inputs" if node.get("mutates") is True else "tree"
            if (node.get("replay") != allowed or node.get("fresh") is True
                    or node.get("irreversible") or node.get("contract") == "RedTeamV1"
                    or not (node.get("mutates") is True or node.get("independent") is True)):
                raise ReplayError("graph does not permit memo replay for " + str(record.get("node")))
        payload = record.get("payload") or {}
        compiler = payload.get("compiler")
        _validate_record(record, summary, summary.get("launch"), compiler)
        source = record.get("source") or {}
        data = build_memo(root, source.get("runId"), summary.get("launch"), compiler, projects)
        original = data["_memo"].get(record.get("key"))
        if (not original or original["agentId"] != source.get("agentId")
                or any(canonical(original.get(k)) != canonical(record.get(k)) for k in ("prompt", "result", "payload"))):
            raise ReplayError("REPLAYED-MEMO source does not corroborate result")
        checked.append({"node": record["node"], "key": record["key"], "source": source})
    # A provenance label cannot bypass checks by omitting its memo receipt.
    labelled = [r for r in summary.get("provenance", []) if isinstance(r, dict)
                and r.get("status") in ("REPLAYED-MEMO", "REPLAYED")]
    if any(r.get("node") not in {x["node"] for x in checked} for r in labelled):
        raise ReplayError("replay label has no independently verified memo receipt")
    return checked


def verify_rejudges(root, summary, graph=None):
    """Keep the earlier verdict beside a reached gate's explicit rejudgment."""
    rows = summary.get("rejudges", [])
    if not isinstance(rows, list):
        raise ReplayError("rejudges must be an array")
    inputs = summary.get("inputs") or {}
    requested = inputs.get("rejudge", {})
    if not isinstance(requested, dict):
        raise ReplayError("rejudge input must be an object")
    if not rows and not requested:
        return []
    nodes = _graph_nodes(graph) if graph is not None else None
    result, reached = [], set()
    for row in rows:
        if not isinstance(row, dict) or row.get("gate") in reached:
            raise ReplayError("invalid or duplicate rejudge receipt")
        gate, nonce = row.get("gate"), row.get("nonce")
        if (not isinstance(gate, str) or not isinstance(nonce, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", nonce)
                or not isinstance(row.get("reason"), str) or not row["reason"].strip()
                or not isinstance(row.get("note", ""), str)):
            raise ReplayError("invalid rejudge gate, nonce, reason or note")
        if nodes is not None:
            node = nodes.get(gate) or {}
            if not node.get("independent") and not str(node.get("verify", "")).startswith("prove:"):
                raise ReplayError("rejudge does not name an independent gate")
        declared = requested.get(gate)
        if (not isinstance(declared, dict) or any(row.get(k, "") != declared.get(k, "")
                                                for k in ("nonce", "reason", "note"))):
            raise ReplayError("rejudge receipt differs from operator inputs")
        if gate not in (summary.get("results") or {}) or not isinstance(row.get("tree"), dict):
            raise ReplayError("rejudged gate was not reached with a sentinel reading")
        source = artifact(root, row.get("sourceRunId"))["summary"]
        if canonical(source.get("launch")) != canonical(summary.get("launch")):
            raise ReplayError("rejudge source belongs to another launch")
        previous = (source.get("results") or {}).get(gate)
        if previous is None or canonical(previous) != canonical(row.get("priorVerdict")):
            raise ReplayError("rejudge prior verdict differs from source artifact")
        if not any(isinstance(r, dict) and r.get("point") == "before " + gate
                   and all(r.get(k) == row["tree"].get(k) for k in ("head", "root", "porcelain"))
                   for r in (summary.get("tree") or {}).get("checks", [])):
            raise ReplayError("rejudge tree has no matching gate sentinel reading")
        reached.add(gate)
        result.append(copy.deepcopy(row))
    if set(requested) != reached:
        raise ReplayError("rejudge requested a gate that was not reached")
    return result
