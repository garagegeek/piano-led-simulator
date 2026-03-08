#!/usr/bin/env python3
"""
Piano LED Simulator — Transcription Server
==========================================
Converts audio files to note events using Spotify's basic-pitch AI model,
then serves them to the browser simulator over a local HTTP API.

Requirements: Python 3.11  (basic-pitch needs TensorFlow 2.15 / Python ≤ 3.11)
Usage:
    py -3.11 server.py

API:
  GET  /ping         →  {"status": "ok", "model": "basic-pitch ICASSP 2022"}
  POST /transcribe   →  body:   raw audio bytes (MP3/WAV/FLAC/OGG)
                         header: X-Filename: song.mp3
                      ←  {"events": [...], "note_count": N}
                         events: [{time, type:"on"|"off", note, vel}, ...]
"""

import os, sys, json, tempfile, traceback, glob, socketserver
from http.server import HTTPServer, BaseHTTPRequestHandler

PORT = 8765

# ── Find ffmpeg (installed via winget) ───────────────────────────────────────
_ffmpeg = glob.glob(
    r'C:\Users\*\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin')
if _ffmpeg:
    os.environ['PATH'] = _ffmpeg[0] + os.pathsep + os.environ.get('PATH', '')

# ── Load basic-pitch model once at startup ────────────────────────────────────
print("\nPiano LED Simulator — Transcription Server")
print("==========================================")
print("Loading basic-pitch model...")

from basic_pitch.inference import predict, Model
from basic_pitch import ICASSP_2022_MODEL_PATH

MODEL = Model(ICASSP_2022_MODEL_PATH)
print(f"OK - Model ready  (port {PORT})\n")


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    """Handle each request in a separate thread so /ping works during transcription."""
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  {fmt % args}")

    def _cors(self):
        self.send_header('Access-Control-Allow-Origin',  '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, X-Filename')
        self.send_header('Access-Control-Expose-Headers', 'X-Note-Count')

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.startswith('/ping'):
            body = json.dumps({
                'status': 'ok',
                'model':  'basic-pitch ICASSP 2022'
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._cors()
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != '/transcribe':
            self.send_response(404)
            self.end_headers()
            return

        content_length = int(self.headers.get('Content-Length', 0))
        filename       = self.headers.get('X-Filename', 'audio.mp3')
        audio_bytes    = self.rfile.read(content_length)

        print(f"Transcribing: {filename}  ({len(audio_bytes) // 1024} KB)")

        try:
            ext = os.path.splitext(filename)[1] or '.mp3'
            with tempfile.TemporaryDirectory() as tmpdir:
                audio_path = os.path.join(tmpdir, f'input{ext}')
                with open(audio_path, 'wb') as f:
                    f.write(audio_bytes)

                _, midi_data, _ = predict(audio_path, MODEL)

            # Convert pretty_midi notes → simulator event format
            events = []
            for instrument in midi_data.instruments:
                for note in instrument.notes:
                    if 21 <= note.pitch <= 108:   # clamp to 88-key piano range
                        events.append({
                            'time': round(float(note.start), 6),
                            'type': 'on',
                            'note': int(note.pitch),
                            'vel':  int(note.velocity)
                        })
                        events.append({
                            'time': round(float(note.end), 6),
                            'type': 'off',
                            'note': int(note.pitch),
                            'vel':  0
                        })

            events.sort(key=lambda e: (e['time'], 0 if e['type'] == 'off' else 1))
            note_count = sum(1 for e in events if e['type'] == 'on')
            print(f"OK - {note_count} notes detected")

            body = json.dumps({'events': events, 'note_count': note_count}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.send_header('X-Note-Count', str(note_count))
            self._cors()
            self.end_headers()
            self.wfile.write(body)

        except Exception as exc:
            traceback.print_exc()
            body = json.dumps({'error': str(exc)}).encode()
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self._cors()
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)


if __name__ == '__main__':
    server = ThreadingHTTPServer(('localhost', PORT), Handler)
    print(f"Listening at http://localhost:{PORT}")
    print(f"Open piano-led-simulator.html and click 🎵 Transcribe.")
    print(f"Press Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
