import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

/**
 * Dictate a question with the microphone (Phase 23 — voice assistant).
 *
 * Wraps the browser's `SpeechRecognition` (vendor-prefixed `webkitSpeechRecognition` in
 * Chrome/Edge/Safari; absent in Firefox, where the whole affordance is hidden). It produces *text*
 * and nothing else — the transcript flows into the same Ask composer a typed question uses, so the
 * grounding, entitlement and masking guarantees downstream are entirely unaffected. The mic never
 * reaches the answer path; it only fills the input.
 *
 * **Privacy, stated plainly.** Browser speech recognition streams microphone audio to the browser
 * vendor's servers to transcribe it (Google for Chrome, Apple for Safari) — it is not on-device. A
 * spoken banking question can contain a customer's name, so this is genuinely third-party data
 * egress, and the feature is therefore strictly opt-in: nothing listens until the user presses the
 * mic. The consuming UI surfaces this. A production deployment would swap this hook for an
 * on-device or self-hosted recogniser behind the same interface.
 */

export type ListeningState = 'idle' | 'listening';

export interface SpeechInput {
  /** Whether the browser exposes a speech recogniser at all. False ⇒ callers hide the mic. */
  readonly supported: boolean;
  readonly state: ListeningState;
  /** The best-guess transcript so far this session, updated live as the user speaks. */
  readonly transcript: string;
  /** A human message when recognition fails (no mic permission, network, etc.), else null. */
  readonly error: string | null;
  /** Begin listening. Calls `onFinal` once with the settled transcript when the user stops. */
  readonly start: (onFinal: (text: string) => void) => void;
  readonly stop: () => void;
}

function recogniserConstructor(): SpeechRecognitionConstructor | undefined {
  if (typeof window === 'undefined') {
    return undefined;
  }
  return window.SpeechRecognition ?? window.webkitSpeechRecognition;
}

/** Map a recogniser error code to a short, non-technical message. */
function messageFor(code: string): string {
  switch (code) {
    case 'not-allowed':
    case 'service-not-allowed':
      return 'Microphone access was blocked. Allow it in your browser to dictate.';
    case 'no-speech':
      return "Didn't catch that — try again.";
    case 'audio-capture':
      return 'No microphone was found.';
    case 'network':
      return 'Speech recognition is offline right now.';
    default:
      return 'Could not use the microphone.';
  }
}

export function useSpeechInput(): SpeechInput {
  const Ctor = useMemo(() => recogniserConstructor(), []);
  const supported = Ctor !== undefined;

  const [state, setState] = useState<ListeningState>('idle');
  const [transcript, setTranscript] = useState('');
  const [error, setError] = useState<string | null>(null);

  const recognitionRef = useRef<SpeechRecognition | null>(null);
  // Held in a ref so the recogniser's own callbacks can reach the caller's handler without the
  // recogniser instance depending on it.
  const onFinalRef = useRef<((text: string) => void) | null>(null);
  const finalTextRef = useRef('');

  const stop = useCallback((): void => {
    recognitionRef.current?.stop();
  }, []);

  const start = useCallback(
    (onFinal: (text: string) => void): void => {
      if (Ctor === undefined || recognitionRef.current !== null) {
        return;
      }
      const recognition = new Ctor();
      recognition.lang = 'en-US';
      recognition.continuous = false; // one utterance, then settle — a question, not a dictation
      recognition.interimResults = true; // show words as they land, so it feels responsive
      recognition.maxAlternatives = 1;

      onFinalRef.current = onFinal;
      finalTextRef.current = '';
      setTranscript('');
      setError(null);

      recognition.onstart = (): void => {
        setState('listening');
      };

      recognition.onresult = (event: SpeechRecognitionEvent): void => {
        let interim = '';
        for (let i = event.resultIndex; i < event.results.length; i++) {
          const result = event.results[i];
          if (result === undefined) {
            continue;
          }
          const phrase = result[0]?.transcript ?? '';
          if (result.isFinal) {
            finalTextRef.current = `${finalTextRef.current} ${phrase}`.trim();
          } else {
            interim += phrase;
          }
        }
        setTranscript(`${finalTextRef.current} ${interim}`.trim());
      };

      recognition.onerror = (event: SpeechRecognitionErrorEvent): void => {
        setError(messageFor(event.error));
      };

      recognition.onend = (): void => {
        setState('idle');
        recognitionRef.current = null;
        const settled = finalTextRef.current.trim();
        if (settled !== '') {
          onFinalRef.current?.(settled);
        }
        onFinalRef.current = null;
      };

      recognitionRef.current = recognition;
      try {
        recognition.start();
      } catch {
        // `start()` throws if called while already starting; treat as a no-op and reset.
        recognitionRef.current = null;
        setState('idle');
      }
    },
    [Ctor],
  );

  // Abort a live recognition if the panel unmounts, so the mic is never left open off-screen.
  useEffect(
    () => () => {
      recognitionRef.current?.abort();
      recognitionRef.current = null;
    },
    [],
  );

  return { supported, state, transcript, error, start, stop };
}
