"""Exercise the real waveform matcher on synthetic recordings and clocks."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from media_analysis import alignment_module, timing_windows


class VideoTimingTests(unittest.TestCase):
    rate = 4000
    length = 240
    start = 12

    def setUp(self):
        self.rng = np.random.default_rng(801)
        self.audio = self.rng.normal(0, 1, self.length * self.rate)
        self.video = np.zeros((self.length + self.start + 4) * self.rate)
        beginning = self.start * self.rate
        self.video[beginning:beginning + self.audio.size] = self.audio

    def measure(self, speech=None):
        def decode(source, start, seconds, *, band=None):
            samples = self.audio if source == "music" else self.video
            if source == "video" and speech is not None and band is None:
                samples = speech
            first = round(start * self.rate)
            return samples[first:first + round(seconds * self.rate)]

        song = {"duration": self.length, "chapter": {"start": self.start, "evidence": "audioMatched"}}
        with patch("media_analysis.decoded_window", side_effect=decode) as reads:
            evidence = timing_windows(song, "music", "video", alignment_module())
        return evidence, reads.call_count

    def test_clear_unit_rate_keeps_the_existing_fast_path(self):
        evidence, reads = self.measure()
        self.assertEqual(evidence["status"], "measured")
        self.assertEqual(reads, 6)

    def add_competing_phrases(self):
        for at, offset in ((0.08, -1.8), (0.14, 1.6)):
            music_start = round(at * self.length * self.rate)
            phrase = self.audio[music_start:music_start + 5 * self.rate]
            expected = music_start + self.start * self.rate
            competitor = expected + round(offset * self.rate)
            self.video[expected:expected + phrase.size] *= 0.55
            self.video[competitor:competitor + phrase.size] += phrase

    def test_separate_equal_clock_samples_resolve_two_repeated_phrase_peaks(self):
        self.add_competing_phrases()
        evidence, reads = self.measure()
        self.assertEqual(evidence["status"], "measured")
        self.assertEqual(len(evidence["windows"]), 3)
        self.assertTrue(all(abs(row["offsetMS"]) <= 1 for row in evidence["windows"]))
        self.assertEqual(reads, 24)

    def displace_video_section(self, first_second, last_second):
        first, last = first_second * self.rate, last_second * self.rate
        phrase = self.audio[first:last]
        expected = self.start * self.rate + first
        shifted = expected + round(1.6 * self.rate)
        self.video[expected:shifted + phrase.size] = 0
        self.video[shifted:shifted + phrase.size] = phrase

    def test_real_edit_vetoes_otherwise_aligned_alternate_windows(self):
        # The original early window is displaced while all three alternate
        # windows remain on the nominal clock.
        self.displace_video_section(32, 41)
        evidence, reads = self.measure()
        self.assertEqual(evidence["status"], "review")
        self.assertEqual(reads, 12)

    def test_real_edit_in_holdout_cannot_be_skipped_to_pass(self):
        self.add_competing_phrases()
        self.displace_video_section(51, 61)
        evidence, reads = self.measure()
        self.assertEqual(evidence["status"], "review")
        self.assertEqual(reads, 24)

    def test_split_band_fallback_requires_a_confirmed_shared_clock(self):
        speech = self.rng.normal(0, 1, self.video.shape)
        evidence, _ = self.measure(speech=speech)
        self.assertEqual(evidence.get("method"), "splitBands")
        self.assertEqual(len(evidence["windows"]), 6)
        self.assertTrue(all(abs(item["offsetMS"]) <= 1 for item in evidence["windows"]))

    def test_uniform_drift_and_indistinguishable_repeats_remain_unverified(self):
        for kind in ("drift", "repeated"):
            with self.subTest(kind=kind):
                if kind == "drift":
                    stretched_indices = np.arange(round(self.audio.size * 1.0025)) / 1.0025
                    track = np.interp(stretched_indices, np.arange(self.audio.size), self.audio)
                else:
                    self.audio = np.tile(self.audio[:2 * self.rate], self.length // 2)
                    track = self.audio
                self.video.fill(0)
                beginning = self.start * self.rate
                self.video[beginning:beginning + track.size] = track
                evidence, _ = self.measure()
                self.assertEqual(evidence["status"], "review")


if __name__ == "__main__":
    unittest.main()
