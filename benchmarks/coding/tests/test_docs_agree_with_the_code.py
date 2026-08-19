"""Documentation that states a fact the repository can check.

`check_doc_links.py` verifies that a link resolves. It cannot verify that a sentence is true,
and the audit's docs findings are almost all of the second kind: `reference.html §6` asserted the
opposite of `configs/grading-env.yaml`, `§2` contradicted `case.schema.json`, `§5` listed 7 of 13
calibrations. These are the claims that can be checked against their source, so they are.

Every test here fails at `982a9c4`.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar

import pytest
import yaml

from aibench import secrets_scan
from aibench.io_util import repo_root
from aibench.tiers import TIER_SPECS

ROOT = repo_root()
SRC = ROOT / "docs/html/_src"
CALIBRATIONS = ROOT / "benchmarks/ai_coding/calibrations"


def _reference() -> str:
    return (SRC / "reference.html").read_text(encoding="utf-8")


class TestTheGradingEnvironmentIsDescribedAsItIs:
    """RP-21. `reference.html §6` — the page `README.md` bills as the authority on 数据格式 and
    门禁规则, and the only page with a 复现环境说明 section — said the grading environment has no
    numpy and filters such cases. `configs/grading-env.yaml` and `grading_env.py` say the
    opposite. A reader concludes the `grading` extra is unnecessary and walks straight into
    RP-09."""

    def test_the_page_names_every_package_the_manifest_promises(self):
        """All of them, not a sample. Checking four of the fourteen is how the page went eight
        names short — `httpx`, `jsonschema`, `openpyxl`, `pymysql`, `pytest`, `rich`,
        `sqlalchemy` and `yaml` are promised by the manifest and were absent from the page,
        with the sentence still reading as if the list were complete."""
        manifest = yaml.safe_load((ROOT / "configs/grading-env.yaml").read_text(encoding="utf-8"))
        promised = set(manifest["python"])
        body = _reference()
        section = body[body.index('id="repro-env"') :]
        missing = sorted(p for p in promised if p not in section)
        assert not missing, (
            f"promised by configs/grading-env.yaml but not named on the page: {missing}"
        )

    def test_the_page_does_not_understate_the_count(self):
        manifest = yaml.safe_load((ROOT / "configs/grading-env.yaml").read_text(encoding="utf-8"))
        body = _reference()
        section = body[body.index('id="repro-env"') :]
        assert f"{len(manifest['python'])} 个第三方包" in section, (
            "the page states a count; it must be the manifest's count"
        )

    def test_the_page_asserts_availability_rather_than_absence(self):
        body = _reference()
        section = body[body.index('id="repro-env"') :]
        assert "评分环境提供" in section
        # The old sentence survives only as history, and must be labelled as such.
        if "未安装 numpy" in section:
            assert "此前" in section, "the old claim is repeated without saying it was wrong"


class TestTheCaseFormatPageMatchesTheSchema:
    """RP-42. §2 contradicted `case.schema.json` on `grader.mode` and on the file roles — and
    the role it omitted was `distractor`, which is the entire mechanism of the T4 tier."""

    def _schema(self) -> dict:
        return json.loads(
            (ROOT / "benchmarks/ai_coding/schemas/case.schema.json").read_text(encoding="utf-8")
        )

    def test_every_grader_mode_is_named(self):
        modes = self._schema()["properties"]["grader"]["properties"]["mode"]["enum"]
        body = _reference()
        for mode in modes:
            assert f"<code>{mode}</code>" in body, f"grader.mode {mode!r} is unlisted"

    def test_every_file_role_is_named(self):
        files = self._schema()["properties"]["context"]["properties"]["files"]
        roles = files["items"]["properties"]["role"]["enum"]
        body = _reference()
        for role in roles:
            assert f"<dt>{role}</dt>" in body, f"context.files[].role {role!r} is unlisted"


class TestEveryPublishedCalibrationIsListed:
    """RP-42's other half: §5 listed 7 of 13, and the missing ones included the file behind the
    site's own headline number."""

    def test_the_page_lists_them_all(self):
        body = _reference()
        published = sorted(p.name for p in CALIBRATIONS.glob("*.json"))
        assert len(published) >= 10
        missing = [name for name in published if name not in body]
        assert not missing, f"reference.html §5 omits {missing}"


class TestTheDocumentedNodeFloorIsTheRealOne:
    """RP-10's documentation half. The page said "≥ 22"; the real floor is 22.18, and below it
    `node --test` exits 0 having discovered nothing — which grades as a pass."""

    def test_the_manual_states_the_floor_the_code_enforces(self):
        from aibench.languages import MIN_NODE_VERSION

        floor = ".".join(str(n) for n in MIN_NODE_VERSION)
        body = (SRC / "manual.html").read_text(encoding="utf-8")
        assert floor in body, f"the manual does not state the {floor} floor"


class TestThePublishedRecomputeRecipeMatchesItsFile:
    """RP-46 and RP-47. The calibrations README paired 105-case band figures with the 126-case
    mean, and two of its per-anchor rates disagreed with the file they cite."""

    def _readme(self) -> str:
        return (CALIBRATIONS / "README.md").read_text(encoding="utf-8")

    def test_the_anchor_rates_match_the_file(self):
        stored = json.loads(
            (CALIBRATIONS / "reverse-v1-hidden1_3anchor_20260808.json").read_text(encoding="utf-8")
        )
        rates: dict[str, list[float]] = {}
        for case in stored["cases"]:
            for anchor, rate in (case.get("by_anchor") or {}).items():
                rates.setdefault(anchor, []).append(rate)
        body = self._readme()
        for anchor in ("weak-single-turn", "strong-tool-loop"):
            values = rates[anchor]
            mean = sum(values) / len(values) * 100
            assert f"{mean:.1f}%" in body, f"{anchor} mean is {mean:.1f}%, not what the README says"

    def test_the_auto_v0_row_states_its_own_denominator(self):
        body = self._readme()
        # The two populations, both named, so neither can be read as the other.
        assert "105" in body and "126" in body
        assert "62.7 / 13.5 / 23.8" in body, "the 126-case figures are not stated anywhere"

    def test_the_pre_v3_fingerprint_files_are_called_out(self):
        """RP-54. Three files carry path-only fingerprints, so their numbers are recomputable
        but their content is not verifiable."""
        body = self._readme()
        assert "v3 之前的口径" in body
        for name in ("auto-v0_3anchor", "disc-v0_2anchor_partial", "retrieval-v0_3anchor"):
            assert name in body


class TestTheReadmeSendsPeopleSomewhereReal:
    """The README is the module entry: usage first, parameters in REFERENCE.md."""

    def _readme(self) -> str:
        return (ROOT / "README.md").read_text(encoding="utf-8")

    def test_it_points_at_the_user_guide_and_reference(self):
        body = self._readme()
        assert "docs/USER_GUIDE.md" in body
        assert "docs/REFERENCE.md" in body

    def test_no_parameter_question_is_routed_to_the_html_site(self):
        body = self._readme()
        for line in body.splitlines():
            if "docs/html/reference.html" in line and "参考手册" in line:
                pytest.fail(f"routes a parameter question at the HTML site: {line.strip()}")

    def test_the_licence_is_discoverable(self):
        assert "LicenseRef-Proprietary" in self._readme()

    def test_there_is_an_english_entry_point(self):
        body = self._readme()
        assert "## English" in body
        english = body.split("## English", 1)[1]
        assert "trace" in english.lower()
        assert "ablation" in english.lower()


class TestTheReferenceManualCoversEverySubcommand:
    """RP-43. `docs/REFERENCE.md` is billed as the parameter-level authority and omitted a whole
    subcommand, five script flags, and 25 of the 40 config files."""

    def _reference_md(self) -> str:
        return (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")

    def test_every_cli_subcommand_appears(self):
        cli = (ROOT / "src/aibench/cli.py").read_text(encoding="utf-8")
        names = set(re.findall(r'sub\.add_parser\(\s*\n?\s*"([a-z-]+)"', cli))
        assert len(names) > 10, "the parser shape changed; update this test"
        body = self._reference_md()
        missing = sorted(n for n in names if f"`{n}`" not in body)
        assert not missing, f"REFERENCE.md never documents: {missing}"

    def test_every_config_file_appears(self):
        body = self._reference_md()
        configs = sorted(
            f"{sub}/{p.name}"
            for sub in ("agents", "models", "runs")
            for p in (ROOT / "configs" / sub).glob("*.yaml")
        )
        missing = [c for c in configs if c not in body]
        assert not missing, f"REFERENCE.md §7.1 omits {len(missing)}: {missing[:5]}"

    def test_every_e2e_flag_appears(self):
        script = (ROOT / "scripts/e2e_pipeline.sh").read_text(encoding="utf-8")
        flags = set(re.findall(r"^\s{4}(--[a-z-]+)\)", script, re.M))
        assert len(flags) > 8, "the option parser shape changed; update this test"
        body = self._reference_md()
        missing = sorted(f for f in flags if f"`{f}`" not in body)
        assert not missing, f"REFERENCE.md §9.2 omits {missing}"


class TestTheOverviewStatesTheGoal:
    def test_the_overview_states_the_goal(self):
        body = (SRC / "overview.html").read_text(encoding="utf-8")
        head = body[:4000]
        assert "真实" in head
        assert "区分" in head


class TestTheShellTemplateDoesNotShipTheWarnedAgainstExample:
    """RP-53. `configs/agents/shell.yaml` carried an `opencode run ...` example that
    `REFERENCE.md §12.3` warns against — the worst place to put a warned-against invocation."""

    def test_no_command_template_example_names_opencode(self):
        body = (ROOT / "configs/agents/shell.yaml").read_text(encoding="utf-8")
        examples = [
            line for line in body.splitlines() if "command_template" in line and "opencode" in line
        ]
        assert not examples, f"still offers opencode as a template: {examples}"
        assert "opencode.yaml" in body, "it should say what to use instead"


class TestRunCitationsAreMarkedAsLocal:
    """RP-22's gate half. Sixteen tracked citations name `runs/*_<timestamp>/` directories that
    a clone never has, and `check_doc_links.py` could not see them: they are code spans rather
    than links, and `runs` is in its `SKIP_DIRS`."""

    def test_the_checker_flags_an_unmarked_citation(self, tmp_path, monkeypatch):
        import importlib.util
        import sys

        spec = importlib.util.spec_from_file_location(
            "_links_probe", ROOT / "scripts/check_doc_links.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        try:
            spec.loader.exec_module(module)
            doc = tmp_path / "d.md"
            doc.write_text("see `runs/ablation_20260814_111227/report.md`\n", encoding="utf-8")
            assert module.check(doc), "an unmarked run citation must be reported"

            doc.write_text(
                "本地产物，未随仓库分发\n\nsee `runs/ablation_20260814_111227/report.md`\n",
                encoding="utf-8",
            )
            assert module.check(doc) == []
        finally:
            sys.modules.pop(spec.name, None)


class TestThePagesQuoteTheRuntimeFloorsTheCodeEnforces:
    """D-04 and D-07's other half. `reference.html` said Node "< 22" while `languages.py` has
    enforced 22.18 since the version gate landed, and the same page's package list was eight
    names short. A documented floor below the real one is the dangerous direction: between the
    two, `node --test` discovers no TypeScript test file and exits 0, which is a pass on the
    defective stub."""

    def test_the_node_floor_on_the_page_is_the_floor_in_the_code(self):
        from aibench.languages import MIN_NODE_VERSION

        floor = ".".join(str(n) for n in MIN_NODE_VERSION)
        section = (body := _reference())[body.index('id="repro-env"') :]
        assert floor in section, f"the page never states the real floor {floor}"


class TestTheManifestFieldTableIsTheManifest:
    """RP-58's documentation half. The code stopped writing `python_executable` and
    `working_directory` — the two fields that carried a home directory — and §16.4 went on
    documenting them, privacy warning included, for fields no manifest carries. Nothing checked
    the table against the thing it describes."""

    def _section(self) -> str:
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        start = body.index("### 16.4")
        return body[start : body.index("### 16.5", start)]

    def test_every_recorded_field_is_documented(self):
        from aibench.provenance import environment

        section = self._section()
        missing = sorted(k for k in environment() if f"`{k}`" not in section)
        assert not missing, f"written into every manifest but undocumented: {missing}"

    def test_no_retired_field_is_still_described(self):
        from aibench.provenance import environment

        section = self._section()
        recorded = set(environment())
        stale = [f for f in ("python_executable", "working_directory") if f in recorded]
        assert not stale, (
            "these were retired for carrying a home directory; the code brought them back"
        )
        for field in ("python_executable", "working_directory"):
            if f"`{field}`" in section:
                assert "2026-08-17 起" in section or "此前" in section, (
                    f"§16.4 describes `{field}` as current; no manifest carries it"
                )


class TestTheConfigCountIsCounted:
    """RP-43/RP-55. §7.1 claims to list every file under `configs/` and stated 39; there are 40.
    The omission was `configs/grading-env.yaml` — the manifest that decides which drafts survive
    the import gate — and the pinning test globs only `agents/models/runs`, so it structurally
    could not catch it."""

    def test_the_stated_count_matches_the_directory(self):
        import re

        actual = len(list((ROOT / "configs").rglob("*.yaml")))
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        stated = re.search(r"`configs/` 下\*\*全部 (\d+) 个 YAML\*\*", body)
        assert stated, "§7.1 no longer states a count in the form this test reads"
        assert int(stated.group(1)) == actual


class TestTheGraderModeMixIsNotOverstated:
    """RP-42's other half. `reference.html` said the mainline "only produces `script`" while
    4,994 of the 13,307 cases on disk are `gold` — including the committed `seed-v0/case_003`,
    which is in the repository and contradicts the sentence without leaving the checkout."""

    def test_the_page_states_the_measured_mix(self):
        """Checks the page states *a* gold count, not that the count is right — the corpus it
        was measured on is gitignored, so a clone cannot re-derive it. What a clone *can* check
        is the counterexample below, which is committed."""
        page = _reference()
        assert "<code>gold</code> <strong>4,994</strong>" in page, (
            "the page must state the gold count it used to deny"
        )
        # The old sentence may survive as history, but only if it is labelled as history.
        if "主线只产出" in page:
            assert "此前" in page, "the retracted claim is repeated without saying it was wrong"

    def test_a_committed_fixture_is_gold(self):
        from aibench.cases import load_cases

        modes = {c.case_id: c.grader.mode for c in load_cases("seed-v0", validate=False)}
        assert "gold" in modes.values(), (
            f"the counterexample this test is built on is gone: {modes}"
        )


class TestEveryGateTheCodeEmitsIsInTheGateTable:
    """§14.2 presents itself as the complete gate list and was missing four codes — including
    one that two other documents described as "未修" while it had been implemented, tested and
    armed at error severity. Nothing compared the table to the code that emits the codes."""

    def _emitted(self) -> set[str]:
        source = (ROOT / "src/aibench/validity.py").read_text(encoding="utf-8")
        codes = set(re.findall(r'ValidityIssue\(\s*\n?\s*"([a-z_0-9]+)"', source))
        codes |= set(re.findall(r'ValidityIssue\(f?"([a-z_0-9]+)', source))
        return codes

    def test_the_table_names_them_all(self):
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        missing = sorted(c for c in self._emitted() if c not in body)
        assert not missing, f"emitted by validity.py but absent from REFERENCE.md: {missing}"

    def test_the_scan_finds_a_realistic_number(self):
        """Guard on the guard: a regex that matched nothing would make the check above vacuous."""
        assert len(self._emitted()) >= 15


class TestTheAblationSectionIsTheAblationMatrix:
    """§17.2 described a four-row matrix as two rows and said the others were commented out."""

    def test_every_matrix_row_is_named(self):
        import yaml

        matrix = yaml.safe_load(
            (ROOT / "configs/runs/ablation-matrix.yaml").read_text(encoding="utf-8")
        )
        names = [r["experiment_name"] for r in matrix["runs"]]
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        section = body[body.index("### 17.2") : body.index("### 17.3")]
        missing = [n for n in names if n not in section]
        assert not missing, f"active matrix rows absent from §17.2: {missing}"


class TestTheMainlineFlagIsInEveryRecipeAndTheTable:
    """`--reverse` is the mainline, and it was in none of the hand-written `generate-cases`
    recipes and in no parameter table — while the same documents declare the forward path dead.
    A newcomer following them spends model budget on the abandoned route."""

    RECIPE_DOCS = ("README.md", "docs/USER_GUIDE.md", "docs/REFERENCE.md")

    @pytest.mark.parametrize("relative", RECIPE_DOCS)
    def test_no_recipe_invokes_the_abandoned_generator(self, relative: str):
        body = (ROOT / relative).read_text(encoding="utf-8")
        recipes = re.findall(r"aibench generate-cases[^`]*?(?=\n\n|\n```)", body)
        assert recipes, f"{relative}: no generate-cases recipe found; the check would be vacuous"
        checked = 0
        for recipe in recipes:
            # `generate-cases ... --workers 4` is an elision illustrating one flag, not a recipe
            # anyone runs; and `--heuristic-only` deliberately calls no model at all.
            if "..." in recipe or "--heuristic-only" in recipe:
                continue
            checked += 1
            assert "--reverse" in recipe, f"{relative}: recipe runs the forward path:\n{recipe}"
        assert checked, f"{relative}: every generate-cases block was elided; nothing was checked"

    def test_the_parameter_table_documents_the_mainline_flags(self):
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        section = body[body.index("### 8.7") : body.index("### 8.8")]
        for flag in ("--reverse", "--resume", "--no-deduplicate"):
            assert f"`{flag}`" in section, f"§8.7 does not document {flag}"


class TestEveryExportGateSaysWhetherItHasAnEscape:
    """Three documents stated the export gates could not be bypassed, while `cli.py` grew two
    new bypass flags on this branch and had a third all along."""

    def test_every_allow_flag_is_documented(self):
        cli = (ROOT / "src/aibench/cli.py").read_text(encoding="utf-8")
        block = cli[
            cli.index('"export-bundle"') : cli.index('"ablation"', cli.index('"export-bundle"'))
        ]
        flags = set(re.findall(r'"(--allow-[a-z-]+|--no-require-audit|--max-verbatim)"', block))
        assert flags, "no export-bundle flags found; the check would be vacuous"
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        missing = sorted(f for f in flags if f"`{f}`" not in body)
        assert not missing, f"export-bundle flags absent from REFERENCE.md: {missing}"

    def test_no_document_claims_the_secrets_gate_has_no_escape(self):
        for relative in ("docs/REFERENCE.md", "docs/html/_src/manual.html"):
            body = (ROOT / relative).read_text(encoding="utf-8")
            assert "无豁免开关" not in body, f"{relative} still says the gate has no escape"


class TestTheSecretsExclusionListIsTheOneInTheCode:
    """§8.15 said the scanner has 「两处例外」 and named `grader.key_lines` as one of them.
    `_HARNESS_RECORDS` holds one entry, and the comment above it says in so many words that
    `key_lines` is deliberately not in the set — `grading.py` decides pass/fail from it, so it
    is case content. A reader who trusts the manual believes a scanned field is exempt."""

    #: 一/二/三 rather than a formatted number: the section is prose, and the digit forms do not
    #: appear in it.
    COUNT_WORDS: ClassVar[dict[int, str]] = {1: "一处例外", 2: "两处例外", 3: "三处例外"}

    def _section(self) -> str:
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        return body[body.index("### 8.15") : body.index("### 8.16")]

    def test_the_section_states_the_count_the_code_holds(self):
        excluded = secrets_scan._HARNESS_RECORDS
        assert excluded, "_HARNESS_RECORDS is empty; the check below would be vacuous"
        assert len(excluded) in self.COUNT_WORDS, (
            f"_HARNESS_RECORDS now holds {len(excluded)} paths; extend COUNT_WORDS"
        )
        section = self._section()
        expected = self.COUNT_WORDS[len(excluded)]
        assert expected in section, (
            f"_HARNESS_RECORDS holds {len(excluded)} path(s); §8.15 must say {expected!r}"
        )
        wrong = [w for n, w in self.COUNT_WORDS.items() if n != len(excluded) and w in section]
        assert not wrong, f"§8.15 also still says {wrong}, which contradicts the code"

    def test_the_sentence_naming_the_exclusions_names_only_real_ones(self):
        excluded = {p.replace(":", ".") for p in secrets_scan._HARNESS_RECORDS}
        section = self._section()
        start = section.index("例外")
        sentence = section[start : section.index("。", start) + 1]
        named = set(re.findall(r"`((?:grader|metadata)\.[a-z_]+)`", sentence))
        # Guard on the guard, on the variable actually asserted: a rephrased sentence that
        # names nothing would make the comparison below trivially true.
        assert named, f"no field path found in the sentence that names the exclusions: {sentence!r}"
        assert named <= excluded, (
            f"§8.15 presents {sorted(named - excluded)} as excluded, but the code scans them; "
            f"_HARNESS_RECORDS holds {sorted(excluded)}"
        )

    def test_the_section_says_key_lines_is_scanned_while_the_code_scans_it(self):
        """The half a bare count cannot express. The old text's damage was not the number, it
        was naming a scanned field as exempt."""
        if "grader.key_lines" in {p.replace(":", ".") for p in secrets_scan._HARNESS_RECORDS}:
            return  # the code changed; the section may legitimately say the opposite
        section = self._section()
        assert "`grader.key_lines` **不在**例外里" in section, (
            "§8.15 must say that `grader.key_lines` is scanned, not merely omit it"
        )


class TestTheHiddenSymbolBreakdownAddsUp:
    """§14.3.8 split 8 harder cases into 「7 条挂在断言上」 plus 「另有 2 条」. Seven and two are
    nine. `check_hidden_tests_are_inferable`'s docstring says seven and one."""

    #: The docstring writes its counts as English words.
    WORDS: ClassVar[dict[str, int]] = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }

    def _section(self) -> str:
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        return body[body.index("#### 14.3.8") : body.index("#### 14.3.9")]

    def _stated(self) -> dict[str, int]:
        section = self._section()
        found = {}
        for name, pattern in (
            ("total", r"(\d+) 条变难的用例里"),
            ("assertions", r"(\d+) 条挂在断言上"),
            ("unknowable", r"另有 (\d+) 条挂在"),
        ):
            match = re.search(pattern, section)
            # Guard on the guard: a rephrased sentence must fail here with a readable message,
            # not raise AttributeError on `None.group` and read as a broken test.
            assert match, f"§14.3.8 no longer states {name}; pattern {pattern!r} matched nothing"
            found[name] = int(match.group(1))
        return found

    def test_the_two_addends_equal_the_total(self):
        stated = self._stated()
        assert stated["assertions"] + stated["unknowable"] == stated["total"], (
            f"§14.3.8 splits {stated['total']} into {stated['assertions']} + {stated['unknowable']}"
        )

    def test_the_split_is_the_docstring_s_split(self):
        source = (ROOT / "src/aibench/validity.py").read_text(encoding="utf-8")
        start = source.index("def check_hidden_tests_are_inferable")
        doc = source[start : start + 2000]
        pair = re.search(r"(\w+) failed on assertions .*? and (\w+) failed with", doc, re.S)
        assert pair, "the docstring no longer states the assertions/unknowable split"
        words = [pair.group(1).lower(), pair.group(2).lower()]
        unknown = [w for w in words if w not in self.WORDS]
        assert not unknown, f"docstring counts {unknown} are not words this test can read"
        stated = self._stated()
        assert (stated["assertions"], stated["unknowable"]) == (
            self.WORDS[words[0]],
            self.WORDS[words[1]],
        ), (
            f"§14.3.8 says {stated['assertions']}/{stated['unknowable']}; the docstring says "
            f"{self.WORDS[words[0]]}/{self.WORDS[words[1]]}"
        )


class TestTheTierTableCarriesEveryStructuralInvariant:
    """§13.5.2's column is headed 「结构不变量……逐条校验」. The T5 row omitted
    `min_solution_files=2` — the invariant that makes T5 a cross-file tier at all — and the T3
    row inherited T2's file ceiling of 4 while the code raises it to 6."""

    #: Each row is written as an increment on the row it names, so a field the child changes
    #: has to be restated or the reader inherits the wrong value.
    PARENT: ClassVar[dict[str, str]] = {"T3": "T2", "T4": "T3", "T5": "T4"}

    def _rows(self) -> dict[str, str]:
        """The invariants cell only. Matching the whole row let `A6` in the 能力轴 column stand
        in for a file ceiling of 6, so this check passed on the very text it was written for."""
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        section = body[body.index("### 13.5.2") : body.index("### 13.5.3")]
        rows = {}
        for line in re.findall(r"^\| T\d \|.*$", section, re.M):
            cells = [c.strip() for c in line.split("|")]
            assert len(cells) >= 6, f"§13.5.2 row is not a four-column row: {line}"
            rows[cells[1]] = cells[3]
        assert len(rows) == len(TIER_SPECS), (
            f"§13.5.2 has {len(rows)} tier rows for {len(TIER_SPECS)} specs"
        )
        return rows

    def test_every_tier_that_needs_two_solution_files_says_so(self):
        rows = self._rows()
        checked = 0
        for tier, spec in TIER_SPECS.items():
            if spec.min_solution_files >= 2:
                checked += 1
                assert f"≥{spec.min_solution_files} 个文件" in rows[tier], (
                    f"{tier} requires {spec.min_solution_files} solution files; "
                    f"its invariants cell does not say so: {rows[tier]}"
                )
        assert checked, "no tier requires two solution files; the check would be vacuous"

    def test_every_tier_that_raises_the_file_ceiling_says_so(self):
        rows = self._rows()
        checked = 0
        for tier, parent in self.PARENT.items():
            spec, parent_spec = TIER_SPECS[tier], TIER_SPECS[parent]
            if spec.max_files is None or spec.max_files == parent_spec.max_files:
                continue
            checked += 1
            assert str(spec.max_files) in rows[tier], (
                f"{tier} raises the file ceiling from {parent_spec.max_files} to "
                f"{spec.max_files}; its invariants cell inherits {parent}'s without restating "
                f"it: {rows[tier]}"
            )
        assert checked, "no tier raises a finite file ceiling; the check would be vacuous"

    def test_every_tier_row_states_its_hidden_test_floor(self):
        rows = self._rows()
        for tier, spec in TIER_SPECS.items():
            if not spec.min_hidden_test_fns:
                continue
            # Rows write this as 「≥2 个隐藏测试函数」 or 「≥3 隐藏测试」; both are the floor.
            assert re.search(rf"≥\s*{spec.min_hidden_test_fns}\s*个?隐藏测试", rows[tier]), (
                f"{tier} needs {spec.min_hidden_test_fns} hidden test functions; "
                f"its invariants cell does not state that floor: {rows[tier]}"
            )


class TestTheDocumentedFlagDefaultsAreArgparseDefaults:
    """§8.8 gave `ablation --parallel` a default of `1`. It is `None`, and the difference is the
    whole point of the change that made it `None`: with `1` the CLI short-circuited the matrix's
    own `parallel:` key, and eleven of twelve shipped matrices ran serially."""

    #: The workaround the defect required. Once the defect is fixed, a document still telling a
    #: reader to apply the workaround documents behaviour that no longer exists.
    STALE_ADVICE = "短路成串行"

    def test_ablation_parallel_still_defaults_to_none(self):
        cli = (ROOT / "src/aibench/cli.py").read_text(encoding="utf-8")
        start = cli.index('sub.add_parser("ablation"')
        block = cli[start : cli.index("sub.add_parser(", start + 10)]
        assert '"--parallel"' in block, "the ablation parser no longer declares --parallel"
        declaration = block[block.index('"--parallel"') :][:200]
        assert "default=None" in declaration, (
            "ablation --parallel no longer defaults to None; §8.8 and USER_GUIDE §3.4 both "
            "describe the None behaviour and need rechecking"
        )

    def test_the_default_column_does_not_claim_a_value_argparse_does_not_set(self):
        body = (ROOT / "docs/REFERENCE.md").read_text(encoding="utf-8")
        section = body[body.index("### 8.8") : body.index("### 8.9")]
        rows = [ln for ln in section.splitlines() if ln.startswith("| `--parallel`")]
        assert len(rows) == 1, f"expected one --parallel row in §8.8, found {len(rows)}"
        cells = [c.strip() for c in rows[0].split("|")]
        assert len(cells) >= 5, f"§8.8's --parallel row is not a four-column row: {rows[0]}"
        default = cells[3]
        assert default != "`1`", (
            "§8.8 states `1` as the default for ablation --parallel, which is the value that "
            f"short-circuited the matrix key: {rows[0]}"
        )
        assert "`parallel:`" in default or "parallel:" in default, (
            f"§8.8's default cell must say the effective default comes from the matrix: {default!r}"
        )

    def test_no_document_still_prescribes_the_workaround(self):
        offenders = []
        for path in ROOT.rglob("*"):
            if path.suffix.lower() not in {".md", ".html"} or not path.is_file():
                continue
            if any(part in {".venv", "runs", ".agent", ".git"} for part in path.parts):
                continue
            if self.STALE_ADVICE in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(ROOT)))
        assert not offenders, (
            f"these documents still tell the reader to work around the fixed short-circuit: "
            f"{offenders}"
        )


class TestTheRankingReversalIsRecomputableFromOneCorpus:
    """The reversal sentence paired bare-model rates from `bare-model-two-models` with
    tool-loop rates lifted from the single-run five-model ablation — the batch the same file
    voids one section later. Both halves now come from the three-repeat pair, and the same
    wrong pair had a third home in `manual.html`'s 胜者 column."""

    #: Every document that states the reversal. Correcting two of three is how a wrong number
    #: gets re-imported from the copy nobody edited.
    SITES = (
        "benchmarks/ai_coding/calibrations/README.md",
        "docs/html/_src/overview.html",
        "docs/html/overview.html",
    )

    def _bare_totals(self) -> dict[str, tuple[int, int]]:
        stored = json.loads(
            (CALIBRATIONS / "bare-model-two-models_20260810.json").read_text(encoding="utf-8")
        )
        totals: dict[str, list[int]] = {}
        for row in stored["rows"]:
            seen = totals.setdefault(row["model"], [0, 0])
            seen[0] += 1 if row["passed"] else 0
            seen[1] += 1
        return {k: (v[0], v[1]) for k, v in totals.items()}

    def _tool_loop_totals(self) -> dict[str, tuple[int, int]]:
        stored = json.loads(
            (CALIBRATIONS / "ablation-two-models-3reps_20260810.json").read_text(encoding="utf-8")
        )
        totals: dict[str, list[int]] = {}
        for run in stored["runs"]:
            seen = totals.setdefault(run["algorithm_name"], [0, 0])
            seen[0] += run["success_count"]
            seen[1] += run["effective_case_count"]
        return {k: (v[0], v[1]) for k, v in totals.items()}

    @staticmethod
    def _short(model: str) -> str:
        """`Deepseek-V4-Flash-0731` in the JSON, `Deepseek` in the prose."""
        return model.split("-V4")[0] if model.startswith("Deepseek") else model

    def _assert_winner_stated(self, text: str, totals: dict[str, tuple[int, int]], where: str):
        """The winner's own count and rate, on the winning side. A test that only looks for the
        two fractions anywhere in the file passes on a sentence that swaps the models — which
        is precisely the defect, since the claim is about ordering."""
        ranked = sorted(totals.items(), key=lambda kv: kv[1][0] / kv[1][1], reverse=True)
        (winner, (wp, wn)), (loser, (lp, ln)) = ranked[0], ranked[1]
        for model, passed, n in ((winner, wp, wn), (loser, lp, ln)):
            assert f"{passed}/{n}" in text, (
                f"{where}: {model} is {passed}/{n}; the sentence does not state that"
            )
            assert f"{passed / n * 100:.1f}%" in text, (
                f"{where}: {model} is {passed / n * 100:.1f}%; the sentence does not state that"
            )
        head = text[: text.index(f"{lp}/{ln}")]
        assert f"{wp}/{wn}" in head, (
            f"{where}: {self._short(winner)} ({wp}/{wn}) outscores {self._short(loser)} "
            f"({lp}/{ln}), but the sentence puts the loser first"
        )

    @pytest.mark.parametrize("relative", SITES)
    def test_each_site_states_both_halves_with_the_winner_first(self, relative):
        body = (ROOT / relative).read_text(encoding="utf-8")
        marker = "排序反转" if "排序反转" in body else "颠倒了排序"
        assert marker in body, f"{relative} no longer states the reversal"
        claim = body[body.index(marker) :]
        # Stop before the correction note, which quotes the old wrong pair on purpose.
        for stop in ("（此前", "<p>三轮合并"):
            if stop in claim:
                claim = claim[: claim.index(stop)]
        assert len(claim) > 40, f"{relative}: the reversal claim sliced down to {claim!r}"
        self._assert_winner_stated(claim, self._bare_totals(), f"{relative} (bare)")
        self._assert_winner_stated(claim, self._tool_loop_totals(), f"{relative} (tool_loop)")

    def test_the_voided_single_run_rate_is_not_part_of_any_claim(self):
        for relative in self.SITES:
            body = (ROOT / relative).read_text(encoding="utf-8")
            marker = "排序反转" if "排序反转" in body else "颠倒了排序"
            claim = body[body.index(marker) :]
            for stop in ("（此前", "<p>三轮合并"):
                if stop in claim:
                    claim = claim[: claim.index(stop)]
            assert len(claim) > 40, f"{relative}: the reversal claim sliced down to {claim!r}"
            assert "71.0" not in claim, (
                f"{relative}: the reversal claim still quotes the single-run five-model rate, "
                f"which calibrations/README.md voids in 「单次运行的消融结论一律不可用」"
            )

    def test_no_page_labels_the_tool_loop_winner_with_the_single_run_rate(self):
        """`manual.html`'s 胜者 column sat in a table whose caption says 各 3 次重复 and whose
        other three columns come from the three-repeat file."""
        totals = self._tool_loop_totals()
        passed, n = totals["Deepseek-V4-Flash"]
        rate = f"{passed / n * 100:.1f}%"
        lines = (SRC / "manual.html").read_text(encoding="utf-8").splitlines()
        # The 胜者 table specifically. `<code>tool_loop</code>` also appears in the anchor-panel
        # table above it, and matching that one made this check pass on the wrong row.
        heads = [i for i, ln in enumerate(lines) if "<th>胜者</th>" in ln]
        assert len(heads) == 1, f"expected one 胜者 table in manual.html, found {len(heads)}"
        rows = [ln for ln in lines[heads[0] : heads[0] + 10] if "<code>tool_loop</code>" in ln]
        assert len(rows) == 1, f"expected one tool_loop row under the 胜者 header, found {rows}"
        assert rate in rows[0], (
            f"tool_loop's winner rate over three repeats is {rate} ({passed}/{n}); "
            f"the row says: {rows[0].strip()}"
        )
