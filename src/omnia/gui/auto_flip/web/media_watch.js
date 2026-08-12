/**
 * Auto-flip HTML5 media watcher (injected on both reviewer sides).
 *
 * Card templates increasingly play audio through their own JS on an HTML5 <audio>/<video>
 * element (a hidden `<audio id="dynPlayer">` fed by a play button or an autoplay script, or a
 * detached `new Audio(...)`) instead of Anki `[sound:...]` AV tags. Anki's av_player never sees
 * those, so auto-flip's wait-for-audio would arm immediately and flip/grade mid-playback.
 *
 * This watcher tracks every playing media element and tells the Python side over the pycmd
 * bridge:
 *   - "media_busy"  — some <audio>/<video> started playing (hold any pending countdown);
 *   - "media_idle"  — everything stopped (safe to start the countdown).
 *
 * Mechanics:
 *   - Installed ONCE per webview (window.__omniaAutoFlipMedia guard); the per-side re-eval only
 *     re-syncs (drops the previous card's players, re-reports the current state).
 *   - TWO detection paths feeding one tracker, because neither alone covers everything:
 *       1. `HTMLMediaElement.prototype.play` is wrapped to track the element BEFORE playback —
 *          this catches DETACHED elements (`new Audio(...)`) whose events never reach document
 *          listeners.
 *       2. Capture-phase `document` listeners ("play" doesn't bubble, but capturing listeners
 *          still see it for any element in the tree) — this catches in-DOM playback that never
 *          goes through JS `play()` (e.g. the `autoplay` attribute).
 *     track() attaches per-element start/stop listeners exactly once, so both paths converge.
 *   - Idle is debounced (350ms): templates chain clips (word ends -> play definition), and the
 *     gap between `ended` and the next `play` must not flap busy->idle->busy.
 *   - The per-side re-eval forces a re-report (busySent=false + update): the Python side resets
 *     its busy flag on every new side, so a still-playing element must re-assert busy and a
 *     clean side must not inherit a stale one.
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
      // Anything paused/ended is done. Attachment is NOT checked: a detached-but-playing
      // element (new Audio(), or a card swap detaching a live player) is still audible and
      // its per-element listeners still fire, so it keeps holding until it really stops.
      W.playing = W.playing.filter(function (el) {
        return !el.paused && !el.ended;
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

    function addPlaying(el) {
      if (W.playing.indexOf(el) === -1) {
        W.playing.push(el);
      }
      update();
    }

    function removePlaying(el) {
      var i = W.playing.indexOf(el);
      if (i !== -1) {
        W.playing.splice(i, 1);
      }
      update();
    }

    // Attach per-element listeners once: they fire even for detached elements.
    function track(el) {
      if (!isMedia(el) || el.__omniaAfTracked) {
        return;
      }
      el.__omniaAfTracked = true;
      ["play", "playing"].forEach(function (name) {
        el.addEventListener(name, function () {
          addPlaying(el);
        });
      });
      ["ended", "pause", "error", "emptied"].forEach(function (name) {
        el.addEventListener(name, function () {
          removePlaying(el);
        });
      });
    }

    // Path 1: wrap play() so detached elements (new Audio()) are tracked before they start.
    var origPlay = HTMLMediaElement.prototype.play;
    HTMLMediaElement.prototype.play = function () {
      track(this);
      return origPlay.apply(this, arguments);
    };

    // Path 2: capture-phase document listener for in-DOM playback that bypasses JS play()
    // (autoplay attribute). addPlaying immediately — the per-element listener attached this
    // tick may miss the very event that's mid-flight.
    document.addEventListener(
      "play",
      function (e) {
        if (isMedia(e.target)) {
          track(e.target);
          addPlaying(e.target);
        }
      },
      true
    );

    W.resync = function () {
      // New side: force a state re-report — Python reset its busy flag, so a still-playing
      // element must re-assert busy (and a genuinely idle watcher must not resend idle).
      if (W.idleTimer) {
        clearTimeout(W.idleTimer);
        W.idleTimer = null;
      }
      W.busySent = false;
      prune();
      if (W.playing.length > 0) {
        W.busySent = true;
        report("media_busy");
      }
    };
  }
  // Per-side re-eval: re-sync against the new card's state.
  W.resync();
})();
