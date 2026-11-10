from __future__ import annotations

import unittest

from self_speculation import StreamChunk, default_decoder


class DeepSeekDsmlParserTest(unittest.TestCase):
    def test_decodes_typed_dsml_parameters_across_chunks(self) -> None:
        decoder = default_decoder("deepseek_dsml")
        first = tuple(
            decoder.feed(
                StreamChunk(
                    text=(
                        '<｜DSML｜tool_calls><｜DSML｜invoke name="weather">'
                        '<｜DSML｜parameter name="city" string="true">杭州'
                    )
                )
            )
        )
        second = tuple(
            decoder.feed(
                StreamChunk(
                    text=(
                        '</｜DSML｜parameter>'
                        '<｜DSML｜parameter string="false" name="days">3'
                        '</｜DSML｜parameter>'
                        '<｜DSML｜parameter name="metric" string="false">true'
                        '</｜DSML｜parameter>'
                        '</｜DSML｜invoke></｜DSML｜tool_calls>'
                    )
                )
            )
        )

        self.assertEqual(first, ())
        self.assertEqual(second[0].name, "weather")
        self.assertEqual(
            second[0].arguments,
            {"city": "杭州", "days": 3, "metric": True},
        )

    def test_reconstructs_forced_dsml_invoke_prefix(self) -> None:
        decoder = default_decoder(
            "deepseek_dsml",
            initial_text='<｜DSML｜tool_calls><｜DSML｜invoke name="',
        )
        calls = tuple(
            decoder.feed(
                StreamChunk(
                    text='ping"></｜DSML｜invoke></｜DSML｜tool_calls>'
                )
            )
        )
        self.assertEqual(calls[0].name, "ping")
        self.assertEqual(calls[0].arguments, {})

    def test_supports_parallel_invokes_and_ascii_pipe_variant(self) -> None:
        calls = tuple(
            default_decoder().feed(
                StreamChunk(
                    text=(
                        '<|DSML|tool_calls>'
                        '<|DSML|invoke name="a"></|DSML|invoke>'
                        '<|DSML|invoke name="b">'
                        '<|DSML|parameter name="items" string="false">[1,2]'
                        '</|DSML|parameter></|DSML|invoke>'
                        '</|DSML|tool_calls>'
                    )
                )
            )
        )
        self.assertEqual([call.name for call in calls], ["a", "b"])
        self.assertEqual(calls[1].arguments, {"items": [1, 2]})

    def test_supports_attribute_order_quotes_and_call_id(self) -> None:
        calls = tuple(
            default_decoder("deepseek_dsml").feed(
                StreamChunk(
                    text=(
                        "<｜DSML｜tool_calls>"
                        "<｜DSML｜invoke id='call-7' name='lookup'>"
                        "<｜DSML｜parameter string='false' name='filters'>"
                        '{"active":true}'
                        "</｜DSML｜parameter>"
                        "</｜DSML｜invoke></｜DSML｜tool_calls>"
                    )
                )
            )
        )

        self.assertEqual(calls[0].name, "lookup")
        self.assertEqual(calls[0].call_id, "call-7")
        self.assertEqual(calls[0].arguments, {"filters": {"active": True}})

    def test_rejects_duplicate_or_dangling_parameters(self) -> None:
        duplicate = (
            '<｜DSML｜invoke name="x">'
            '<｜DSML｜parameter name="a">1</｜DSML｜parameter>'
            '<｜DSML｜parameter name="a">2</｜DSML｜parameter>'
            "</｜DSML｜invoke>"
        )
        dangling = (
            '<｜DSML｜invoke name="x">'
            '<｜DSML｜parameter name="a">1'
            "</｜DSML｜invoke>"
        )

        self.assertEqual(
            tuple(
                default_decoder("deepseek_dsml").feed(
                    StreamChunk(text=duplicate)
                )
            ),
            (),
        )
        self.assertEqual(
            tuple(
                default_decoder("deepseek_dsml").feed(
                    StreamChunk(text=dangling)
                )
            ),
            (),
        )

    def test_rejects_an_invalid_non_string_parameter(self) -> None:
        malformed = (
            '<｜DSML｜invoke name="x">'
            '<｜DSML｜parameter name="count" string="false">not-json'
            '</｜DSML｜parameter></｜DSML｜invoke>'
        )

        self.assertEqual(
            tuple(
                default_decoder("deepseek_dsml").feed(
                    StreamChunk(text=malformed)
                )
            ),
            (),
        )

    def test_rejects_duplicate_or_unparsed_attributes(self) -> None:
        ambiguous_invoke = (
            '<｜DSML｜invoke name="safe" name="danger">'
            "</｜DSML｜invoke>"
        )
        ambiguous_parameter = (
            '<｜DSML｜invoke name="x">'
            '<｜DSML｜parameter name="count" string="true" string="false">1'
            "</｜DSML｜parameter></｜DSML｜invoke>"
        )
        unparsed_attribute = (
            '<｜DSML｜invoke name="x" invalid>'
            "</｜DSML｜invoke>"
        )

        for malformed in (
            ambiguous_invoke,
            ambiguous_parameter,
            unparsed_attribute,
        ):
            with self.subTest(malformed=malformed):
                self.assertEqual(
                    tuple(
                        default_decoder("deepseek_dsml").feed(
                            StreamChunk(text=malformed)
                        )
                    ),
                    (),
                )


if __name__ == "__main__":
    unittest.main()
