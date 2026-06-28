"""Per-deck auto-flip options: a small dialog + the deck-list gear-menu glue.

The deck browser's gear (options) menu gets an "Omnia: Auto-Flip…" action that opens
:class:`AutoFlipDeckDialog` for that deck. The dialog edits one deck's override (the two-flag
``use_global`` / ``enabled`` gate plus question/answer delays); on accept the glue merges it
into ``auto_flip.per_deck``, persists through the
:class:`~omnia.core.config.repository.ConfigRepository`, and reloads the plugin so the new
delays take effect.

Native Deck-Options tab — deferred. The reference add-on injects its options into Anki's
native Deck Options screen via the ``deck_options_did_load`` hook (``option.js`` /
``option.html``), persisting into the deck config's ``auxData``. That screen is the Svelte
``$deckOptions`` bundle whose ``addHtmlAddon`` / ``auxData`` surface is undocumented and
shifts between Anki releases (it changed shape in the 23.10 → 25.09 line), so wiring it
cleanly is version-fragile. Correctness + clean teardown win over the exact surface here, so
this gear-menu dialog is the per-deck surface; the native tab can be revisited once that API
stabilises.

This module subclasses ``QDialog`` and so imports ``aqt.qt`` at the top — it is therefore
imported lazily by the auto_flip feature (only inside Anki), never at headless load time.
The heavier Qt widget classes are still imported inside methods to keep the module body
light.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aqt.qt import QDialog

if TYPE_CHECKING:
    from omnia.core.config.models import AutoFlipSettings
    from omnia.core.plugin import PluginContext


class AutoFlipDeckDialog(QDialog):
    """Edits one deck's auto-flip override (use-global / enable toggles + delays)."""

    def __init__(
        self,
        deck_id: int,
        settings: AutoFlipSettings,
        parent: object | None = None,
    ) -> None:
        super().__init__(parent)
        self._deck_id = deck_id
        self._settings = settings
        self.setWindowTitle("Omnia — Auto-Flip for this deck")
        self.setMinimumWidth(360)
        self._build(settings)

    def _build(self, settings: AutoFlipSettings) -> None:
        from aqt.qt import (
            QCheckBox,
            QDialogButtonBox,
            QFormLayout,
            QVBoxLayout,
        )

        override = settings.per_deck.get(str(self._deck_id))
        use_global = override.use_global if override is not None else False
        enabled = override.enabled if override is not None else True
        q_default = (
            override.delay_question_seconds
            if override is not None
            else settings.delay_question_seconds
        )
        a_default = (
            override.delay_answer_seconds
            if override is not None
            else settings.delay_answer_seconds
        )

        outer = QVBoxLayout(self)

        self._use_global = QCheckBox("Use the global delays for this deck")
        self._use_global.setChecked(use_global)
        outer.addWidget(self._use_global)

        self._enabled = QCheckBox("Auto-flip in this deck")
        self._enabled.setChecked(enabled)
        outer.addWidget(self._enabled)

        form = QFormLayout()
        form.setSpacing(10)
        self._q_delay = self._make_delay_spin(q_default)
        self._a_delay = self._make_delay_spin(a_default)
        form.addRow("Delay before flipping to answer (s)", self._q_delay)
        form.addRow("Delay before auto-grading (s)", self._a_delay)
        outer.addLayout(form)

        # Disable the per-deck delays while deferring to the global ones, mirroring the
        # reference's "use general" behaviour.
        self._use_global.toggled.connect(self._sync_enabled_state)
        self._enabled.toggled.connect(self._sync_enabled_state)
        self._sync_enabled_state()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self._restore_defaults
        )
        outer.addWidget(buttons)

    def _sync_enabled_state(self) -> None:
        """Grey out the per-deck delays when deferring to global delays or when off."""
        use_deck_delays = self._enabled.isChecked() and not self._use_global.isChecked()
        self._q_delay.setEnabled(use_deck_delays)
        self._a_delay.setEnabled(use_deck_delays)

    def _restore_defaults(self) -> None:
        """Reset the controls to this deck's defaults (the global delays, deck on)."""
        self._use_global.setChecked(False)
        self._enabled.setChecked(True)
        self._q_delay.setValue(float(self._settings.delay_question_seconds))
        self._a_delay.setValue(float(self._settings.delay_answer_seconds))
        self._sync_enabled_state()

    @staticmethod
    def _make_delay_spin(value: float) -> Any:
        from aqt.qt import QDoubleSpinBox

        spin = QDoubleSpinBox()
        spin.setDecimals(2)
        spin.setSingleStep(0.1)
        spin.setRange(0.0, 120.0)
        spin.setValue(float(value))
        return spin

    def override_values(self) -> dict[str, Any]:
        """Return this deck's edited override as a plain dict for persistence."""
        return {
            "use_global": self._use_global.isChecked(),
            "enabled": self._enabled.isChecked(),
            "delay_question_seconds": self._q_delay.value(),
            "delay_answer_seconds": self._a_delay.value(),
        }


def add_deck_menu_action(menu: Any, deck_id: int, ctx: PluginContext) -> None:
    """Add the "Omnia: Auto-Flip…" action to a deck's gear (options) menu.

    Args:
        menu: The ``QMenu`` from ``deck_browser_will_show_options_menu``.
        deck_id: The deck whose override the action edits.
        ctx: The plugin context (used to read/persist settings + reload the plugin).
    """
    from aqt.qt import QAction

    action = QAction("Omnia: Auto-Flip…", menu)
    action.triggered.connect(lambda: _open_deck_dialog(deck_id, ctx))
    menu.addAction(action)


def _open_deck_dialog(deck_id: int, ctx: PluginContext) -> None:
    """Open the per-deck dialog; on accept, merge the override and reload the plugin."""
    from omnia.core import anki_compat
    from omnia.core.config.models import AutoFlipSettings

    settings = ctx.config.feature_settings("auto_flip")
    if not isinstance(settings, AutoFlipSettings):
        settings = AutoFlipSettings()

    dialog = AutoFlipDeckDialog(deck_id, settings, anki_compat.main_window())
    if not dialog.exec():
        return

    # Merge into the existing per_deck map (dump models to plain dicts for persistence).
    per_deck = {key: value.model_dump() for key, value in settings.per_deck.items()}
    per_deck[str(deck_id)] = dialog.override_values()
    ctx.config.update_section("auto_flip", {"per_deck": per_deck})
    ctx.reload_self()
