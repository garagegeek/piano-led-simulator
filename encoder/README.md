# LMP Encoder

Merges an MP3 (or WAV/FLAC/OGG) audio file and a MIDI file into a single `.lmp`
(Light Music Package) container for the ESP32 piano LED strip project.

## Why a single file?

If you keep the MP3 and MIDI as separate files and start them both playing at
the same time, they will eventually drift apart — even by a few milliseconds —
because they run on independent clocks. The `.lmp` format eliminates drift by
design: audio chunks and note events are interleaved in a single sequential
stream, so their relative timing is baked into the file's structure rather than
managed at runtime. The ESP32 just reads forward and reacts to whatever it
encounters next.

## Installation

```bash
cd encoder
pip install -r requirements.txt
```

You also need **ffmpeg** installed on your system for MP3 decoding.
- macOS: `brew install ffmpeg`
- Ubuntu/Debian: `sudo apt install ffmpeg`
- Windows: download from https://ffmpeg.org/download.html and add to PATH

## Usage

**Basic — auto-selects the MIDI track with the most note events:**
```bash
python encode_lmp.py song.mp3 song.mid output.lmp
```

**List all MIDI tracks first (recommended for multi-track files):**
```bash
python encode_lmp.py song.mp3 song.mid output.lmp --list-tracks
```

**Manually specify which track to encode:**
```bash
python encode_lmp.py song.mp3 song.mid output.lmp --track 1
```

**Reduce file size by halving the sample rate (saves ~50% space, some quality loss):**
```bash
python encode_lmp.py song.mp3 song.mid output.lmp --sample-rate 22050
```

## Output

After encoding, the tool automatically verifies the output file by reading it
back and printing the timestamps of the first 20 note events. This is a quick
sanity check — if the times look reasonable (matching the musical rhythm of the
piece), the file is good to copy to the SD card.

## File Format Reference

```
[4 bytes]  magic = "LMP1"
[4 bytes]  sample_rate  (uint32 big-endian)
[4 bytes]  total_samples (uint32 big-endian)

Then a sequential stream of chunks:

  AUDIO  (0x01):
    [1]  type = 0x01
    [2]  num_samples (uint16 BE)
    [N*4] stereo int16 PCM — interleaved L0 R0 L1 R1 ...

  NOTE ON  (0x02):
    [1]  type = 0x02
    [1]  note index 0–87  (MIDI note - 21)
    [1]  velocity 0–127

  NOTE OFF (0x03):
    [1]  type = 0x03
    [1]  note index 0–87
```

Note events always appear immediately *before* the audio chunk they correspond
to — so the ESP32 fires the LED event at the same moment those audio samples
begin playing.
