"""Moteur whisper.cpp (carte graphique via Vulkan), testé avec un faux whisper-server."""

from __future__ import annotations

import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tiktok_search import pipeline, transcribe, whispercpp
from tiktok_search.config import Settings

pytest.importorskip("av")

FAKE_SERVER = textwrap.dedent('''\
    #!{python}
    """Imite whisper-server : /health (503 pendant le chargement, puis 200), POST /inference."""
    import email.parser, email.policy, json, sys, time, wave, io
    from http.server import BaseHTTPRequestHandler, HTTPServer
    args = sys.argv[1:]
    opt = lambda k: args[args.index(k) + 1]
    port, lang, model = int(opt("--port")), opt("-l"), opt("-m")
    open(model, "rb").read(1)
    started = time.monotonic()
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        def do_GET(self):
            if self.path == "/health":
                ready = time.monotonic() - started > 0.6
                self._json(200 if ready else 503, {{"status": "ok" if ready else "loading model"}})
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\\r\\n\\r\\n" + raw)
            form = {{p.get_param("name", header="content-disposition"): p.get_payload(decode=True) for p in msg.iter_parts()}}
            with wave.open(io.BytesIO(form["file"])) as w:
                assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
                dur = w.getnframes() / 16000
            assert form["response_format"] == b"verbose_json"
            self._json(200, {{"duration": dur, "text": " bonjour tout le monde", "segments": [
                {{"id": 0, "text": " bonjour", "start": 0.0, "end": 1.2}},
                {{"id": 1, "text": " bonjour", "start": 1.2, "end": 1.5}},
                {{"id": 2, "text": " tout le monde ({{}})".format(form["language"].decode()), "start": 1.5, "end": round(dur, 2)}}]}})
    HTTPServer(("127.0.0.1", port), H).serve_forever()
''')


@pytest.fixture
def server_dir(tmp_path):
    d = tmp_path / "whisper-vulkan" / "whisper-vulkan"  # le .zip peut contenir un sous-dossier
    d.mkdir(parents=True)
    exe = d / whispercpp.EXE
    exe.write_text(FAKE_SERVER.format(python=sys.executable))
    exe.chmod(0o755)
    yield tmp_path / "whisper-vulkan"
    whispercpp.shutdown()


@pytest.fixture
def video(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg nécessaire pour fabriquer la vidéo de test")
    out = tmp_path / "v.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                    "-f", "lavfi", "-i", "color=c=blue:s=320x568:d=2", "-c:v", "libx264", "-c:a", "aac", "-ac", "2",
                    "-ar", "44100", "-shortest", str(out)], check=True)
    return out


@pytest.fixture
def settings(tmp_path, server_dir):
    s = Settings(data_dir=tmp_path / "data")
    s.data_dir.mkdir()
    (s.data_dir / "models").mkdir()
    (s.data_dir / "models" / "model.bin").write_bytes(b"ggml")
    s.whispercpp_dir, s.whispercpp_model = str(server_dir), str(s.data_dir / "models" / "model.bin")
    s.whisper_engine, s.whisper_lang = "auto", ""
    return s


@pytest.mark.skipif(sys.platform == "win32", reason="faux serveur en script Unix")
def test_transcription_on_gpu_server(settings, video, tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_whispercpp_broken", "")
    assert transcribe.engine(settings) == "whispercpp" and transcribe.available(settings)
    assert "carte graphique" in transcribe.engine_label(settings)
    segs = transcribe.transcribe_for(settings, video, tmp_path / "work")
    assert segs == [{"start": 0.0, "end": 1.2, "text": "bonjour"},
                    {"start": 1.5, "end": 2.0, "text": "tout le monde (auto)"}]  # doublon retiré, langue auto
    assert not list((tmp_path / "work").glob("*.wav"))  # WAV temporaire supprimé
    # Le serveur reste lancé : la vidéo suivante ne recharge pas le modèle.
    proc = whispercpp._server.proc
    transcribe.transcribe_for(settings, video, tmp_path / "work")
    assert whispercpp._server.proc is proc and proc.poll() is None


def test_falls_back_to_cpu_when_server_cannot_start(settings, video, tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_whispercpp_broken", "")
    exe = whispercpp.find_server(settings.whispercpp_dir)
    exe.write_text("#!/bin/sh\nexit 3\n")  # plante au démarrage (ex. pilote Vulkan absent)
    monkeypatch.setattr(transcribe, "is_available", lambda: True)
    monkeypatch.setattr(transcribe, "transcribe_segments", lambda *a: [{"start": 0, "end": 1, "text": "cpu"}])
    assert transcribe.transcribe_for(settings, video, tmp_path / "work") == [{"start": 0, "end": 1, "text": "cpu"}]
    assert transcribe.engine(settings) == "faster-whisper"  # on ne réessaie pas à chaque vidéo
    assert "whisper.cpp" in transcribe.engine_label(settings)


def test_engine_choice(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(transcribe, "_whispercpp_broken", "")
    settings.whisper_engine = "faster-whisper"
    assert transcribe.engine(settings) == "faster-whisper"
    settings.whisper_engine, settings.whispercpp_dir = "auto", str(tmp_path / "absent")
    assert transcribe.engine(settings) == "faster-whisper"


def test_wav_conversion_and_model_path(video, tmp_path):
    import wave

    wav = whispercpp.to_wav16k(video, tmp_path / "a.wav")
    with wave.open(str(wav)) as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)
        assert abs(w.getnframes() / 16000 - 2.0) < 0.1
    model = tmp_path / "m.bin"
    model.write_bytes(b"x")
    assert whispercpp.ensure_model(str(model), tmp_path / "models") == model
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "ggml-large-v3-turbo-q5_0.bin").write_bytes(b"x")
    assert whispercpp.ensure_model("large-v3-turbo-q5_0", tmp_path / "models").name == "ggml-large-v3-turbo-q5_0.bin"


def test_pipeline_uses_gpu_engine(settings, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(pipeline.transcribe, "transcribe_for", lambda s, p, w: calls.append(p) or [])
    monkeypatch.setattr(pipeline.transcribe, "is_available", lambda: False)  # faster-whisper absent
    monkeypatch.setattr(transcribe, "_whispercpp_broken", "")
    assert pipeline._media_needs(settings, False, False)[0] is True  # whisper.cpp suffit
