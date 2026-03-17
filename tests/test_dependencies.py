"""Tests for dependencies.py — DAG, dependency parsing."""

from __future__ import annotations

import pytest

from bmad_automate.dependencies import (
    CycleError,
    DAG,
    build_dag,
    parse_comment_dependencies,
    parse_structured_block,
)


class TestParseStructuredBlock:
    def test_basic(self):
        data = {"epic_dependencies": {5: [4], 6: [4, 5]}}
        result = parse_structured_block(data)
        assert result == {5: [4], 6: [4, 5]}

    def test_string_keys(self):
        data = {"epic_dependencies": {"5": [4], "6": [4, 5]}}
        result = parse_structured_block(data)
        assert result == {5: [4], 6: [4, 5]}

    def test_empty(self):
        assert parse_structured_block({}) == {}
        assert parse_structured_block({"epic_dependencies": None}) == {}

    def test_single_int_value(self):
        data = {"epic_dependencies": {5: 4}}
        result = parse_structured_block(data)
        assert result == {5: [4]}


class TestParseCommentDependencies:
    def test_arrow_notation(self):
        text = "# Dependency: Epic 20 -> 21 -> 25\n"
        result = parse_comment_dependencies(text)
        assert 21 in result
        assert 20 in result[21]
        assert 25 in result
        assert 21 in result[25]

    def test_unicode_arrow(self):
        text = "# Epic 20 → 21\n"
        result = parse_comment_dependencies(text)
        assert 21 in result
        assert 20 in result[21]

    def test_set_notation(self):
        text = "# Dependency: Epic 21 -> {22, 23, 24}\n"
        result = parse_comment_dependencies(text)
        assert 22 in result and 21 in result[22]
        assert 23 in result and 21 in result[23]
        assert 24 in result and 21 in result[24]

    def test_non_comment_lines_ignored(self):
        text = "development_status:\n  1-1-foo: done\n"
        result = parse_comment_dependencies(text)
        assert result == {}

    def test_empty(self):
        assert parse_comment_dependencies("") == {}


class TestDAG:
    def test_no_dependencies(self):
        dag = DAG({}, [1, 2, 3])
        ready = dag.get_ready_epics(set())
        assert ready == [1, 2, 3]

    def test_linear_chain(self):
        deps = {2: [1], 3: [2]}
        dag = DAG(deps, [1, 2, 3])

        assert dag.get_ready_epics(set()) == [1]
        assert dag.get_ready_epics({1}) == [2]
        assert dag.get_ready_epics({1, 2}) == [3]

    def test_diamond(self):
        deps = {2: [1], 3: [1], 4: [2, 3]}
        dag = DAG(deps, [1, 2, 3, 4])

        assert dag.get_ready_epics(set()) == [1]
        assert dag.get_ready_epics({1}) == [2, 3]
        assert dag.get_ready_epics({1, 2}) == [3]
        assert dag.get_ready_epics({1, 2, 3}) == [4]

    def test_cycle_detection(self):
        deps = {1: [2], 2: [1]}
        with pytest.raises(CycleError):
            DAG(deps, [1, 2])

    def test_topological_order(self):
        deps = {2: [1], 3: [1]}
        dag = DAG(deps, [1, 2, 3])
        order = dag.topological_order
        assert order.index(1) < order.index(2)
        assert order.index(1) < order.index(3)

    def test_has_dependencies(self):
        dag1 = DAG({}, [1, 2])
        assert dag1.has_dependencies() is False

        dag2 = DAG({2: [1]}, [1, 2])
        assert dag2.has_dependencies() is True

    def test_get_dependencies(self):
        dag = DAG({3: [1, 2]}, [1, 2, 3])
        assert dag.get_dependencies(3) == [1, 2]
        assert dag.get_dependencies(1) == []

    def test_completed_epics_excluded_from_ready(self):
        dag = DAG({}, [1, 2, 3])
        assert dag.get_ready_epics({1}) == [2, 3]


class TestDAGTiers:
    """Test the user's example: 1 is root, 2 and 4 depend on 1,
    3 depends on 2, 5 depends on 4, 6 depends on both 3 and 5."""

    def _make_dag(self):
        deps = {2: [1], 4: [1], 3: [2], 5: [4], 6: [3, 5]}
        return DAG(deps, [1, 2, 3, 4, 5, 6])

    def test_tiers(self):
        dag = self._make_dag()
        tiers = dag.get_tiers()
        assert tiers[0] == [1]        # root
        assert sorted(tiers[1]) == [2, 4]  # depend on 1
        assert sorted(tiers[2]) == [3, 5]  # depend on 2/4
        assert tiers[3] == [6]        # depends on 3 and 5

    def test_ready_epics_respects_gates(self):
        dag = self._make_dag()
        # Nothing done: only 1 can start
        assert dag.get_ready_epics(set()) == [1]
        # After 1: 2 and 4 unlock
        assert dag.get_ready_epics({1}) == [2, 4]
        # After 1,2: 3 unlocks but not 5 (needs 4)
        assert dag.get_ready_epics({1, 2}) == [3, 4]
        # After 1,2,4: 3 and 5 ready
        assert dag.get_ready_epics({1, 2, 4}) == [3, 5]
        # After 1,2,3,4,5: 6 unlocks
        assert dag.get_ready_epics({1, 2, 3, 4, 5}) == [6]

    def test_edges(self):
        dag = self._make_dag()
        edges = dag.get_edges()
        assert (1, 2) in edges
        assert (1, 4) in edges
        assert (2, 3) in edges
        assert (4, 5) in edges
        assert (3, 6) in edges
        assert (5, 6) in edges
        assert len(edges) == 6

    def test_to_dict(self):
        dag = self._make_dag()
        d = dag.to_dict()
        assert len(d["nodes"]) == 6
        assert len(d["edges"]) == 6
        assert len(d["tiers"]) == 4
        # Check node tier assignment
        node_tiers = {n["id"]: n["tier"] for n in d["nodes"]}
        assert node_tiers[1] == 0
        assert node_tiers[2] == 1
        assert node_tiers[6] == 3

    def test_no_deps_single_tier(self):
        dag = DAG({}, [1, 2, 3])
        tiers = dag.get_tiers()
        assert len(tiers) == 1
        assert sorted(tiers[0]) == [1, 2, 3]

    def test_chains(self):
        dag = self._make_dag()
        chains = dag.get_chains()
        assert len(chains) == 2
        assert [1, 2, 3, 6] in chains
        assert [1, 4, 5, 6] in chains

    def test_critical_path_by_story_count(self):
        dag = self._make_dag()
        # Chain A: 1(1) + 2(10) + 3(5) + 6(2) = 18
        # Chain B: 1(1) + 4(2)  + 5(3) + 6(2) = 8
        counts = {1: 1, 2: 10, 3: 5, 4: 2, 5: 3, 6: 2}
        crit = dag.get_critical_path(counts)
        assert crit == [1, 2, 3, 6]

    def test_critical_path_other_chain(self):
        dag = self._make_dag()
        # Make chain B heavier
        counts = {1: 1, 2: 2, 3: 1, 4: 10, 5: 8, 6: 1}
        crit = dag.get_critical_path(counts)
        assert crit == [1, 4, 5, 6]

    def test_chains_no_deps(self):
        dag = DAG({}, [1, 2, 3])
        chains = dag.get_chains()
        # Each independent epic is its own chain
        assert len(chains) == 3

    def test_to_dict_includes_chains_and_critical(self):
        dag = self._make_dag()
        d = dag.to_dict()
        assert "chains" in d
        assert len(d["chains"]) == 2


class TestBuildDag:
    def test_structured_takes_precedence(self):
        yaml_data = {"epic_dependencies": {2: [1]}}
        yaml_text = "# Epic 3 -> 2\n"  # would create different deps
        dag = build_dag(yaml_data, yaml_text, [1, 2])
        assert dag.get_dependencies(2) == [1]

    def test_falls_back_to_comments(self):
        yaml_data = {}
        yaml_text = "# Epic 1 -> 2\n"
        dag = build_dag(yaml_data, yaml_text, [1, 2])
        assert 1 in dag.get_dependencies(2)

    def test_no_dependencies(self):
        dag = build_dag({}, "", [1, 2, 3])
        assert dag.has_dependencies() is False
        assert dag.get_ready_epics(set()) == [1, 2, 3]
