# order(deps) -> list[str]

`deps` maps each node name (a `str`) to the list of names it depends on.
A dependency that is not itself a key is still a node, with no
dependencies of its own. Return every node exactly once, each after all of
its dependencies.

Among the nodes that are ready at any point, take the lexicographically
smallest first, so the output is fully deterministic.

Duplicate entries in a dependency list count once. A node depending on
itself is a cycle.

If the graph has a cycle, raise `CycleError` (a subclass of `ValueError`)
whose `.cycle` attribute is one cycle as a list of node names `[a, b, ...,
a]`, where each node depends on the next one and the first and last
element are the same. Choose it deterministically:

1. Start at the lexicographically smallest node that lies on any cycle.
2. Repeatedly move to the lexicographically smallest dependency of the
   current node that lies on a cycle together with the start node (that is,
   in the same strongly connected component), until you reach a node you
   have already visited.
3. The cycle is the walk from the first visit of that node to its repeat.
   Rotate it so the lexicographically smallest node on it comes first (and
   last).

It must handle 100000 nodes in a chain without hitting the recursion limit.
