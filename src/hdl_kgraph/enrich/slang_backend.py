"""pyslang enrichment backend (M7) — elaboration-accurate SystemVerilog facts.

`slang <https://github.com/MikePopoloski/slang>`_ is a full IEEE-1800 frontend;
its Python binding (``pyslang``) elaborates a design — resolving parameters,
unrolling ``generate`` loops and instance arrays, applying ``defparam`` — which
the tree-sitter tier cannot. This backend walks the elaborated instance tree
and reconciles it with the heuristic graph:

* the syntactic ``INSTANTIATES`` edge of a confirmed instantiation is upgraded
  to confidence ``1.0`` and stamped ``attrs["source"] = "elaborated"``;
* a generate/array instantiation whose elaborated multiplicity exceeds one is
  recorded as an ``instance_count`` discrepancy, the syntactic instance node is
  annotated with ``attrs["elaborated_count"]``, and one elaborated ``INSTANCE``
  node per iteration is added so the graph reflects elaborated reality;
* an instantiation whose elaborated target differs from the heuristic guess is
  recorded as a ``wrong_target`` discrepancy.

``pyslang`` is an optional dependency (the ``enrich`` extra), imported lazily
inside the methods below so importing this module never requires it; the
backend reports itself unavailable when it is absent. Elaboration of an
incomplete or erroneous design never raises out of :meth:`enrich` — failures
degrade to the heuristic graph and surface as diagnostics.

Scope (v0.7 first cut): instance-count correction and ``INSTANTIATES``
confirmation. Full type/width propagation and ``CONNECTS``/``PARAMETERIZES``
value upgrades are a documented follow-on within M7.
"""

from __future__ import annotations

from collections import defaultdict
from time import perf_counter
from typing import Any

import networkx as nx

from hdl_kgraph.enrich._graphutil import enclosing_module, instantiates_target
from hdl_kgraph.enrich._profile import add, count, phase
from hdl_kgraph.enrich.base import (
    Capabilities,
    Discrepancy,
    EdgeUpgrade,
    EnrichmentInput,
    EnrichmentResult,
)
from hdl_kgraph.ids import elab_node_id
from hdl_kgraph.schema import CONFIDENCE_RESOLVED, Edge, EdgeKind, Language, Node, NodeKind

_SUFFIXES = frozenset({".v", ".vh", ".sv", ".svh"})


class SlangBackend:
    """SystemVerilog/Verilog elaboration via pyslang."""

    name = "slang"
    suffixes = _SUFFIXES

    def available(self) -> bool:
        # pyslang is an optional dependency (the `enrich` extra); the probe
        # stays cheap and import-safe so a bare install degrades gracefully.
        try:
            import pyslang  # noqa: F401
        except ImportError:
            return False
        return True

    def capabilities(self) -> Capabilities:
        return Capabilities(
            resolves_params=True,
            unrolls_generates=True,
            resolves_types=True,
            resolves_defparam=True,
        )

    def enrich(self, inp: EnrichmentInput, graph: nx.MultiDiGraph) -> EnrichmentResult:
        result = EnrichmentResult()
        try:
            children = _elaborate(inp, result)
        except Exception as exc:  # never fail the build on an elaboration error
            result.diagnostics.append(f"slang: elaboration failed, kept heuristic graph ({exc})")
            return result
        if children is None:
            return result
        with phase("slang/reconcile"):
            _reconcile(graph, children, result)
        return result


# (module_def, instance_base_name) -> {target_def -> [module-relative paths]}.
# The multiplicity of one syntactic instantiation within a single instance of
# its enclosing module; >1 means a generate loop or instance array.
_ChildMap = dict[tuple[str, str], dict[str, list[str]]]


def _elaborate(inp: EnrichmentInput, result: EnrichmentResult) -> _ChildMap | None:
    """Compile *inp* and summarize each module's direct child instances."""
    import pyslang
    from pyslang.ast import Compilation, CompilationOptions, SymbolKind
    from pyslang.parsing import PreprocessorOptions
    from pyslang.syntax import SyntaxTree

    with phase("slang/setup"):
        pp = PreprocessorOptions()
        pp.predefines = [
            f"{name}={value}" if value is not None else name for name, value in inp.defines.items()
        ]
        pp.additionalIncludePaths = [str(d) for d in inp.incdirs]
        tree_bag = pyslang.Bag([pp])

        comp_opts = CompilationOptions()
        if inp.tops:
            comp_opts.topModules = set(inp.tops)
        comp = Compilation(pyslang.Bag([comp_opts]))
        source_manager = pyslang.SourceManager()

    added = 0
    with phase("slang/parse_trees"):
        for path in inp.files:
            try:
                comp.addSyntaxTree(SyntaxTree.fromFile(str(path), source_manager, tree_bag))
                added += 1
            except Exception as exc:
                result.diagnostics.append(f"slang: could not read {path} ({exc})")
    if not added:
        return None

    # ``getRoot`` forces elaboration; ``getAllDiagnostics`` then realizes any
    # deferred elaboration diagnostics — both are prime suspects for the pass's
    # cost, so they are timed apart from the Python-side tree walk below.
    with phase("slang/elaborate_root"):
        root = comp.getRoot()
    with phase("slang/diagnostics"):
        error_count = sum(1 for d in comp.getAllDiagnostics() if _is_error(d))
    if error_count:
        result.diagnostics.append(
            f"slang: {error_count} elaboration diagnostic(s); "
            "facts taken where elaboration succeeded"
        )

    # Record each container *instance*'s direct child instances keyed by its
    # hierarchical path, then take the max multiplicity per (module def, child
    # name, target def) across all instances of that module. Max (rather than a
    # single representative) keeps a plain duplicate instantiation at its true
    # count of one — every instance of the module has the same one child — while
    # still reporting the largest expansion when parameterized specializations
    # of the same module elaborate to different generate/array counts.
    per_instance: defaultdict[str, dict[tuple[str, str], list[str]]] = defaultdict(dict)
    container_def_of: dict[str, str] = {}

    def members(scope: Any) -> list[Any]:
        try:
            return list(scope)
        except TypeError:
            return []

    # Hot loop: ``walk`` runs once per elaborated instance (generate-unrolled),
    # so it dominates the enrichment pass on large designs. The two suspected
    # costs — forcing lazy member elaboration (``members``) and reconstructing
    # each instance's hierarchical-path string (``hierarchicalPath``) — are timed
    # with bare perf_counter accumulators (not ``phase()`` per node, whose
    # context-manager overhead would itself distort a per-instance measurement),
    # then recorded once below. ``inst`` counts instances so the CLI can report
    # per-instance cost. See docs/benchmarks.md and slang_backend profiling.
    walk_members_s = 0.0
    walk_path_s = 0.0
    inst = 0

    def walk(scope: Any, container_path: str, container_def: str) -> None:
        nonlocal walk_members_s, walk_path_s, inst
        container_def_of[container_path] = container_def
        _t = perf_counter()
        scope_members = members(scope)
        walk_members_s += perf_counter() - _t
        for m in scope_members:
            kind = getattr(m, "kind", None)
            if kind == SymbolKind.Instance:
                inst += 1
                child_def = m.definition.name if getattr(m, "definition", None) else ""
                _t = perf_counter()
                path = m.hierarchicalPath
                walk_path_s += perf_counter() - _t
                suffix = path[len(container_path) + 1 :] if container_path else path
                per_instance[container_path].setdefault((m.name, child_def), []).append(suffix)
                walk(getattr(m, "body", m), path, child_def)
            elif kind in (SymbolKind.GenerateBlock, SymbolKind.GenerateBlockArray):
                walk(m, container_path, container_def)

    with phase("slang/walk_tree"):
        for top in root.topInstances:
            top_def = top.definition.name if getattr(top, "definition", None) else top.name
            walk(getattr(top, "body", top), top.hierarchicalPath, top_def)
    # Sub-breakdown of walk_tree: members (lazy elaboration) vs hierarchicalPath
    # (string reconstruction) vs the residual (pure Python recursion).
    add("slang/walk_members", walk_members_s)
    add("slang/walk_hierpath", walk_path_s)
    count("walk_instances", inst)

    with phase("slang/summarize"):
        children: _ChildMap = {}
        for container_path, groups in per_instance.items():
            container_def = container_def_of[container_path]
            for (base, child_def), suffixes in groups.items():
                target_map = children.setdefault((container_def, base), {})
                rel_paths = [f"{container_def}.{suffix}" for suffix in suffixes]
                if len(rel_paths) > len(target_map.get(child_def, [])):
                    target_map[child_def] = rel_paths
    return children


def _is_error(diag: Any) -> bool:
    try:
        return bool(diag.isError())
    except Exception:
        return True


def _reconcile(graph: nx.MultiDiGraph, children: _ChildMap, result: EnrichmentResult) -> None:
    """Match elaborated multiplicities against the heuristic INSTANCE nodes."""
    for inst_id, data in list(graph.nodes(data=True)):
        if data["kind"] is not NodeKind.INSTANCE:
            continue
        module = enclosing_module(graph, inst_id)
        target = instantiates_target(graph, inst_id)
        if module is None or target is None:
            continue
        module_id, module_name = module
        target_id, target_name = target
        child_map = children.get((module_name, data["name"]))
        if child_map is None:
            continue  # elaboration never reached this instance (e.g. dead top)

        if target_name not in child_map:
            elaborated = ", ".join(sorted(child_map)) or "(none)"
            result.discrepancies.append(
                Discrepancy(
                    kind="wrong_target",
                    backend=SlangBackend.name,
                    detail=f"{module_name}.{data['name']} binds to {elaborated}, not {target_name}",
                    node_id=inst_id,
                    src=inst_id,
                    dst=target_id,
                    heuristic=target_name,
                    elaborated=elaborated,
                )
            )
            continue

        rel_paths = child_map[target_name]
        count = len(rel_paths)
        result.upgrades.append(
            EdgeUpgrade(
                src=inst_id,
                dst=target_id,
                kind=EdgeKind.INSTANTIATES,
                confidence=CONFIDENCE_RESOLVED,
                attrs={
                    "source": "elaborated",
                    "backend": SlangBackend.name,
                    "elaborated_count": count,
                },
            )
        )
        if count > 1:
            result.node_annotations[inst_id] = {"elaborated_count": count}
            result.discrepancies.append(
                Discrepancy(
                    kind="instance_count",
                    backend=SlangBackend.name,
                    detail=(
                        f"{module_name}.{data['name']} (target {target_name}) elaborates to "
                        f"{count} instances; tree-sitter saw 1"
                    ),
                    node_id=inst_id,
                    heuristic="1",
                    elaborated=str(count),
                )
            )
            _add_elaborated(graph, data, module_id, target_id, target_name, rel_paths, result)


def _add_elaborated(
    graph: nx.MultiDiGraph,
    inst_data: dict[str, Any],
    module_id: str,
    target_id: str,
    target_name: str,
    rel_paths: list[str],
    result: EnrichmentResult,
) -> None:
    """One elaborated INSTANCE node (+ DECLARES/INSTANTIATES) per iteration."""
    language = inst_data.get("language", Language.UNKNOWN)
    file = inst_data.get("file", "")
    for rel_path in rel_paths:
        node_id = elab_node_id(NodeKind.INSTANCE, rel_path)
        result.new_nodes.append(
            Node(
                id=node_id,
                kind=NodeKind.INSTANCE,
                name=rel_path.rsplit(".", 1)[-1],
                qualified_name=rel_path,
                file=file,
                language=language,
                attrs={
                    "target": target_name,
                    "source": "elaborated",
                    "backend": SlangBackend.name,
                    "elaborated_from": inst_data.get("qualified_name", ""),
                },
            )
        )
        result.new_edges.append(
            Edge(src=module_id, dst=node_id, kind=EdgeKind.DECLARES, confidence=CONFIDENCE_RESOLVED)
        )
        result.new_edges.append(
            Edge(
                src=node_id,
                dst=target_id,
                kind=EdgeKind.INSTANTIATES,
                confidence=CONFIDENCE_RESOLVED,
                attrs={"source": "elaborated", "backend": SlangBackend.name},
            )
        )
