from __future__ import annotations

import unittest

from self_speculation import StreamChunk, default_decoder


class QwenXmlParserTest(unittest.TestCase):
    def test_decodes_qwen_xml_across_chunks(self) -> None:
        decoder = default_decoder("qwen_xml")
        first = tuple(
            decoder.feed(
                StreamChunk(
                    text=(
                        "<tool_call>\n<function=search>\n"
                        "<parameter=query>\nSPORK"
                    )
                )
            )
        )
        second = tuple(
            decoder.feed(
                StreamChunk(
                    text="\n</parameter>\n</function>\n</tool_call>"
                )
            )
        )

        self.assertEqual(first, ())
        self.assertEqual(second[0].name, "search")
        self.assertEqual(second[0].arguments, {"query": "SPORK"})

    def test_reconstructs_forced_function_prefix(self) -> None:
        decoder = default_decoder(
            "qwen_xml", initial_text="<tool_call>\n<function="
        )
        calls = tuple(
            decoder.feed(
                StreamChunk(
                    text=(
                        "lookup>\n<parameter=id>\n7\n</parameter>"
                        "\n</function>\n</tool_call>"
                    )
                )
            )
        )
        self.assertEqual(calls[0].name, "lookup")
        self.assertEqual(calls[0].arguments, {"id": "7"})

    def test_supports_multiple_xml_function_calls(self) -> None:
        decoder = default_decoder()
        calls = tuple(
            decoder.feed(
                StreamChunk(
                    text=(
                        "<tool_call><function=a></function></tool_call>"
                        "<tool_call><function=b>"
                        "<parameter=x>2</parameter></function></tool_call>"
                    )
                )
            )
        )
        self.assertEqual(
            [(call.index, call.name, call.arguments) for call in calls],
            [(0, "a", {}), (1, "b", {"x": "2"})],
        )

    def test_rejects_nested_or_dangling_parameter_tags(self) -> None:
        malformed = (
            "<tool_call><function=find_user>"
            "<parameter=name>Mei<parameter=zip>28236</parameter>"
            "</function></tool_call>"
        )

        decoder = default_decoder("qwen_xml")

        self.assertEqual(tuple(decoder.feed(StreamChunk(text=malformed))), ())

    def test_rejects_duplicate_or_empty_parameter_names(self) -> None:
        duplicate = (
            "<tool_call><function=x>"
            "<parameter=a>1</parameter><parameter=a>2</parameter>"
            "</function></tool_call>"
        )
        empty = (
            "<tool_call><function=x>"
            "<parameter=&#32;>1</parameter>"
            "</function></tool_call>"
        )

        for malformed in (duplicate, empty):
            with self.subTest(malformed=malformed):
                self.assertEqual(
                    tuple(
                        default_decoder("qwen_xml").feed(
                            StreamChunk(text=malformed)
                        )
                    ),
                    (),
                )


if __name__ == "__main__":
    unittest.main()
