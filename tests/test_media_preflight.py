"""Regressions for source timing and the creator's audio policy."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from media_analysis import balance_audio
from qa_preview import inspect_loudness
from ssroutine import chapter_preflight


def loudness_report(quiet=-25.0, peak=-1.0):
    return {
        "checks": {"audio": {"issues": [], "conditions": []}},
        "blockingIssues": [],
        "tracks": [
            {"section": "T1", "variant": "standard", "decoded": True, "durationMatches": True,
             "lufs": -16.0, "truePeakDBTP": peak},
            {"section": "T2", "variant": "standard", "decoded": True, "durationMatches": True,
             "lufs": quiet, "truePeakDBTP": -4.0},
            {"section": "T3", "variant": "standard", "decoded": True, "durationMatches": True,
             "lufs": -16.0, "truePeakDBTP": -1.5},
        ],
    }


class AudioPolicyTests(unittest.TestCase):
    def test_keep_music_preserves_relative_levels_and_still_rejects_excess_peaks(self):
        report = loudness_report(peak=0.4)
        inspect_loudness(report, None, keep_music=True)
        self.assertEqual(report["loudnessPolicy"], "keepOriginalDynamics")
        self.assertEqual(report["checks"]["audio"]["conditions"], [])
        self.assertEqual(len(report["blockingIssues"]), 1)
        self.assertIn("+0.4 dBTP", report["blockingIssues"][0])
        self.assertNotIn("loudnessReview", report["tracks"][1])

    def test_package_median_and_explicit_target_still_flag_differences(self):
        for target in (None, -16.0):
            with self.subTest(target=target):
                report = loudness_report()
                inspect_loudness(report, target)
                self.assertEqual(len(report["checks"]["audio"]["conditions"]), 1)
                self.assertIn("T2", report["checks"]["audio"]["conditions"][0])
                self.assertEqual(report["blockingIssues"], [])

    @patch("media_analysis.probe", return_value=(30.0, {"streams": [{"codec_type": "audio", "codec_name": "mp3"}]}))
    @patch("media_analysis.measure")
    @patch("media_analysis.run")
    def test_peak_repair_falls_back_to_supported_lossless_if_aac_repeatedly_exceeds_the_limit(self, run, measure, probe):
        codecs = []

        def encode(*args):
            if "-encoders" in args:
                return b"aac"
            codecs.append(args[args.index("-c:a") + 1])
            Path(args[-1]).touch()
            return b""

        run.side_effect = encode
        measure.side_effect = [(-10.0, 0.4), (-12.4, 0.6), (-12.4, 0.2), (-15.2, 0.5), (-12.4, -2.0)]
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp3"
            output = Path(folder) / "output.m4a"
            repaired, receipt = balance_audio(source, output, None)
            self.assertEqual(repaired, output)
            self.assertTrue(output.exists())
        self.assertEqual(codecs[-1], "alac")
        self.assertAlmostEqual(receipt["gainDB"], -2.4)
        self.assertEqual(receipt["truePeakDBTP"], -2.0)


class MediaPreflightTests(unittest.TestCase):
    @patch("ssroutine.alignment_module", return_value=object())
    @patch("ssroutine.timing_windows")
    def test_remeasures_confirmed_chapters_preserves_offset_sign_and_checks_bounds(self, replay, alignment):
        def song(section, start, end, evidence="audioMatched"):
            return {"section": section, "variant": "standard", "duration": 120,
                    "chapter": {"start": start, "end": end, "evidence": evidence, "approved": True}}

        failed = {"status": "review", "windows": [
            {"position": 0.14, "offsetMS": -215, "correlation": 0.7, "runnerUpGap": 0.2},
            {"position": 0.48, "offsetMS": 20, "correlation": 0.6, "runnerUpGap": 0.2},
            {"position": 0.8, "offsetMS": 215, "correlation": 0.7, "runnerUpGap": 0.2},
        ]}
        replay.side_effect = lambda candidate, *_: failed if candidate["section"] == "T4" else {"status": "measured"}
        candidates = [song("T4", 12, 132), song("T6", 132, 252), song("T7", 300, 420),
                      song("T9", 224, 310, "visuallyConfirmed")]
        messages = chapter_preflight(candidates, ["4", "6", "7", "9"], "video", 350)
        self.assertEqual(replay.call_count, 2)
        self.assertEqual(len(messages), 2)
        self.assertIn("-215.0 ms", messages[0])
        self.assertIn("+215.0 ms", messages[0])
        self.assertIn("430.0 ms", messages[0])
        self.assertIn("T7", messages[1])
        self.assertIn("超出视频时长", messages[1])


if __name__ == "__main__":
    unittest.main()
