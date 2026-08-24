"""A fake collection for the transfer tests.

The name is deliberate on both counts. NOT ``conftest.py``: several modules here import the
ROOT conftest by name (``from conftest import FakeCard``), and a second ``conftest`` in a
non-package directory goes on ``sys.path`` ahead of it and shadows it for the whole run.
NOT ``fakes.py`` either: ``tests/benchmarks/fakes.py`` already claims that module name, and
pytest puts every non-package test directory on ``sys.path`` — whichever imports first wins
and the other silently gets the wrong module.

``collection.py`` takes the collection and the tool loader as arguments precisely so the same
code runs under the GUI, under a script, and here. This is the "here": enough of Anki's surface
to exercise export, planning and import headlessly, and no more.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest


class FakeModels:
    """``col.models``: note types as Anki's legacy dicts, keyed by name."""

    def __init__(self) -> None:
        self._by_name: dict[str, dict[str, Any]] = {}
        self.added: list[dict[str, Any]] = []
        self._next_id = 1000

    def add_note_type(
        self, name: str, fields: list[str], **extra: Any
    ) -> dict[str, Any]:
        """Test helper: register a note type the way a real collection would hold it."""
        self._next_id += 1
        model = {
            "id": self._next_id,
            "name": name,
            "type": 0,
            "usn": -1,
            "mod": 1_700_000_000,
            "css": ".card { font-size: 20px; }",
            "flds": [{"name": f, "ord": i} for i, f in enumerate(fields)],
            "tmpls": [
                {
                    "name": "Card 1",
                    "ord": 0,
                    "qfmt": "{{" + fields[0] + "}}",
                    "afmt": "",
                }
            ],
        }
        model.update(extra)
        self._by_name[name] = model
        return model

    def by_name(self, name: str) -> dict[str, Any] | None:
        return self._by_name.get(name)

    def add_dict(self, model: dict[str, Any]) -> None:
        # Anki's backend REQUIRES both keys and rejects the dict without them; a fake that
        # accepted anything would have let the missing-'mod' bug through.
        for required in ("id", "usn", "mod", "name", "flds"):
            if required not in model:
                raise ValueError(f"missing field `{required}`")
        # ``added`` keeps the dict AS PASSED, before this fake mints an id the way Anki does —
        # otherwise a test cannot see what the caller actually handed over, which is the only
        # thing under test here.
        self.added.append(copy.deepcopy(model))
        stored = copy.deepcopy(model)
        self._next_id += 1
        stored["id"] = self._next_id
        self._by_name[stored["name"]] = stored

    def all_names(self) -> list[str]:
        return sorted(self._by_name)


class FakeDecks:
    def __init__(self) -> None:
        self._by_name: dict[str, dict[str, Any]] = {}
        self._by_id: dict[int, str] = {}

    def add(self, name: str, deck_id: int) -> None:
        self._by_name[name] = {"id": deck_id, "name": name}
        self._by_id[deck_id] = name

    def by_name(self, name: str) -> dict[str, Any] | None:
        return self._by_name.get(name)

    def name(self, deck_id: int) -> str:
        return self._by_id.get(deck_id, "")


class FakeCollection:
    def __init__(self) -> None:
        self.models = FakeModels()
        self.decks = FakeDecks()
        self._config: dict[str, Any] = {}

    def get_config(self, key: str, default: Any = None) -> Any:
        return copy.deepcopy(self._config.get(key, default))

    def set_config(self, key: str, value: Any) -> None:
        self._config[key] = copy.deepcopy(value)


class FakeToolStore:
    """``UserToolStore``: slug -> source text, in memory."""

    def __init__(self, installed: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(installed or {})

    def slugs(self) -> list[str]:
        return sorted(self.files)

    def read(self, slug: str) -> Any:
        text = self.files.get(slug)
        if text is None:
            return None
        return _Source(slug, text)

    def write(self, source: Any) -> str:
        self.files[source.slug] = source.render()
        return f"<{source.slug}.py>"


class _Source:
    def __init__(self, slug: str, text: str) -> None:
        self.slug = slug
        self._text = text

    def render(self) -> str:
        return self._text


class FakeToolLoader:
    """``UserToolLoader``: owns a store, and records which slugs were LOADED (registered).

    The distinction is the whole point of one of these tests: writing a tool's file does not
    make ``get_tool`` resolve it — only loading does.
    """

    def __init__(
        self, store: FakeToolStore | None = None, fail: set[str] | None = None
    ) -> None:
        self.store = store or FakeToolStore()
        self.loaded: list[str] = []
        self._fail = fail or set()

    def load(self, slug: str) -> Any:
        self.loaded.append(slug)
        if slug in self._fail:
            return _Load(False, f"{slug} would not compile")
        return _Load(True, "")


class _Load:
    def __init__(self, ok: bool, error: str) -> None:
        self.ok = ok
        self.error = error


@pytest.fixture
def col() -> FakeCollection:
    return FakeCollection()


@pytest.fixture
def loader() -> FakeToolLoader:
    return FakeToolLoader()
