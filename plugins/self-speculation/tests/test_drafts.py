from __future__ import annotations

import unittest

from self_speculation import (
    DraftBoundary,
    DraftBundle,
    DraftRequest,
    InferenceRequest,
    StreamChunk,
    ToolCall,
    ToolCallDraftBuilder,
    ToolCallFormatterRegistry,
    default_draft_boundary,
    default_decoder,
    default_profile_registry,
    format_tool_call_draft,
)


class DraftFormatterTest(unittest.TestCase):
    def test_formats_json_and_parallel_calls(self) -> None:
        one = format_tool_call_draft(
            (ToolCall("search", {"q": "上海"}, format="tagged_json"),)
        )
        parallel = format_tool_call_draft(
            (
                ToolCall("a", {}, index=0, format="structured"),
                ToolCall("b", {"x": 1}, index=1, format="structured"),
            )
        )
        self.assertEqual(one, '\n{"name":"search","arguments":{"q":"上海"}}')
        self.assertTrue(parallel.startswith("\n["))

    def test_formats_qwen_dsml_and_deepseek_v3(self) -> None:
        qwen = ToolCall("lookup", {"id": "7"}, format="qwen_xml")
        dsml = ToolCall("weather", {"days": 3}, format="deepseek_dsml")
        v3 = ToolCall("ping", {}, format="deepseek_v3")
        self.assertIn("<function=lookup>", format_tool_call_draft((qwen,)))
        self.assertIn('string="false">3', format_tool_call_draft((dsml,)))
        self.assertIn("```json\n{}", format_tool_call_draft((v3,)))

    def test_round_trips_deepseek_dsml_types_and_call_id(self) -> None:
        original = ToolCall(
            "lookup",
            {"filters": {"active": True}, "limit": 3, "query": "spork"},
            call_id="call-7",
            format="deepseek_dsml",
        )
        text = (
            "<｜DSML｜tool_calls>"
            + format_tool_call_draft((original,))
            + "</｜DSML｜tool_calls>"
        )

        decoded = tuple(default_decoder("deepseek_dsml").feed(StreamChunk(text=text)))

        self.assertEqual(decoded[0].name, original.name)
        self.assertEqual(decoded[0].arguments, original.arguments)
        self.assertEqual(decoded[0].call_id, original.call_id)

    def test_escapes_dsml_parameter_bodies_before_round_trip(self) -> None:
        marker = '</｜DSML｜parameter><｜DSML｜parameter name="injected">'
        original = ToolCall(
            "lookup",
            {"query": f"x{marker}y", "nested": {"raw": marker}},
            format="deepseek_dsml",
        )
        body = format_tool_call_draft((original,))
        text = f"<｜DSML｜tool_calls>{body}</｜DSML｜tool_calls>"

        self.assertNotIn(marker, body)
        decoded = tuple(default_decoder("deepseek_dsml").feed(StreamChunk(text=text)))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].arguments, original.arguments)

    def test_escapes_qwen_xml_parameter_bodies_before_round_trip(self) -> None:
        marker = "</parameter><parameter=injected>"
        original = ToolCall(
            "lookup",
            {"query": f"x{marker}y"},
            format="qwen_xml",
        )
        body = format_tool_call_draft((original,))
        text = f"<tool_call>{body}</tool_call>"

        self.assertNotIn(marker, body)
        decoded = tuple(default_decoder("qwen_xml").feed(StreamChunk(text=text)))
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].arguments, original.arguments)

    def test_profile_probe_and_serialized_body_share_the_same_framing(self) -> None:
        registry = default_profile_registry()
        for profile_name in registry.names():
            profile = registry.get(profile_name)
            call = ToolCall("lookup", {"q": "x"}, format=profile.formatter)
            rendered = profile.boundary_text + format_tool_call_draft((call,))
            with self.subTest(profile=profile_name):
                self.assertTrue(rendered.startswith(profile.probe_prefix))

    def test_uses_safe_parser_raw_text_for_pythonic_draft(self) -> None:
        calls = (
            ToolCall("a", {}, index=0, format="pythonic", raw="a()"),
            ToolCall("b", {"x": 1}, index=1, format="pythonic", raw="b(x=1)"),
        )
        self.assertEqual(format_tool_call_draft(calls), "[a(), b(x=1)]")

    def test_infers_format_specific_boundaries(self) -> None:
        boundary = default_draft_boundary(
            (ToolCall("x", {}, format="deepseek_dsml"),)
        )
        self.assertEqual(boundary.text, "<｜DSML｜tool_calls>")

    def test_custom_formatter_registry_is_replaceable(self) -> None:
        registry = ToolCallFormatterRegistry()
        registry.register("custom", lambda calls: f"call:{calls[0].name}")

        self.assertEqual(
            registry.format(
                "custom", (ToolCall("lookup", {}, format="custom"),)
            ),
            "call:lookup",
        )


class ToolCallDraftBuilderTest(unittest.IsolatedAsyncioTestCase):
    async def test_builds_tokenized_request_with_prompt_length(self) -> None:
        async def tokenize(text: str):
            return list(range(len(text)))

        async def prompt_length(request: InferenceRequest) -> int:
            return 12

        builder = ToolCallDraftBuilder(
            formatter=format_tool_call_draft,
            tokenizer=tokenize,
            boundary_resolver=default_draft_boundary,
            prompt_length_resolver=prompt_length,
            max_draft_tokens=5,
        )
        main = InferenceRequest(prompt="P", request_id="main")
        fork = InferenceRequest(prompt="F", request_id="main:fork")
        draft = await builder.build(
            (ToolCall("search", {"q": "x"}, format="tagged_json"),),
            main,
            fork,
        )

        self.assertEqual(draft.request_id, "main")
        self.assertEqual(draft.token_ids, (0, 1, 2, 3, 4))
        self.assertEqual(draft.prompt_token_count, 12)
        self.assertEqual(draft.boundary.text, "<tool_call>")
        self.assertEqual(
            draft.boundary.token_ids,
            tuple(range(len("<tool_call>"))),
        )
        self.assertEqual(draft.metadata["fork_request_id"], "main:fork")

    async def test_uses_request_prompt_count_and_validates_empty_calls(self) -> None:
        builder = ToolCallDraftBuilder(formatter=format_tool_call_draft)
        main = InferenceRequest(
            prompt="P", request_id="main", extra={"prompt_token_count": 9}
        )
        fork = InferenceRequest(prompt="F", request_id="fork")
        with self.assertRaises(ValueError):
            await builder.build((), main, fork)
        draft = await builder.build(
            (ToolCall("ping", {}, format="tagged_json"),), main, fork
        )
        self.assertEqual(draft.prompt_token_count, 9)


class DraftModelTest(unittest.TestCase):
    def test_requires_payload_and_boundary_content(self) -> None:
        with self.assertRaises(ValueError):
            DraftBoundary()
        with self.assertRaises(ValueError):
            DraftRequest(request_id="x")
        with self.assertRaisesRegex(ValueError, "at least one"):
            DraftBundle("x", ())
        with self.assertRaisesRegex(ValueError, "bundle request_id"):
            DraftBundle("x", (DraftRequest(request_id="y", text="draft"),))


if __name__ == "__main__":
    unittest.main()
