"""Optional: transcribe new recordings with faster-whisper (`pip install coursesync[transcribe]`).

With `videos: false` the video is removed once its transcript exists. Writes next to the video: <name>.transcript.json ({segments: [[start, end, text]], duration, language, model})
and <name>.transcript.md (readable, a timestamp every ~minute)."""

from __future__ import annotations

import json
import re
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


def _load_cuda_libs() -> None:
    """The `gpu` extra's CUDA libraries are not on the loader's path: load them so CTranslate2 finds them by name."""
    import ctypes
    import importlib.util
    for pkg, lib in (("nvidia.cublas", "libcublas.so.12"), ("nvidia.cudnn", "libcudnn.so.9")):
        spec = importlib.util.find_spec(pkg)
        if spec and spec.submodule_search_locations:
            ctypes.CDLL(str(Path(spec.submodule_search_locations[0]) / "lib" / lib), mode=ctypes.RTLD_GLOBAL)


def _whisper(audio, model: str, language: str | None) -> tuple[list, object, str, str]:
    from faster_whisper import WhisperModel
    options = dict(language=language, vad_filter=True, beam_size=5, condition_on_previous_text=False)
    try:
        _load_cuda_libs()
        segments, info = WhisperModel(model, device="cuda", compute_type="float16").transcribe(audio, **options)
        return list(segments), info, model, "cuda"  # segments are lazy: a CUDA problem only shows up here
    except Exception:  # no usable GPU: smaller model on CPU
        model = "medium" if model.startswith("large") else model
        segments, info = WhisperModel(model, device="cpu", compute_type="int8").transcribe(audio, **options)
        return list(segments), info, model, "cpu"


def transcribe(path: Path, model: str = "large-v3-turbo", language: str | None = None) -> Path:
    out = path.with_name(path.stem + ".transcript.json")
    if out.exists():
        return out
    segments, info, model, device = _whisper(_audio(path), model, language)
    segs = [[round(s.start, 2), round(s.end, 2), s.text.strip()] for s in segments]
    return save_transcript(path, segs, {"duration": info.duration, "language": info.language, "model": model,
                                        "device": device})


def seconds(stamp: str) -> float:
    parts = stamp.replace(",", ".").split(":")
    return round(sum(float(p) * 60 ** i for i, p in enumerate(reversed(parts))), 2)


def from_captions(path: Path) -> Path:
    """The transcript of <name>.mp4 from its captions <name>.vtt, which needs neither the video nor Whisper."""
    segs = []
    for cue in path.with_suffix(".vtt").read_text().split("\n\n"):
        lines = cue.strip().splitlines()
        at = next((i for i, line in enumerate(lines) if "-->" in line), None)
        text = " ".join(re.sub(r"<[^>]+>", "", line).strip() for line in lines[at + 1:]) if at is not None else ""
        if text:
            start, end = lines[at].split("-->")
            segs.append([seconds(start.strip()), seconds(end.split()[0]), text])
    return save_transcript(path, segs, {"duration": segs[-1][1] if segs else 0, "language": None, "model": "captions",
                                        "device": None})


def save_transcript(path: Path, segs: list, meta: dict) -> Path:
    out = path.with_name(path.stem + ".transcript.json")
    out.write_text(json.dumps({"segments": segs, **meta}))
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
