// Telling you when momo needs you: a synthesised mew (any time) and a desktop
// notification (only while the window is away), plus the "(•)" unseen marker.

import { $, loadJSON, saveJSON, toast } from "./dom.js";

// "Away" is hidden *or* unfocused: `document.hidden` alone misses the common
// layout of the browser sitting open next to the editor — visible, but not the
// window you are looking at.
let focused = document.hasFocus();
export const away = () => document.hidden || !focused;

// What app.js supplies: whether events are live (not the backlog replay after a
// reconnect), and a hook for the unseen marker.
const hooks = { isLive: () => true, onUnseenChange: () => {} };
export const notify = { unseen: false };   // new activity while the window was away

function setUnseen(on) {
  if (notify.unseen === on) return;
  notify.unseen = on;
  hooks.onUnseenChange();
}

// `momo.notify` held a bare boolean before the sound option existed.
function loadNotifyPrefs() {
  const raw = loadJSON("momo.notify", null);
  if (raw === true) return { desktop: true, sound: false };
  if (raw && typeof raw === "object") return { desktop: !!raw.desktop, sound: !!raw.sound };
  return { desktop: false, sound: false };
}
const notifyPrefs = loadNotifyPrefs();
let desktopOn = notifyPrefs.desktop && "Notification" in window && Notification.permission === "granted";
const saveNotifyPrefs = () => saveJSON("momo.notify", notifyPrefs);

// A kitten's mew, synthesised so the front end stays asset-free. A kitten is
// high (~1 kHz), soft and nearly pure, so the source is a sine with a few soft
// overtones; a lowpass opening and closing on it is the mouth ("m-ew"), and a
// quick vibrato makes it wobble like a small voice. "ask" ends on an upturn (a
// question), "finish" falls away (a resolution), so they tell apart by ear alone.
const MEW = {
  //         pitch (Hz) at 0, 30%, 65%, 100% of the mew
  ask:    [900, 1200, 980, 1550],
  finish: [950, 1300, 1150, 800],
};
const MEW_LEN = 0.4;  // short: a long mew turns into a wail
let audioCtx = null;
let mewWave = null;
function audio() {
  if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return audioCtx;
}

function chime(kind) {
  let ctx;
  try { ctx = audio(); } catch { return; }  // no WebAudio in this browser
  const play = () => {
    try {
      const at = ctx.currentTime, end = at + MEW_LEN;
      const [p0, p1, p2, p3] = MEW[kind] || MEW.finish;

      // Source: fundamental plus modest 2nd–4th harmonics — enough for the "ee"
      // formant to have something to lift. A full sawtooth reads as a horn, a
      // bare sine as a whistle.
      if (!mewWave) mewWave = ctx.createPeriodicWave(
        new Float32Array([0, 0, 0, 0, 0]), new Float32Array([0, 1, 0.35, 0.2, 0.08]));
      const osc = ctx.createOscillator();
      osc.setPeriodicWave(mewWave);
      osc.frequency.setValueAtTime(p0, at);
      osc.frequency.exponentialRampToValueAtTime(p1, at + MEW_LEN * 0.3);
      osc.frequency.exponentialRampToValueAtTime(p2, at + MEW_LEN * 0.65);
      osc.frequency.exponentialRampToValueAtTime(p3, end);
      // Vibrato in cents, so it scales with the pitch.
      const lfo = ctx.createOscillator();
      const lfoDepth = ctx.createGain();
      lfo.frequency.value = 6;
      lfoDepth.gain.value = 12;
      lfo.connect(lfoDepth).connect(osc.detune);

      // The mouth: closed ("m") muffles the overtones, open lets them through.
      const mouth = ctx.createBiquadFilter();
      mouth.type = "lowpass";
      mouth.Q.value = 0.7;  // no resonant peak: a peak is what made it shrill
      mouth.frequency.setValueAtTime(700, at);
      mouth.frequency.exponentialRampToValueAtTime(3800, at + MEW_LEN * 0.2);
      mouth.frequency.exponentialRampToValueAtTime(1600, end);
      // The vowel: a lift high up is "ee"; sliding it down turns it into "ew".
      // Without it the open mouth alone says "aw".
      const vowel = ctx.createBiquadFilter();
      vowel.type = "peaking";
      vowel.Q.value = 2;
      vowel.gain.value = 10;
      vowel.frequency.setValueAtTime(3600, at);
      vowel.frequency.setValueAtTime(3600, at + MEW_LEN * 0.2);
      vowel.frequency.exponentialRampToValueAtTime(1200, end);

      // Warmth: a gentle lift under the fundamental gives the voice some body.
      const warm = ctx.createBiquadFilter();
      warm.type = "lowshelf";
      warm.frequency.value = 1500;
      warm.gain.value = 6;

      // Soft onset for the "m", swell, then fade; ramped rather than switched,
      // because a bare start/stop clicks.
      const gain = ctx.createGain();
      gain.gain.setValueAtTime(0, at);
      gain.gain.linearRampToValueAtTime(0.025, at + 0.04);
      gain.gain.linearRampToValueAtTime(0.04, at + MEW_LEN * 0.25);
      gain.gain.setValueAtTime(0.04, at + MEW_LEN * 0.45);
      gain.gain.exponentialRampToValueAtTime(0.0001, end);

      osc.connect(warm).connect(vowel).connect(mouth).connect(gain).connect(ctx.destination);
      osc.start(at); lfo.start(at);
      osc.stop(end + 0.01); lfo.stop(end + 0.01);
    } catch { /* the device went away */ }
  };
  // Scheduling into a suspended context loses the sound outright, so resume
  // first and schedule in the callback.
  if (ctx.state === "suspended") ctx.resume().then(play, () => {});
  else play();
}


// A sound setting restored from an earlier visit has had no user gesture yet, and
// the browser blocks audio until there is one — the first click or keypress is it.
function unlockAudio() {
  if (notifyPrefs.sound) { try { audio().resume(); } catch { /* ignore */ } }
}

let notifyThrew = false;  // the service-worker-only complaint is made once

// kind: "ask" (momo needs an answer) | "finish" (the turn is over)
// The sound is deliberately NOT gated on focus: a focused window does not mean
// you are watching it, and being told without having to look is the whole point
// of a cue you can hear. The desktop notification is gated, because notifying
// about the window you are staring at is just noise.
export function signal(kind, title, body) {
  if (!hooks.isLive()) return;
  if (notifyPrefs.sound) chime(kind);
  if (!away()) return;
  setUnseen(true);
  if (!desktopOn) return;
  try {
    const n = new Notification(title, { body: body.slice(0, 180), tag: "momo", renotify: true });
    n.onclick = () => { window.focus(); n.close(); };
  } catch {
    // A few browsers only allow notifications from a service worker. Say so once:
    // failing silently here is indistinguishable from the feature being broken.
    if (!notifyThrew) {
      notifyThrew = true;
      toast("This browser needs a service worker for notifications — the sound still works.");
    }
  }
}

// Any other new event while away: just the marker.
export function noteActivity() {
  if (away() && hooks.isLive()) setUnseen(true);
}

// The View menu's two checkboxes show the current choice.
export function syncNotifyControls() {
  $("#notify-desktop").checked = desktopOn;
  $("#notify-sound").checked = notifyPrefs.sound;
}

export function initNotify(h) {
  Object.assign(hooks, h);
  addEventListener("pointerdown", unlockAudio, { once: true });
  addEventListener("keydown", unlockAudio, { once: true });

  // Coming back clears the marker. Both events are wired: minimising fires blur,
  // switching tabs fires visibilitychange, and which arrives varies by platform.
  const onPresence = () => { if (!away()) setUnseen(false); };
  document.addEventListener("visibilitychange", onPresence);
  addEventListener("focus", () => { focused = true; onPresence(); });
  addEventListener("blur", () => { focused = false; });

  $("#notify-desktop").onchange = async (e) => {
    if (e.target.checked) {
      // Plain HTTP on a LAN address is not a secure context, so the API is simply
      // absent there — say so instead of blaming the browser's settings.
      if (!isSecureContext) {
        e.target.checked = false;
        return toast("Notifications need a secure origin — open http://localhost or tunnel over SSH.");
      }
      if (!("Notification" in window)) {
        e.target.checked = false;
        return toast("This browser has no notifications.");
      }
      const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      desktopOn = perm === "granted";
      e.target.checked = desktopOn;
      if (!desktopOn) toast("Notifications are blocked for this page in the browser settings.");
    } else desktopOn = false;
    notifyPrefs.desktop = desktopOn;
    saveNotifyPrefs();
  };
  $("#notify-sound").onchange = (e) => {
    notifyPrefs.sound = e.target.checked;
    saveNotifyPrefs();
    if (notifyPrefs.sound) chime("ask");  // this click is the gesture that unlocks audio
  };
  syncNotifyControls();
}
