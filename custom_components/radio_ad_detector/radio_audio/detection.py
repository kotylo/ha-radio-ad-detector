"""Streaming decision logic; independent of inference and Home Assistant."""
from dataclasses import dataclass
import math


@dataclass
class Decision:
    ad_detected: bool
    reason: str
    speech: float
    music: float
    singing: float
    rms: float


class Detector:
    def __init__(self, config):
        self.threshold = float(config.get("speech_threshold", 0.5))
        self.singing_margin = float(config.get("singing_margin", 0.1))
        self.speech_windows = int(config.get("speech_windows", 2))
        self.non_music_threshold = float(config.get("non_music_threshold", 0.15))
        self.non_music_windows = int(config.get("non_music_windows", 6))
        self.clear_windows = int(config.get("clear_windows", 4))
        if not 0 < self.threshold <= 1 or not 0 <= self.non_music_threshold <= 1:
            raise ValueError("Thresholds must be within 0..1")
        if min(self.speech_windows, self.non_music_windows, self.clear_windows) < 1:
            raise ValueError("Window counts must be positive")
        self.speech_count = self.non_music_count = self.clear_count = 0
        self.active = False
        self.reason = "warming_up"

    def update(self, speech, music, singing, rms):
        if not all(math.isfinite(v) for v in (speech, music, singing, rms)):
            raise ValueError("Audio scores must be finite")
        # Music is multi-label: speech over a music bed must still trigger.
        is_speech = speech >= self.threshold and speech >= singing + self.singing_margin
        # Singing/rapping and digital silence should not be called non-music ads.
        is_non_music = music < self.non_music_threshold and singing < 0.25 and rms > 0.003
        self.speech_count = self.speech_count + 1 if is_speech else 0
        self.non_music_count = self.non_music_count + 1 if is_non_music else 0
        if self.speech_count >= self.speech_windows:
            self.active, self.reason = True, "speech"
            self.clear_count = 0
        elif self.non_music_count >= self.non_music_windows:
            self.active, self.reason = True, "non_music"
            self.clear_count = 0
        elif self.active:
            # Ambiguous audio holds the alarm; music or silence can clear it.
            clear = not is_speech and (music >= 0.35 or singing >= 0.35 or rms <= 0.003)
            self.clear_count = self.clear_count + 1 if clear else 0
            if self.clear_count >= self.clear_windows:
                self.active, self.reason = False, "music" if rms > 0.003 else "silence"
        else:
            self.reason = "speech_candidate" if is_speech else "music" if music >= 0.35 else "uncertain"
        return Decision(self.active, self.reason, speech, music, singing, rms)
