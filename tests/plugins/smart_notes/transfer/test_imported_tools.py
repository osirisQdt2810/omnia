"""An imported ``user:`` tool has to be REGISTERED, not merely written to disk.

This is the case the whole mechanism exists for: a chain uses a user-authored tool, the bundle
carries that tool's source, and the import maps the fields onto different names. Writing the
file is not enough — a chain resolves a tool through the registry, and the remap asks the
registry which of a tool's params name fields. A tool that is on disk but unregistered is
invisible to both, so the param silently keeps the OLD field name while ``depends_on`` is
rewritten to the new one: the graph and the tool then disagree, and the tool reads a field
that does not exist.
"""

from __future__ import annotations

from typing import Any

import pytest
from transfer_fakes import (
    FakeCollection,
    FakeToolLoader,
    FakeToolStore,
)

from omnia.plugins.smart_notes.config import (
    FieldToolConfig,
    SmartNotesFieldConfig,
    SmartNotesNoteTypeConfig,
)
from omnia.plugins.smart_notes.engine.tools.base import Tool
from omnia.plugins.smart_notes.transfer.collection import (
    MODE_OVERWRITE,
    SMART_NOTES_KEY,
    apply_bundle,
    build_bundle,
    plan_import,
)

TOOL = "user:my_cloze"
SLUG = "my_cloze"


class DeclaringTool(Tool):
    """A user tool that declares its field param, exactly as a real one does."""

    name = TOOL

    @classmethod
    def referenced_fields(cls, params: Any) -> list[str]:
        value = str(params.get("sentence_field", "") or "").strip()
        return [value] if value else []

    def run(self, request, ctx):  # pragma: no cover - never invoked here
        raise NotImplementedError


@pytest.fixture
def registered_on_load(monkeypatch):
    """Make ``get_tool`` resolve the tool only AFTER the loader has loaded its slug.

    That is the real relationship: ``UserToolStore.write`` puts the file on disk and
    ``UserToolLoader.load`` compiles and registers it — two different objects.
    """
    from omnia.plugins.smart_notes.engine.tools import registry

    loader = FakeToolLoader(FakeToolStore())
    real_get = registry.get_tool

    def _get(name: str):
        if name == TOOL:
            return DeclaringTool if SLUG in loader.loaded else None
        return real_get(name)

    monkeypatch.setattr(registry, "get_tool", _get)
    return loader


def _source_collection(col):
    col.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])
    config = SmartNotesNoteTypeConfig(
        note_type="Vocab",
        base_field="Word",
        fields=[
            SmartNotesFieldConfig(
                field="Meaning",
                enabled=True,
                depends_on=[{"field": "Sentence", "kind": "hard"}],
                tools=[
                    FieldToolConfig(tool=TOOL, params={"sentence_field": "Sentence"})
                ],
            )
        ],
    )
    col.set_config(SMART_NOTES_KEY, {"note_types": [config.dict()]})
    return build_bundle(col, "Vocab", tool_store=FakeToolStore({SLUG: "# the tool"}))


class TestImportingAChainThatUsesACarriedTool:
    """With the tool APPROVED — the ordinary case of moving your own setup between your own
    machines. What happens without approval is TestACarriedToolIsNotRunWithoutConsent.
    """

    def test_the_tool_is_loaded_before_the_remap_reads_it(self, registered_on_load):
        col = FakeCollection()
        bundle = _source_collection(col)
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Term", "Example", "Gloss"])
        renames = {"Word": "Term", "Sentence": "Example", "Meaning": "Gloss"}

        plan = plan_import(
            target,
            bundle,
            mode=MODE_OVERWRITE,
            target_name="Vocab",
            renames=renames,
            tool_loader=registered_on_load,
            approved_tools=[TOOL],
        )
        result = apply_bundle(target, bundle, plan, tool_loader=registered_on_load)

        entry = target.get_config(SMART_NOTES_KEY)["note_types"][0]
        rule = entry["fields"][0]
        # The param names a field. If the tool was not registered before the remap ran, this
        # is still "Sentence" — a field the target note type does not have.
        assert rule["tools"][0]["params"]["sentence_field"] == "Example"
        # ...and it must NOT have been demoted to "check this by hand".
        assert result.remap.unchecked_tool_params == []

    def test_the_graph_edge_and_the_tool_param_agree_afterwards(
        self, registered_on_load
    ):
        """The failure this guards is a config where they disagree — the edge points at the
        new field while the tool still reads the old one."""
        col = FakeCollection()
        bundle = _source_collection(col)
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Term", "Example", "Gloss"])

        plan = plan_import(
            target,
            bundle,
            mode=MODE_OVERWRITE,
            target_name="Vocab",
            renames={"Word": "Term", "Sentence": "Example", "Meaning": "Gloss"},
            tool_loader=registered_on_load,
            approved_tools=[TOOL],
        )
        apply_bundle(target, bundle, plan, tool_loader=registered_on_load)

        rule = target.get_config(SMART_NOTES_KEY)["note_types"][0]["fields"][0]
        edges = [dep["field"] for dep in rule["depends_on"]]
        assert edges == [rule["tools"][0]["params"]["sentence_field"]]

    def test_the_tool_file_is_written_and_the_slug_loaded(self, registered_on_load):
        col = FakeCollection()
        bundle = _source_collection(col)
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])

        plan = plan_import(
            target, bundle, tool_loader=registered_on_load, approved_tools=[TOOL]
        )
        result = apply_bundle(target, bundle, plan, tool_loader=registered_on_load)

        # ``UserToolSource.render`` prepends the tool's metadata header, so the file is
        # the source PLUS that line rather than a byte-for-byte copy.
        assert "# the tool" in registered_on_load.store.files[SLUG]
        assert registered_on_load.loaded == [SLUG]
        assert result.tools_written == [TOOL]

    def test_a_tool_already_installed_here_is_not_rewritten(self, registered_on_load):
        """Someone else's edit to a same-named tool must not be silently replaced."""
        col = FakeCollection()
        bundle = _source_collection(col)
        registered_on_load.store.files[SLUG] = "# MY version"
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])

        plan = plan_import(
            target, bundle, tool_loader=registered_on_load, approved_tools=[TOOL]
        )
        apply_bundle(target, bundle, plan, tool_loader=registered_on_load)

        assert registered_on_load.store.files[SLUG] == "# MY version"

    def test_a_tool_that_will_not_load_is_reported_not_swallowed(self):
        """The import still applies — but the user has to know the chain will not run."""
        from omnia.plugins.smart_notes.engine.tools import registry

        col = FakeCollection()
        bundle = _source_collection(col)
        loader = FakeToolLoader(FakeToolStore(), fail={SLUG})
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])

        plan = plan_import(target, bundle, tool_loader=loader, approved_tools=[TOOL])
        result = apply_bundle(target, bundle, plan, tool_loader=loader)

        assert any(SLUG in failure for failure in result.tools_failed)
        assert registry is not None  # the import completed rather than raising


class TestACarriedToolIsNotRunWithoutConsent:
    """Installing a carried tool EXECUTES its module body. A bundle can come from another
    person — the PR's own use case — so importing one must never run code the reader has not
    looked at. The add-on's stated safety boundary for user tools is the read-and-run review
    (see ``risky_operations``), not the import allowlist, which had to permit ``os`` and
    ``subprocess``."""

    def _bundle_with_tool(self, col):
        return _source_collection(col)

    def test_an_unapproved_tool_is_neither_written_nor_loaded(self):
        col = FakeCollection()
        bundle = self._bundle_with_tool(col)
        loader = FakeToolLoader(FakeToolStore())
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])

        plan = plan_import(target, bundle, tool_loader=loader)
        result = apply_bundle(target, bundle, plan, tool_loader=loader)

        assert plan.tools_to_install == []
        assert plan.unapproved_tools == [TOOL]
        assert loader.store.files == {}  # nothing written
        assert loader.loaded == []  # and nothing executed
        assert result.tools_written == []

    def test_the_user_is_told_why(self):
        col = FakeCollection()
        bundle = self._bundle_with_tool(col)
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])

        plan = plan_import(target, bundle, tool_loader=FakeToolLoader(FakeToolStore()))

        assert any("read them" in warning for warning in plan.warnings)

    def test_an_approved_tool_is_installed(self):
        col = FakeCollection()
        bundle = self._bundle_with_tool(col)
        loader = FakeToolLoader(FakeToolStore())
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])

        plan = plan_import(target, bundle, tool_loader=loader, approved_tools=[TOOL])
        result = apply_bundle(target, bundle, plan, tool_loader=loader)

        assert plan.tools_to_install == [TOOL]
        assert plan.unapproved_tools == []
        assert loader.loaded == [SLUG]
        assert result.tools_written == [TOOL]

    def test_approving_a_tool_the_bundle_does_not_carry_installs_nothing(self):
        """Approval names a tool; it cannot conjure a source that is not in the file."""
        col = FakeCollection()
        bundle = self._bundle_with_tool(col)
        loader = FakeToolLoader(FakeToolStore())
        target = FakeCollection()
        target.models.add_note_type("Vocab", ["Word", "Sentence", "Meaning"])

        plan = plan_import(
            target, bundle, tool_loader=loader, approved_tools=["user:not_in_here"]
        )

        assert plan.tools_to_install == []
        assert plan.unapproved_tools == [TOOL]
