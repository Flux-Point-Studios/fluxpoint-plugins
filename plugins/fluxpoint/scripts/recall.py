#!/usr/bin/env python3
"""Hybrid recall: the memory the harness already keeps, made findable.

memory.jsonl remembers what campaigns established, but the only way back in
is an exact tag|dedupeKey match — a lesson about beacon-prefix derivation is
invisible to a session working on two-way asset beacons unless someone
guesses the tag. The relations that would connect them already exist, typed
and machine-produced: lesson→run provenance, supersession chains, path
components inside dedupe keys, node and campaign membership, pinned
counterexamples. Nothing walks them.

This tool closes that gap without adding a store or a writer. It compiles a
DERIVED index — a typed graph plus a lexical index plus optional dense
vectors — from the append-only stores that already exist, the way
compile-graph.py compiles the IR: deterministically, with no model in the
loop, byte-identical for identical sources. Repository indexes live under
gitignored .claude/fluxpoint/index/<schema>/; external corpus indexes live
once in the user's cache directory and are fused at read time. Both are
rebuildable caches; the stores
stay the only truth, and a malformed line in a SOURCE store is still a hard
error, while a damaged INDEX file is deleted and rebuilt out loud.

Retrieval fuses up to four kinds of evidence with weighted reciprocal-rank
fusion (k=60): BM25 over an identifier-aware tokenization of the query,
BM25 over path/symbol tokens from files touched, cosine against API
embeddings when a key is present (see embedder.py — the one sanctioned
non-deterministic input, and it can only reorder), and personalized
PageRank over the graph from tag/file/campaign seeds. Killed lessons are
retrieved and labelled — they are the half a fresh context cannot
reconstruct — and never used to suppress anything: recall output reaches
prompts and SessionStart context only, exactly like memory.py seeds, so a
re-found item is still judged on its merits.

  recall.py --build [--embed]          compile the index (embed = backfill vectors)
  recall.py --verify                   exit 1 when the index lags its sources
  recall.py --stats                    what is indexed, what is pending, what dropped
  recall.py --query "..." [--files a,b] [--tag t]... [--center id]
            [--k 12] [--as-of TS] [--include-superseded] [--offline]
            [--format lines|json|seedmap]
  recall.py --for-session              the SessionStart section: offline, capped, factual

--format seedmap emits exactly the {tag: {keys, killed}} shape
memory.py --load prints, with keys ordered by relevance instead of file
order, so /fluxpoint:graph-run can feed args._seen from here and the
compiled graph's advisory-seed contract is untouched.
"""
import argparse
import atexit
import datetime
import json
import math
import inspect
import os
import re
import subprocess
import hashlib
import tempfile
import time
import uuid
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import memory as memory_mod  # noqa: E402 - sibling module, house idiom
import embedder  # noqa: E402
import recall_sources  # noqa: E402
from workfile_lock import workfile_lock  # noqa: E402

INDEX_BASE = os.path.join(".claude", "fluxpoint", "index")
RUNS_DIR = os.path.join(".claude", "fluxpoint", "runs")
CEX = ".fluxpoint-cex.jsonl"
_REFRESH_CHILDREN = []
atexit.register(lambda: [child.poll() for child in _REFRESH_CHILDREN])
GRAPH_F, LEX_F, META_F = "graph.json", "lex.json", "index.meta.json"

DOC_KINDS = {"lesson", "cex", "decision", "primitive", "note"}
NODE_KINDS = DOC_KINDS | {"tag", "file", "run", "campaign", "gnode"}
EDGE_KINDS = {"TAGGED", "PRODUCED_IN", "KILLED_BY", "AT_NODE", "TOUCHES",
              "PART_OF", "PINS", "SUPERSEDES", "DECIDED_IN", "CONSUMES",
              "LOCATES"}
# How much rank mass each edge kind carries in the graph walk. SUPERSEDES
# is deliberately zero: the walk must never ride into a replaced row.
# KILLED_BY stays walkable and never expires — the kill is a currently-valid
# fact even though the claim it killed is not, which is how "kills are
# priors, never suppressors" holds in the type system instead of code
# convention.
EDGE_WEIGHT = {"TOUCHES": 1.0, "PINS": 0.7, "PRODUCED_IN": 0.7,
               "KILLED_BY": 0.7, "DECIDED_IN": 0.7, "CONSUMES": 0.7,
               "LOCATES": 0.7, "TAGGED": 0.5, "AT_NODE": 0.5,
               "PART_OF": 0.5, "SUPERSEDES": 0.0}
K1, B = 1.2, 0.4          # BM25, short-document setting
RRF_K = 60                # Cormack's fusion constant
LEG_WEIGHT = {"lex": 1.0, "ident": 1.0, "dense": 1.0, "graph": 0.7}
MIN_DENSE = 0.6           # cosine floor for the dense leg
DAMPING = 0.5             # PPR restart mass stays near the seeds
PATH_RE = re.compile(r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)+$")
# substrate.json is another plugin's hand-edited manifest, so its strings get
# substrate's own hostile-input discipline before they can reach an injected
# line: control and separator characters stripped, lengths capped.
CTRL_RE = re.compile("[\\u0000-\\u001f\\u007f-\\u009f\\u2028\\u2029]")


def _clean(v, cap=500):
    return CTRL_RE.sub(" ", str(v))[:cap]


def _in_repo(root, part):
    """True when part resolves to an existing path INSIDE root.

    Path strings arriving from dedupe keys, cex pins, and substrate
    manifests are data someone else wrote; an absolute path or a
    ..-segment must not be able to edge the graph at files outside the
    tree, however plausible the file it names.
    """
    if not part or part.startswith(("/", "\\")) or ".." in part.split("/"):
        return False
    full = os.path.abspath(os.path.normpath(os.path.join(root, part)))
    base = os.path.abspath(root)
    if full != base and not full.startswith(base + os.sep):
        return False
    return os.path.exists(full)


def _dumps(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_ts(ts):
    try:
        value = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def tokenize(text):
    """fpl-ident-v1: identifier-aware, deterministic, no stemming.

    Every compound token (path, snake_case name, hex run) is kept whole AND
    split, so an exact-identifier query lands and a prose paraphrase still
    overlaps. Stemming is refused: it varies by implementation and this
    corpus's jargon is exactly what stemmers mangle.
    """
    toks = []
    for raw in re.findall(r"[A-Za-z0-9_./-]+", text or ""):
        whole = raw.strip("./-_").lower()
        if len(whole) >= 2:
            toks.append(whole)
        spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", raw)
        for piece in re.split(r"[^A-Za-z0-9]+", spaced):
            piece = piece.lower()
            if len(piece) >= 2 and piece != whole:
                toks.append(piece)
    return toks


def index_dir(root):
    return os.path.abspath(os.path.join(root, INDEX_BASE, INDEX_SCHEMA))


def shared_index_dir(root):
    corpora = sorted(recall_sources.corpus_roots(root), key=_dumps)
    identity = {"corpora": corpora}
    # Codex has a user-wide store but its reader applies a project cwd policy.
    # Different policy views must never reuse each other's private passages.
    if any(c["format"] == "codex-memories" for c in corpora):
        identity["scope"] = os.path.normcase(os.path.realpath(root))
    key = hashlib.sha256(_dumps(identity).encode()).hexdigest()
    cache = os.environ.get("XDG_CACHE_HOME") or (os.path.join(os.environ["LOCALAPPDATA"], "Cache") if os.name == "nt" and os.environ.get("LOCALAPPDATA") else os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.abspath(os.path.join(cache, "fluxpoint", "recall", INDEX_SCHEMA, key))


def writer_identity():
    try:
        with open(os.path.join(HERE, "..", "plugin.json"), encoding="utf-8") as source:
            version = json.load(source).get("version", "unknown")
    except (OSError, ValueError):
        version = "unknown"
    return {"version": version, "schema": INDEX_SCHEMA}


# ---------------------------------------------------------------- sources

def _run_directories(root):
    shared = memory_mod.runs_path(root) if hasattr(memory_mod, "runs_path") else os.path.join(root, RUNS_DIR)
    return list(dict.fromkeys([os.path.abspath(os.path.join(root, RUNS_DIR)), os.path.abspath(shared)]))


def _read_runs(root):
    """Shared durable artifacts plus legacy checkout artifacts; malformed sources refuse."""
    out = {}
    for directory in _run_directories(root):
        if not os.path.isdir(directory): continue
        for filename in sorted(os.listdir(directory)):
            if not filename.endswith(".json"): continue
            path = os.path.join(directory, filename)
            try:
                with open(path, encoding="utf-8") as fh: artifact = json.load(fh)
            except (OSError, json.JSONDecodeError) as exc:
                raise SystemExit(f"recall: {path} is not readable JSON: {exc}")
            if not isinstance(artifact, dict):
                raise SystemExit(f"recall: {path} must contain a JSON object")
            run_id = artifact.get("runId") or filename[:-5]
            previous = out.get(run_id)
            if previous is None or str(artifact.get("when") or "") >= str(previous.get("when") or ""):
                out[run_id] = artifact
    return out


def _read_cex(root):
    p = os.path.join(root, CEX)
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise SystemExit(f"recall: {p}:{i} is not valid JSON: {e}")
    return rows


def _read_substrate(root):
    """The repo's own substrate.json primitives, sanitized. This is another
    plugin's hand-edited manifest, not one of this layer's machine-produced
    stores, so it gets substrate's own discipline — treat as hostile input,
    sanitize, and skip damage by name — rather than the hard-fail rule the
    stores earn. Returns (primitives, note-or-None)."""
    p = os.path.join(root, "substrate.json")
    if not os.path.exists(p):
        return [], None
    try:
        with open(p, encoding="utf-8") as fh:
            m = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        return [], f"substrate.json unreadable ({e}); primitives not indexed"
    prims = m.get("primitives") if isinstance(m, dict) else None
    if not isinstance(prims, list):
        return [], "substrate.json carries no primitives list; nothing to index"
    repo = _clean((m.get("repo") or ""), 200)
    out = []
    for pr in prims[:200]:
        if not isinstance(pr, dict) or not pr.get("id"):
            continue
        # Container types are hostile input too: a string-valued `paths`
        # would slice into characters, a dict would raise mid-build.
        paths = pr.get("paths") if isinstance(pr.get("paths"), list) else []
        consumes = (pr.get("consumes")
                    if isinstance(pr.get("consumes"), list) else [])
        out.append({
            "id": _clean(pr["id"], 200),
            "repo": repo,
            "kind": _clean(pr.get("kind") or "", 80),
            "status": _clean(pr.get("status") or "", 80),
            "desc": _clean(pr.get("desc") or "", 500),
            "paths": [_clean(x, 500) for x in paths[:50]
                      if isinstance(x, str)],
            "consumes": [_clean(x, 200) for x in consumes[:50]
                         if isinstance(x, str)],
        })
    note = (f"substrate.json lists {len(prims)} primitives; indexed the "
            f"first 200" if len(prims) > 200 else None)
    return out, note


def _source_fingerprints(root):
    fp = {}
    paths = memory_mod.source_paths(root) if hasattr(memory_mod, "source_paths") else [memory_mod.path_for(root)]
    existing = [path for path in paths if os.path.exists(path)]
    fp["memory.jsonl"] = hashlib.sha256("|".join(path + ":" + _sha_file(path) for path in existing).encode()).hexdigest() if existing else "absent"
    sub = os.path.join(root, "substrate.json")
    fp["substrate.json"] = _sha_file(sub) if os.path.exists(sub) else "absent"
    cex = os.path.join(root, CEX)
    fp["cex.jsonl"] = _sha_file(cex) if os.path.exists(cex) else "absent"
    directories = _run_directories(root)
    parts = []
    for directory in directories:
        if os.path.isdir(directory):
            for filename in sorted(os.listdir(directory)):
                if filename.endswith(".json"):
                    parts.append(f"{directory}/{filename}:{_sha_file(os.path.join(directory, filename))}")
    fp["runs"] = hashlib.sha256("|".join(parts).encode()).hexdigest() if any(os.path.isdir(d) for d in directories) else "absent"
    return fp


def _corpus_fingerprints(root):
    rows = []
    for corpus in sorted(recall_sources.corpus_roots(root), key=_dumps):
        files = []
        for path in recall_sources.files(corpus):
            try:
                stat = path.stat()
                files.append([path.relative_to(corpus["path"]).as_posix(), stat.st_size, stat.st_mtime_ns])
            except OSError:
                files.append([str(path), "unreadable"])
        rows.append({**corpus, "files": files})
    return hashlib.sha256(_dumps(rows).encode()).hexdigest()


# Schema identity describes everything needed to interpret derived files. Older
# plugin versions keep their own slot and never overwrite this reader's cache.
INDEX_SCHEMA = hashlib.sha256(_dumps({
    "format": 2, "docs": sorted(DOC_KINDS), "nodes": sorted(NODE_KINDS),
    "edges": sorted(EDGE_KINDS), "tokenizer": inspect.getsource(tokenize),
    "fingerprints": inspect.getsource(_source_fingerprints) + inspect.getsource(_corpus_fingerprints),
    "corpusReader": inspect.getsource(recall_sources.notes),
}).encode()).hexdigest()[:20]
INDEX_DIR = os.path.join(INDEX_BASE, INDEX_SCHEMA)


def _source_doc_counts(root):
    """Current documents in each typed source, without building an index."""
    lessons = memory_mod.current(root)
    decisions = set()
    for art in _read_runs(root).values():
        summary = art.get("summary") if isinstance(art, dict) else None
        rows = summary.get("decisions") if isinstance(summary, dict) else None
        if isinstance(rows, dict):
            decisions.update(k for k, v in rows.items()
                             if isinstance(v, dict))
    counterexamples = {str(row.get("cexId")) for row in _read_cex(root)
                       if isinstance(row, dict) and row.get("cexId")}
    primitives, _ = _read_substrate(root)
    return {"lesson": len(lessons), "decision": len(decisions),
            "cex": len(counterexamples),
            "primitive": len({row["id"] for row in primitives}),
            "note": sum(len(recall_sources.files(corpus)) for corpus in recall_sources.corpus_roots(root))}


# ------------------------------------------------------------------ build

def _atomic_text(path, payload):
    fd, temporary = tempfile.mkstemp(prefix=".recall-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5: raise
                time.sleep(.02 * (attempt + 1))
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def _atomic_json(path, obj):
    _atomic_text(path, _dumps(obj))


def _compile_lex(nodes):
    lex_docs, df = {}, {}
    for nid, node in sorted(nodes.items()):
        if node.get("kind") not in DOC_KINDS:
            continue
        toks = tokenize(node.get("text") or "")
        tf = {}
        for token in toks:
            tf[token] = tf.get(token, 0) + 1
        lex_docs[nid] = {"len": len(toks), "tf": tf}
        for token in tf:
            df[token] = df.get(token, 0) + 1
    count = len(lex_docs)
    return {"version": 1, "tokenizer": "fpl-ident-v1", "k1": K1, "b": B,
            "N": count, "avgdl": sum(d["len"] for d in lex_docs.values()) / count if count else 0.,
            "df": df, "docs": lex_docs}


def _slot_stale(slot, sources):
    meta = _read_json_path(os.path.join(slot, META_F))
    if not isinstance(meta, dict) or meta.get("sources") != sources:
        return True
    for filename, expected in (meta.get("files") or {}).items():
        try:
            stat = os.stat(os.path.join(slot, filename))
            if [stat.st_size, stat.st_mtime_ns] != expected:
                return True
        except OSError:
            return True
    return False


def _shared_index(root):
    slot = shared_index_dir(root)
    graph, lex, meta = [_read_json_path(os.path.join(slot, fn)) for fn in (GRAPH_F, LEX_F, META_F)]
    if any(not isinstance(obj, dict) for obj in (graph, lex, meta)):
        return None, None, "shared memory index not built yet"
    if not isinstance(graph.get("nodes"), dict) or not isinstance(lex.get("docs"), dict):
        return None, None, "shared memory index schema mismatch; rebuild pending"
    if len({obj.get("generation") for obj in (graph, lex, meta)}) != 1:
        return None, None, "shared memory index generation mismatch; rebuild pending"
    if any(node.get("kind") not in NODE_KINDS for node in graph.get("nodes", {}).values()):
        return None, None, "shared memory index schema mismatch; rebuild pending"
    return graph, lex, None


def _meta_view(root):
    meta = _load_json(root, META_F)
    if meta is None:
        return None
    meta = dict(meta)
    meta["kinds"] = dict(meta.get("kinds") or {})
    if not meta["kinds"]:
        graph = _load_json(root, GRAPH_F) or {}
        meta["kinds"] = {kind: 0 for kind in DOC_KINDS}
        for node in graph.get("nodes", {}).values():
            kind = node.get("kind")
            if kind in meta["kinds"]:
                meta["kinds"][kind] += 1
    shared = _read_json_path(os.path.join(shared_index_dir(root), META_F)) or {}
    for key in ("docs", "nodes", "edges", "pending"):
        meta[key] = meta.get(key, 0) + shared.get(key, 0)
    for kind, count in shared.get("kinds", {}).items():
        meta["kinds"][kind] = meta["kinds"].get(kind, 0) + count
    return meta


def _build_shared(root, embed_backfill=False, transport=None):
    slot = shared_index_dir(root)
    os.makedirs(slot, mode=0o700, exist_ok=True)
    with workfile_lock(os.path.join(slot, "build")):
        source_fp = {"notes": _corpus_fingerprints(root)}
        dense_spec = embedder.resolve()
        if not _slot_stale(slot, source_fp):
            meta = _read_json_path(os.path.join(slot, META_F))
            if not embed_backfill and meta.get("denseSpec") == dense_spec:
                return meta
            nodes = _read_json_path(os.path.join(slot, GRAPH_F))["nodes"]
            meta.update(_sync_vectors(root, nodes, embed_backfill, transport, slot=slot))
            meta.update(writer=writer_identity(), denseSpec=dense_spec)
            _atomic_json(os.path.join(slot, META_F), meta)
            return meta
        nodes, diagnostics = recall_sources.notes(root)
        for diagnostic in diagnostics:
            print("recall: " + diagnostic, file=sys.stderr)
        graph = {"version": 1, "nodes": nodes, "edges": []}
        lex = _compile_lex(nodes)
        generation = hashlib.sha256(_dumps({"sources": source_fp, "graph": graph, "lex": lex}).encode()).hexdigest()
        graph["generation"] = lex["generation"] = generation
        for filename, obj in ((GRAPH_F, graph), (LEX_F, lex)):
            _atomic_json(os.path.join(slot, filename), obj)
        dense = _sync_vectors(root, nodes, embed_backfill, transport, slot=slot)
        meta = {"version": 2, "writer": writer_identity(), "sources": source_fp,
                "generation": generation, "docs": lex["N"], "nodes": len(nodes), "edges": 0,
                "kinds": {"note": len(nodes)}, "denseSpec": dense_spec, **dense,
                "files": {fn: [os.stat(os.path.join(slot, fn)).st_size, os.stat(os.path.join(slot, fn)).st_mtime_ns]
                          for fn in (GRAPH_F, LEX_F)}}
        _atomic_json(os.path.join(slot, META_F), meta)
        return meta


def build(root, embed_backfill=False, transport=None):
    idx = index_dir(root)
    os.makedirs(idx, exist_ok=True)
    with workfile_lock(os.path.join(idx, "build")):
        stats = _build(root, embed_backfill, transport)
    shared = _build_shared(root, embed_backfill, transport)
    for key in ("docs", "nodes", "edges", "pending"):
        stats[key] += shared.get(key, 0)
    for kind, count in shared.get("kinds", {}).items():
        stats["kinds"][kind] = stats["kinds"].get(kind, 0) + count
    return stats


def _build(root, embed_backfill=False, transport=None):
    """Compile the index. Returns a stats dict; degradations are in it.

    graph.json and lex.json are byte-identical across rebuilds from
    identical sources (sorted keys, sorted edges, no timestamps in the
    body). The vector file depends on the active provider and is cached by
    content hash, so a rebuild re-embeds only new or changed cards.
    """
    source_fp = _source_fingerprints(root)
    rows = memory_mod.read(root)          # hard-fails on a malformed line
    runs = _read_runs(root)
    cexes = _read_cex(root)

    histories = {}
    for r in rows:
        histories.setdefault(f"{r.get('tag')}|{r.get('dedupeKey')}", []).append(r)

    nodes, edges = {}, set()
    dropped_paths = 0

    def entity(nid, kind, **attrs):
        if nid not in nodes:
            nodes[nid] = {"kind": kind, **attrs}
        return nid

    for ident, hist in sorted(histories.items()):
        hist.sort(key=memory_mod.history_order)
        latest = hist[-1]
        tag, key = latest.get("tag"), latest.get("dedupeKey")
        nid = f"lesson:{ident}"
        # The retrieval card: claim and objection carry the meaning, the
        # identifiers (dedupe key, tag, node) carry the exact-match tokens
        # a diff-derived query lands on. Rendering uses the bare claim.
        text = str(latest.get("claim") or "")
        if latest.get("objection"):
            text += " || objection: " + str(latest["objection"])
        text += f" || {key} {tag} {latest.get('node') or ''}"
        run_ids = sorted({h.get("provenance", {}).get("runId")
                          for h in hist if h.get("provenance", {}).get("runId")})
        nodes[nid] = {
            "kind": "lesson", "tag": tag, "dedupeKey": key, "text": text,
            "claim": str(latest.get("claim") or ""),
            "status": latest.get("status"),
            "objection": latest.get("objection") or "",
            "kills": latest.get("kills", 0), "node": latest.get("node"),
            "runs": run_ids,
            "created": hist[0].get("establishedWhen"),
            "when": latest.get("establishedWhen"),
            "versions": [{"when": h.get("establishedWhen"),
                          "status": h.get("status"),
                          "claim": h.get("claim"),
                          "objection": h.get("objection") or "",
                          "runId": h.get("provenance", {}).get("runId")}
                         for h in hist],
        }
        edges.add((nid, "TAGGED", entity(f"tag:{tag}", "tag")))
        for rid in run_ids:
            edges.add((nid, "PRODUCED_IN", entity(f"run:{rid}", "run")))
        if latest.get("status") == "killed":
            kill_rid = latest.get("provenance", {}).get("runId")
            if kill_rid:
                edges.add((nid, "KILLED_BY",
                           entity(f"run:{kill_rid}", "run")))
        if latest.get("node"):
            edges.add((nid, "AT_NODE",
                       entity(f"gnode:{latest['node']}", "gnode")))
        for part in str(key or "").split("|"):
            part = part.strip()
            if PATH_RE.match(part):
                if _in_repo(root, part):
                    edges.add((nid, "TOUCHES", entity(f"file:{part}", "file")))
                else:
                    dropped_paths += 1

    for rid, art in sorted(runs.items()):
        rnid = entity(f"run:{rid}", "run")
        nodes[rnid]["when"] = art.get("when")
        camp = (art.get("summary") or {}).get("campaign") or ""
        if camp:
            nodes[rnid]["campaign"] = camp
            edges.add((rnid, "PART_OF", entity(f"campaign:{camp}", "campaign")))

    # Decisions, keyed by the id the compiler already resolves imports by —
    # the one row class in WORK.md with a stable machine identity, read from
    # the run artifacts (pure JSON) rather than the markdown table. A later
    # run re-deciding the same id wins, and both runs stay on the edge list.
    for rid, art in sorted(runs.items(),
                           key=lambda kv: (str(kv[1].get("when") or ""),
                                           kv[0])):
        decs = (art.get("summary") or {}).get("decisions")
        if not isinstance(decs, dict):
            continue
        for did, d in sorted(decs.items()):
            if not isinstance(d, dict):
                continue
            nid = f"decision:{did}"
            text = " ".join(str(d.get(k) or "")
                            for k in ("question", "chosen", "rationale"))
            prior = nodes.get(nid, {})
            run_list = [r for r in prior.get("runs", []) if r != rid] + [rid]
            # Versions accumulate in decided order so --as-of can serve the
            # choice that actually governed at that time — a time-travel
            # query answering with today's chosen would be an anachronism
            # wearing a timestamp.
            versions = prior.get("versions", []) + [
                {"when": art.get("when"), "text": text.strip(),
                 "chosen": str(d.get("chosen") or ""), "runId": rid}]
            nodes[nid] = {"kind": "decision", "text": text.strip(),
                          "chosen": str(d.get("chosen") or ""),
                          "when": art.get("when"), "runs": run_list,
                          "versions": versions}
            edges.add((nid, "DECIDED_IN", f"run:{rid}"))

    for row in cexes:
        cid = row.get("cexId")
        if not cid:
            continue
        nid = f"cex:{cid}"
        text = " ".join(str(row.get(k) or "")
                        for k in ("title", "module", "tool")).strip()
        nodes[nid] = {"kind": "cex", "text": text,
                      "status": row.get("status") or "",
                      "when": (row.get("pin") or {}).get("pinnedWhen")
                      or row.get("when") or ""}
        pin = row.get("pin") or {}
        if pin.get("file") and _in_repo(root, str(pin["file"])):
            edges.add((nid, "PINS", entity(f"file:{pin['file']}", "file")))

    # The repo's declared primitives join the graph, so "does this already
    # exist" and "what did we learn about it" answer from one walk: a lesson
    # touching a primitive's file is two hops from the primitive, and its
    # consumers are one more.
    prims, sub_note = _read_substrate(root)
    if sub_note:
        print(f"recall: {sub_note}", file=sys.stderr)
    for pr in prims:
        nodes[f"prim:{pr['id']}"] = {
            "kind": "primitive",
            "text": f"{pr['desc']} || {pr['id']} {pr['kind']}".strip(),
            "repo": pr["repo"], "status": pr["status"]}
    for pr in prims:
        nid = f"prim:{pr['id']}"
        for tgt in pr["consumes"]:
            tid = f"prim:{tgt}"
            if tid not in nodes:
                # A consumed primitive declared by another repo: a stub with
                # its id as the only text, kept so the CONSUMES chain stays
                # walkable rather than snapping at the repo boundary.
                nodes[tid] = {"kind": "primitive", "text": tgt,
                              "repo": "", "status": ""}
            edges.add((nid, "CONSUMES", tid))
        for path_ in pr["paths"]:
            if _in_repo(root, path_):
                edges.add((nid, "LOCATES", entity(f"file:{path_}", "file")))

    # gnode -> campaign membership, via the runs that filed lessons there.
    for nid, n in list(nodes.items()):
        if n["kind"] != "lesson" or not n.get("node"):
            continue
        for rid in n["runs"]:
            camp = nodes.get(f"run:{rid}", {}).get("campaign")
            if camp:
                edges.add((f"gnode:{n['node']}", "PART_OF", f"campaign:{camp}"))

    # Stale kills, decided at build time so no hook ever spawns git. A
    # killed lesson whose touched file changed after the kill is MARKED —
    # never dropped or down-ranked — because a finding that comes back
    # after the code moved is exactly the regression a sweep exists to
    # catch, and hiding the kill would hide it.
    stale_kills = _mark_stale_kills(root, nodes, edges)

    graph = {"version": 1, "nodes": nodes,
             "edges": sorted(list(e) for e in edges)}

    docs = {nid: n for nid, n in nodes.items() if n["kind"] in DOC_KINDS}
    lex = _compile_lex(docs)
    n_docs = lex["N"]

    generation = hashlib.sha256(_dumps({"sources": source_fp, "graph": graph, "lex": lex}).encode()).hexdigest()
    graph["generation"] = lex["generation"] = generation
    idx = index_dir(root)
    os.makedirs(idx, exist_ok=True)
    ignore = os.path.join(root, ".claude", "fluxpoint", ".gitignore")
    if not os.path.exists(ignore):
        with open(ignore, "w", encoding="utf-8") as fh: fh.write("*\n")
    for fn, obj in ((GRAPH_F, graph), (LEX_F, lex)):
        _atomic_json(os.path.join(idx, fn), obj)

    kinds = {kind: sum(1 for n in docs.values() if n["kind"] == kind)
             for kind in sorted(DOC_KINDS)}
    stats = {"docs": n_docs, "nodes": len(nodes), "edges": len(edges),
             "droppedPaths": dropped_paths, "staleKills": stale_kills,
             "primitives": len(prims), "kinds": kinds,
             "pending": 0, "provider": "none"}
    stats.update(_sync_vectors(root, docs, embed_backfill, transport))

    meta = {"version": 2, "writer": writer_identity(), "sources": source_fp, "generation": generation,
            "files": {fn: [os.stat(os.path.join(idx, fn)).st_size, os.stat(os.path.join(idx, fn)).st_mtime_ns] for fn in (GRAPH_F, LEX_F)},
            "docs": n_docs, "pending": stats["pending"],
            "provider": stats["provider"],
            "droppedPaths": dropped_paths, "staleKills": stale_kills,
            "kinds": kinds}
    _atomic_json(os.path.join(idx, META_F), meta)
    return stats


def _mark_stale_kills(root, nodes, edges):
    """Sets node['stale'] = <path> on killed lessons whose touched file
    changed after the kill. Returns how many were marked."""
    touches = {}
    for src, kind, dst in edges:
        if kind == "TOUCHES":
            touches.setdefault(src, []).append(dst[len("file:"):])
    checked, marked = {}, 0
    for nid in sorted(nodes):
        n = nodes[nid]
        if n["kind"] != "lesson" or n.get("status") != "killed":
            continue
        killed_at = _parse_ts(n.get("when"))
        if killed_at is None:
            continue
        for path in sorted(touches.get(nid, [])):
            if path not in checked:
                try:
                    out = subprocess.run(
                        ["git", "log", "-1", "--format=%cI", "--", path],
                        capture_output=True, text=True, timeout=10, cwd=root)
                    checked[path] = _parse_ts(out.stdout.strip())
                except (OSError, subprocess.TimeoutExpired):
                    checked[path] = None
            if checked[path] is not None and checked[path] > killed_at:
                n["stale"] = path
                marked += 1
                break
    return marked


def _vec_path(root, spec, slot=None):
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", spec["model"])
    return os.path.join(slot or index_dir(root),
                        f"vec-{spec['provider']}-{safe_model}-{spec['dims']}.jsonl")


def _load_vectors(root, spec, slot=None):
    """{docId: {sha, vec}} from the active provider's file. A damaged
    vector file is a cache, not a store: it is discarded out loud and the
    rows become pending, because a hook must never die on it."""
    p = _vec_path(root, spec, slot)
    if not os.path.exists(p):
        return {}
    out = {}
    try:
        with open(p, encoding="utf-8") as fh:
            header = json.loads(fh.readline())
            if (header.get("model") != spec["model"]
                    or header.get("dims") != spec["dims"]):
                print(f"recall: {p} header names a different model/dims; "
                      f"rebuilding that dense file", file=sys.stderr)
                return {}
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                out[row["id"]] = {"sha": row["sha"],
                                  "vec": embedder.unpack(row["v"], spec["dims"])}
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        print(f"recall: discarding damaged vector cache {p}: {e}",
              file=sys.stderr)
        return {}
    return out


def _sync_vectors(root, docs, backfill, transport, slot=None):
    spec = embedder.resolve()
    if spec["provider"] == "none":
        return {"pending": len(docs), "provider": "none"}
    have = _load_vectors(root, spec, slot)
    want = {nid: (docs[nid].get("text") or "") for nid in sorted(docs)
            if docs[nid].get("kind") != "note" or os.environ.get("FPL_EMBEDDER") not in (None, "", "none")}
    keep, missing = {}, []
    for nid, text in want.items():
        sha = embedder.text_sha(text)
        if nid in have and have[nid]["sha"] == sha:
            keep[nid] = {"sha": sha, "vec": have[nid]["vec"]}
        else:
            missing.append((nid, text, sha))
    embedded = 0
    if backfill and missing:
        try:
            vecs = embedder.embed([t for _, t, _ in missing], "document",
                                  spec, transport=transport)
            for (nid, _, sha), vec in zip(missing, vecs):
                keep[nid] = {"sha": sha, "vec": vec}
                embedded += 1
            missing = []
        except embedder.EmbedError as e:
            print(f"recall: dense backfill failed ({e}); "
                  f"{len(missing)} card(s) stay pending", file=sys.stderr)
    p = _vec_path(root, spec, slot)
    lines = [_dumps({"version": 1, "provider": spec["provider"],
                     "model": spec["model"], "dims": spec["dims"],
                     "dtype": "f32", "normalized": True})]
    for nid in sorted(keep):
        lines.append(_dumps({"id": nid, "sha": keep[nid]["sha"],
                             "v": embedder.pack(keep[nid]["vec"])}))
    _atomic_text(p, "\n".join(lines) + "\n")
    return {"pending": len(missing), "provider": spec["provider"],
            "embedded": embedded}


# ------------------------------------------------------------------- load

def _load_json(root, fn):
    return _read_json_path(os.path.join(index_dir(root), fn))


def _read_json_path(p):
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        print(f"recall: discarding damaged index file {p}: {e}",
              file=sys.stderr)
        return None


def is_stale(root):
    meta = _load_json(root, META_F)
    if not meta:
        return True
    if meta.get("sources") != _source_fingerprints(root): return True
    for filename, expected in (meta.get("files") or {}).items():
        try:
            actual = os.stat(os.path.join(index_dir(root), filename))
            if [actual.st_size, actual.st_mtime_ns] != expected: return True
        except OSError: return True
    return _slot_stale(shared_index_dir(root), {"notes": _corpus_fingerprints(root)})


def ensure_index(root, offline=True, allow_rebuild=True):
    """Fresh graph+lex where allowed, stale-but-named where not.

    Returns (graph, lex, note-or-None). With allow_rebuild=False — the hook
    contract — a stale index is USED and the staleness named, because a
    SessionStart that spends its 15s window rebuilding injects nothing at
    all; a missing index returns (None, None, note) and the caller falls
    back. CLI paths rebuild on any mismatch.
    """
    note = None
    if is_stale(root):
        if allow_rebuild:
            build(root, embed_backfill=False)
        else:
            note = ("memory index lags the stores; ranking reflects the "
                    "last build — refresh with: py.sh recall.py --build")
    graph, lex = _load_json(root, GRAPH_F), _load_json(root, LEX_F)
    if (graph is None or lex is None) and allow_rebuild:
        build(root, embed_backfill=False)
        graph, lex = _load_json(root, GRAPH_F), _load_json(root, LEX_F)
    if graph is None or lex is None:
        if not allow_rebuild:
            return None, None, "memory index not built yet"
        raise SystemExit("recall: index rebuild produced no readable index")
    meta = _load_json(root, META_F) or {}
    generations = [obj.get("generation") for obj in (graph, lex, meta)]
    if len(set(generations)) != 1:
        if allow_rebuild:
            build(root, embed_backfill=False)
            return ensure_index(root, offline=offline, allow_rebuild=False)
        return None, None, "memory index generation mismatch; rebuild pending"
    for nid, n in graph["nodes"].items():
        if n.get("kind") not in NODE_KINDS:
            raise SystemExit(
                f"recall: {nid} has unknown kind {n.get('kind')!r} — the "
                f"index registry is closed; rebuild with --build")
    shared, shared_lex, shared_note = _shared_index(root)
    if shared_note:
        return None, None, shared_note
    if shared is not None:
        generation = hashlib.sha256((graph["generation"] + shared["generation"]).encode()).hexdigest()
        graph = {**graph, "nodes": {**graph["nodes"], **shared["nodes"]},
                 "edges": graph["edges"] + shared["edges"], "generation": generation}
        docs = {**lex["docs"], **shared_lex["docs"]}
        df = dict(lex["df"])
        for token, count in shared_lex["df"].items():
            df[token] = df.get(token, 0) + count
        lex = {"version": 1, "tokenizer": "fpl-ident-v1", "k1": K1, "b": B,
               "N": len(docs), "avgdl": sum(d["len"] for d in docs.values()) / len(docs) if docs else 0.,
               "docs": docs, "df": df, "partitions": [lex, shared_lex], "generation": generation}
    return graph, lex, note


# -------------------------------------------------------------------- legs

def bm25(lex, query_tokens, limit=50):
    if lex.get("partitions"):
        scores = {}
        for partition in lex["partitions"]:
            for rank, (nid, _) in enumerate(bm25(partition, query_tokens, limit)):
                scores[nid] = scores.get(nid, 0) + 1 / (RRF_K + rank + 1)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
    if not query_tokens or not lex["N"]:
        return []
    scores = {}
    seen_q = []
    for t in query_tokens:
        if t not in seen_q:
            seen_q.append(t)
    for t in seen_q:
        n_t = lex["df"].get(t)
        if not n_t:
            continue
        idf = math.log(1 + (lex["N"] - n_t + 0.5) / (n_t + 0.5))
        for nid, d in lex["docs"].items():
            tf = d["tf"].get(t)
            if not tf:
                continue
            denom = tf + K1 * (1 - B + B * d["len"] / (lex["avgdl"] or 1))
            scores[nid] = scores.get(nid, 0.0) + idf * tf * (K1 + 1) / denom
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]


def dense_leg(root, query, offline, diagnostics, limit=50):
    spec = embedder.resolve()
    if spec["provider"] == "none":
        diagnostics.append("dense leg absent (no embedder key; "
                           "lexical + graph only)")
        return []
    if offline:
        diagnostics.append(f"dense leg skipped (offline mode; provider "
                           f"{spec['provider']} configured)")
        return []
    vecs = _load_vectors(root, spec)
    vecs.update(_load_vectors(root, spec, shared_index_dir(root)))
    if not vecs:
        diagnostics.append(f"dense leg empty (no vectors cached for "
                           f"{spec['provider']} {spec['model']}; run --build --embed)")
        return []
    try:
        qv = embedder.embed([query], "query", spec)[0]
    except embedder.EmbedError as e:
        diagnostics.append(f"dense leg skipped ({e})")
        return []
    scored = []
    for nid in sorted(vecs):
        s = sum(a * b for a, b in zip(qv, vecs[nid]["vec"]))
        if s >= MIN_DENSE:
            scored.append((nid, s))
    return sorted(scored, key=lambda kv: (-kv[1], kv[0]))[:limit]


def _adjacency(graph):
    adj = {}
    for src, kind, dst in graph["edges"]:
        w = EDGE_WEIGHT.get(kind, 0.0)
        if w <= 0:
            continue
        adj.setdefault(src, []).append((dst, w))
        adj.setdefault(dst, []).append((src, w))
    return {k: sorted(v) for k, v in sorted(adj.items())}


def ppr(graph, seeds, limit=50, iters=30, tol=1e-9):
    """Personalized PageRank, sparse dict power iteration, deterministic.

    Seeds are weighted by specificity — 1/degree for entity nodes — the
    graph-native IDF that keeps hub files from dominating every walk.
    """
    adj = _adjacency(graph)
    seed_w = {}
    for nid, w in sorted(seeds.items()):
        if nid not in graph["nodes"]:
            continue
        deg = len(adj.get(nid, [])) or 1
        kind = graph["nodes"][nid]["kind"]
        spec = 1.0 / deg if kind not in DOC_KINDS else 1.0
        seed_w[nid] = w * spec
    total = sum(seed_w.values())
    if total <= 0:
        return []
    seed_w = {k: v / total for k, v in seed_w.items()}
    p = dict(seed_w)
    for _ in range(iters):
        nxt = {k: DAMPING * v for k, v in seed_w.items()}
        for nid in sorted(p):
            mass, out = p[nid], adj.get(nid, [])
            if not out:
                continue
            wsum = sum(w for _, w in out)
            share = (1 - DAMPING) * mass / wsum
            for dst, w in out:
                nxt[dst] = nxt.get(dst, 0.0) + share * w
        delta = sum(abs(nxt.get(k, 0.0) - p.get(k, 0.0))
                    for k in set(p) | set(nxt))
        p = nxt
        if delta < tol:
            break
    docs = [(nid, s) for nid, s in p.items()
            if graph["nodes"].get(nid, {}).get("kind") in DOC_KINDS]
    return sorted(docs, key=lambda kv: (-kv[1], kv[0]))[:limit]


def _hops_from(graph, center, cap=6):
    adj = _adjacency(graph)
    dist, frontier = {center: 0}, [center]
    d = 0
    while frontier and d < cap:
        d += 1
        nxt = []
        for nid in frontier:
            for dst, _ in adj.get(nid, []):
                if dst not in dist:
                    dist[dst] = d
                    nxt.append(dst)
        frontier = sorted(nxt)
    return dist


# ------------------------------------------------------------------ search

def _jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def search(root, query="", files=None, tags=None, center=None, as_of=None,
           include_superseded=False, offline=False, k=12,
           allow_rebuild=True):
    """The pipeline. Returns (records, diagnostics)."""
    files, tags = files or [], tags or []
    diagnostics = []
    graph, lex, note = ensure_index(root, offline=offline,
                                    allow_rebuild=allow_rebuild)
    if note:
        diagnostics.append(note)
    if graph is None:
        return [], diagnostics
    nodes = graph["nodes"]

    legs = {}
    q_tokens = tokenize(query)
    if q_tokens:
        legs["lex"] = bm25(lex, q_tokens)
    ident_tokens = tokenize(" ".join(files))
    if ident_tokens:
        legs["ident"] = bm25(lex, ident_tokens)
    if query:
        d = dense_leg(root, query, offline, diagnostics)
        if d:
            legs["dense"] = d

    seeds = {}
    for t in tags:
        seeds[f"tag:{t}"] = 1.0
    for f in files:
        seeds[f"file:{f}"] = 1.0
    if center:
        seeds[center] = 1.0
    ranked_union = []
    for leg in ("lex", "ident", "dense"):
        ranked_union.extend(nid for nid, _ in legs.get(leg, [])[:5])
    for nid in ranked_union[:5]:
        seeds.setdefault(nid, 0.5)
    if seeds:
        g = ppr(graph, seeds)
        if g:
            legs["graph"] = g

    if not legs:
        return [], diagnostics + ["no query, files, tags, or center given"]

    fused, membership = {}, {}
    for leg, ranked in legs.items():
        w = LEG_WEIGHT[leg]
        for rank, (nid, _) in enumerate(ranked):
            fused[nid] = fused.get(nid, 0.0) + w / (RRF_K + rank + 1)
            membership.setdefault(nid, {})[leg] = rank

    as_of_dt = _parse_ts(as_of) if as_of else None
    if as_of and as_of_dt is None:
        raise SystemExit(f"recall: --as-of {as_of!r} is not an ISO timestamp")
    hops = _hops_from(graph, center) if center else None
    now = _now()

    records = []
    for nid, score in fused.items():
        n = nodes.get(nid)
        if not n:
            continue
        view = dict(n)
        if n["kind"] == "decision" and as_of_dt is not None:
            view = _decision_as_of(n, as_of_dt)
            if view is None:
                continue
        if n["kind"] == "note":
            note_when = _parse_ts(n.get("when"))
            if as_of_dt is not None and note_when is not None and note_when > as_of_dt:
                continue
            if n.get("status") == "superseded" and not include_superseded:
                continue
            score *= n.get("weight", 1.0)
        if n["kind"] in ("lesson", "note"):
            if as_of_dt is not None and n["kind"] == "lesson":
                view = _version_as_of(n, as_of_dt)
                if view is None:
                    continue
            elif n.get("status") == "superseded" and not include_superseded:
                continue
            est = len(n.get("runs") or []) or 1
            score *= math.log2(1 + est) if est > 1 else 1.0
            when = _parse_ts(view.get("when"))
            if when is not None:
                hours = max(0.0, (now - when).total_seconds() / 3600)
                score *= 0.999 ** hours
        if hops is not None:
            score *= (1.0 / (1 + hops[nid])) if nid in hops else 0.25
        ranks = membership.get(nid, {})
        records.append({"id": nid, "score": score,
                        "legs": sorted(ranks), "ranks": ranks, **view})

    records.sort(key=lambda r: (-r["score"], r["id"]))

    kept, tag_count, note_files = [], {}, set()
    for r in records:
        if r["kind"] == "note":
            if r.get("file") in note_files: continue
            note_files.add(r.get("file"))
        toks = tokenize(r.get("text") or "")
        if any(_jaccard(toks, tokenize(o.get("text") or "")) > 0.85
               for o in kept):
            continue
        t = r.get("tag")
        if t:
            if tag_count.get(t, 0) >= 3:
                continue
            tag_count[t] = tag_count.get(t, 0) + 1
        kept.append(r)
        if len(kept) >= k:
            break

    total = len(records)
    if total > len(kept):
        diagnostics.append(f"{total - len(kept)} more candidate(s) beyond "
                           f"k={k}; re-run with --k or --format json")
    return kept, diagnostics


def _version_as_of(node, as_of_dt):
    versions = node.get("versions") or []
    chosen = None
    for i, v in enumerate(versions):
        start = _parse_ts(v.get("when"))
        end = _parse_ts(versions[i + 1].get("when")) \
            if i + 1 < len(versions) else None
        if start is not None and start <= as_of_dt \
                and (end is None or as_of_dt < end):
            chosen = v
    if chosen is None:
        return None
    view = dict(node)
    view.update({"text": str(chosen.get("claim") or ""),
                 "claim": str(chosen.get("claim") or ""),
                 "status": chosen.get("status"),
                 "objection": chosen.get("objection") or "",
                 "when": chosen.get("when"),
                 "runs": [chosen.get("runId")] if chosen.get("runId") else []})
    return view


def _decision_as_of(node, as_of_dt):
    """The decision version that governed at the as-of instant, or None
    when the id had not been decided yet. Versions arrive in decided
    order, so the last one at or before T wins."""
    chosen = None
    for v in node.get("versions") or []:
        w = _parse_ts(v.get("when"))
        if w is not None and w <= as_of_dt:
            chosen = v
    if chosen is None:
        return None
    view = dict(node)
    view.update({"text": chosen.get("text") or "",
                 "chosen": chosen.get("chosen") or "",
                 "when": chosen.get("when"),
                 "runs": [chosen.get("runId")] if chosen.get("runId") else []})
    return view


# ------------------------------------------------------------------ render

def render_lines(records, diagnostics, budget_bytes=4000, header=None):
    out = []
    if header:
        out.append(_clean(header, 400))
    for r in records:
        status = r.get("status") or r["kind"]
        label = f"{r.get('tag')}/{r.get('dedupeKey')}" \
            if r["kind"] == "lesson" else r["id"]
        body = r.get("claim") or r.get("text") or ""
        if r.get("objection"):
            body += f" || objection: {r['objection']}"
        line = f"    ({status}) {label} — {body[:160]}"
        runs = r.get("runs") or []
        bits = []
        if r.get("origin"): bits.append(str(r["origin"]))
        if r.get("session"): bits.append(str(r["session"]))
        if runs:
            bits.append(f"run {runs[-1]}")
        if r.get("when"):
            bits.append(str(r["when"])[:10])
        if r.get("stale"):
            bits.append(f"stale: {r['stale']} changed since")
        if bits:
            line += f" [{'; '.join(bits)}]"
        # Claims and objections are agent-authored text landing in
        # model-visible context: strip control and separator characters so
        # a crafted claim cannot forge additional lines.
        out.append(_clean(line, 400))
    for d in diagnostics:
        out.append(_clean(f"    [{d}]", 400))
    rendered = []
    used = 0
    for i, line in enumerate(out):
        separator = 1 if rendered else 0
        encoded = line.encode("utf-8")
        if used + separator + len(encoded) > budget_bytes:
            elision = (f"    [elided: {len(out) - i} more line(s) — run "
                       "py.sh recall.py directly]")
            remaining = budget_bytes - used - separator
            if remaining > 0:
                clipped = elision.encode("utf-8")[:remaining].decode(
                    "utf-8", errors="ignore")
                if clipped:
                    rendered.append(clipped)
            break
        rendered.append(line)
        used += separator + len(encoded)
    return "\n".join(rendered)


def emit_lines(records, diagnostics, budget_bytes=4000, header=None):
    """Write one UTF-8 payload, including its final LF, within the budget."""
    text = render_lines(records, diagnostics,
                        budget_bytes=max(0, budget_bytes - 1), header=header)
    if not text:
        return
    payload = (text + "\n").encode("utf-8")
    stream = getattr(sys.stdout, "buffer", None)
    if stream is None:
        sys.stdout.write(payload.decode("utf-8"))
    else:
        stream.write(payload)


def render_seedmap(root, records, tags):
    """memory.py --load's exact shape, relevance-ordered and capped.

    Keys reach the {{seen}} prompt token only — the compiled graph keeps
    its dedup set empty regardless of what arrives here, so nothing this
    ranking drops or keeps can suppress a re-found item.
    """
    base = memory_mod.seed_map(root, tags)
    ranked_by_tag = {}
    for r in records:
        if r["kind"] == "lesson" and r.get("tag") in base:
            ranked_by_tag.setdefault(r["tag"], []).append(r)
    out = {}
    for tag in tags:
        entry = base.get(tag, {"keys": [], "killed": []})
        ranked = [r["dedupeKey"] for r in ranked_by_tag.get(tag, [])]
        rest = [k for k in entry["keys"] if k not in ranked]
        keys = (ranked + rest)[:40]
        killed_ranked = [{"claim": r.get("claim") or "",
                          "objection": r.get("objection") or ""}
                         for r in ranked_by_tag.get(tag, [])
                         if r.get("status") == "killed"]
        seen_claims = {k["claim"] for k in killed_ranked}
        killed_rest = [k for k in entry["killed"]
                       if k.get("claim") not in seen_claims]
        out[tag] = {"keys": keys,
                    "killed": (killed_ranked + killed_rest)[:10]}
    return out


# ------------------------------------------------------------- for-session

def for_session(root, refresh_index=False):
    """The SessionStart section: offline, capped, never rebuilding, silent
    when there is nothing to say. Exit 0 unconditionally — a hook that can
    wedge a session is a worse failure than the memory it was protecting."""
    if refresh_index:
        try: request_refresh(root)
        except (OSError, ValueError, SystemExit) as exc:
            print(f"- FluxPoint recall refresh unavailable: {exc}")
    query = _work_query(root)
    if not query:
        try:
            source_docs = sum(_source_doc_counts(root).values())
        except SystemExit as e:
            print(f"- FluxPoint recall unavailable: {e}")
            return 0
        if not source_docs:
            return 0
        meta = _meta_view(root) or {}
        indexed = int(meta.get("docs") or 0)
        if not meta:
            print(f"- FluxPoint recall has {source_docs} source document(s) "
                  "available, but the index is not built yet; run "
                  "/fluxpoint:recall build.")
        elif is_stale(root):
            print(f"- FluxPoint recall index lags the stores; {source_docs} "
                  "source document(s) available. Run /fluxpoint:recall build "
                  "to refresh it.")
        elif indexed:
            print(f"- FluxPoint recall has {indexed} indexed document(s), but "
                  "there is no WORK.md or LOOP.md query to rank them against; "
                  "run /fluxpoint:recall for an explicit query.")
        else:
            print(f"- FluxPoint recall index contains no documents despite "
                  f"{source_docs} document(s) in its current sources; run "
                  "/fluxpoint:recall build.")
        return 0
    files = _session_files(root)
    try:
        records, diagnostics = search(root, query=query, files=files,
                                      offline=True, k=5,
                                      allow_rebuild=False)
    except SystemExit as e:
        print(f"- FluxPoint recall unavailable: {e}")
        return 0
    if not records:
        # No index yet: the newest filed lessons still orient a fresh
        # context, and the line names how to get ranking back.
        rows = memory_mod.current(root)
        if rows:
            rows.sort(key=lambda r: str(r.get("establishedWhen") or ""),
                      reverse=True)
            note = "; ".join(diagnostics) or "no recall candidates"
            fallback = []
            for row in rows[:3]:
                record = dict(row)
                record["kind"] = "lesson"
                record["id"] = (f"lesson:{row.get('tag')}|"
                                f"{row.get('dedupeKey')}")
                fallback.append(record)
            emit_lines(fallback, [], budget_bytes=1200,
                       header=f"- Newest filed lessons ({note}):")
            return 0
        try:
            source_docs = sum(_source_doc_counts(root).values())
        except SystemExit as e:
            print(f"- FluxPoint recall unavailable: {e}")
            return 0
        if source_docs:
            index_notes = [d for d in diagnostics if "index" in d]
            if index_notes:
                print(f"- FluxPoint recall: {'; '.join(index_notes)}; "
                      f"{source_docs} source document(s) available. Run "
                      "/fluxpoint:recall build to refresh the ranked index.")
            else:
                print("- No task-specific FluxPoint context matched the "
                      f"current index; {source_docs} source document(s) "
                      "remain available for an explicit query.")
        return 0
    source = ("FPL_RECALL_QUERY" if os.environ.get("FPL_RECALL_QUERY", "").strip() else
              "FPL_RECALL_QUERY_FILE" if os.environ.get("FPL_RECALL_QUERY_FILE") else "WORK.md or LOOP.md")
    header = (f"- FluxPoint context recalled for this work, ranked against {source}, most relevant "
              "first (offline ranking; advisory — a re-found item is "
              "still judged on its merits):")
    emit_lines(records[:5], diagnostics, budget_bytes=1200, header=header)
    return 0


STOPWORDS = set("a an and are as at be been but by can do does for from had has have how i if in into is it its me my of on or our should so than that the their them then there these they this those to was we were what when where which who why will with would you your".split())


def _informative_overlap(lex, query, nid):
    """Count distinct input words, excluding common English stopwords."""
    if not lex or nid not in (lex.get("docs") or {}): return 0
    n = max(1, lex.get("N") or 1)
    doc_tf = lex["docs"][nid].get("tf") or {}
    hits = set()
    for raw in re.findall(r"[A-Za-z0-9_./-]+", query or ""):
        for token in tokenize(raw):
            df = lex.get("df", {}).get(token, 0)
            if len(token) >= 3 and token not in STOPWORDS and df and df / n <= .5 and token in doc_tf:
                hits.add(raw.lower()); break
    return len(hits)


def for_prompt(root):
    """The UserPromptSubmit hook body: dark by default, precision-floored.

    SWE-ContextBench's result is the design constraint here — wrongly
    retrieved memories cost more than none — so this surface ships gated
    behind FPL_MEM_PROMPT=1 (the shell wrapper enforces it) and injects
    only items with real corroboration. The graph leg does NOT count as
    corroboration: with only a prompt to go on, its seeds come from the
    lexical leg's own top ranks, so lex + graph is one signal counted
    twice. What passes: two genuinely independent legs (lexical + dense),
    or a lexical match on at least two informative query tokens — never a
    lone stopword overlap. Reads the hook's JSON payload on stdin, prints
    at most 3 items in 1200 bytes, and exits 0 no matter what — a hook
    that can wedge a prompt is worse than the recall it was adding.
    """
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    prompt = str(payload.get("prompt") or "")[:512]
    if not prompt.strip():
        return 0
    try:
        records, diagnostics = search(root, query=prompt, offline=True, k=8,
                                      allow_rebuild=False)
    except SystemExit:
        return 0
    if not records:
        index_notes = [d for d in diagnostics if "index" in d]
        if index_notes:
            try:
                source_docs = sum(_source_doc_counts(root).values())
            except SystemExit:
                return 0
            if source_docs:
                print("FluxPoint prompt recall skipped (" +
                      "; ".join(index_notes) + f"); {source_docs} source "
                      "document(s) available. Run /fluxpoint:recall build.")
        return 0
    strong = []
    _, lex, _ = ensure_index(root, offline=True, allow_rebuild=False)
    for r in records:
        independent = {"lex", "dense", "ident"} & set(r.get("legs") or [])
        if (len(independent) >= 2
                or _informative_overlap(lex, prompt, r["id"]) >= 2):
            strong.append(r)
        if len(strong) >= 3:
            break
    if not strong:
        return 0
    emit_lines(
        strong, [d for d in diagnostics if "index" in d], budget_bytes=1200,
        header="FluxPoint context relevant to this prompt (advisory; a re-found "
               "item is still judged on its merits):")
    return 0


def _work_query(root):
    explicit = os.environ.get("FPL_RECALL_QUERY", "").strip()
    if explicit: return explicit[:512]
    path = os.environ.get("FPL_RECALL_QUERY_FILE")
    if path:
        try:
            with open(os.path.join(root, path), encoding="utf-8") as fh:
                text = fh.read(16384)
            return recall_sources._front(text)[1].strip().encode("utf-8")[:512].decode("utf-8", errors="ignore")
        except OSError:
            return ""
    for fn in ("WORK.md", "LOOP.md"):
        p = os.path.join(root, fn)
        if not os.path.exists(p):
            continue
        try:
            lines = open(p, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            return ""
        picked = []
        for ln in lines[:12]:
            if re.match(r"^(#|STATUS:|MODE:)", ln.strip()):
                picked.append(ln.strip().lstrip("# "))
        open_items = [ln.strip()[6:] for ln in lines
                      if re.match(r"^\s*-\s*\[( |~)\]", ln)][:3]
        return " ".join(picked + open_items)[:512]
    return ""


def _session_files(root):
    try:
        out = subprocess.run(["git", "diff", "HEAD", "--name-only"],
                             capture_output=True, text=True, timeout=10,
                             cwd=root)
        return [f for f in out.stdout.splitlines() if f][:20]
    except (OSError, subprocess.TimeoutExpired):
        return []


# -------------------------------------------------------------------- main

def request_refresh(root):
    """One detached keyless rebuild per root, never on an empty corpus."""
    cfg = recall_sources.config(root)
    if os.environ.get("FPL_RECALL_REFRESH", str(int(cfg.get("refresh", True)))) == "0": return False
    if not is_stale(root): return False
    memory_paths = memory_mod.source_paths(root) if hasattr(memory_mod, "source_paths") else [memory_mod.path_for(root)]
    have_sources = any(os.path.isfile(p) and os.path.getsize(p) for p in memory_paths)
    have_sources = have_sources or bool(_read_substrate(root)[0]) or os.path.isfile(os.path.join(root, CEX))
    have_sources = have_sources or any(os.path.isdir(d) and any(f.endswith(".json") for f in os.listdir(d)) for d in _run_directories(root))
    have_sources = have_sources or any(recall_sources.files(c) for c in recall_sources.corpus_roots(root))
    if not have_sources: return False
    idx = index_dir(root)
    os.makedirs(idx, exist_ok=True)
    lock = os.path.join(idx, "refresh.lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            if time.time() - os.stat(lock).st_mtime <= 300: return False
            os.unlink(lock)
        except FileNotFoundError: pass
        try: fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError: return False
    token = uuid.uuid4().hex
    with os.fdopen(fd, "w", encoding="ascii") as fh: fh.write(token)
    argv = [sys.executable, os.path.abspath(__file__), "--root", os.path.abspath(root),
            "--build", "--refresh-lock", lock, "--refresh-lock-token", token]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    try:
        if os.name == "nt":
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try: child = subprocess.Popen(argv, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
            except OSError: child = subprocess.Popen(argv, creationflags=flags, **kwargs)
        else: child = subprocess.Popen(argv, start_new_session=True, **kwargs)
        _REFRESH_CHILDREN[:] = [p for p in _REFRESH_CHILDREN if p.poll() is None]
        _REFRESH_CHILDREN.append(child)
    except BaseException:
        os.unlink(lock); raise
    return True


def refresh(root, embed=False):
    """record-run.py's pass: rebuild after filing. Returns a stats dict."""
    return build(root, embed_backfill=embed)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--build", action="store_true")
    g.add_argument("--verify", action="store_true")
    g.add_argument("--stats", action="store_true")
    g.add_argument("--query", default=None)
    g.add_argument("--for-session", action="store_true")
    g.add_argument("--for-prompt", action="store_true",
                   help="UserPromptSubmit hook body: hook JSON on stdin")
    ap.add_argument("--refresh", action="store_true", help="with --for-session: request detached keyless refresh")
    ap.add_argument("--refresh-lock", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--refresh-lock-token", default=None, help=argparse.SUPPRESS)
    ap.add_argument("--embed", action="store_true",
                    help="with --build: backfill dense vectors (needs a key)")
    ap.add_argument("--files", default="",
                    help="comma-separated paths for the identifier leg and seeds")
    ap.add_argument("--tag", action="append", default=[])
    ap.add_argument("--center", default=None,
                    help="graph node id (e.g. campaign:slug) for distance rerank")
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--include-superseded", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-rebuild", action="store_true",
                    help="use a stale index and say so, instead of rebuilding")
    ap.add_argument("--k", type=int, default=12)
    ap.add_argument("--budget-bytes", type=int, default=4000)
    ap.add_argument("--format", default="lines",
                    choices=("lines", "json", "seedmap"))
    a = ap.parse_args()

    if a.for_session:
        return for_session(a.root, refresh_index=a.refresh)

    if a.for_prompt:
        return for_prompt(a.root)

    if a.build:
        try:
            stats = build(a.root, embed_backfill=a.embed)
        finally:
            if a.refresh_lock:
                try:
                    with open(a.refresh_lock, encoding="ascii") as fh: owner = fh.read()
                    if owner == a.refresh_lock_token: os.unlink(a.refresh_lock)
                except FileNotFoundError: pass
        print(f"recall: indexed {stats['docs']} doc(s), {stats['nodes']} "
              f"node(s), {stats['edges']} edge(s); dense: "
              f"{stats['provider']}" +
              (f", {stats['pending']} pending" if stats["pending"] else "") +
              (f"; {stats['staleKills']} stale kill(s) marked"
               if stats["staleKills"] else "") +
              (f"; {stats['droppedPaths']} unresolved path component(s) "
               f"dropped" if stats["droppedPaths"] else ""))
        return 0

    if a.verify:
        if is_stale(a.root):
            print("recall: index is stale or absent — run --build")
            return 1
        print("recall: index matches its sources")
        return 0

    if a.stats:
        source_fp = _source_fingerprints(a.root)
        source_state = ", ".join(
            f"{label} {'absent' if source_fp[key] == 'absent' else 'present'}"
            for key, label in (("memory.jsonl", "memory.jsonl"),
                               ("runs", "runs"),
                               ("cex.jsonl", ".fluxpoint-cex.jsonl"),
                               ("substrate.json", "substrate.json")))
        meta = _meta_view(a.root)
        if not meta:
            print("recall: no index built yet — run --build; recognized "
                  f"sources: {source_state}")
            return 0
        state = "stale" if is_stale(a.root) else "current"
        kinds = meta.get("kinds")
        if not isinstance(kinds, dict):
            kinds = {kind: 0 for kind in DOC_KINDS}
            graph = _load_json(a.root, GRAPH_F) or {}
            for node in (graph.get("nodes") or {}).values():
                kind = node.get("kind") if isinstance(node, dict) else None
                if kind in kinds:
                    kinds[kind] += 1
        print(f"recall: {meta['docs']} doc(s) indexed ({state}); dense "
              f"provider {meta['provider']}" +
              (f", {meta['pending']} pending embedding"
               if meta.get("pending") else "") +
              f"; documents: lessons {kinds.get('lesson', 0)}, "
              f"decisions {kinds.get('decision', 0)}, "
              f"counterexamples {kinds.get('cex', 0)}, "
              f"primitives {kinds.get('primitive', 0)}, notes {kinds.get('note', 0)}; recognized sources: "
              f"{source_state}")
        corpus_writer = (_read_json_path(os.path.join(shared_index_dir(a.root), META_F)) or {}).get("writer", "not built")
        print(f"recall: schema {INDEX_SCHEMA}; repo writer {meta.get('writer', writer_identity())}; repo slot {index_dir(a.root)}; corpus writer {corpus_writer}; corpus slot {shared_index_dir(a.root)}")
        return 0

    if a.format == "seedmap" and not a.tag:
        ap.error("--format seedmap requires at least one --tag")
    files = [f.strip() for f in a.files.split(",") if f.strip()]
    records, diagnostics = search(
        a.root, query=a.query or "", files=files, tags=a.tag,
        center=a.center, as_of=a.as_of,
        include_superseded=a.include_superseded, offline=a.offline, k=a.k,
        allow_rebuild=not a.no_rebuild)
    if a.format == "seedmap":
        # Always valid JSON, even empty: a cold start is different from a
        # loader that failed.
        print(json.dumps(render_seedmap(a.root, records, a.tag)))
    elif a.format == "json":
        print(json.dumps({"results": records, "diagnostics": diagnostics}))
    else:
        print(render_lines(records, diagnostics,
                           budget_bytes=a.budget_bytes) or "(nothing recalled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
