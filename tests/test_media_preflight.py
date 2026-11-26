"""Regressions for source timing and the creator's audio policy."""

import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from media_analysis import balance_audio
from qa_preview import inspect_loudness
from ssroutine import chapter_preflight, identity, read_manifest


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
    def test_default_preserves_relative_levels_and_still_rejects_excess_peaks(self):
        report = loudness_report(peak=0.4)
        inspect_loudness(report)
        self.assertEqual(report["loudnessPolicy"], "keepOriginalDynamics")
        self.assertEqual(report["checks"]["audio"]["conditions"], [])
        self.assertEqual(len(report["blockingIssues"]), 1)
        self.assertIn("+0.4 dBTP", report["blockingIssues"][0])
        self.assertNotIn("loudnessReview", report["tracks"][1])

    def test_explicit_target_still_flags_differences(self):
        report = loudness_report()
        inspect_loudness(report, -16.0)
        self.assertEqual(report["loudnessPolicy"], "targetLoudness")
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


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class OriginalAudioCLITests(unittest.TestCase):
    def test_default_finish_and_resubmission_keep_the_original_songs(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"

        def invoke(script, *args):
            result = subprocess.run([sys.executable, str(scripts / script), *map(str, args)],
                                    capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            songs, findings = [], []
            for index, level in enumerate((0.40, 0.12, 0.32), 1):
                audio = folder / f"T{index}.wav"
                with wave.open(str(audio), "wb") as writer:
                    writer.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
                    writer.writeframes(b"".join(struct.pack("<h", round(32767 * level * math.sin(sample * math.tau / 8)))
                                                for sample in range(24000)))
                songs.append({"file": str(audio), "section": f"T{index}", "variant": "standard", "title": f"Test {index}"})
                findings.append({"source": identity(audio), "duration": 3.0})
            review = folder / "review.json"
            review.write_text(json.dumps({"plan": {"program": "DEMO", "routine": "1", "revision": 1, "music": songs},
                                          "tracks": findings}), encoding="utf-8")
            batch = folder / "batches"
            invoke("ssroutine.py", "finish", review, "--output-dir", batch, "--batch-id", "original", "--source-limited")
            package = batch / "original" / "套路资源包" / "DEMO_1_1.ssroutine"
            with package.open("rb") as stream:
                _, manifest = read_manifest(stream)
                for found, song in zip(findings, manifest["music"], strict=True):
                    self.assertEqual(song["asset"], found["source"])
                for original, song in zip(songs, manifest["music"], strict=True):
                    self.assertEqual(stream.read(song["asset"]["bytes"]), Path(original["file"]).read_bytes())

            invoke("ssroutine.py", "submit", package, "--output-dir", batch, "--batch-id", "resubmitted", "--source-limited")
            self.assertTrue((batch / "resubmitted" / "resubmitted.ssfingerprints.json").is_file())
            qa = folder / "qa"
            invoke("qa_preview.py", package, "--output-dir", qa, "--strict", "--source-limited")
            result = json.loads((qa / "DEMO_1_1.qa" / "qa.json").read_text(encoding="utf-8"))
            self.assertEqual(result["loudnessPolicy"], "keepOriginalDynamics")
            self.assertEqual(result["checks"]["audio"], {"issues": [], "conditions": []})
            levels = [row["lufs"] for row in result["tracks"]]
            self.assertGreater(max(levels) - min(levels), 9.0)

            normalized = folder / "normalized"
            out = json.loads(invoke("ssroutine.py", "build", review, "--output-dir", normalized, "--lufs", "-16"))
            self.assertEqual(out["musicPolicy"], "targetLoudness")
            self.assertTrue(all(abs(song["outputLUFS"] + 16.0) <= 1.0 for song in out["loudness"]))
            with (normalized / package.name).open("rb") as stream:
                _, manifest = read_manifest(stream)
            self.assertTrue(all(song["asset"]["sha256"] != found["source"]["sha256"]
                                for song, found in zip(manifest["music"], findings, strict=True)))
            invoke("ssroutine.py", "submit", normalized / package.name, "--output-dir", batch,
                   "--batch-id", "verified-target", "--source-limited", "--target-lufs", "-16")
            self.assertTrue((batch / "verified-target" / "verified-target.ssfingerprints.json").is_file())

            kept = folder / "explicit-keep"
            invoke("ssroutine.py", "build", review, "--output-dir", kept, "--keep-music", "--lufs", "-18")
            self.assertEqual((kept / package.name).read_bytes(), package.read_bytes())


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
