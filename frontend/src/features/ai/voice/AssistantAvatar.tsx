import { useState } from 'react';

/**
 * The assistant avatar (Phase 23 — voice assistant).
 *
 * A portrait shown in the Ask panel's left rail, like a chat contact. It prefers a **real
 * photographic/3D-rendered headshot** dropped at `/assistant-avatar.png` (see `frontend/public/`),
 * and falls back to a soft-shaded illustrated portrait when that file is absent — so the layout
 * works today and swapping in a real face is a one-file drop, no code change.
 *
 * Whichever it shows, it is a still image, not a rendered video: a genuinely live talking-head
 * avatar needs a paid streaming service, adds per-utterance latency, and ships customer-relevant
 * text to a third party — all of which this platform's rules forbid. The lip-sync is done cheaply
 * and honestly instead: while the assistant speaks, a small mouth overlay animates from the live
 * {@link ./useSpeechOutput} signal (the very utterance the user hears, so lips and audio cannot
 * drift). The listening/speaking ring pulses because the microphone is actually live. Nothing here
 * implies a capability the platform does not have. There is no headset mic on the face — the mic
 * control lives in the composer and on the Speak pill.
 *
 * Presentational only. `aria-hidden`: the meaning is the answer text and the live-region
 * announcements, so a screen-reader user loses nothing by not seeing the face.
 */

export type AvatarState = 'idle' | 'listening' | 'thinking' | 'speaking';

/** Where a supplied portrait image is looked for. Absent by default; drop one in to use it. */
const PORTRAIT_SRC = '/assistant-avatar.png';

export function AssistantAvatar({
  state,
  mouth,
  size = 132,
}: {
  readonly state: AvatarState;
  /** 0…1 mouth openness, from the speech-output hook. Ignored unless `state` is `speaking`. */
  readonly mouth: number;
  readonly size?: number;
}): React.JSX.Element {
  // Try the real portrait first; drop to the drawn one if it fails to load (the default today).
  const [usePhoto, setUsePhoto] = useState(true);
  const speaking = state === 'speaking';

  return (
    <div
      className={`assistant-avatar assistant-avatar--${state}`}
      aria-hidden="true"
      style={{ width: size, height: size }}
    >
      <div className="assistant-avatar__ring" />
      <div className="assistant-avatar__frame">
        {usePhoto ? (
          <img
            className="assistant-avatar__photo"
            src={PORTRAIT_SRC}
            alt=""
            onError={() => {
              setUsePhoto(false);
            }}
          />
        ) : (
          <IllustratedPortrait />
        )}

        {/* Lip-sync overlay, only on the *drawn* fallback. A generic mouth patch placed over a real
            photo never lands exactly on that photo's lips and reads worse than no overlay, so on a
            supplied portrait the "speaking" cue is carried by the pulsing ring and the rail status
            instead — honest, and never misaligned. */}
        {speaking && !usePhoto && (
          <span
            className="assistant-avatar__lips"
            style={{ transform: `translateX(-50%) scaleY(${0.4 + mouth * 1.4})` }}
          />
        )}

        {state === 'thinking' && (
          <span className="assistant-avatar__thinking" aria-hidden="true">
            <span />
            <span />
            <span />
          </span>
        )}
      </div>
    </div>
  );
}

/**
 * The fallback illustrated portrait — a soft-shaded head, used until a real image is supplied. No
 * headset. Framed as a headshot so it crops the same way a photo would.
 */
function IllustratedPortrait(): React.JSX.Element {
  return (
    <svg viewBox="0 0 120 132" className="assistant-avatar__svg" role="presentation">
      <defs>
        <linearGradient id="av-bg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="var(--color-accent)" />
          <stop offset="60%" stopColor="var(--color-accent-strong)" />
          <stop offset="100%" stopColor="var(--color-accent)" stopOpacity="0.9" />
        </linearGradient>
        <radialGradient id="av-skin" cx="42%" cy="34%" r="78%">
          <stop offset="0%" stopColor="#fbd7b5" />
          <stop offset="62%" stopColor="#f0c09a" />
          <stop offset="100%" stopColor="#dca77f" />
        </radialGradient>
        <linearGradient id="av-hair" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="#7a5637" />
          <stop offset="100%" stopColor="#5a3d27" />
        </linearGradient>
        <linearGradient id="av-shoulders" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#4a5573" />
          <stop offset="100%" stopColor="#333b52" />
        </linearGradient>
        <radialGradient id="av-vignette" cx="50%" cy="42%" r="64%">
          <stop offset="70%" stopColor="#000000" stopOpacity="0" />
          <stop offset="100%" stopColor="#1a1f33" stopOpacity="0.3" />
        </radialGradient>
      </defs>

      <rect x="0" y="0" width="120" height="132" fill="url(#av-bg)" />
      <circle cx="24" cy="24" r="26" fill="#ffffff" opacity="0.06" />
      <circle cx="104" cy="50" r="34" fill="#ffffff" opacity="0.05" />

      {/* Shoulders */}
      <path
        d="M14 132 C 16 104, 36 92, 60 92 C 84 92, 104 104, 106 132 Z"
        fill="url(#av-shoulders)"
      />
      <path d="M60 92 L 52 108 L 60 116 L 68 108 Z" fill="#eef1f8" opacity="0.9" />

      {/* Neck */}
      <rect x="52" y="74" width="16" height="22" rx="8" fill="#e3ab82" />
      <path
        d="M46 78 C 52 86, 68 86, 74 78 L 74 86 C 66 92, 54 92, 46 86 Z"
        fill="#cf9770"
        opacity="0.6"
      />

      {/* Hair back */}
      <path
        d="M28 56 C 26 30, 42 18, 60 18 C 78 18, 94 30, 92 56 L 92 80 C 92 68, 86 62, 80 62 L 40 62 C 34 62, 28 68, 28 80 Z"
        fill="url(#av-hair)"
      />

      {/* Face */}
      <ellipse cx="60" cy="54" rx="27" ry="30" fill="url(#av-skin)" />
      <path
        d="M33 52 C 33 68, 40 80, 48 84 C 40 76, 36 64, 36 52 Z"
        fill="#cf9770"
        opacity="0.35"
      />
      <path
        d="M87 52 C 87 68, 80 80, 72 84 C 80 76, 84 64, 84 52 Z"
        fill="#cf9770"
        opacity="0.35"
      />
      <ellipse cx="45" cy="64" rx="6" ry="4.5" fill="#eaa07e" opacity="0.45" />
      <ellipse cx="75" cy="64" rx="6" ry="4.5" fill="#eaa07e" opacity="0.45" />

      {/* Ears */}
      <ellipse cx="34" cy="56" rx="4" ry="6" fill="#eab189" />
      <ellipse cx="86" cy="56" rx="4" ry="6" fill="#eab189" />

      {/* Hair front */}
      <path
        d="M32 54 C 31 30, 46 18, 60 18 C 78 18, 90 32, 90 52 C 84 42, 72 37, 62 38 C 53 39, 45 44, 41 54 C 39 48, 34 49, 32 54 Z"
        fill="url(#av-hair)"
      />
      <path d="M60 18 C 46 18, 34 27, 33 48 C 39 34, 50 28, 60 28 Z" fill="#8a5f42" opacity="0.7" />

      {/* Brows */}
      <g className="assistant-avatar__brows">
        <path
          d="M44 44 Q 50 41.5, 55 44"
          fill="none"
          stroke="#5c3d29"
          strokeWidth="2.6"
          strokeLinecap="round"
        />
        <path
          d="M65 44 Q 70 41.5, 76 44"
          fill="none"
          stroke="#5c3d29"
          strokeWidth="2.6"
          strokeLinecap="round"
        />
      </g>

      {/* Eyes */}
      <g className="assistant-avatar__eyes">
        <ellipse cx="50" cy="53" rx="6" ry="4.6" fill="#ffffff" />
        <ellipse cx="70" cy="53" rx="6" ry="4.6" fill="#ffffff" />
        <circle cx="51" cy="53.4" r="3.1" fill="#5b4127" />
        <circle cx="71" cy="53.4" r="3.1" fill="#5b4127" />
        <circle cx="51" cy="53.4" r="1.5" fill="#20160d" />
        <circle cx="71" cy="53.4" r="1.5" fill="#20160d" />
        <circle cx="52.4" cy="52" r="1" fill="#ffffff" />
        <circle cx="72.4" cy="52" r="1" fill="#ffffff" />
      </g>

      {/* Nose */}
      <path d="M60 56 L 56 68 C 56 70, 64 70, 64 68 Z" fill="#dfa079" opacity="0.85" />

      {/* Resting smile (the animated overlay handles the open mouth on top). */}
      <path
        d="M52 80 Q 60 87, 68 80"
        fill="none"
        stroke="#b5515a"
        strokeWidth="3"
        strokeLinecap="round"
      />

      <rect x="0" y="0" width="120" height="132" fill="url(#av-vignette)" />
    </svg>
  );
}
