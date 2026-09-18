import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

/**
 * Speak text aloud in the browser, and expose a live "mouth openness" signal the avatar animates
 * from (Phase 23 — voice assistant).
 *
 * Everything here is `window.speechSynthesis`, which ships in every current browser except older
 * Firefox releases and costs nothing — no third-party service, no network round trip, no audio or
 * text leaving the tab. That is the whole reason the avatar is low-latency: the same utterance the
 * user hears drives the face, so there is no video stream to fetch and no per-word server call.
 *
 * The mouth signal is derived, not measured. The Web Speech API does not expose an audio amplitude
 * envelope, so a real viseme track is impossible without a heavier synthesiser. Instead, while an
 * utterance is speaking, `mouth` oscillates on a timer with a little jitter — enough for the face to
 * read as *talking* rather than as a still image with audio playing behind it. It is honest
 * decoration: it animates because speech is playing, and it stops the instant speech does. `boundary`
 * events (fired per word where supported) nudge the openness so the motion loosely tracks word onsets.
 */

export type SpeechState = 'idle' | 'speaking';

export interface SpeechOutput {
  /** Whether the browser can speak at all. When false, callers hide the "speak" affordances. */
  readonly supported: boolean;
  /** Whether the user has turned spoken replies on. Persisted across mounts. */
  readonly enabled: boolean;
  readonly setEnabled: (on: boolean) => void;
  readonly state: SpeechState;
  /** 0 (closed) … 1 (open) — the avatar's mouth openness this frame. */
  readonly mouth: number;
  /** Speak `text`, cancelling anything already queued. A no-op when unsupported or disabled. */
  readonly speak: (text: string) => void;
  /** Stop immediately and close the mouth. */
  readonly cancel: () => void;
}

const _STORAGE_KEY = 'c360.voice.speakReplies';

/** Read the persisted preference once, defaulting off — voice output is opt-in. */
function initialEnabled(): boolean {
  try {
    return window.localStorage.getItem(_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

export function useSpeechOutput(): SpeechOutput {
  const supported = useMemo(() => typeof window !== 'undefined' && 'speechSynthesis' in window, []);
  const [enabled, setEnabledState] = useState(supported && initialEnabled());
  const [state, setState] = useState<SpeechState>('idle');
  const [mouth, setMouth] = useState(0);

  const animationRef = useRef<number | null>(null);
  const startedAtRef = useRef(0);
  // A per-word emphasis the boundary handler bumps, decaying between words so motion tracks onsets.
  const emphasisRef = useRef(0);

  const stopAnimation = useCallback((): void => {
    if (animationRef.current !== null) {
      cancelAnimationFrame(animationRef.current);
      animationRef.current = null;
    }
    setMouth(0);
  }, []);

  // The mouth loop: a base oscillation plus a decaying per-word emphasis, clamped to 0..1. Pure
  // presentation — no audio is analysed — so it is deliberately cheap and stops the moment speech
  // ends. Frozen for a user who prefers reduced motion: the face stays gently open rather than
  // flapping (the sound is what matters; the movement is reinforcement).
  const animate = useCallback((): void => {
    const reduced =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true;
    if (reduced) {
      setMouth(0.35);
      return;
    }
    const tick = (): void => {
      const elapsed = (performance.now() - startedAtRef.current) / 1000;
      // Two detuned sines read as speech cadence rather than a metronome.
      const base = 0.28 + 0.22 * Math.sin(elapsed * 11) + 0.12 * Math.sin(elapsed * 19 + 1);
      emphasisRef.current *= 0.86; // decay the last word's bump
      const open = Math.max(0, Math.min(1, base + emphasisRef.current));
      setMouth(open);
      animationRef.current = requestAnimationFrame(tick);
    };
    animationRef.current = requestAnimationFrame(tick);
  }, []);

  const cancel = useCallback((): void => {
    // Re-check the API at call time rather than trusting the mount-time `supported`: an unmount
    // cleanup can run after a test has torn the global down, and defensively it costs nothing.
    if (typeof window !== 'undefined' && 'speechSynthesis' in window) {
      window.speechSynthesis.cancel();
    }
    stopAnimation();
    setState('idle');
  }, [stopAnimation]);

  const speak = useCallback(
    (text: string): void => {
      const trimmed = text.trim();
      if (!supported || !enabled || trimmed === '') {
        return;
      }
      window.speechSynthesis.cancel(); // never let two answers overlap

      const utterance = new SpeechSynthesisUtterance(trimmed);
      utterance.rate = 1.02;
      utterance.pitch = 1.0;

      utterance.onstart = (): void => {
        startedAtRef.current = performance.now();
        emphasisRef.current = 0;
        setState('speaking');
        animate();
      };
      utterance.onboundary = (): void => {
        // A word just began — open a touch wider, then let the decay in `animate` close it.
        emphasisRef.current = 0.25;
      };
      const finish = (): void => {
        stopAnimation();
        setState('idle');
      };
      utterance.onend = finish;
      utterance.onerror = finish;

      window.speechSynthesis.speak(utterance);
    },
    [supported, enabled, animate, stopAnimation],
  );

  const setEnabled = useCallback(
    (on: boolean): void => {
      setEnabledState(on);
      try {
        window.localStorage.setItem(_STORAGE_KEY, String(on));
      } catch {
        /* storage unavailable (private mode); the in-memory value still applies this session */
      }
      if (!on) {
        cancel(); // turning it off silences anything mid-sentence
      }
    },
    [cancel],
  );

  // Stop speaking if the panel unmounts mid-answer, so a spoken reply never outlives its screen.
  useEffect(() => cancel, [cancel]);

  return { supported, enabled, setEnabled, state, mouth, speak, cancel };
}
