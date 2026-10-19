"""Build replay inputs from completed Workflow calls, never from caller claims.

The Workflow journal is an internal interface. Unknown, missing or ambiguous
records cannot produce a replay entry. Old runs without compiler call receipts
remain resumable, but do not supply a node memo.
"""
import copy
import datetime
from functools import lru_cache
import hashlib
import json
import math
import os
from pathlib import Path
import re
import run_artifacts


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
            if isinstance(v, int) and abs(v) < 1e21:
                return str(v)
            text = repr(v).lower()
            if "e" in text:
                mantissa, exponent = text.split("e")
                n = int(exponent)
                if -6 <= n < 21:
                    from decimal import Decimal
                    return format(Decimal(text), "f")
                return mantissa.removesuffix(".0") + "e" + ("+" if n >= 0 else "-") + str(abs(n))
            return text.removesuffix('.0')
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


def artifact(root, run_id, artifact_sha256=None):
    try:
        return run_artifacts.read(root, run_id, artifact_sha256)[0]
    except (OSError, ValueError) as exc:
        raise ReplayError(str(exc)) from exc


def _source_hash(source):
    digest = source.get('artifactSha256') if isinstance(source, dict) else None
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ReplayError('replay source requires an immutable artifact SHA-256')
    return digest


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
            if not call["done"] or call["done"].get("type") == "failed":
                call.update(prompt=None, cost={"minutes": None, "tokens": None})
                continue
            raise ReplayError("source agent needs exactly one transcript: " + aid)
        path = hits.pop()
        try:
            call["prompt"] = _prompt(path)
        except ReplayError:
            if not call['done'] or call['done'].get('type') == 'failed':
                call.update(prompt=None, cost={"minutes": None, "tokens": None})
                continue
            raise
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
    failed = isinstance(call.get("done"), dict) and call["done"].get("type") == "failed"
    result = None if failed else _result(call["done"])
    if canonical(receipt.get("result")) != canonical(result):
        raise ReplayError("source journal result differs from receipt")
    start = call["start"]
    raw_opts = start.get("opts", start.get("options"))
    label = start.get("label", raw_opts.get("label") if isinstance(raw_opts, dict) else None)
    if label is not None and label != receipt["opts"].get("label"):
        raise ReplayError("source journal label differs from receipt")
    if raw_opts is not None and canonical(raw_opts) != canonical(receipt["opts"]):
        raise ReplayError("source journal options differ from receipt")
    header = "Fluxpoint-Call-Opts-SHA256: " + memo_key(receipt["opts"]) + "\n"
    if (raw_opts is None or receipt["prompt"].startswith("Fluxpoint-Call-Opts-SHA256:")) and not receipt["prompt"].startswith(header):
        raise ReplayError("source options have no matching journal or transcript SHA-256 header")


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
    tree = payload["tree"]
    if tree is not None and (record.get('replay') == 'tree' or isinstance(tree, dict) and tree.get('branch') is not None):
        if (not isinstance(tree, dict) or not isinstance(tree.get("tip"), str)
                or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", tree["tip"])
                or not isinstance(record.get("result"), dict) or record["result"].get("sha") != tree["tip"]):
            raise ReplayError("gate verdict does not name the sentinel's exact branch tip")


def _tree_root(value):
    value = str(value).strip().replace('\\', '/')
    unc = value.startswith('//')
    value = re.sub('/+', '/', value)
    if unc:
        value = '/' + value
    value = value.rstrip('/') if len(value) > 1 else value
    return value.lower() if re.match(r'^[A-Za-z]:', value) else value


def _porcelain(value, root, checkout, graph):
    import ast
    prefix = ''
    project, checkout = _tree_root(str(root)), _tree_root(checkout)
    if project.startswith(checkout.rstrip('/') + '/'):
        prefix = project[len(checkout.rstrip('/')) + 1:].rstrip('/') + '/'
    graph = str(graph or 'WORK.md').replace('\\', '/')
    if Path(graph).is_absolute():
        try:
            graph = Path(graph).relative_to(root).as_posix()
        except ValueError:
            raise ReplayError('tree ignore graph escapes project')
    if '..' in Path(graph).parts:
        raise ReplayError('tree ignore graph escapes project')
    files = {prefix + name for name in ('WORK.md', 'LOOP.md', graph)}
    prefixes = [prefix + name for name in ('.claude/fluxpoint/', '.claude/workflows/', '.claude/worktrees/')]

    def unquote(path):
        if path.startswith('"') and path.endswith('"'):
            try:
                decoded = ast.literal_eval(path)
                if re.search(r'\\[0-7]{3}', path):
                    decoded = decoded.encode('latin1').decode('utf-8')
                return decoded
            except (ValueError, SyntaxError, UnicodeError):
                pass
        return path

    def ignored(line):
        if line == '?? ' + prefix + '.claude/':
            return True
        paths = [unquote(p.strip()) for p in re.sub(r'^\S{1,2}\s+', '', line).split(' -> ')]
        return all(p in files or any(p == base.rstrip('/') or p.startswith(base) for base in prefixes) for p in paths)
    return '\n'.join(sorted(line.strip() for line in value.split('\n') if line.strip() and not ignored(line.strip())))


@lru_cache(maxsize=1)
def _sentinel_base():
    import ast
    source = ast.parse(Path(__file__).with_name('compile-graph.py').read_text(encoding='utf-8'))
    preamble = next(ast.literal_eval(n.value) for n in source.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'LONG_PREAMBLE' for t in n.targets))
    return preamble + ('Run exactly three commands in the repository at the current working directory and return their output verbatim in the '
                       'schema, nothing else: `git rev-parse --show-toplevel` as root, `git rev-parse HEAD` as head, and `git status '
                       '--porcelain` as porcelain (empty string when clean). Do not fix, clean, or explain anything.')


def _sentinel(root, summary, calls, index, reading, point, graph=None):
    """Tie a normalized reading to a live, fixed-command raw agent call."""
    if type(index) is not int or not 0 <= index < len(calls):
        raise ReplayError('tree reading has no valid raw sentinel call index')
    call = calls[index]
    if not call or not call.get('live') or call.get('result') is None:
        raise ReplayError('tree sentinel was not a live completed call')
    options = call['opts']
    if options.get('label') != 'tree-check:' + point or options.get('effort') != 'low':
        raise ReplayError('tree reading points at another agent, not its fixed sentinel')
    if not isinstance(reading, dict) or any(key not in reading for key in ('root', 'head', 'porcelain', 'repository', 'branch', 'tip')):
        raise ReplayError('tree reading is incomplete')
    raw = call['result']
    if not isinstance(raw, dict) or any(not isinstance(raw.get(k), str) for k in ('root', 'head', 'porcelain')):
        raise ReplayError('raw tree sentinel result is incomplete')
    normalized = dict(root=_tree_root(raw['root']), head=raw['head'].strip(),
                      porcelain=_porcelain(raw['porcelain'], root, raw['root'], graph))
    if reading['branch'] is not None:
        if any(not isinstance(raw.get(k), str) for k in ('repository', 'branch', 'tip')):
            raise ReplayError('raw sentinel has no branch reading')
        normalized.update(repository=raw['repository'], branch=raw['branch'], tip=raw['tip'].strip().lower())
    else:
        normalized.update(repository=normalized['root'], branch=None, tip=normalized['head'])
    if canonical(normalized) != canonical(reading):
        raise ReplayError('normalized reading differs from raw sentinel result')
    if not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', normalized['tip']):
        raise ReplayError('raw sentinel tip is not a full commit identity')

    # This literal is the compiler's fixed sentinel, not a worker-authored
    # request containing a few convenient git command substrings.
    prompt = call['prompt']
    header = 'Fluxpoint-Call-Opts-SHA256: ' + memo_key(options) + '\n'
    if prompt.startswith(header):
        prompt = prompt[len(header):]
    base = _sentinel_base() + '\nLIVE TREE READING NONCE: '
    if not prompt.startswith(base):
        raise ReplayError('tree sentinel prompt is not the fixed live command')
    nonce, separator, rest = prompt[len(base):].partition(' point ' + point)
    if not separator or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', nonce):
        raise ReplayError('tree sentinel has no fresh launch nonce')
    expected = ''
    if point.startswith('before '):
        request = (summary.get('inputs') or {}).get('rejudge', {}).get(point[7:])
        if request:
            expected += '\nREJUDGE NONCE: ' + request['nonce'] + '\nREJUDGE LAUNCH NONCE: ' + nonce
            if 'note' in request:
                expected += '\nOPERATOR REJUDGE NOTE (verbatim):\n' + request['note']
    if reading['branch'] is not None:
        expected += ('\nAlso run exactly: git -C ' + json.dumps(reading['repository'], ensure_ascii=False)
                     + ' ls-remote origin ' + json.dumps('refs/heads/' + reading['branch'], ensure_ascii=False)
                     + '. Return the sole full hexadecimal commit in tip, and the exact declared repository and branch as repository and branch. '
                     'No matches or multiple matches means tip must be empty. Never use a worker-reported sha.')
    if rest != expected:
        raise ReplayError('tree sentinel command target or rejudge note differs')
    return dict(call, readingNonce=nonce)


def _source_calls(root, run_id, launch, compiler, projects, seen, cache=None, artifact_sha256=None):
    cache = {} if cache is None else cache
    if artifact_sha256 is not None:
        identity = (run_id, artifact_sha256)
        if identity in seen:
            raise ReplayError('cyclic replay provenance')
        if identity in cache:
            return cache[identity]
    try:
        row, digest = run_artifacts.read(root, run_id, artifact_sha256)
    except (OSError, ValueError) as exc:
        raise ReplayError(str(exc)) from exc
    identity = (run_id, digest)
    if identity in seen:
        raise ReplayError("cyclic replay provenance")
    if identity in cache:
        return cache[identity]
    value = _validate_summary(root, row['summary'], run_id, digest, launch, compiler, projects,
                              seen | {identity}, cache, row.get('graph'))
    cache[identity] = value
    return value


def _validate_summary(root, summary, run_id, digest, launch, compiler, projects, seen, cache, graph=None):
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
    prefix_open = True
    for position, receipt in enumerate(receipts):
        if not isinstance(receipt, dict):
            raise ReplayError("invalid compiler call receipt")
        source = receipt.get("source")
        mode = receipt.get('mode', 'live')
        if mode not in ('live', 'prefix') or (mode == 'prefix') != bool(source):
            raise ReplayError('raw call mode disagrees with its replay source')
        if source:
            if not prefix_open:
                raise ReplayError('runtime prefix receipt follows a live or failed call')
            if not isinstance(source, dict):
                raise ReplayError("invalid replay call source")
            _, source_calls, _ = _source_calls(root, source.get("runId"), launch, compiler, projects, seen, cache,
                                              _source_hash(source))
            matches = [c for c in source_calls if c and c["agentId"] == source.get("agentId")
                       and c.get('runId') == source.get('runId') and c.get('artifactSha256') == source['artifactSha256']]
            if len(matches) != 1:
                raise ReplayError("replayed call source missing or ambiguous")
            original = matches[0]
            if source_calls.index(original) != position:
                raise ReplayError('runtime prefix source changed its call position')
            if any(canonical(receipt.get(k)) != canonical(original.get(k)) for k in ("prompt", "opts", "result")):
                raise ReplayError("replayed call differs from original call")
            validated.append(dict(copy.deepcopy(original), live=False))
            continue
        prefix_open = False
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
            if receipt.get('result') is None:
                validated.append(None)  # Failed/unfinished calls fence prefix replay.
                continue
            raise ReplayError("compiler call has no matching completed journal/transcript")
        call = matches[0]
        unused.remove(call)
        validated.append(dict(prompt=receipt["prompt"], opts=receipt["opts"], result=receipt.get("result"),
                              runId=run_id, artifactSha256=digest, agentId=call["agentId"], cost=call["cost"],
                              live=not receipt.get('source') and receipt.get('mode', 'live') == 'live'))
    verified = []
    for record in records:
        if isinstance(record, dict) and record.get('result') is None and record.get('mode') != 'memo':
            continue
        _validate_record(record, summary, launch, compiler)
        if record.get("mode") == "memo":
            source = record.get("source") or {}
            _, _, previous = _source_calls(root, source.get("runId"), launch, compiler, projects, seen, cache,
                                          _source_hash(source))
            candidates = [x for x in previous if x["key"] == record["key"] and x["agentId"] == source.get("agentId")
                          and x.get('runId') == source.get('runId') and x.get('artifactSha256') == source['artifactSha256']
                          and _memo_eligible(x)]
        else:
            candidates = [c for c in validated if c and c["prompt"] == record["prompt"]
                          and canonical(c["opts"]) == canonical(record["payload"]["opts"])
                          and canonical(c["result"]) == canonical(record.get("result"))]
        if len(candidates) != 1:
            raise ReplayError("memo receipt has no unique corroborated agent call")
        origin = candidates[0]
        if canonical(origin.get("result")) != canonical(record.get("result")):
            raise ReplayError("memo result differs from source")
        if record['mode'] == 'prefix':
            source = record.get('source') or {}
            if origin.get('live') or any(origin.get(k) != source.get(k) for k in ('runId', 'artifactSha256', 'agentId')):
                raise ReplayError('prefix receipt has no corroborated replay source')
        for key in record['payload']['ancestors']:
            ancestors = [r for r in records if isinstance(r, dict) and r.get('key') == key]
            if record['mode'] == 'memo' and (len(ancestors) != 1 or ancestors[0].get('mode') not in ('prefix', 'memo')):
                raise ReplayError('memo follows a live or missing ancestor')
        entry = dict(key=record["key"], prompt=record["prompt"], result=record.get("result"),
                     payload=record["payload"], runId=origin["runId"], agentId=origin["agentId"],
                     artifactSha256=origin.get('artifactSha256'),
                     cost=origin.get("cost", {"minutes": None, "tokens": None}),
                     replay=record.get("replay"), fresh=record.get("fresh", False),
                     irreversible=record.get("irreversible", False), contract=record.get("contract"))
        reading = record.get('reading') if record['mode'] == 'memo' else record['payload']['tree']
        if reading is not None and (record['mode'] == 'memo' or reading.get('branch') is not None):
            point = ('replay ' if record.get('replay') == 'inputs' else 'before ') + record['node']
            _sentinel(root, summary, validated, record.get('readingCallIndex'), reading, point, graph)
        if record['mode'] == 'memo':
            expected = origin['payload']['tree'] or origin.get('verifiedReading')
            if canonical(reading) != canonical(expected):
                raise ReplayError('memo has no matching live tree/branch sentinel reading')
        if record.get("verifiedReading") is not None:
            reading = record["verifiedReading"]
            # A builder's replay binds to an actual verifying gate's sentinel,
            # never a head claimed by either worker.
            verifier = record.get("verifiedByNode")
            declared = record.get("verifiedBy", [])
            if (not isinstance(declared, list) or verifier not in declared
                    or record['mode'] != 'memo' and not any(r.get("node") == verifier and record["node"] in (
                        r.get("verifies") if isinstance(r.get("verifies"), list) else [r.get("verifies")])
                        and r.get("payload", {}).get("tree") == reading for r in records)):
                raise ReplayError("builder verified reading has no verifying gate receipt")
            if record['mode'] == 'memo':
                if canonical(reading) != canonical(origin.get('verifiedReading')):
                    raise ReplayError('builder replay differs from original verifying sentinel')
            else:
                _sentinel(root, summary, validated, record.get('verifiedReadingCallIndex'), reading,
                          'before ' + verifier, graph)
            entry["verifiedReading"] = reading
            entry["verifiedByNode"] = verifier
        verified.append(entry)
    return summary, validated, verified


def _memo_eligible(entry):
    tree = entry['payload']['tree'] if entry.get('replay') == 'tree' else entry.get('verifiedReading')
    return (entry.get('replay') in ('inputs', 'tree') and not entry.get('fresh')
            and not entry.get('irreversible') and entry.get('contract') != 'RedTeamV1'
            and isinstance(tree, dict) and isinstance(tree.get('branch'), str) and bool(tree['branch'])
            and isinstance(tree.get('repository'), str) and bool(tree['repository']))


def build_memo(root, run_id, launch, compiler_version, projects=None, artifact_sha256=None):
    """Return launcher-only memo and prefix inputs after independent verification."""
    _, calls, entries = _source_calls(root, run_id, launch, compiler_version, projects, set(),
                                     artifact_sha256=artifact_sha256)
    memo = {}
    for entry in entries:
        if not _memo_eligible(entry):
            continue
        if entry["key"] in memo and canonical(memo[entry["key"]]) != canonical(entry):
            raise ReplayError("conflicting source memo entries")
        memo[entry["key"]] = entry
    prefix = []
    for c in calls:
        if not c or c.get('result') is None:
            break
        prefix.append(dict(promptSha256=hashlib.sha256(c["prompt"].encode("utf-8")).hexdigest(),
                   optsSha256=memo_key(c["opts"]), resultSha256=memo_key(c["result"]),
                   label=c["opts"].get("label"), runId=c["runId"], artifactSha256=c.get('artifactSha256'),
                   agentId=c["agentId"], cost=c.get("cost")))
    return {"_memo": memo, "_prefix": prefix}


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


def validate_current(root, summary, run_id, projects=None, graph=None):
    """Corroborate this attempt before a record can advance the latest pointer."""
    _component(run_id, 'current run id')
    compiler = _json(Path(__file__).resolve().parents[1] / '.claude-plugin/plugin.json')['version']
    return _validate_summary(root, summary, run_id, None, summary.get('launch'), compiler,
                             projects, set(), {}, graph)


def verify_replays(root, summary, projects=None, graph=None, run_id=None, validated=None):
    """Recorder check: re-read every memo replay's original journal and transcript."""
    records = summary.get("memoRecords", [])
    if not isinstance(records, list):
        raise ReplayError("memoRecords must be an array")
    checked = []
    sources = {}
    compiler_version = _json(Path(__file__).resolve().parents[1] / ".claude-plugin/plugin.json")["version"]
    nodes = _graph_nodes(graph) if graph is not None and records else None
    if records:
        validated = validated or validate_current(root, summary, run_id, projects, graph)
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
        if compiler != compiler_version:
            raise ReplayError("memo compiler version differs from the installed compiler")
        _validate_record(record, summary, summary.get("launch"), compiler)
        if canonical((summary.get("results") or {}).get(record["node"])) != canonical(record.get("result")):
            raise ReplayError("memo result differs from the node's recorded result")
        for key in payload["ancestors"]:
            ancestor = [r for r in records if isinstance(r, dict) and r.get("key") == key]
            if len(ancestor) != 1 or ancestor[0].get("mode") not in ("prefix", "memo"):
                raise ReplayError("memo follows a live or missing ancestor")
        source = record.get("source") or {}
        source_id = source.get("runId")
        identity = (source_id, _source_hash(source))
        if identity not in sources:
            sources[identity] = build_memo(root, source_id, summary.get("launch"), compiler, projects,
                                            artifact_sha256=identity[1])
        data = sources[identity]
        original = data["_memo"].get(record.get("key"))
        if (not original or original["agentId"] != source.get("agentId")
                or original.get('artifactSha256') != identity[1]
                or any(canonical(original.get(k)) != canonical(record.get(k)) for k in ("prompt", "result", "payload"))):
            raise ReplayError("REPLAYED-MEMO source does not corroborate result")
        expected_reading = payload["tree"] if payload["tree"] is not None else original.get("verifiedReading")
        if not isinstance(expected_reading, dict) or canonical(record.get("reading")) != canonical(expected_reading):
            raise ReplayError("memo has no matching live tree/branch sentinel reading")
        checked.append({"node": record["node"], "key": record["key"], "source": source})
    # A provenance label cannot bypass checks by omitting its memo receipt.
    labelled = [r for r in summary.get("provenance", []) if isinstance(r, dict)
                and r.get("status") in ("REPLAYED-MEMO", "REPLAYED")]
    if any(r.get("node") not in {x["node"] for x in checked} for r in labelled):
        raise ReplayError("replay label has no independently verified memo receipt")
    return checked


def verify_rejudges(root, summary, graph=None, projects=None, run_id=None, validated=None):
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
    validated = validated or validate_current(root, summary, run_id, projects, graph)
    _, current_calls, _ = validated
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
        source_hash = _source_hash({'artifactSha256': row.get('sourceArtifactSha256')})
        source, source_calls, source_entries = _source_calls(root, row.get('sourceRunId'), summary.get('launch'),
                _json(Path(__file__).resolve().parents[1] / '.claude-plugin/plugin.json')['version'],
                projects, set(), artifact_sha256=source_hash)
        if canonical(source.get("launch")) != canonical(summary.get("launch")):
            raise ReplayError("rejudge source belongs to another launch")
        previous = (source.get("results") or {}).get(gate)
        if previous is None or canonical(previous) != canonical(row.get("priorVerdict")):
            raise ReplayError("rejudge prior verdict differs from source artifact")
        prior_calls = [call for call in source_calls if call and call.get('result') is not None
                       and call['opts'].get('label') == gate and canonical(call['result']) == canonical(previous)]
        prior_entries = [entry for entry in source_entries if entry.get('payload', {}).get('id') == gate
                         and canonical(entry['result']) == canonical(previous)]
        if not prior_calls and not prior_entries:
            raise ReplayError('rejudge prior verdict has no corroborated completed agent call')
        gate_calls = [call for call in current_calls if call and call.get('live')
                      and call.get('result') is not None and call['opts'].get('label') == gate
                      and canonical(call['result']) == canonical(summary['results'][gate])]
        if len(gate_calls) != 1:
            raise ReplayError('rejudged gate has no unique live completed agent call')
        index = row.get('readingCallIndex')
        if index is None:
            matches = [r for r in summary.get('memoRecords', []) if isinstance(r, dict) and r.get('node') == gate]
            if len(matches) == 1:
                index = matches[0].get('readingCallIndex')
        sentinel = _sentinel(root, summary, current_calls, index, row['tree'], 'before ' + gate, graph)
        suffix = '\nREJUDGE NONCE: ' + nonce + '\nREJUDGE LAUNCH NONCE: ' + sentinel['readingNonce']
        if 'note' in declared:
            suffix += '\nOPERATOR REJUDGE NOTE (verbatim):\n' + declared['note']
        if suffix not in gate_calls[0]['prompt']:
            raise ReplayError('rejudged gate did not receive its exact fresh nonce and operator note')
        if row.get('gateCallIndex') is not None:
            index = row['gateCallIndex']
            if type(index) is not int or not 0 <= index < len(current_calls) or current_calls[index] is not gate_calls[0]:
                raise ReplayError('rejudge gate call index differs from its completed call')
        if not any(isinstance(r, dict) and r.get("point") == "before " + gate
                   and all(r.get(k) == row["tree"].get(k) for k in ("head", "root", "porcelain"))
                   for r in (summary.get("tree") or {}).get("checks", [])):
            raise ReplayError("rejudge tree has no matching gate sentinel reading")
        reached.add(gate)
        result.append(copy.deepcopy(row))
    if set(requested) != reached:
        raise ReplayError("rejudge requested a gate that was not reached")
    return result
