"""Dependency analysis — parse epic dependencies and build a DAG."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import yaml

DEPS_FILENAME = "epic-dependencies.yaml"


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


def parse_box_diagram_dependencies(yaml_text: str) -> dict[int, list[int]]:
    """Parse ASCII box-drawing dependency diagrams from YAML comments.

    Handles merge/split notation such as::

        #   [39 Observability]──┐
        #                       ├──[41 Ingestion]──[43 Core]──┬──[44 Advanced]
        #   [40 New Services]───┘                             └──[46 Archive]

    Inline annotations like ``(also needs 40+44)`` are also honoured.
    """
    BOX_MERGE_IN = frozenset("┐┘┬")   # chars that feed *into* a junction column
    BOX_OUT = frozenset("├└")          # chars that emit *from* a junction column
    H_BRIDGE = frozenset("─┬┴┼")       # chars allowed in a same-row bridge
    NODE_RE = re.compile(r"\[(\d+)[^\]]*\]")
    EXTRA_RE = re.compile(r"\((?:also\s+)?needs\s+([\d\s+,·&]+)\)")

    deps: dict[int, list[int]] = {}

    def _add(epic: int, prereq: int) -> None:
        if epic == prereq:
            return
        deps.setdefault(epic, [])
        if prereq not in deps[epic]:
            deps[epic].append(prereq)

    # Collect consecutive comment lines that contain box chars or [N …] nodes.
    BOX_CHARS = frozenset("─┐┘├└┬┴┼┤│")
    blocks: list[list[str]] = []
    current: list[str] = []

    for raw in yaml_text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("#"):
            content = stripped[1:]
            if any(c in BOX_CHARS for c in content) or NODE_RE.search(content):
                current.append(content)
                continue
        if current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)

    for block in blocks:
        if not any(NODE_RE.search(line) for line in block):
            continue

        width = max(len(line) for line in block)
        grid = [line.ljust(width) for line in block]

        # Locate all [N …] nodes: (row, col_start, col_end, epic)
        all_nodes: list[tuple[int, int, int, int]] = []
        for r, line in enumerate(grid):
            for m in NODE_RE.finditer(line):
                epic = int(m.group(1))
                all_nodes.append((r, m.start(), m.end(), epic))
                extra = EXTRA_RE.search(line[m.end():])
                if extra:
                    for n in re.findall(r"\d+", extra.group(1)):
                        _add(epic, int(n))

        # Phase 1 — horizontal same-row chains: [A]──[B] → B depends on A.
        by_row: dict[int, list[tuple[int, int, int]]] = {}
        for r, cs, ce, ep in all_nodes:
            by_row.setdefault(r, []).append((cs, ce, ep))

        for r, row_nodes in by_row.items():
            row_nodes.sort()
            for i in range(len(row_nodes) - 1):
                _, ce_a, ep_a = row_nodes[i]
                cs_b, _, ep_b = row_nodes[i + 1]
                bridge = grid[r][ce_a:cs_b]
                # Must contain at least one '─' and only H_BRIDGE chars (no ├└┐┘│).
                if bridge and "─" in bridge and all(c in H_BRIDGE for c in bridge):
                    _add(ep_b, ep_a)

        # Phase 2 — merge / split junctions.
        # Junction column = any column where '├' or '└' appears.
        junction_cols: set[int] = set()
        for line in grid:
            for c, ch in enumerate(line):
                if ch in BOX_OUT:
                    junction_cols.add(c)

        for mc in junction_cols:
            # Output nodes: rows where line[mc] ∈ BOX_OUT, look right for a node.
            out_epics: list[int] = []
            for line in grid:
                if mc >= len(line) or line[mc] not in BOX_OUT:
                    continue
                j = mc + 1
                while j < len(line) and line[j] == "─":
                    j += 1
                m = NODE_RE.match(line[j:])
                if m:
                    out_epics.append(int(m.group(1)))

            # Input nodes: rows where line[mc] ∈ BOX_MERGE_IN,
            # rightmost node to the left connected via H_BRIDGE chars.
            in_epics: list[int] = []
            for r, line in enumerate(grid):
                if mc >= len(line) or line[mc] not in BOX_MERGE_IN:
                    continue
                candidates = sorted(
                    [
                        (cs, ce, ep)
                        for (rr, cs, ce, ep) in all_nodes
                        if rr == r and ce <= mc
                    ],
                    key=lambda x: x[0],
                )
                if not candidates:
                    continue
                cs, ce, ep = candidates[-1]
                bridge = line[ce:mc]
                if all(c in H_BRIDGE for c in bridge):
                    in_epics.append(ep)

            # Extension: some rows have only '─' at mc (the wire passes
            # through rather than terminating here).  Scan those rows
            # leftward to find the actual source junction and its inputs.
            for r, line in enumerate(grid):
                if mc >= len(line) or line[mc] != "─":
                    continue
                c = mc - 1
                while c >= 0 and line[c] == "─":
                    c -= 1
                if c < 0 or line[c] not in BOX_MERGE_IN:
                    continue
                # Found a source junction at col c — find the node left of it.
                candidates = sorted(
                    [
                        (cs, ce, ep)
                        for (rr, cs, ce, ep) in all_nodes
                        if rr == r and ce <= c
                    ],
                    key=lambda x: x[0],
                )
                if not candidates:
                    continue
                cs, ce, ep = candidates[-1]
                bridge = line[ce:c]
                if all(ch in H_BRIDGE for ch in bridge) and ep not in in_epics:
                    in_epics.append(ep)

            for out_ep in out_epics:
                for in_ep in in_epics:
                    _add(out_ep, in_ep)

    return deps


def parse_comment_dependencies(yaml_text: str) -> dict[int, list[int]]:
    """Parse dependency declarations from YAML comments.

    Supports arrow notation including sets and parenthetical / bracketed labels::

        # Epic 20 → 21 [GATE] → {22, 23, 24} → 25 → 26
        # Epic 29 (Foundation) → Epic 30 (Lifecycle) → Epic 31
        # Epic 32 (Manufacturer API) independent after Epic 29
    """
    deps: dict[int, list[int]] = {}

    # Labels that can appear after an epic number before an arrow or end of segment.
    # Handles both (round) and [square] bracket forms, possibly repeated.
    _OPT_LABELS = r"(?:\s*(?:\([^)]*\)|\[[^\]]*\]))*"
    _ARROW_RE = re.compile(r"->|-->|→|──→")
    # Matches a single epic number with optional labels
    _EPIC_RE = re.compile(rf"(?:Epic\s+)?(\d+){_OPT_LABELS}")
    # Matches a set of epic numbers: {22, 23, 24}
    _SET_RE = re.compile(r"\{([^}]+)\}")

    # Prose: "Epic N (label) ... after/follows/depends on (Epic) M"
    _prose_re = re.compile(
        rf"(?:Epic\s+)?(\d+){_OPT_LABELS}[^→\n]*?"
        rf"\b(?:independent\s+after|after|follows?|depends?\s+on)\s+(?:Epic\s+)?(\d+)",
        re.IGNORECASE,
    )

    def _nums_in(seg: str) -> list[int]:
        """Return the epic number(s) represented by a chain segment."""
        s = _SET_RE.search(seg)
        if s:
            return [int(n) for n in re.findall(r"\d+", s.group(1))]
        m = _EPIC_RE.search(seg)
        return [int(m.group(1))] if m else []

    def _is_list(seg: str) -> bool:
        """True if seg is a comma-separated list rather than a single epic reference.

        Commas inside {set} notation are intentional and must not trigger this.
        """
        return "," in re.sub(r"\{[^}]*\}", "", seg)

    def _add(epic: int, prereq: int) -> None:
        if epic == prereq:
            return
        deps.setdefault(epic, [])
        if prereq not in deps[epic]:
            deps[epic].append(prereq)

    has_arrow = _ARROW_RE.search

    for line in yaml_text.splitlines():
        line = line.strip()
        if not line.startswith("#"):
            continue

        if has_arrow(line):
            # Split on every arrow; adjacent segments are source → target pairs.
            # Each segment may be a single epic or a set {N, M, P}.
            # Skip pairs where either side is a comma-separated list (narrative text).
            segs = _ARROW_RE.split(line)
            for i in range(len(segs) - 1):
                if _is_list(segs[i]) or _is_list(segs[i + 1]):
                    continue
                for src in _nums_in(segs[i]):
                    for tgt in _nums_in(segs[i + 1]):
                        _add(tgt, src)
        else:
            # No arrow — look for prose patterns only.
            for m in _prose_re.finditer(line):
                _add(int(m.group(1)), int(m.group(2)))

    return deps


def _infer_all(
    yaml_data: dict, yaml_text: str, epics: list[int],
) -> dict[int, list[int]]:
    """Combine all inference methods and return a deps entry for every epic."""
    structured = parse_structured_block(yaml_data)
    if structured:
        combined: dict[int, list[int]] = {}
        for epic, prereqs in structured.items():
            combined[epic] = list(prereqs)
    else:
        combined = {}
        for src in (
            parse_comment_dependencies(yaml_text),
            parse_box_diagram_dependencies(yaml_text),
        ):
            for epic, prereqs in src.items():
                for prereq in prereqs:
                    combined.setdefault(epic, [])
                    if prereq not in combined[epic]:
                        combined[epic].append(prereq)

    return {epic: combined.get(epic, []) for epic in epics}


def _write_deps_file(
    path: Path, deps: dict[int, list[int]], source_name: str,
) -> None:
    """Serialise *deps* to a human-editable YAML file."""
    lines = [
        f"# Epic dependency map — auto-inferred from {source_name}",
        f"# Generated: {date.today()}",
        "# Edit this file to override. Re-run to add newly introduced epics.",
        "",
        "epic_dependencies:",
    ]

    tier0 = sorted(e for e, prereqs in deps.items() if not prereqs)
    with_deps = sorted(e for e, prereqs in deps.items() if prereqs)

    if tier0:
        lines.append("  # Tier 0 — no prerequisites (start immediately)")
        for e in tier0:
            lines.append(f"  {e}: []")

    if with_deps:
        if tier0:
            lines.append("")
        lines.append("  # Dependent epics")
        for e in with_deps:
            prereqs_str = ", ".join(str(p) for p in sorted(deps[e]))
            lines.append(f"  {e}: [{prereqs_str}]")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_or_create_deps_file(
    sprint_yaml_path: Path,
    yaml_data: dict,
    yaml_text: str,
    epics: list[int],
) -> dict[int, list[int]]:
    """Return the effective epic deps, maintaining *epic-dependencies.yaml*.

    * If the file does not exist it is created by inferring deps from the
      sprint-status YAML (box diagrams, arrow comments, structured block).
    * If the file exists, it is loaded as-is (user edits are preserved).
    * Either way, any epics missing from the file are appended as tier 0
      and the file is updated.
    """
    deps_path = sprint_yaml_path.parent / DEPS_FILENAME

    if deps_path.exists():
        raw = yaml.safe_load(deps_path.read_text(encoding="utf-8")) or {}
        file_deps_raw = raw.get("epic_dependencies", {})

        loaded: dict[int, list[int]] = {}
        for k, v in file_deps_raw.items():
            try:
                epic = int(k)
                loaded[epic] = [int(x) for x in (v or [])]
            except (ValueError, TypeError):
                continue

        missing = [e for e in epics if e not in loaded]
        if missing:
            for e in missing:
                loaded[e] = []
            all_deps = {e: loaded.get(e, []) for e in sorted(set(list(loaded) + epics))}
            _write_deps_file(deps_path, all_deps, sprint_yaml_path.name)

        return {e: loaded.get(e, []) for e in epics}

    # File absent — infer and create.
    deps = _infer_all(yaml_data, yaml_text, epics)
    _write_deps_file(deps_path, deps, sprint_yaml_path.name)
    return deps


def build_dag(
    yaml_data: dict,
    yaml_text: str,
    epics: list[int],
    sprint_yaml_path: Path | None = None,
) -> DAG:
    """Build a dependency DAG from sprint-status data.

    When *sprint_yaml_path* is provided the adjacent ``epic-dependencies.yaml``
    file is used as the authoritative source (created / updated automatically).
    Without a path the legacy inline-parsing fallback is used.
    """
    if sprint_yaml_path is not None:
        deps = load_or_create_deps_file(sprint_yaml_path, yaml_data, yaml_text, epics)
        filtered = {
            k: [d for d in v if d in epics]
            for k, v in deps.items()
            if k in epics
        }
        return DAG(filtered, epics)

    # Legacy path — no file management.
    structured = parse_structured_block(yaml_data)
    if structured:
        filtered = {
            k: [d for d in v if d in epics or d in structured]
            for k, v in structured.items()
            if k in epics
        }
        return DAG(filtered, epics)

    comment_deps = parse_comment_dependencies(yaml_text)
    box_deps = parse_box_diagram_dependencies(yaml_text)
    merged: dict[int, list[int]] = {}
    for src in (comment_deps, box_deps):
        for epic, prereqs in src.items():
            for prereq in prereqs:
                merged.setdefault(epic, [])
                if prereq not in merged[epic]:
                    merged[epic].append(prereq)

    if merged:
        filtered = {
            k: [d for d in v if d in epics or d in merged]
            for k, v in merged.items()
            if k in epics
        }
        return DAG(filtered, epics)

    return DAG({}, epics)
