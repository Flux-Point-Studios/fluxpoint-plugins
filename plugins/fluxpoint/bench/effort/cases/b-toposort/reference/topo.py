import heapq


class CycleError(ValueError):
    def __init__(self, cycle):
        super().__init__("cycle: " + " -> ".join(cycle))
        self.cycle = cycle


def order(deps):
    adj = {}
    for k, vs in deps.items():
        adj.setdefault(k, set()).update(vs)
        for v in vs:
            adj.setdefault(v, set())
    indeg = {k: len(v) for k, v in adj.items()}
    users = {k: [] for k in adj}
    for k, vs in adj.items():
        for v in vs:
            users[v].append(k)
    ready = [k for k, d in indeg.items() if d == 0]
    heapq.heapify(ready)
    out = []
    while ready:
        k = heapq.heappop(ready)
        out.append(k)
        for u in users[k]:
            indeg[u] -= 1
            if indeg[u] == 0:
                heapq.heappush(ready, u)
    if len(out) == len(adj):
        return out
    left = {k for k, d in indeg.items() if d > 0}
    raise CycleError(_best_cycle(adj, left))


def _sccs(adj, nodes):
    index, low, on, stack, out, counter = {}, {}, set(), [], [], [0]
    for root in sorted(nodes):
        if root in index:
            continue
        work = [(root, iter(sorted(v for v in adj[root] if v in nodes)))]
        index[root] = low[root] = counter[0]; counter[0] += 1
        stack.append(root); on.add(root)
        while work:
            node, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter[0]; counter[0] += 1
                    stack.append(w); on.add(w)
                    work.append((w, iter(sorted(v for v in adj[w] if v in nodes))))
                    advanced = True
                    break
                if w in on:
                    low[node] = min(low[node], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = set()
                while True:
                    w = stack.pop(); on.discard(w); comp.add(w)
                    if w == node:
                        break
                out.append(comp)
    return out


def _best_cycle(adj, left):
    comps = [c for c in _sccs(adj, left) if len(c) > 1 or any(v in adj[v] for v in c)]
    comp = min(comps, key=min)
    cur, path, at = min(comp), [], {}
    while cur not in at:
        at[cur] = len(path)
        path.append(cur)
        cur = min(v for v in adj[cur] if v in comp)
    cyc = path[at[cur]:]
    i = cyc.index(min(cyc))
    cyc = cyc[i:] + cyc[:i]
    return cyc + [cyc[0]]
