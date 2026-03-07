#!/usr/bin/env python3
"""
encode_lmp.py — Piano LED Show Encoder
=======================================
Merges an MP3 (or WAV) audio file and a MIDI file into a single .lmp
(Light Music Package) container. The ESP32 firmware streams this file
sequentially: audio chunks feed the I2S DAC, note events feed the LED
effects engine. Because everything shares one timeline (position in file
= timestamp), audio and LEDs are structurally in sync — no clock drift
is possible.

Usage:
    python encode_lmp.py song.mp3 song.mid output.lmp
    python encode_lmp.py song.mp3 song.mid output.lmp --track 1
    python encode_lmp.py song.mp3 song.mid output.lmp --list-tracks
    python encode_lmp.py song.mp3 song.mid output.lmp --sample-rate 22050

Dependencies:
    pip install mido pydub numpy
    Also requires ffmpeg installed on your system for MP3 decoding.
    Install ffmpeg: https://ffmpeg.org/download.html
"""

import argparse
import struct
import sys
import os
import numpy as np

try:
    import mido
except ImportError:
    sys.exit("Missing dependency: pip install mido")

import subprocess


# ─────────────────────────────────────────────────────────────────────────────
#  FILE FORMAT SPECIFICATION
#
#  The .lmp file is a simple sequential binary stream. There are no seek
#  tables or random-access structures — it is designed to be read front-to-back
#  on an embedded device with minimal RAM.
#
#  FILE HEADER (12 bytes):
#    [4]  magic      = b'LMP1'
#    [4]  sample_rate  (uint32 big-endian)  e.g. 44100
#    [4]  total_samples (uint32 big-endian) total stereo sample pairs
#
#  CHUNK TYPES (variable length, appear in chronological order):
#    AUDIO  (type 0x01):
#      [1]  type = 0x01
#      [2]  num_samples (uint16 big-endian) — number of stereo sample PAIRS
#      [N]  interleaved int16 PCM: L0 R0 L1 R1 ... (N * 4 bytes total)
#
#    NOTE ON (type 0x02):
#      [1]  type = 0x02
#      [1]  note index 0–87  (MIDI note minus 21, so A0=0, C8=87)
#      [1]  velocity 0–127
#
#    NOTE OFF (type 0x03):
#      [1]  type = 0x03
#      [1]  note index 0–87
# ─────────────────────────────────────────────────────────────────────────────

CHUNK_TYPE_AUDIO    = 0x01
CHUNK_TYPE_NOTE_ON  = 0x02
CHUNK_TYPE_NOTE_OFF = 0x03

AUDIO_CHUNK_SAMPLES = 512   # stereo sample pairs per audio chunk (~11.6ms at 44100Hz)
MAGIC               = b'LMP1'


# ─────────────────────────────────────────────────────────────────────────────
#  MIDI PARSER
#
#  We parse the MIDI file ourselves rather than relying solely on mido's
#  higher-level abstractions. This gives us full control over tempo handling,
#  which is the most important part of getting the timing right.
#
#  Key concept: MIDI stores time as "delta ticks". Each event says "fire me
#  N ticks after the previous event in this track." We accumulate these into
#  absolute tick positions, then convert to real seconds using the tempo map.
#
#  The tempo map is a sorted list of (tick, microseconds_per_beat) entries.
#  Between any two entries, the tempo is constant, so we can convert ticks to
#  seconds with simple arithmetic within each segment.
# ─────────────────────────────────────────────────────────────────────────────

def parse_midi(path):
    """
    Parse a MIDI file and return a list of tracks, each containing
    time-stamped note events in seconds.

    Returns:
        {
          'format': int,
          'tracks': [
            {
              'name': str,
              'note_count': int,
              'events': [{'time': float, 'type': 'on'|'off', 'note': int, 'vel': int}]
            },
            ...
          ]
        }
    """
    mid = mido.MidiFile(path)
    tpb = mid.ticks_per_beat

    # Step 1: Collect all tempo change events across ALL tracks.
    # In Format 1 MIDI, tempo events live in the conductor track (track 0)
    # but logically apply to every track simultaneously.
    raw_tempo_map = []   # list of (absolute_tick, microseconds_per_beat)

    for track in mid.tracks:
        abs_tick = 0
        for msg in track:
            abs_tick += msg.time   # msg.time is the delta tick
            if msg.type == 'set_tempo':
                raw_tempo_map.append((abs_tick, msg.tempo))

    # Sort by tick and ensure there's always a segment at tick 0.
    raw_tempo_map.sort(key=lambda x: x[0])
    if not raw_tempo_map or raw_tempo_map[0][0] > 0:
        raw_tempo_map.insert(0, (0, 500000))  # default: 120 BPM

    # Step 2: Pre-compute the start time in seconds for each tempo segment.
    # This is a "prefix sum" over the tempo timeline. Each segment inherits
    # the end time of the previous segment as its start time.
    tempo_segs = []
    for i, (tick, tempo) in enumerate(raw_tempo_map):
        if i == 0:
            start_sec = 0.0
        else:
            prev_tick, prev_tempo, prev_start = (
                raw_tempo_map[i-1][0], tempo_segs[i-1]['tempo'], tempo_segs[i-1]['start_sec'])
            start_sec = prev_start + ((tick - prev_tick) / tpb) * (prev_tempo / 1e6)
        tempo_segs.append({'tick': tick, 'tempo': tempo, 'start_sec': start_sec})

    def ticks_to_secs(tick):
        """Convert an absolute tick position to real seconds using the tempo map."""
        # Find the last segment that starts at or before this tick.
        seg = tempo_segs[0]
        for s in tempo_segs:
            if s['tick'] <= tick:
                seg = s
            else:
                break
        return seg['start_sec'] + ((tick - seg['tick']) / tpb) * (seg['tempo'] / 1e6)

    # Step 3: Parse each track's note events and convert their times to seconds.
    result_tracks = []
    for t_idx, track in enumerate(mid.tracks):
        name = track.name.strip() if track.name.strip() else f'Track {t_idx}'
        events = []
        abs_tick = 0

        for msg in track:
            abs_tick += msg.time
            if msg.type == 'note_on' and msg.velocity > 0:
                events.append({
                    'time': ticks_to_secs(abs_tick),
                    'type': 'on',
                    'note': msg.note,
                    'vel':  msg.velocity
                })
            elif msg.type == 'note_off' or (msg.type == 'note_on' and msg.velocity == 0):
                events.append({
                    'time': ticks_to_secs(abs_tick),
                    'type': 'off',
                    'note': msg.note,
                    'vel':  0
                })

        events.sort(key=lambda e: e['time'])
        note_count = sum(1 for e in events if e['type'] == 'on')
        result_tracks.append({'name': name, 'note_count': note_count, 'events': events})

    return {'format': mid.type, 'tracks': result_tracks}


# ─────────────────────────────────────────────────────────────────────────────
#  AUDIO LOADER
#
#  pydub wraps ffmpeg and can decode virtually any audio format (MP3, WAV,
#  FLAC, OGG, AAC...) into raw PCM samples. We convert to the target sample
#  rate and ensure stereo output so the I2S DAC always gets L+R channels.
# ─────────────────────────────────────────────────────────────────────────────

def load_audio(path, target_sample_rate):
    """
    Load an audio file and return a numpy array of shape (2, N) containing
    stereo int16 samples, along with the actual sample rate used.
    Uses ffmpeg directly to decode any audio format to raw PCM.
    """
    print(f"  Loading audio: {path}")
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-i', path,
        '-ar', str(target_sample_rate),
        '-ac', '2',
        '-f', 's16le',
        'pipe:1'
    ]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        sys.exit(f"ffmpeg error: {result.stderr.decode()}")

    samples = np.frombuffer(result.stdout, dtype=np.int16)
    stereo = samples.reshape(-1, 2).T

    duration = stereo.shape[1] / target_sample_rate
    print(f"  Audio: {stereo.shape[1]} samples, {duration:.2f}s, {target_sample_rate} Hz stereo")
    return stereo, target_sample_rate


# ─────────────────────────────────────────────────────────────────────────────
#  ENCODER
#
#  This is the heart of the tool. The algorithm walks through the audio in
#  fixed-size chunks. Before writing each audio chunk, it checks the note
#  event list for any events whose sample position falls within the upcoming
#  chunk. Those events are written first — so the ESP32 encounters the note
#  event just before the audio samples it corresponds to, firing the LED
#  at exactly the right moment in the stream.
#
#  Think of it like inserting bookmarks into a book before the page they
#  refer to, so a front-to-back reader always hits the bookmark first.
# ─────────────────────────────────────────────────────────────────────────────

def encode_lmp(stereo_audio, sample_rate, note_events, output_path):
    """
    Interleave stereo PCM audio chunks and note events into an .lmp file.

    stereo_audio: numpy array shape (2, N) of int16 samples
    sample_rate:  int, e.g. 44100
    note_events:  list of {'time': float, 'type': 'on'|'off', 'note': int, 'vel': int}
    output_path:  str, path to write the .lmp file
    """
    total_samples = stereo_audio.shape[1]
    duration      = total_samples / sample_rate

    # Convert note event times (seconds) → sample positions.
    # We clamp to [0, total_samples - 1] to handle any rounding at the very end.
    events_by_sample = []
    for ev in note_events:
        sample_pos = int(ev['time'] * sample_rate)
        sample_pos = max(0, min(total_samples - 1, sample_pos))
        events_by_sample.append({**ev, 'sample_pos': sample_pos})

    # Sort by sample position. For events at the same sample, note-offs come
    # before note-ons so a key re-press registers cleanly.
    events_by_sample.sort(key=lambda e: (e['sample_pos'], 0 if e['type'] == 'off' else 1))

    notes_written = 0
    audio_chunks  = 0
    evt_idx       = 0  # pointer into events_by_sample
    total_events  = len(events_by_sample)

    print(f"\n  Encoding {total_samples} samples ({duration:.2f}s) + {total_events} note events")
    print(f"  Output: {output_path}")

    with open(output_path, 'wb') as f:

        # ── FILE HEADER ─────────────────────────────────────────────────
        f.write(MAGIC)
        f.write(struct.pack('>II', sample_rate, total_samples))

        # ── INTERLEAVED CHUNKS ──────────────────────────────────────────
        sample_pos = 0
        last_pct   = -1

        while sample_pos < total_samples:
            chunk_end = min(sample_pos + AUDIO_CHUNK_SAMPLES, total_samples)

            # Write all note events that occur before the end of this chunk.
            # "Before the end" means: fire the LED at the same moment the
            # corresponding audio samples start playing.
            while evt_idx < total_events and events_by_sample[evt_idx]['sample_pos'] < chunk_end:
                ev   = events_by_sample[evt_idx]
                note = ev['note'] - 21   # convert MIDI note to 0-87 index

                # Silently skip notes outside the 88-key piano range
                if 0 <= note <= 87:
                    if ev['type'] == 'on':
                        f.write(struct.pack('BBB', CHUNK_TYPE_NOTE_ON, note, ev['vel']))
                    else:
                        f.write(struct.pack('BB',  CHUNK_TYPE_NOTE_OFF, note))
                    notes_written += 1

                evt_idx += 1

            # Write the audio chunk.
            # We extract a slice of the stereo array, interleave L and R into
            # a single flat array (L0 R0 L1 R1 ...), then write as raw bytes.
            chunk    = stereo_audio[:, sample_pos:chunk_end]  # shape (2, chunk_size)
            num_samp = chunk_end - sample_pos

            # np.vstack gives us [[L0,L1,...],[R0,R1,...]], then .T gives
            # [[L0,R0],[L1,R1],...], then flatten gives L0 R0 L1 R1 ...
            interleaved = np.vstack(chunk).T.flatten().astype(np.int16)

            f.write(struct.pack('>BH', CHUNK_TYPE_AUDIO, num_samp))
            f.write(interleaved.tobytes())

            sample_pos   = chunk_end
            audio_chunks += 1

            # Progress indicator
            pct = int(sample_pos / total_samples * 100)
            if pct != last_pct and pct % 10 == 0:
                print(f"  {pct}%... ", end='', flush=True)
                last_pct = pct

    file_size = os.path.getsize(output_path)
    print(f"\n\n  ✓ Done!")
    print(f"  Audio chunks : {audio_chunks}")
    print(f"  Note events  : {notes_written} (of {total_events} parsed)")
    print(f"  File size    : {file_size / 1024 / 1024:.2f} MB")
    print(f"  Output       : {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
#  VERIFICATION (optional but very useful for debugging)
#
#  After encoding, we can do a quick sanity check by reading the .lmp file
#  back and verifying the first few events appear at reasonable times.
#  This catches any byte-swapping or off-by-one errors in the encoder.
# ─────────────────────────────────────────────────────────────────────────────

def verify_lmp(path):
    """
    Read back the .lmp file and print a summary of the first 20 events,
    showing their reconstructed timestamps. This confirms the encoder placed
    events at the correct positions in the stream.
    """
    print("\n  Verifying output...")
    NOTE_NAMES = ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B']

    with open(path, 'rb') as f:
        magic = f.read(4)
        if magic != MAGIC:
            print(f"  ✗ Bad magic bytes: {magic!r}")
            return

        sample_rate, total_samples = struct.unpack('>II', f.read(8))
        print(f"  Header: {sample_rate} Hz, {total_samples} samples "
              f"({total_samples/sample_rate:.2f}s)")

        current_sample = 0
        event_count    = 0
        shown          = 0

        while True:
            type_byte = f.read(1)
            if not type_byte:
                break
            t = type_byte[0]

            if t == CHUNK_TYPE_AUDIO:
                num_samp, = struct.unpack('>H', f.read(2))
                f.read(num_samp * 4)   # skip PCM data (int16 × 2 channels × num_samp)
                current_sample += num_samp

            elif t == CHUNK_TYPE_NOTE_ON:
                note_idx, vel = struct.unpack('BB', f.read(2))
                midi_note = note_idx + 21
                name = NOTE_NAMES[midi_note % 12] + str(midi_note // 12 - 1)
                time_sec = current_sample / sample_rate
                if shown < 20:
                    print(f"    [{time_sec:7.3f}s]  NOTE ON   {name:4s}  vel={vel}")
                    shown += 1
                event_count += 1

            elif t == CHUNK_TYPE_NOTE_OFF:
                note_idx, = struct.unpack('B', f.read(1))
                event_count += 1

            else:
                print(f"  ✗ Unknown chunk type: 0x{t:02X} at sample {current_sample}")
                break

        print(f"  Total note events in file: {event_count}")
        print(f"  Final sample position: {current_sample} (expected {total_samples})")


# ─────────────────────────────────────────────────────────────────────────────
#  CLI
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Encode MP3 + MIDI → .lmp (Light Music Package) for ESP32 piano LED strip',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    parser.add_argument('mp3',    help='Input audio file (MP3, WAV, FLAC, OGG, ...)')
    parser.add_argument('midi',   help='Input MIDI file (.mid)')
    parser.add_argument('output', help='Output .lmp file path')
    parser.add_argument('--track',       type=int, default=-1,
                        help='MIDI track index to encode (default: auto-select track with most notes)')
    parser.add_argument('--list-tracks', action='store_true',
                        help='List all MIDI tracks and exit (useful for finding the right track)')
    parser.add_argument('--sample-rate', type=int, default=44100,
                        help='Output sample rate in Hz (default: 44100; use 22050 to halve file size)')
    parser.add_argument('--no-verify',   action='store_true',
                        help='Skip verification step after encoding')
    args = parser.parse_args()

    print("\n═══════════════════════════════════════════")
    print("  Piano LED Show Encoder — .lmp v1")
    print("═══════════════════════════════════════════\n")

    # ── PARSE MIDI ──────────────────────────────────────────────────────────
    print(f"[1/3] Parsing MIDI: {args.midi}")
    midi_data = parse_midi(args.midi)
    tracks    = midi_data['tracks']

    print(f"  Format {midi_data['format']}, {len(tracks)} track(s)\n")
    for i, t in enumerate(tracks):
        marker = ' ◄ auto-select' if t['note_count'] == max(x['note_count'] for x in tracks) and args.track == -1 else ''
        print(f"  [{i}] {t['name']:<30s}  {t['note_count']:>5} note-on events{marker}")

    if args.list_tracks:
        print()
        sys.exit(0)

    # Select track
    if args.track >= 0:
        if args.track >= len(tracks):
            sys.exit(f"\nError: track {args.track} does not exist (file has {len(tracks)} tracks)")
        selected = tracks[args.track]
    else:
        selected = max(tracks, key=lambda t: t['note_count'])

    print(f"\n  Using track: [{tracks.index(selected)}] '{selected['name']}' "
          f"({selected['note_count']} events)\n")

    # ── LOAD AUDIO ──────────────────────────────────────────────────────────
    print(f"[2/3] Loading audio: {args.mp3}")
    stereo, sr = load_audio(args.mp3, args.sample_rate)

    # ── ENCODE ──────────────────────────────────────────────────────────────
    print(f"\n[3/3] Encoding → {args.output}")
    encode_lmp(stereo, sr, selected['events'], args.output)

    if not args.no_verify:
        verify_lmp(args.output)

    print("\n  Ready to copy to ESP32 SD card.\n")


if __name__ == '__main__':
    main()
