/**
 * Audio-speed applier (injected on both reviewer sides).
 *
 * Anki plays `[sound:...]` tags through mpv, which the Python side controls directly. But a
 * card template can also play audio itself through an HTML5 <audio>/<video> element — a
 * `<audio src="{{text:Definition (audio filename)}}">` on the answer side is exactly how the
 * reporting deck does it — and those elements are created fresh every time a side renders,
 * with `playbackRate = 1.0`. A rate set on the question side's elements is simply gone by the
 * time the answer's elements exist. So the rate has to be RE-APPLIED on every render, and to
 * every element that starts playing later, not just the ones present when a key was pressed.
 *
 * Mechanics:
 *   - Installed ONCE per webview (window.__omniaAudioSpeed guard); each side's re-eval only
 *     pushes the current rate through `apply`.
 *   - THREE application paths, because no single one covers every player a template can use:
 *       1. `apply(rate)` walks every <audio>/<video> in the document right now.
 *       2. `HTMLMediaElement.prototype.play` is wrapped so a DETACHED player (`new Audio(...)`)
 *          — which never appears in the document — still gets the rate the instant it plays.
 *       3. Capture-phase "play"/"loadedmetadata" listeners catch in-DOM playback that never
 *          goes through JS `play()` (the `autoplay` attribute) and elements whose media loads
 *          after `apply` ran.
 *   - `defaultPlaybackRate` is set alongside `playbackRate`: some players reset the latter to
 *     the former on `load()`, and the reset must land on OUR rate, not 1.0.
 *   - The Python side may run before this file on a given render; it then leaves the rate in
 *     `window.__omniaAudioSpeedPending` and the installer below consumes it. Order-independent.
 */
(function () {
  "use strict";
  var S = window.__omniaAudioSpeed;
  if (!S) {
    S = window.__omniaAudioSpeed = { rate: 1.0 };

    function applyTo(el) {
      if (!el || typeof el.playbackRate !== "number") return;
      try {
        el.defaultPlaybackRate = S.rate;
        el.playbackRate = S.rate;
      } catch (e) {
        /* a player that refuses the rate (some codecs) must not break the card */
      }
    }

    S.apply = function (rate) {
      if (typeof rate === "number" && isFinite(rate) && rate > 0) S.rate = rate;
      var els = document.querySelectorAll("audio,video");
      for (var i = 0; i < els.length; i++) applyTo(els[i]);
      return S.rate;
    };

    var proto = window.HTMLMediaElement && window.HTMLMediaElement.prototype;
    if (proto && !proto.__omniaSpeedWrapped) {
      var origPlay = proto.play;
      proto.play = function () {
        applyTo(this);
        return origPlay.apply(this, arguments);
      };
      proto.__omniaSpeedWrapped = true;
    }

    document.addEventListener("play", function (ev) { applyTo(ev.target); }, true);
    document.addEventListener("loadedmetadata", function (ev) { applyTo(ev.target); }, true);
  }

  if (typeof window.__omniaAudioSpeedPending === "number") {
    S.apply(window.__omniaAudioSpeedPending);
    delete window.__omniaAudioSpeedPending;
  } else {
    S.apply(S.rate);
  }
})();
