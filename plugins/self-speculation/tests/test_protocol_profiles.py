from __future__ import annotations

import unittest

from self_speculation import (
    ProfileRegistrationError,
    ProfileRegistry,
    ProfileResolutionError,
    ToolCallProfile,
    default_profile_registry,
)


class ProfileRegistryTest(unittest.TestCase):
    def test_resolves_qwen_families_without_confusing_qwen3_8b(self) -> None:
        registry = default_profile_registry()

        self.assertEqual(
            registry.resolve(model_name="Qwen/Qwen3-8B").profile.profile_id,
            "qwen3_tagged_json",
        )
        self.assertEqual(
            registry.resolve(model_name="Qwen/Qwen3.8-27B").profile.profile_id,
            "qwen35_xml",
        )
        self.assertEqual(
            registry.resolve(
                model_name="Qwen/Qwen3.8-27B",
                format_name="tagged_json",
            ).profile.profile_id,
            "qwen35_tagged_json",
        )

    def test_resolves_deepseek_v4_from_model_or_dsml_template(self) -> None:
        registry = default_profile_registry()

        model = registry.resolve(model_name="deepseek-ai/DeepSeek-V4-Preview")
        template = registry.resolve(
            chat_template=(
                '<｜DSML｜tool_calls><｜DSML｜invoke name="x">'
                "</｜DSML｜invoke></｜DSML｜tool_calls>"
            )
        )

        self.assertEqual(model.profile.profile_id, "deepseek_v4_dsml")
        self.assertEqual(template.profile.profile_id, "deepseek_v4_dsml")
        self.assertEqual(template.source, "template")

    def test_explicit_profile_wins_over_legacy_format(self) -> None:
        resolution = default_profile_registry().resolve(
            "qwen35_xml",
            model_name="Qwen/Qwen3.8-27B",
            format_name="tagged_json",
        )

        self.assertEqual(resolution.profile.formatter, "qwen_xml")
        self.assertEqual(resolution.source, "explicit")

    def test_generic_tagged_json_is_not_mislabeled_as_qwen35(self) -> None:
        resolution = default_profile_registry().resolve(
            format_name="tagged_json"
        )
        self.assertEqual(resolution.profile.profile_id, "tagged_json")

    def test_rejects_unknown_and_duplicate_profiles(self) -> None:
        registry = ProfileRegistry()
        profile = ToolCallProfile(
            profile_id="custom",
            parser="custom",
            formatter="custom",
            prompt_renderer="custom",
            boundary_text="<call>",
            probe_prefix="<call>",
        )
        registry.register(profile)
        with self.assertRaises(ProfileRegistrationError):
            registry.register(profile)
        with self.assertRaises(ProfileResolutionError):
            registry.resolve("missing")

if __name__ == "__main__":
    unittest.main()
