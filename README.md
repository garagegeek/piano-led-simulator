# 🎹 Piano LED Simulator

A browser-based simulator for a physical **ESP32 + WS2812B LED strip** piano visualizer project. Load an MP3 and a MIDI file, and watch 88 LEDs light up in perfect sync with the music — with glow effects, velocity-driven brightness, and multiple color modes.

This simulator runs entirely in the browser (no install, no server) and is used to preview and tune the LED effects before deploying to the ESP32 hardware.

---

## How It Works

The simulator loads two files:

- **MP3** — the audio, played via the Web Audio API
- **MIDI** — the note event data (which keys are pressed and when), parsed entirely in JavaScript

Both files are driven by the same `AudioContext` clock, so there is **zero drift** between the audio and the LED events. The MIDI parser builds a precise tempo map from the file's tempo change events, then converts every note's tick timestamp into real seconds before playback begins.

### Why separate files in the simulator (vs. one file on ESP32)?

On the PC simulator, the Web Audio API gives us a high-precision shared clock, so keeping the files separate is fine and makes iteration easy. On the ESP32, we will merge them into a single interleaved `.lmp` container file where audio chunks and note events share the same timeline — eliminating any possibility of drift on hardware too.

---

## Features

- **88-key piano rendering** with white and black keys, drawn on a Canvas
- **Gaussian glow splash** — each note lights up its neighbouring LEDs with a bell-curve falloff, so chords create beautiful overlapping halos
- **Additive blending** — simultaneous notes accumulate brightness, just like real LEDs would
- **Per-frame decay** — LEDs fade out naturally without needing explicit note-off handling
- **MIDI track selector** — Format 1 MIDI files have multiple tracks; you can pick which one drives the LEDs
- **5 color modes** — Warm Amber, Octave Rainbow, Velocity Heat, Ice Blue, Pure White
- **Tunable parameters** — Glow Sigma (spread width) and Decay (fade speed) are adjustable live
- **DEMO mode** — fires random notes so you can preview effects without any files loaded

---

## Usage

Open `piano-led-simulator.html` in any modern browser (Chrome, Firefox, Edge, Safari).

1. Click **▶ Load MP3** and pick your audio file
2. Click **♩ Load MIDI** and pick the matching MIDI file
3. A **track selector** appears — pick the track with the most note events (usually the melody or full piano part)
4. Press **Play** or hit `Space`
5. Adjust **Glow Sigma**, **Decay**, and **Color** in real time to tune the effect

Keyboard shortcuts: `Space` to play/pause, `Esc` to stop.

---

## Project Architecture

This is part of a larger ESP32 hardware project. The full system architecture is:

```
PC (prep tool, runs once)
  MP3 file  ──┐
              ├──► Python encoder ──► song.lmp  (custom container)
  MIDI file ──┘

ESP32 (runtime)
  SD card (song.lmp)
    ├── Audio chunks   ──► I2S DAC ──► Stereo speaker
    └── Note events    ──► Effects engine ──► WS2812B LED strip (88 LEDs)
```

The `.lmp` (Light Music Package) format interleaves stereo PCM audio chunks and note events in chronological order. The ESP32 reads the file sequentially — when it hits an audio chunk it feeds the DAC, when it hits a note event it fires the LED effects engine. Since position in the file *is* the timestamp, sync is structural rather than managed at runtime.

### Effects Engine (Layer 3)

The effects engine is deliberately decoupled from the playback and file-reading layers. It receives only `triggerNote(note, velocity)` and `releaseNote(note)` calls, and maintains its own 88-element brightness array that it renders at ~60fps. This makes it easy to swap in new visual effects without touching any audio or MIDI logic.

---

## Hardware Target

| Component | Purpose |
|---|---|
| ESP32 (dual-core, 240MHz) | Main controller |
| I2S DAC (e.g. MAX98357A) | Stereo audio output |
| WS2812B LED strip (88 LEDs) | One LED per piano key |
| MicroSD card | Stores the `.lmp` file |

Core 0 handles the audio stream to the I2S DAC. Core 1 runs the MIDI event reader and the LED effects engine at 60fps. At that frame rate, Core 1 has ~16ms per frame to process events and write 264 bytes (88 × 3 RGB channels) to the strip — well within the ESP32's capabilities.

---

## Roadmap

- [ ] PC-side `.lmp` encoder (Python: merges MP3 + MIDI into the container format)
- [ ] ESP32 firmware (C++ / Arduino framework, dual-core task assignment)
- [ ] Additional effect modes (ripple, strobe on beat, sustain pedal support)
- [ ] Velocity-to-color mapping refinement

---

## License

MIT
