"""Minimal directed-graph helper used for structural validation (DAGs and lineage). Pure."""

from __future__ import annotations

import heapq
from collections.abc import Iterable


def topological_order(
    nodes: Iterable[str], edges: Iterable[tuple[str, str]]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Kahn's algorithm with deterministic tie-breaking (smallest id first).

    ``edges`` are ``(upstream, downstream)`` pairs; pairs with an endpoint outside ``nodes`` are ignored
    (referential problems are reported elsewhere). Returns ``(order, remaining)``; ``remaining`` is empty
    iff the graph is acyclic, otherwise it lists the nodes that sit on or behind a cycle.
    """
    node_list = sorted(set(nodes))
    indegree = {n: 0 for n in node_list}
    successors: dict[str, list[str]] = {n: [] for n in node_list}
    for upstream, downstream in set(edges):
        if upstream in indegree and downstream in indegree:
            successors[upstream].append(downstream)
            indegree[downstream] += 1
    heap = [n for n in node_list if indegree[n] == 0]
    heapq.heapify(heap)
    order: list[str] = []
    while heap:
        current = heapq.heappop(heap)
        order.append(current)
        for nxt in sorted(successors[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                heapq.heappush(heap, nxt)
    placed = set(order)
    remaining = tuple(n for n in node_list if n not in placed)
    return tuple(order), remaining
