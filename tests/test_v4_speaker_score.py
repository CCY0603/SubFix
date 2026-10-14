"""speaker_score_db must discriminate a loud unit from a quiet one.

Regression test for the "分贝择优恒为 0" defect: with identical synthetic
audio, a unit covering speech-band energy must score clearly above a unit
covering silence, so cross-mic speaker arbitration can actually use the
value instead of every candidate tying at 0 dB.
"""

from array import array
import importlib.util
import math
from pathlib import Path
import wave


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("v4_score_tests", ROOT / "subfix_generate_v4.py")
v4 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(v4)


SAMPLE_RATE = 16000
FPS = 30.0


def _tone_samples(duration: float, amplitude: int, frequency: float = 220.0) -> array:
    count = round(duration * SAMPLE_RATE)
    return array(
        "h",
        (max(-32767, min(32767, int(amplitude * math.sin(2.0 * math.pi * frequency * i / SAMPLE_RATE))))
         for i in range(count)),
    )


def _write_wav(path: Path, sections: list[tuple[float, int]]) -> None:
    samples = array("h")
    for duration, amplitude in sections:
        samples.extend(_tone_samples(duration, amplitude))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(samples.tobytes())


def test_speaker_score_separates_loud_unit_from_silence(tmp_path):
    # 2s silence + 2s loud tone + 2s silence, window audio starts at timeline frame 100.
    audio = tmp_path / "score.wav"
    _write_wav(audio, [(2.0, 0), (2.0, 6000), (2.0, 0)])
    window_start = 100
    quiet_unit = {
        "text": "静",
        "start_frame": window_start + int(0.5 * FPS),
        "end_frame": window_start + int(1.5 * FPS),
    }
    loud_unit = {
        "text": "响",
        "start_frame": window_start + int(2.2 * FPS),
        "end_frame": window_start + int(3.8 * FPS),
    }
    scored = v4.score_aligned_units_from_audio(
        audio, [quiet_unit, loud_unit], window_start, FPS, alignment_coverage=1.0
    )
    quiet_db = float(scored[0]["speaker_score_db"])
    loud_db = float(scored[1]["speaker_score_db"])
    assert loud_db > 20.0, f"loud speech unit must score well above floor, got {loud_db}"
    assert loud_db - quiet_db > 18.0, f"score lost discrimination: loud={loud_db} quiet={quiet_db}"


def test_speaker_score_uses_timeline_frame_origin(tmp_path):
    # Audio starts at a non-zero timeline frame; offsets must be measured from
    # window_start_frame, not from absolute timeline zero.
    audio = tmp_path / "offset.wav"
    _write_wav(audio, [(1.0, 0), (1.0, 6000), (1.0, 0)])
    window_start = 9000  # 5 minutes in at 30fps
    loud_unit = {
        "text": "响",
        "start_frame": window_start + int(1.05 * FPS),
        "end_frame": window_start + int(1.95 * FPS),
    }
    scored = v4.score_aligned_units_from_audio(audio, [loud_unit], window_start, FPS, 1.0)
    assert float(scored[0]["speaker_score_db"]) > 20.0
