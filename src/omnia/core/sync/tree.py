"""The deck tree the user picks from, and what picking one deck means.

Anki stores decks flat, as ``Parent::Child::Grandchild`` names. Everything a picker needs — who
is whose child, what to indent, what a click selects — is derivable from those names, and doing
it here rather than in the page means the rule is written once and can be tested without a
browser.

Two rules carry the whole thing:

* **A deck is picked with its sub-decks.** "Sync this deck" almost always means the deck and
  everything under it; a picker that made you tick forty children individually is one nobody
  finishes. One click covers the subtree, and so does the click that undoes it.
* **A parent whose children are picked separately is not itself picked.** It renders as partial,
  because showing it as selected would promise to bring a parent that was never chosen.

Pure data — no ``aqt``, no ``anki``, no HTML.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Optional

from omnia.core.sync.inventory import DeckEntry

SEPARATOR = "::"

#: What a deck's checkbox shows.
PICKED = "picked"
PARTIAL = "partial"
UNPICKED = "unpicked"


@dataclass(frozen=True)
class DeckNode:
    """One deck and the decks under it."""

    entry: DeckEntry
    children: tuple[DeckNode, ...] = ()

    @property
    def name(self) -> str:
        """The full ``Parent::Child`` name, as Anki stores it."""
        return self.entry.name

    @property
    def cards(self) -> int:
        """Cards in this deck alone. Sub-decks report their own."""
        return self.entry.cards

    def walk(self) -> Iterator[DeckNode]:
        """This node and every node under it, parents first."""
        yield self
        for child in self.children:
            yield from child.walk()

    @property
    def total_cards(self) -> int:
        """Cards here and in everything below — what picking this deck would bring."""
        return sum(node.cards for node in self.walk())


@dataclass(frozen=True)
class DeckRow:
    """One line of the rendered tree."""

    node: DeckNode
    depth: int
    #: Whether the row gets an arrow. A leaf with no arrow is a leaf, visibly.
    expandable: bool = False

    @property
    def label(self) -> str:
        """What to write on the row.

        The leaf name for anything nested — the indentation already says where it sits, and
        repeating ``Japanese::Kanji::`` on every descendant is noise. A row at the TOP keeps its
        whole name, because an orphan (a deck whose parent is not in the list) surfaces as a root
        and its leaf name alone would say nothing about where it came from.
        """
        if self.depth == 0:
            return self.node.name
        return self.node.name.rsplit(SEPARATOR, 1)[-1]


@dataclass
class DeckTree:
    """The decks of one machine, as a tree."""

    roots: tuple[DeckNode, ...] = ()

    @classmethod
    def from_entries(cls, entries: Iterable[DeckEntry]) -> DeckTree:
        """Build the tree from a flat list of decks.

        Structure comes from the NAMES, never from the order they arrived in, so a source that
        serves its decks unsorted still renders correctly. A deck whose parent is missing from
        the list — a filtered deck's home, a collection mid-edit — becomes a root rather than
        disappearing into a gap.
        """
        by_name = {entry.name: entry for entry in entries if entry.name}
        children: dict[str, list[str]] = {name: [] for name in by_name}
        roots: list[str] = []
        for name in sorted(by_name, key=str.lower):
            parent = name.rsplit(SEPARATOR, 1)[0] if SEPARATOR in name else ""
            if parent and parent in children:
                children[parent].append(name)
            else:
                roots.append(name)

        def build(name: str) -> DeckNode:
            return DeckNode(
                entry=by_name[name],
                children=tuple(build(child) for child in children[name]),
            )

        return cls(roots=tuple(build(name) for name in roots))

    def rows(self) -> list[DeckRow]:
        """Every deck as a row, parents before their children."""
        out: list[DeckRow] = []

        def visit(node: DeckNode, depth: int) -> None:
            out.append(DeckRow(node=node, depth=depth, expandable=bool(node.children)))
            for child in node.children:
                visit(child, depth + 1)

        for root in self.roots:
            visit(root, 0)
        return out

    def node(self, name: str) -> Optional[DeckNode]:
        """The node called ``name``, or None."""
        for root in self.roots:
            for node in root.walk():
                if node.name == name:
                    return node
        return None

    def __len__(self) -> int:
        return sum(1 for _ in self.walk())

    def walk(self) -> Iterator[DeckNode]:
        """Every node, parents first."""
        for root in self.roots:
            yield from root.walk()


class DeckSelection:
    """Which decks are picked, and what a click does to that.

    Args:
        tree: The decks being picked from. Held rather than passed per call because every
            operation needs it — a click is a statement about a SUBTREE, and the subtree is the
            tree's to know.
    """

    def __init__(self, tree: DeckTree, picked: Optional[Iterable[str]] = None) -> None:
        self._tree = tree
        self._picked: set[str] = set(picked or ())

    @property
    def names(self) -> set[str]:
        """Every deck currently picked."""
        return set(self._picked)

    @property
    def cards(self) -> int:
        """How many cards the picked decks hold — what the button is about to bring."""
        return sum(
            node.cards for node in self._tree.walk() if node.name in self._picked
        )

    def apply(self, name: str) -> dict[str, str]:
        """Toggle ``name`` and return the new state of every row that changed because of it.

        The rows that can change are the subtree (all of it flips) and the ancestors above it
        (their partial state is derived from what is below). Returning exactly those is what lets
        the page repaint without owning a second copy of the rule — an earlier version had the
        script work it out, and it painted every expandable sibling partial because it queried
        the set while it was still being mutated.
        """
        self.toggle(name)
        changed: dict[str, str] = {}
        node = self._tree.node(name)
        if node is not None:
            for below in node.walk():
                changed[below.name] = self.state(below)
        else:
            changed[name] = PICKED if name in self._picked else UNPICKED
        for ancestor in self._ancestors(name):
            changed[ancestor.name] = self.state(ancestor)
        return changed

    def _ancestors(self, name: str) -> list[DeckNode]:
        """The decks ``name`` sits under, nearest first."""
        out: list[DeckNode] = []
        at = name
        while SEPARATOR in at:
            at = at.rsplit(SEPARATOR, 1)[0]
            node = self._tree.node(at)
            if node is not None:
                out.append(node)
        return out

    def toggle(self, name: str) -> bool:
        """Pick ``name`` and everything under it, or unpick them. Returns the new state.

        Both directions cover the subtree. Unpicking a deck whose child was picked separately
        clears the child too: the user clicked a deck that was lit and expects it to go dark,
        and a child left behind would light the parent again as partial, which reads as the
        click not having worked.
        """
        subtree = self._subtree(name)
        if name in self._picked:
            self._picked -= subtree
            return False
        self._picked |= subtree
        return True

    def state(self, node: DeckNode) -> str:
        """What ``node``'s row shows: picked, partial, or unpicked.

        Partial is a real third state, not a rounding of the other two: a parent whose children
        were picked one by one is not itself picked, and drawing it as picked would promise to
        bring a parent nobody chose.
        """
        if node.name in self._picked:
            return PICKED
        if any(below.name in self._picked for below in node.walk()):
            return PARTIAL
        return UNPICKED

    def _subtree(self, name: str) -> set[str]:
        node = self._tree.node(name)
        if node is None:
            # A deck the page knows about and the tree does not: act on the name alone rather
            # than ignoring the click.
            return {name}
        return {below.name for below in node.walk()}
