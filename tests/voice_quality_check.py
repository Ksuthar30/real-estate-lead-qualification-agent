import argparse
import base64
import json
import os
import re
import struct
import wave
from pathlib import Path
from urllib import request as urlrequest

from dotenv import load_dotenv


REFERENCE_TEXT = (
    "Hi, this is Riya from Proviso Group. "
    "You had a property enquiry. Is this a good time?"
)


DEFAULT_CANDIDATES = [
    {
        "name": "v2_anushka_en_082",
        "model": "bulbul:v2",
        "speaker": "anushka",
        "language": "en-IN",
        "pace": 0.82,
    },
    {
        "name": "v2_anushka_en_074",
        "model": "bulbul:v2",
        "speaker": "anushka",
        "language": "en-IN",
        "pace": 0.74,
    },
    {
        "name": "v2_anushka_hi_082",
        "model": "bulbul:v2",
        "speaker": "anushka",
        "language": "hi-IN",
        "pace": 0.82,
    },
    {
        "name": "v3_ritu_en_082",
        "model": "bulbul:v3",
        "speaker": "ritu",
        "language": "en-IN",
        "pace": 0.82,
    },
    {
        "name": "v3_ritu_hi_082",
        "model": "bulbul:v3",
        "speaker": "ritu",
        "language": "hi-IN",
        "pace": 0.82,
    },
]


IMPORTANT_WORDS = {
    "riya",
    "proviso",
    "group",
    "property",
    "enquiry",
    "good",
    "time",
}

WORD_ALIASES = {
    "ria": "riya",
    "reya": "riya",
    "riyaa": "riya",
    "provisor": "proviso",
    "provisogroup": "proviso",
    "inquiry": "enquiry",
    "enquiries": "enquiry",
}

def _normalize_words(text: str) -> set[str]:
    words = set()
    for word in re.findall(r"[a-z0-9]+", text.casefold()):
        words.add(WORD_ALIASES.get(word, word))
    return words


def _word_overlap(transcript: str) -> float:
    got = _normalize_words(transcript)
    if not IMPORTANT_WORDS:
        return 0.0
    return len(IMPORTANT_WORDS & got) / len(IMPORTANT_WORDS)


def _max_internal_gap_ms() -> int:
    return int(os.getenv("VOICE_GATE_MAX_INTERNAL_GAP_MS", "700"))


def _read_wav_channel(path: Path, channel: int | None = None) -> tuple[int, list[int]]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())

    if sample_width != 2:
        raise ValueError(f"{path} uses {sample_width}-byte samples; only 16-bit PCM WAV is supported")

    samples = struct.unpack("<" + "h" * (len(frames) // 2), frames)
    if channels > 1:
        selected = channel if channel is not None else 0
        samples = samples[selected::channels]
    return sample_rate, list(samples)


def analyze_wav(path: Path, *, channel: int | None = None) -> dict:
    sample_rate, samples = _read_wav_channel(path, channel)
    if not samples:
        raise ValueError(f"{path} has no audio samples")

    window = max(1, int(sample_rate * 0.05))
    rms_values: list[float] = []
    peaks: list[int] = []
    for start in range(0, len(samples) - window + 1, window):
        segment = samples[start : start + window]
        rms = (sum(value * value for value in segment) / len(segment)) ** 0.5
        rms_values.append(rms)
        peaks.append(max(abs(value) for value in segment))

    sorted_rms = sorted(rms_values)
    noise_floor = sorted_rms[max(0, int(len(sorted_rms) * 0.2) - 1)] if sorted_rms else 0
    threshold = max(120.0, noise_floor * 3.0)
    active = [rms > threshold for rms in rms_values]
    active_indices = [idx for idx, is_active in enumerate(active) if is_active]

    max_internal_gap_ms = 0
    if active_indices:
        first, last = active_indices[0], active_indices[-1]
        current_gap = 0
        for is_active in active[first : last + 1]:
            if is_active:
                max_internal_gap_ms = max(max_internal_gap_ms, current_gap * 50)
                current_gap = 0
            else:
                current_gap += 1
        max_internal_gap_ms = max(max_internal_gap_ms, current_gap * 50)

    peak = max(abs(value) for value in samples)
    duration = len(samples) / sample_rate
    max_allowed_internal_gap_ms = _max_internal_gap_ms()
    return {
        "file": str(path),
        "sample_rate": sample_rate,
        "duration_sec": round(duration, 2),
        "peak": peak,
        "clipping_risk": peak >= 32000,
        "noise_floor_rms": round(noise_floor, 1),
        "active_threshold_rms": round(threshold, 1),
        "max_internal_gap_ms": max_internal_gap_ms,
        "max_allowed_internal_gap_ms": max_allowed_internal_gap_ms,
        "dropout_pass": max_internal_gap_ms <= max_allowed_internal_gap_ms,
        "clipping_pass": peak < 32000,
    }


def synthesize_sarvam(candidate: dict, text: str, output_dir: Path) -> Path:
    api_key = os.getenv("SARVAM_API_KEY")
    if not api_key:
        raise RuntimeError("SARVAM_API_KEY is missing")

    payload = {
        "target_language_code": candidate["language"],
        "text": text,
        "speaker": candidate["speaker"],
        "pace": candidate["pace"],
        "model": candidate["model"],
        "speech_sample_rate": 22050,
        "output_audio_codec": "wav",
        "output_audio_bitrate": "128k",
        "enable_preprocessing": True,
    }
    if candidate["model"] == "bulbul:v2":
        payload["loudness"] = 0.9
        payload["pitch"] = 0.0
    else:
        payload["temperature"] = 0.35
        payload["min_buffer_size"] = 120
        payload["max_chunk_length"] = 220

    req = urlrequest.Request(
        "https://api.sarvam.ai/text-to-speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-subscription-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urlrequest.urlopen(req, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))

    audios = body.get("audios") or []
    if not audios:
        raise RuntimeError(f"Sarvam returned no audio for {candidate['name']}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{candidate['name']}.wav"
    output_path.write_bytes(base64.b64decode(audios[0]))
    return output_path


def transcribe_with_deepgram(path: Path) -> str:
    from deepgram import DeepgramClient

    api_key = os.getenv("DEEPGRAM_API_KEY")
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY is missing")

    client = DeepgramClient(api_key=api_key)
    response = client.listen.v1.media.transcribe_file(
        request=path.read_bytes(),
        model="nova-2",
        language="en-IN",
        punctuate=True,
        smart_format=True,
    )
    data = response.model_dump() if hasattr(response, "model_dump") else response
    channels = data.get("results", {}).get("channels", [])
    if not channels:
        return ""
    alternative = (channels[0].get("alternatives") or [{}])[0]
    return alternative.get("transcript") or ""


def evaluate_file(path: Path, *, channel: int | None, transcribe: bool) -> dict:
    metrics = analyze_wav(path, channel=channel)
    transcript = ""
    overlap = None
    if transcribe:
        transcript = transcribe_with_deepgram(path)
        overlap = _word_overlap(transcript)
    metrics.update(
        {
            "transcript": transcript,
            "word_overlap": overlap,
            "transcript_pass": True if overlap is None else overlap >= 0.85,
        }
    )
    metrics["pass"] = (
        metrics["dropout_pass"]
        and metrics["clipping_pass"]
        and metrics["transcript_pass"]
    )
    return metrics


def _candidate_for_file(path: Path) -> dict | None:
    stem = path.stem.casefold()
    for candidate in DEFAULT_CANDIDATES:
        if candidate["name"].casefold() == stem:
            return candidate
    return None


def evaluate_directory(path: Path, *, channel: int | None, transcribe: bool) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(path)

    ordered_paths: list[Path] = []
    for candidate in DEFAULT_CANDIDATES:
        sample_path = path / f"{candidate['name']}.wav"
        if sample_path.exists():
            ordered_paths.append(sample_path)

    known = {sample.resolve() for sample in ordered_paths}
    for sample_path in sorted(path.glob("*.wav")):
        if sample_path.resolve() not in known:
            ordered_paths.append(sample_path)

    results = []
    for sample_path in ordered_paths:
        metrics = evaluate_file(sample_path, channel=channel, transcribe=transcribe)
        candidate = _candidate_for_file(sample_path)
        if candidate:
            metrics["candidate"] = candidate
        results.append(metrics)
    return results


def main() -> int:
    load_dotenv(".env")
    for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        if os.getenv(proxy_name, "").strip().startswith("http://127.0.0.1:9"):
            os.environ.pop(proxy_name, None)

    parser = argparse.ArgumentParser(description="Generate and validate phone-call TTS samples.")
    parser.add_argument("--text", default=REFERENCE_TEXT)
    parser.add_argument("--output-dir", default="voice_samples")
    parser.add_argument("--transcribe", action="store_true", help="Use Deepgram to validate sample intelligibility.")
    parser.add_argument("--fail-on-no-pass", action="store_true")
    parser.add_argument("--analyze-file", help="Analyze an existing WAV instead of generating Sarvam samples.")
    parser.add_argument("--analyze-dir", help="Analyze existing WAV samples in a directory without regenerating them.")
    parser.add_argument("--channel", type=int, help="Channel to analyze for stereo WAVs. Defaults to channel 0.")
    args = parser.parse_args()

    if args.analyze_file:
        result = evaluate_file(Path(args.analyze_file), channel=args.channel, transcribe=args.transcribe)
        print(json.dumps({"mode": "analyze_file", "result": result}, indent=2))
        return 0 if result["pass"] or not args.fail_on_no_pass else 1

    if args.analyze_dir:
        results = evaluate_directory(Path(args.analyze_dir), channel=args.channel, transcribe=args.transcribe)
        passing = [item for item in results if item.get("pass")]
        selected = passing[0].get("candidate") if passing else None
        print(json.dumps({"mode": "analyze_dir", "selected": selected, "results": results}, indent=2))
        return 0 if selected or not args.fail_on_no_pass else 1

    output_dir = Path(args.output_dir)
    results = []
    for candidate in DEFAULT_CANDIDATES:
        try:
            sample_path = synthesize_sarvam(candidate, args.text, output_dir)
            metrics = evaluate_file(sample_path, channel=None, transcribe=args.transcribe)
            metrics["candidate"] = candidate
            results.append(metrics)
        except Exception as exc:
            results.append({"candidate": candidate, "pass": False, "error": str(exc)})

    passing = [item for item in results if item.get("pass")]
    selected = passing[0]["candidate"] if passing else None
    print(json.dumps({"mode": "generate", "selected": selected, "results": results}, indent=2))
    return 0 if selected or not args.fail_on_no_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
