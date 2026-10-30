"""Optional: transcribe new recordings with faster-whisper (`pip install coursesync[transcribe]`).

With `videos: false` the video is removed once its transcript exists. Writes next to the video: <name>.transcript.json ({segments: [[start, end, text]], duration, language, model})
and <name>.transcript.md (readable, a timestamp every ~minute)."""

from __future__ import annotations

import json
from pathlib import Path


def _audio(path: Path):
    import av
    import numpy as np
    with av.open(str(path)) as c:
        stream = c.streams.audio[0]
        res = av.AudioResampler(format="flt", layout="mono", rate=16000)
        chunks = [f.to_ndarray().reshape(-1) for frame in c.decode(stream) for f in res.resample(frame)]
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype="float32")


def _fmt(t: float) -> str:
    t = int(t)
    return f"{t // 3600:02d}:{t % 3600 // 60:02d}:{t % 60:02d}"


def transcribe(path: Path, model: str = "large-v3-turbo", language: str | None = None) -> Path:
    from faster_whisper import WhisperModel
    out = path.with_name(path.stem + ".transcript.json")
    if out.exists():
        return out
    try:
        wm, device = WhisperModel(model, device="cuda", compute_type="float16"), "cuda"
    except Exception:  # no GPU: smaller model on CPU
        model = "medium" if model.startswith("large") else model
        wm, device = WhisperModel(model, device="cpu", compute_type="int8"), "cpu"
    segments, info = wm.transcribe(_audio(path), language=language, vad_filter=True, beam_size=5,
                                   condition_on_previous_text=False)
    segs = [[round(s.start, 2), round(s.end, 2), s.text.strip()] for s in segments]
    out.write_text(json.dumps({"segments": segs, "duration": info.duration, "language": info.language,
                               "model": model, "device": device}))
    lines, para, start = [f"# Transcript: {path.stem}", ""], [], None
    for s, _, text in segs:
        start = s if start is None else start
        para.append(text)
        if s - start >= 60:
            lines += [f"[{_fmt(start)}] " + " ".join(para), ""]
            para, start = [], None
    if para:
        lines += [f"[{_fmt(start)}] " + " ".join(para), ""]
    path.with_name(path.stem + ".transcript.md").write_text("\n".join(lines))
    return out
