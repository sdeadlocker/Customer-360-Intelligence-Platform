# Static assets

Files here are served from the site root by Vite. Drop a file in and reference it as `/name.ext`.

## The assistant portrait

The voice assistant's avatar (Phase 23) shows a real headshot when one is present at:

    frontend/public/assistant-avatar.png

- Any square-ish headshot works; it is cropped to a rounded portrait (`object-fit: cover`).
- A 3D-rendered or photographic face — like a chatbot avatar — reads best. **No headset/mic** in
  the image: the mic control lives in the composer and on the "Speak" pill, not on the face.
- Aim for ~300–600 px square. PNG or JPG (name it `.png` regardless, or change `PORTRAIT_SRC` in
  `src/features/ai/voice/AssistantAvatar.tsx`).

Until this file exists the component falls back to a drawn portrait automatically — no code change
is needed either way. Dropping the image in is the whole swap.
