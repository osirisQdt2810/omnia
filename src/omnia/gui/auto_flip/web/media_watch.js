/**
 * Auto-flip HTML5 media watcher (injected on both reviewer sides).
 *
 * Card templates increasingly play audio through their own JS on an HTML5 <audio>/<video>
 * element (e.g. `new Audio(...)` / a hidden `<audio id="dynPlayer">` fed by a play button or an
 * autoplay script) instead of Anki `[sound:...]` AV tags. Anki's av_player never sees those, so
 * auto-flip's wait-for-audio would arm immediately and flip/grade mid-playback.
 *
 * This watcher tracks every playing media element and tells the Python side over the pycmd
 * bridge:
 *   - "media_busy"  — some <audio>/<video> started playing (hold any pending countdown);
 *   - "media_idle"  — everything stopped (safe to start the countdown).
 *
 * Mechanics:
 *   - Installed ONCE per webview (window.__omniaAutoFlipMedia guard); the per-side re-eval only
 *     prunes elements from the previous card and re-reports.
 *   - Listeners are capture-phase on `document`: play/ended/pause don't bubble, but capturing
 *     listeners still see them for any element in the tree — including elements created later.
 *   - Idle is debounced (350ms): templates chain clips (word ends -> play definition), and the
 *     gap between `ended` and the next `play` must not flap busy->idle->busy.
 *   - Pruning drops detached elements (`!document.contains(el)`): a card swap can remove a
 *     playing element without firing events our document listeners can see.
 */
(function () {
  "use strict";
  var W = window.__omniaAutoFlipMedia;
  if (!W) {
    W = window.__omniaAutoFlipMedia = {
      playing: [],
      idleTimer: null,
      busySent: false,
    };

    function report(op) {
      try {
        pycmd("omnia:" + JSON.stringify({plugin: "auto_flip", op: op, data: {}}));
      } catch (e) {
        /* bridge not ready — nothing to hold yet */
      }
    }

    function isMedia(el) {
      return !!el && (el.tagName === "AUDIO" || el.tagName === "VIDEO");
    }

    function prune() {
      W.playing = W.playing.filter(function (el) {
        return document.contains(el) && !el.paused && !el.ended;
      });
    }

    function update() {
      prune();
      if (W.playing.length > 0) {
        if (W.idleTimer) {
          clearTimeout(W.idleTimer);
          W.idleTimer = null;
        }
        if (!W.busySent) {
          W.busySent = true;
          report("media_busy");
        }
      } else if (W.busySent && !W.idleTimer) {
        W.idleTimer = setTimeout(function () {
          W.idleTimer = null;
          prune();
          if (W.playing.length === 0 && W.busySent) {
            W.busySent = false;
            report("media_idle");
          }
        }, 350);
      }
    }
    W.update = update;

    document.addEventListener(
      "play",
      function (e) {
        if (isMedia(e.target)) {
          if (W.playing.indexOf(e.target) === -1) {
            W.playing.push(e.target);
          }
          update();
        }
      },
      true
    );
    ["ended", "pause", "error", "emptied"].forEach(function (name) {
      document.addEventListener(
        name,
        function (e) {
          if (isMedia(e.target)) {
            var i = W.playing.indexOf(e.target);
            if (i !== -1) {
              W.playing.splice(i, 1);
            }
            update();
          }
        },
        true
      );
    });
  }
  // Per-side re-eval: drop the previous card's (now detached) elements and re-report.
  W.update();
})();
