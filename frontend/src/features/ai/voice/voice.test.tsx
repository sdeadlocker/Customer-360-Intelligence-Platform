import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CitationProvider } from '../CitationContext';

/**
 * The voice assistant layer (Phase 23).
 *
 * jsdom ships neither `speechSynthesis` nor `SpeechRecognition`, so those are stubbed here. That is
 * also the real production truth on Firefox, and the first test pins the resulting behaviour: with
 * no API present, no voice affordance renders and the panel is exactly the typed-only panel. The
 * remaining tests install minimal fakes and assert the wiring — the mic fills the composer, spoken
 * replies are opt-in and persist, and the answer path is untouched by any of it.
 */

// The Ask stream is mocked so a "complete" turn can be produced deterministically without SSE.
const { askMock } = vi.hoisted(() => ({ askMock: vi.fn() }));
let turns: unknown[] = [];
let busy = false;

vi.mock('../useAskStream', () => ({
  useAskStream: () => ({ turns, busy, ask: askMock }),
}));

import { AskPanel } from '../AskPanel';

function renderPanel(): void {
  render(
    <CitationProvider>
      <AskPanel />
    </CitationProvider>,
  );
}

beforeEach(() => {
  turns = [];
  busy = false;
  askMock.mockReset();
  window.localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('voice assistant — unsupported browser', () => {
  it('renders no voice affordances when the APIs are absent', () => {
    // jsdom has neither API; nothing is stubbed.
    renderPanel();
    expect(screen.queryByRole('button', { name: /ask by voice/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /voice o(n|ff)/i })).toBeNull();
    // The typed composer is unchanged and fully usable.
    expect(screen.getByLabelText('Your question')).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------- fakes

/** A `SpeechRecognition` fake whose `start` immediately dictates `phrase` and ends. */
function installRecognition(phrase: string): void {
  class FakeRecognition {
    lang = '';
    continuous = false;
    interimResults = false;
    maxAlternatives = 1;
    onresult: ((event: unknown) => void) | null = null;
    onerror: ((event: unknown) => void) | null = null;
    onend: ((event: unknown) => void) | null = null;
    onstart: ((event: unknown) => void) | null = null;

    start(): void {
      this.onstart?.(new Event('start'));
      this.onresult?.({
        resultIndex: 0,
        results: {
          length: 1,
          0: { isFinal: true, length: 1, 0: { transcript: phrase, confidence: 1 } },
        },
      });
      this.onend?.(new Event('end'));
    }

    stop(): void {
      this.onend?.(new Event('end'));
    }

    abort(): void {
      /* nothing to tear down in the fake */
    }
  }
  vi.stubGlobal('SpeechRecognition', FakeRecognition);
}

/** A `speechSynthesis` fake that records what was spoken and fires the utterance lifecycle. */
function installSynthesis(): { spoken: string[] } {
  const spoken: string[] = [];
  class FakeUtterance {
    text: string;
    rate = 1;
    pitch = 1;
    onstart: (() => void) | null = null;
    onend: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onboundary: (() => void) | null = null;
    constructor(text: string) {
      this.text = text;
    }
  }
  const synth = {
    speak(utterance: FakeUtterance): void {
      spoken.push(utterance.text);
      utterance.onstart?.();
      utterance.onend?.();
    },
    cancel(): void {
      /* the fake speaks synchronously, so there is nothing queued to cancel */
    },
  };
  vi.stubGlobal('speechSynthesis', synth);
  vi.stubGlobal('SpeechSynthesisUtterance', FakeUtterance);
  return { spoken };
}

describe('avatar rail', () => {
  it('shows the portrait rail with the Speak pill beside the chat, not in the header', () => {
    installSynthesis();
    installRecognition('x');
    renderPanel();

    // The "Speak" pill (the rail dictation affordance) is present before any conversation.
    const speak = screen.getByRole('button', { name: /speak your question/i });
    expect(speak).toBeInTheDocument();
    // It sits in the left rail, not the panel header, and the rail is a sibling of the chat column.
    expect(speak.closest('.ask-panel__head')).toBeNull();
    expect(speak.closest('.ask-rail')).not.toBeNull();
    expect(document.querySelector('.ask-layout .ask-chat')).not.toBeNull();
  });

  it('shows the assistant name and a live status in the rail', () => {
    installSynthesis();
    installRecognition('x');
    renderPanel();
    expect(screen.getByText(/AI assistant/i)).toBeInTheDocument();
    expect(screen.getByText('Online')).toBeInTheDocument();
  });

  it('dictates from the rail Speak pill too', async () => {
    installSynthesis();
    installRecognition('who is eligible for a premium card');
    renderPanel();

    await userEvent.click(screen.getByRole('button', { name: /speak your question/i }));
    await waitFor(() => {
      expect(screen.getByLabelText('Your question')).toHaveValue(
        'who is eligible for a premium card',
      );
    });
  });
});

describe('voice input', () => {
  it('dictates a phrase into the composer', async () => {
    installRecognition('what are the top risks for jane doe');
    renderPanel();

    const mic = screen.getByRole('button', { name: /ask by voice/i });
    await userEvent.click(mic);

    await waitFor(() => {
      expect(screen.getByLabelText('Your question')).toHaveValue(
        'what are the top risks for jane doe',
      );
    });
    // Dictation fills the input; it does not submit on its own — the user still presses Ask.
    expect(askMock).not.toHaveBeenCalled();
  });

  it('surfaces the browser privacy note beside the mic', () => {
    installRecognition('x');
    renderPanel();
    expect(screen.getByText(/transcribed by your browser/i)).toBeInTheDocument();
  });
});

describe('voice output', () => {
  it('is off by default and does not speak an answer', () => {
    const { spoken } = installSynthesis();
    turns = [completeTurn('Her risk band is moderate.')];
    renderPanel();
    // The toggle exists and reads "off"; nothing was spoken.
    expect(screen.getByRole('button', { name: /voice off/i })).toBeInTheDocument();
    expect(spoken).toEqual([]);
  });

  it('speaks the latest answer once turned on, and persists the choice', async () => {
    const { spoken } = installSynthesis();
    turns = [completeTurn('Her risk band is moderate.')];
    const { unmount } = render(
      <CitationProvider>
        <AskPanel />
      </CitationProvider>,
    );

    await userEvent.click(screen.getByRole('button', { name: /voice off/i }));
    await waitFor(() => {
      expect(spoken).toContain('Her risk band is moderate.');
    });
    // The preference survives a remount.
    unmount();
    expect(window.localStorage.getItem('c360.voice.speakReplies')).toBe('true');
  });

  it('never speaks a refused answer', async () => {
    const { spoken } = installSynthesis();
    turns = [{ ...completeTurn('withheld'), refused: true }];
    renderPanel();
    await userEvent.click(screen.getByRole('button', { name: /voice off/i }));
    // Give the effect a chance to run.
    await Promise.resolve();
    expect(spoken).toEqual([]);
  });
});

/** A minimal completed Q&A turn, enough for the panel to render and (maybe) speak it. */
function completeTurn(answer: string): Record<string, unknown> {
  return {
    id: 1,
    question: 'q',
    answer,
    factCitations: [],
    passageCitations: [],
    traversalPaths: [],
    status: 'complete',
    refused: false,
    noGuidance: false,
    degraded: false,
    errorMessage: null,
  };
}
