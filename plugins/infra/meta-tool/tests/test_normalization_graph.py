import json

from bate.graph import StateGraphBuilder
from bate.models import SemanticType, ToolCall, Trace
from bate.normalization import (
    NormalizationConfig,
    RegexSubstitution,
    ToolRule,
    TraceNormalizer,
)


def _trace(trace_id, *calls):
    return Trace(trace_id=trace_id, calls=tuple(calls))


def _call(name, **arguments):
    return ToolCall(name=name, arguments=arguments)


def test_accessor_order_and_duplicates_merge_to_same_state():
    config = NormalizationConfig(
        tool_rules=(
            ToolRule(pattern=r"^get\.", semantic_type=SemanticType.ACCESSOR),
        )
    )
    traces = [
        _trace("one", _call("get.a", id=1), _call("get.b", id=2)),
        _trace("two", _call("get.b", id=2), _call("get.a", id=1)),
        _trace(
            "three",
            _call("get.a", id=1),
            _call("get.b", id=2),
            _call("get.b", id=2),
        ),
    ]
    result = StateGraphBuilder(TraceNormalizer(config)).build(traces)
    final_states = [path.nodes[-1] for path in result.paths]
    assert len(set(final_states)) == 1
    assert result.graph.stats().self_loops == 1


def test_disjoint_domains_are_commutative_only_when_enabled():
    rules = (
        ToolRule(pattern=r"^spotify\.", domain="spotify"),
        ToolRule(pattern=r"^gmail\.", domain="gmail"),
    )
    traces = [
        _trace("one", _call("spotify.like", id=1), _call("gmail.send", id=2)),
        _trace("two", _call("gmail.send", id=2), _call("spotify.like", id=1)),
    ]
    merged = StateGraphBuilder(
        TraceNormalizer(
            NormalizationConfig(tool_rules=rules, domain_commutativity=True)
        )
    ).build(traces)
    ordered = StateGraphBuilder(
        TraceNormalizer(
            NormalizationConfig(tool_rules=rules, domain_commutativity=False)
        )
    ).build(traces)
    assert merged.paths[0].nodes[-1] == merged.paths[1].nodes[-1]
    assert ordered.paths[0].nodes[-1] != ordered.paths[1].nodes[-1]


def test_unknown_domain_is_an_ordering_barrier():
    config = NormalizationConfig(
        tool_rules=(
            ToolRule(pattern=r"^spotify\.", domain="spotify"),
            ToolRule(pattern=r"^gmail\.", domain="gmail"),
        ),
        domain_commutativity=True,
    )
    traces = [
        _trace(
            "one",
            _call("spotify.like", id=1),
            _call("unknown.commit", id=9),
            _call("gmail.send", id=2),
        ),
        _trace(
            "two",
            _call("gmail.send", id=2),
            _call("unknown.commit", id=9),
            _call("spotify.like", id=1),
        ),
    ]
    result = StateGraphBuilder(TraceNormalizer(config)).build(traces)
    assert result.paths[0].nodes[-1] != result.paths[1].nodes[-1]


def test_regex_and_placeholder_rules_merge_ephemeral_ids():
    config = NormalizationConfig(
        regex_substitutions=(
            RegexSubstitution(pattern=r"user-\d+", replacement="user-{UID}"),
        ),
        tool_rules=(
            ToolRule(
                pattern=r"^search$",
                placeholder_arguments={"query": "{QUERY}"},
            ),
        ),
    )
    traces = [
        _trace("one", _call("search", query="alice", owner="user-123")),
        _trace("two", _call("search", query="bob", owner="user-987")),
    ]
    result = StateGraphBuilder(TraceNormalizer(config)).build(traces)
    assert result.paths[0].nodes[-1] == result.paths[1].nodes[-1]
    edge = result.graph.edges[("root", result.paths[0].nodes[-1])]
    assert edge.weight == 2


def test_environment_context_can_participate_in_state_identity():
    config = NormalizationConfig(state_context_fields=("url",))
    traces = [
        _trace(
            "one",
            ToolCall(
                name="browser.click",
                arguments={"element_id": 2},
                metadata={"url": "https://shop.test/product/one"},
            ),
        ),
        _trace(
            "two",
            ToolCall(
                name="browser.click",
                arguments={"element_id": 2},
                metadata={"url": "https://shop.test/product/two"},
            ),
        ),
    ]
    result = StateGraphBuilder(TraceNormalizer(config)).build(traces)
    assert result.paths[0].nodes[-1] != result.paths[1].nodes[-1]


def test_tool_rewrite_can_be_conditioned_on_arguments():
    config = NormalizationConfig(
        tool_rules=(
            ToolRule(
                pattern=r"^browser\.type$",
                argument_equals={"element_id": 3},
                canonical_name="write_search",
                ignore_arguments=("element_id",),
                placeholder_arguments={"text": "{QUERY}"},
            ),
        )
    )
    normalizer = TraceNormalizer(config)
    search = normalizer.canonicalize(
        ToolCall("browser.type", {"element_id": 3, "text": "laptop"})
    )
    ordinary = normalizer.canonicalize(
        ToolCall("browser.type", {"element_id": 8, "text": "laptop"})
    )
    assert search.name == "write_search"
    assert search.arguments == {"text": "{QUERY}"}
    assert ordinary.name == "browser.type"


def test_annotators_are_excluded_by_default():
    config = NormalizationConfig(
        tool_rules=(
            ToolRule(
                pattern=r"^log$",
                semantic_type=SemanticType.ANNOTATOR,
            ),
        )
    )
    result = StateGraphBuilder(TraceNormalizer(config)).build(
        [_trace("one", _call("log", text="thinking"), _call("write", value=1))]
    )
    assert len(result.paths[0].actions) == 1
    assert result.ignored_annotators == 1


def test_explicit_recurrent_action_collapses_to_self_loop():
    config = NormalizationConfig(
        tool_rules=(
            ToolRule(pattern=r"^browser\.next_page$", recurrent=True),
        )
    )
    result = StateGraphBuilder(TraceNormalizer(config)).build(
        [
            _trace(
                "one",
                _call("browser.next_page"),
                _call("browser.next_page"),
                _call("browser.next_page"),
            )
        ]
    )
    path = result.paths[0]
    assert path.nodes[1] == path.nodes[2] == path.nodes[3]
    assert result.graph.stats().self_loops == 1


def test_structural_matching_merges_nested_values_with_the_same_shape():
    config = NormalizationConfig(argument_matching="structural")
    traces = [
        _trace(
            "one",
            _call(
                "provider.freeze",
                spec={"hypotheses": ["unroll"], "budget": 300},
            ),
        ),
        _trace(
            "two",
            _call(
                "provider.freeze",
                spec={"hypotheses": ["packing", "scheduling"], "budget": 600},
            ),
        ),
    ]
    result = StateGraphBuilder(TraceNormalizer(config)).build(traces)

    assert result.paths[0].nodes[-1] == result.paths[1].nodes[-1]
    calls = [TraceNormalizer(config).canonicalize(trace.calls[0]) for trace in traces]
    assert calls[0].exact_fingerprint != calls[1].exact_fingerprint
    assert calls[0].structural_fingerprint == calls[1].structural_fingerprint


def test_structural_matching_preserves_operation_discriminators():
    config = NormalizationConfig(argument_matching="structural")
    traces = [
        _trace("create", _call("provider.execute", operation="create", id="a")),
        _trace("delete", _call("provider.execute", operation="delete", id="b")),
    ]
    result = StateGraphBuilder(TraceNormalizer(config)).build(traces)

    assert result.paths[0].nodes[-1] != result.paths[1].nodes[-1]


def test_structural_matching_keeps_shell_and_write_tools_exact():
    config = NormalizationConfig(argument_matching="structural")
    traces = [
        _trace("one", _call("bash", command="echo one")),
        _trace("two", _call("bash", command="echo two")),
    ]
    result = StateGraphBuilder(TraceNormalizer(config)).build(traces)

    assert result.paths[0].nodes[-1] != result.paths[1].nodes[-1]


def test_default_exact_matching_preserves_values_and_mutator_order():
    result = StateGraphBuilder().build([
        _trace("one", _call("update", id=1), _call("update", id=2)),
        _trace("two", _call("update", id=2), _call("update", id=1)),
    ])
    assert result.paths[0].nodes[1] != result.paths[1].nodes[1]
    assert result.paths[0].nodes[-1] != result.paths[1].nodes[-1]


def test_occurrences_preserve_raw_arguments_and_indices_after_normalization():
    config = NormalizationConfig(tool_rules=(
        ToolRule(pattern="^log$", semantic_type=SemanticType.ANNOTATOR),
        ToolRule(pattern="^lookup$", placeholder_arguments={"query": "{QUERY}"}),
    ))
    original = _call("lookup", query="alice", nested={"ids": [1, 2]})
    result = StateGraphBuilder(TraceNormalizer(config)).build([
        _trace("one", _call("log", text="ignored"), original),
    ])
    occurrence = result.paths[0].actions[0].calls[0]
    assert occurrence.trace_id == "one"
    assert occurrence.call_index == 1
    assert occurrence.raw is original
    assert occurrence.canonical.arguments["query"] == "{QUERY}"
    assert original.arguments == {"query": "alice", "nested": {"ids": [1, 2]}}


def test_edge_support_counts_traces_separately_from_traversals():
    config = NormalizationConfig(tool_rules=(
        ToolRule(pattern="^get$", semantic_type=SemanticType.ACCESSOR),
    ))
    result = StateGraphBuilder(TraceNormalizer(config)).build([
        _trace("one", *[_call("get", id=1) for _ in range(3)]),
        _trace("two", _call("get", id=1), _call("get", id=1)),
    ])
    final = result.paths[0].nodes[-1]
    node = result.graph.nodes[final]
    assert node.weight == 2
    assert node.visits == 5
    loop = result.graph.edges[(final, final)]
    assert loop.weight == 2
    assert loop.traversals == 3
    assert len(result.graph.outgoing(final)) == 1
    assert result.graph.outgoing(final, include_self=False) == []
    payload = json.loads(json.dumps(result.graph.to_dict()))
    assert payload["stats"]["total_traversals"] == 5
    assert "digraph meta_tool" in result.graph.to_dot()


def test_structural_matching_preserves_flags_and_exact_environment_context():
    normalizer = TraceNormalizer(NormalizationConfig(
        argument_matching="structural", state_context_fields=("environment.url",),
    ))
    first = normalizer.canonicalize(ToolCall(
        "lookup", {"id": 1, "recursive": False},
        metadata={"environment": {"url": "https://example.test/one"}},
    ))
    flag_changed = normalizer.canonicalize(ToolCall(
        "lookup", {"id": 2, "recursive": True},
        metadata={"environment": {"url": "https://example.test/one"}},
    ))
    context_changed = normalizer.canonicalize(ToolCall(
        "lookup", {"id": 2, "recursive": False},
        metadata={"environment": {"url": "https://example.test/two"}},
    ))
    assert first.fingerprint != flag_changed.fingerprint
    assert first.fingerprint != context_changed.fingerprint
