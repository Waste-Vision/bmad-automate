"""Dependency analysis — parse epic dependencies and build a DAG."""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path


class CycleError(Exception):
    """Raised when a cycle is detected in the dependency graph."""

    def __init__(self, cycle: list[int]) -> None:
        self.cycle = cycle
        path = " -> ".join(str(n) for n in cycle)
        super().__init__(f"Cycle detected: {path}")


class DAG:
    """Directed acyclic graph for epic dependencies.

    Supports querying which epics are ready to run given a set of
    completed epics.
    """

    def __init__(self, deps: dict[int, list[int]], epics: list[int]) -> None:
        self._deps = deps  # epic -> list of prerequisites
        self._epics = set(epics)
        self._validate()

    def _validate(self) -> None:
        """Check for cycles using topological sort (Kahn's algorithm)."""
        # Build adjacency and in-degree
        in_degree: dict[int, int] = defaultdict(int)
        adj: dict[int, list[int]] = defaultdict(list)

        for epic in self._epics:
            in_degree.setdefault(epic, 0)

        for epic, prereqs in self._deps.items():
            for prereq in prereqs:
                adj[prereq].append(epic)
                in_degree[epic] = in_degree.get(epic, 0) + 1

        # Kahn's algorithm
        queue = [n for n in self._epics if in_degree.get(n, 0) == 0]
        sorted_nodes: list[int] = []

        while queue:
            node = queue.pop(0)
            sorted_nodes.append(node)
            for neighbor in adj.get(node, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(sorted_nodes) != len(self._epics):
            # Find a cycle for error reporting
            remaining = self._epics - set(sorted_nodes)
            cycle = self._find_cycle(remaining)
            raise CycleError(cycle)

        self._topo_order = sorted_nodes

    def _find_cycle(self, nodes: set[int]) -> list[int]:
        """Find a cycle in the remaining nodes for error reporting."""

        def dfs(
            node: int, visited: set[int], path: list[int],
        ) -> list[int] | None:
            if node in visited:
                if node in path:
                    idx = path.index(node)
                    return path[idx:] + [node]
                return None
            visited.add(node)
            path.append(node)
            # Follow dependencies (node depends on these)
            for dep in self._deps.get(node, []):
                if dep in nodes:
                    result = dfs(dep, visited, path)
                    if result:
                        return result
            # Follow reverse edges (these depend on node)
            for epic, prereqs in self._deps.items():
                if node in prereqs and epic in nodes:
                    result = dfs(epic, visited, path)
                    if result:
                        return result
            path.pop()
            return None

        for node in nodes:
            result = dfs(node, set(), [])
            if result:
                return result
        return list(nodes)  # fallback

    @property
    def topological_order(self) -> list[int]:
        """Return epics in topological order."""
        return list(self._topo_order)

    def get_ready_epics(self, completed: set[int]) -> list[int]:
        """Return epics whose dependencies are all satisfied."""
        ready: list[int] = []
        for epic in self._epics:
            if epic in completed:
                continue
            prereqs = self._deps.get(epic, [])
            if all(p in completed for p in prereqs):
                ready.append(epic)
        return sorted(ready)

    def get_dependencies(self, epic: int) -> list[int]:
        """Return the prerequisites for an epic."""
        return list(self._deps.get(epic, []))

    def has_dependencies(self) -> bool:
        """Check if any dependencies are declared."""
        return any(len(deps) > 0 for deps in self._deps.values())

    def get_tiers(self) -> list[list[int]]:
        """Group epics into tiers by depth.

        Tier 0 = no dependencies (can start immediately).
        Tier N = all dependencies are in tiers 0..N-1.
        Epics in the same tier can run in parallel.

        Example for: 1 -> {2,4}, 2 -> 3, 4 -> 5, {3,5} -> 6
          Tier 0: [1]
          Tier 1: [2, 4]
          Tier 2: [3, 5]
          Tier 3: [6]
        """
        depths: dict[int, int] = {}

        def _depth(epic: int) -> int:
            if epic in depths:
                return depths[epic]
            prereqs = self._deps.get(epic, [])
            if not prereqs:
                depths[epic] = 0
            else:
                depths[epic] = max(_depth(p) for p in prereqs) + 1
            return depths[epic]

        for epic in self._epics:
            _depth(epic)

        max_depth = max(depths.values(), default=0)
        tiers: list[list[int]] = [[] for _ in range(max_depth + 1)]
        for epic in sorted(self._epics):
            tiers[depths[epic]].append(epic)

        return tiers

    def get_edges(self) -> list[tuple[int, int]]:
        """Return all edges as (from_epic, to_epic) pairs.

        An edge (A, B) means B depends on A (A must complete before B).
        """
        edges: list[tuple[int, int]] = []
        for epic, prereqs in self._deps.items():
            for prereq in prereqs:
                edges.append((prereq, epic))
        return sorted(edges)

    def get_chains(self) -> list[list[int]]:
        """Decompose the DAG into dependency chains.

        Each chain is a maximal path from a root to a leaf (or
        convergence point).  Shared nodes appear in multiple chains.

        Example for: 1->{2,4}, 2->3, 4->5, {3,5}->6
          Chain A: [1, 2, 3, 6]
          Chain B: [1, 4, 5, 6]
        """
        # Build forward adjacency (epic -> list of dependents)
        forward: dict[int, list[int]] = {e: [] for e in self._epics}
        for epic, prereqs in self._deps.items():
            for prereq in prereqs:
                if prereq in forward:
                    forward[prereq].append(epic)

        # Find roots (no dependencies)
        roots = [e for e in self._epics if not self._deps.get(e, [])]

        # DFS from each root to collect all paths to leaves/convergence
        chains: list[list[int]] = []

        def _dfs(node: int, path: list[int]) -> None:
            children = forward.get(node, [])
            if not children:
                # Leaf — emit the chain
                chains.append(list(path))
                return
            for child in sorted(children):
                path.append(child)
                _dfs(child, path)
                path.pop()

        for root in sorted(roots):
            _dfs(root, [root])

        return chains

    def get_critical_path(
        self, story_counts: dict[int, int] | None = None,
    ) -> list[int]:
        """Return the chain with the highest total story count.

        If *story_counts* is not provided, uses chain length as proxy.
        """
        chains = self.get_chains()
        if not chains:
            return list(sorted(self._epics))

        counts = story_counts or {}

        def _weight(chain: list[int]) -> int:
            return sum(counts.get(e, 1) for e in chain)

        return max(chains, key=_weight)

    def to_dict(self) -> dict:
        """Serialize the DAG for JSON API responses."""
        tiers = self.get_tiers()
        chains = self.get_chains()
        nodes = []
        for epic in sorted(self._epics):
            tier = next(
                i for i, t in enumerate(tiers) if epic in t
            )
            nodes.append({
                "id": epic,
                "label": f"Epic {epic}",
                "tier": tier,
                "dependencies": self.get_dependencies(epic),
            })
        return {
            "nodes": nodes,
            "edges": [{"from": a, "to": b} for a, b in self.get_edges()],
            "tiers": [[e for e in tier] for tier in tiers],
            "chains": chains,
        }

    def __repr__(self) -> str:
        lines = []
        for epic in self._topo_order:
            deps = self._deps.get(epic, [])
            if deps:
                lines.append(f"  Epic {epic} <- [{', '.join(str(d) for d in deps)}]")
            else:
                lines.append(f"  Epic {epic} (independent)")
        return "DAG:\n" + "\n".join(lines)


def parse_structured_block(yaml_data: dict) -> dict[int, list[int]]:
    """Parse the ``epic_dependencies:`` block from sprint-status.yaml.

    Expected format:
        epic_dependencies:
          5: [4]
          6: [4, 5]
    """
    block = yaml_data.get("epic_dependencies")
    if not block or not isinstance(block, dict):
        return {}

    deps: dict[int, list[int]] = {}
    for key, value in block.items():
        try:
            epic = int(key)
        except (ValueError, TypeError):
            continue

        if isinstance(value, list):
            prereqs = []
            for v in value:
                try:
                    prereqs.append(int(v))
                except (ValueError, TypeError):
                    continue
            if prereqs:
                deps[epic] = prereqs
        elif isinstance(value, int):
            deps[epic] = [value]

    return deps


def parse_comment_dependencies(yaml_text: str) -> dict[int, list[int]]:
    """Parse dependency declarations from YAML comments.

    Supports arrow notation:
        # Dependency: Epic 20 -> 21 -> {22, 23} -> 25
        # Epic 29 (Foundation)
        #   +-- Epic 30 -> Epic 31
    """
    deps: dict[int, list[int]] = {}

    # Pattern: "Epic N -> N" or "N -> N" with optional arrows
    # Match lines with arrow notation
    arrow_pattern = re.compile(
        r"#.*?(?:Epic\s+)?(\d+)\s*(?:->|-->|→|──→)\s*(?:Epic\s+)?(\d+)"
    )

    # Pattern for set notation: {22, 23, 24}
    set_pattern = re.compile(
        r"#.*?(?:Epic\s+)?(\d+)\s*(?:->|-->|→|──→)\s*\{([^}]+)\}"
    )

    for line in yaml_text.splitlines():
        line = line.strip()
        if not line.startswith("#"):
            continue

        # Check for set notation first (e.g., "21 -> {22, 23}")
        for m in set_pattern.finditer(line):
            source = int(m.group(1))
            targets_str = m.group(2)
            for t in re.findall(r"\d+", targets_str):
                target = int(t)
                deps.setdefault(target, [])
                if source not in deps[target]:
                    deps[target].append(source)

        # Check for simple arrow chains (e.g., "20 -> 21 -> 25")
        numbers = re.findall(
            r"(?:Epic\s+)?(\d+)\s*(?:->|-->|→|──→)", line
        )
        targets_after = re.findall(
            r"(?:->|-->|→|──→)\s*(?:Epic\s+)?(\d+)", line
        )

        if numbers and targets_after:
            # Build chain: each target depends on the preceding number
            chain = [int(numbers[0])]
            for t in targets_after:
                target = int(t)
                if target not in chain:
                    # target depends on the last item in the chain
                    deps.setdefault(target, [])
                    prev = chain[-1]
                    if prev not in deps[target]:
                        deps[target].append(prev)
                    chain.append(target)

    return deps


def build_dag(
    yaml_data: dict,
    yaml_text: str,
    epics: list[int],
) -> DAG:
    """Build a dependency DAG from sprint-status data.

    Priority: structured ``epic_dependencies:`` block > comment parsing.
    """
    # Try structured block first
    structured = parse_structured_block(yaml_data)
    if structured:
        # Filter to only include epics we're processing
        filtered = {
            k: [d for d in v if d in epics or d in structured]
            for k, v in structured.items()
            if k in epics
        }
        return DAG(filtered, epics)

    # Fall back to comment parsing
    comment_deps = parse_comment_dependencies(yaml_text)
    if comment_deps:
        filtered = {
            k: [d for d in v if d in epics or d in comment_deps]
            for k, v in comment_deps.items()
            if k in epics
        }
        return DAG(filtered, epics)

    # No dependencies found — all epics are independent
    return DAG({}, epics)
