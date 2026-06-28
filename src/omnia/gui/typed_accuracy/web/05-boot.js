
  /**
   * Typed-accuracy stats panel — part 5 of 5 of the panel IIFE (load order matters).
   * This fragment CLOSES the IIFE. It watches the stats DOM for re-renders, boots the panel
   * with a bounded poll, and exposes `window.__TA_refresh` for re-injection.
   */

  /** Observe the stats DOM and re-mount/refresh the panel when Anki re-renders it. */
  function watchStatsRerender() {
    const obs = new MutationObserver(() => {
      const card = document.getElementById("ta-card");
      if (!card) {
        ensureMounted();
        refresh(true);
        return;
      }

      // Grid can change without removing the card; keep responsive mode in sync.
      applyCardResponsiveClass(card);
    });

    obs.observe(document.documentElement || document.body, {
      subtree: true,
      childList: true,
    });
  }

  /**
   * Boot the panel: poll briefly for the stats grid, then mount, watch, and refresh.
   * @return {!Promise<void>}
   */
  async function boot() {
    dbg(`[JS] boot href=${location.href}`);

    let tries = 0;
    const tick = async () => {
      tries++;
      const mounted = ensureMounted();
      if (mounted) {
        watchStatsRerender();
        refresh(true);

        setInterval(() => {
          if (document.getElementById("ta-card")) {
            refresh(false);
          }
        }, 2000);
        return;
      }
      // Bounded poll: this JS is eval'd on EVERY styled webview (editor, browser, …), not
      // just the stats screen. Cap at ~3s (60×50ms) so a non-stats page where the grid never
      // appears stops scanning quickly instead of burning 10s of DOM work on, e.g., editor open.
      if (tries < 60) {
        setTimeout(tick, 50);
      }
    };

    tick();
  }

  window.__TA_refresh = refresh;
  boot();
})();
