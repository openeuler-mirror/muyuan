import unittest
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

from bate.naming import assign_short_names, template_fingerprint

from bate.spec import MetaToolSpec


def example():
    return MetaToolSpec.from_dict({
        "name": "meta_tool_01_supervisor_show_profile_supervisor_show_account_pas",
        "description": "A workflow observed in 3 traces.",
        "input_schema": {"type": "object", "properties": {"password": {"type": "string"}},
                         "required": ["password"]},
        "steps": [
            {"id": "step_1", "tool": "supervisor.show_profile", "arguments": {}},
            {"id": "step_2", "tool": "phone.login", "arguments": {
                "username": {"$step": "step_1", "path": ["phone_number"]},
                "password": {"$input": "password"},
            }},
        ],
        "output": {"$step": "step_2", "path": []},
        "metadata": {"support": 3, "execution_guard": "confirm_each_run"},
    })


class NamingTests(unittest.TestCase):
    def test_short_name_preserves_contract_and_guard(self):
        original = example()
        before = original.to_dict()
        renamed, = assign_short_names([original])
        self.assertRegex(renamed.name, r"^mt_[0-9a-f]{12}$")
        for field in ("steps", "input_schema", "output", "description"):
            self.assertEqual(before[field], renamed.to_dict()[field])
        self.assertEqual(renamed.metadata["execution_guard"], "confirm_each_run")
        self.assertEqual(before, original.to_dict())
        self.assertEqual(assign_short_names([renamed])[0], renamed)

    def test_support_ranking_and_description_do_not_rename(self):
        original = example()
        later = replace(original, name="meta_tool_27_long_name", description="30 traces",
                        metadata={"support": 30, "source_trace_ids": ["new"]})
        self.assertEqual(template_fingerprint(original), template_fingerprint(later))

    def test_bound_parameters_distinguish_same_sequence(self):
        original = example()
        payload = deepcopy(original.to_dict())
        payload["steps"][1]["arguments"]["username"]["path"] = ["email"]
        other = MetaToolSpec.from_dict(payload)
        a, b = assign_short_names([original, other])
        self.assertNotEqual(a.name, b.name)
        self.assertEqual(assign_short_names([other, original]), [b, a])

    def test_output_and_input_contract_are_part_of_identity(self):
        original = example()
        self.assertNotEqual(template_fingerprint(original), template_fingerprint(
            replace(original, output={"$step": "step_2", "path": ["access_token"]})))
        optional_input = deepcopy(original.input_schema)
        optional_input["required"] = []
        self.assertNotEqual(template_fingerprint(original), template_fingerprint(
            replace(original, input_schema=optional_input)))

    def test_collision_is_rejected(self):
        original = example()
        other = replace(original, output={"$step": "step_1", "path": []})
        with patch("bate.naming.short_callable_name", return_value="mt_collision"):
            with self.assertRaisesRegex(ValueError, "collision"):
                assign_short_names([original, other])


if __name__ == "__main__":
    unittest.main()
