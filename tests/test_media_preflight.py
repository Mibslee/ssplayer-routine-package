"""Regressions for source timing and the creator's audio policy."""

from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import hashlib
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

from delivery_policy import assess_delivery
from media_analysis import optimize_video, picture_stream, stream_hashes, timed_text_packets, video_info
from qa_preview import inspect_loudness
from ssroutine import chapter_preflight, digest, identity, package as build_package, read_manifest


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
    def test_original_peaks_are_advisories_without_blocking_or_requiring_changes(self):
        report = loudness_report(peak=0.4)
        inspect_loudness(report)
        self.assertEqual(report["loudnessPolicy"], "keepOriginalDynamics")
        self.assertEqual(report["checks"]["audio"], {"issues": [], "conditions": []})
        self.assertEqual(report["blockingIssues"], [])
        self.assertEqual(assess_delivery({}, report), {"status": "ready", "blockers": [], "limitations": []})
        self.assertEqual(len(report["advisories"]), 1)
        self.assertIn("+0.4 dBTP", report["advisories"][0]["detail"])
        self.assertIn("Mix「歌单响度一致化」", report["advisories"][0]["detail"])
        self.assertTrue(report["tracks"][0]["originalPeakAboveZero"])
        self.assertNotIn("originalPeakAboveZero", report["tracks"][1])


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class OriginalAudioCLITests(unittest.TestCase):
    def invoke(self, script, *args, succeeds=True):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        result = subprocess.run([sys.executable, str(scripts / script), *map(str, args)],
                                capture_output=True, text=True, timeout=60)
        if succeeds:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_build_finish_submit_and_revise_keep_original_music_bytes_and_report_peaks(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            songs, findings = [], []
            for index, level in enumerate((1.0, 0.12, 0.32), 1):
                audio = folder / f"T{index}.wav"
                if index == 1:
                    # A near-full-scale sequence with a reproducible intersample peak.
                    frame_rate = 48000
                    pcm = struct.pack("<hhhh", 32765, 32765, -32765, -32765) * 36000
                else:
                    frame_rate = 8000
                    pcm = b"".join(struct.pack("<h", round(32767 * level * math.sin(sample * math.tau / 8)))
                                   for sample in range(3 * frame_rate))
                with wave.open(str(audio), "wb") as writer:
                    writer.setparams((1, 2, frame_rate, 0, "NONE", "not compressed"))
                    writer.writeframes(pcm)
                songs.append({"file": str(audio), "section": f"T{index}", "variant": "standard", "title": f"Test {index}"})
                findings.append({"source": identity(audio), "duration": 3.0})
            review = folder / "review.json"
            review.write_text(json.dumps({"plan": {"program": "DEMO", "routine": "1", "revision": 1, "music": songs},
                                          "tracks": findings}), encoding="utf-8")

            def assert_original_music(package):
                with package.open("rb") as stream:
                    _, manifest = read_manifest(stream)
                    for found, packaged in zip(findings, manifest["music"], strict=True):
                        self.assertEqual(packaged["asset"], found["source"])
                    for original, packaged in zip(songs, manifest["music"], strict=True):
                        self.assertEqual(stream.read(packaged["asset"]["bytes"]), Path(original["file"]).read_bytes())

            kept = folder / "build"
            build = self.invoke("ssroutine.py", "build", review, "--output-dir", kept, "--keep-music")
            receipt = json.loads(build.stdout)
            self.assertEqual(receipt["musicPolicy"], "keepOriginalDynamics")
            self.assertTrue(all(row["gainDB"] == 0 for row in receipt["loudness"]))
            self.assertGreater(receipt["loudness"][0]["truePeakDBTP"], 0)
            self.assertIn("Mix「歌单响度一致化」", build.stderr)
            built = kept / "DEMO_1_1.ssroutine"
            assert_original_music(built)

            batch = folder / "batches"
            self.invoke("ssroutine.py", "finish", review, "--output-dir", batch, "--batch-id", "original", "--source-limited")
            package = batch / "original" / "套路资源包" / built.name
            assert_original_music(package)
            self.assertEqual(digest(package), digest(built))
            self.invoke("ssroutine.py", "submit", package, "--output-dir", batch, "--batch-id", "resubmitted", "--source-limited")
            resubmitted = batch / "resubmitted"
            fingerprints = json.loads((resubmitted / "resubmitted.ssfingerprints.json").read_text(encoding="utf-8"))
            self.assertEqual(fingerprints["packages"][0]["sha256"], digest(package))
            self.assertEqual(digest(resubmitted / "套路资源包" / package.name), digest(package))

            blocked_review = folder / "unready-review.json"
            blocked_review.write_text("{}", encoding="utf-8")
            continued = self.invoke("ssroutine.py", "finish", blocked_review, "--include-package", package,
                                    "--output-dir", batch, "--batch-id", "continued", "--source-limited",
                                    "--keep-going")
            summary = next(json.loads(line) for line in continued.stderr.splitlines() if line.startswith("{"))
            self.assertEqual((summary["status"], summary["ready"]), ("partial", [package.name]))
            self.assertEqual(summary["blocked"][0]["stage"], "编码前核对")
            accepted = batch / "continued" / "套路资源包" / package.name
            self.assertEqual(digest(accepted), digest(package))

            duplicate = self.invoke("ssroutine.py", "submit", package, built, "--output-dir", batch,
                                    "--batch-id", "duplicate", "--source-limited", "--keep-going", succeeds=False)
            self.assertIn("同一项目、套路和修订重复", duplicate.stderr)
            self.assertFalse((batch / "duplicate").exists())

            observations = folder / "captions.json"
            observations.write_text(json.dumps({package.name: {"sha256": "0" * 64, "status": "absent"}}),
                                    encoding="utf-8")
            stale = self.invoke("ssroutine.py", "submit", package, "--output-dir", batch,
                                "--batch-id", "stale", "--source-limited",
                                "--subtitle-observations", observations, succeeds=False)
            self.assertIn("字幕确认对应不同指纹", stale.stderr)
            self.assertFalse((batch / "stale").exists())

            manifest_path = folder / "manifest.json"
            self.invoke("ssroutine.py", "manifest", package, "--output", manifest_path)
            revised = folder / "revision"
            self.invoke("ssroutine.py", "revise", package, manifest_path, "--revision", "2", "--output-dir", revised)
            assert_original_music(revised / "DEMO_1_2.ssroutine")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["music"][0]["asset"]["sha256"] = "f" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            rejected = self.invoke("ssroutine.py", "revise", package, manifest_path, "--revision", "3",
                                   "--output-dir", revised, succeeds=False)
            self.assertIn("Changing media requires building a new package from source", rejected.stderr)
            self.assertFalse((revised / "DEMO_1_3.ssroutine").exists())

            qa = folder / "qa"
            self.invoke("qa_preview.py", package, "--output-dir", qa, "--strict", "--source-limited")
            evidence = qa / "DEMO_1_1.qa"
            result = json.loads((evidence / "qa.json").read_text(encoding="utf-8"))
            self.assertEqual(result["loudnessPolicy"], "keepOriginalDynamics")
            self.assertEqual(result["checks"]["audio"], {"issues": [], "conditions": []})
            self.assertTrue(result["offlineMediaPassed"])
            self.assertEqual(result["delivery"]["status"], "readyWithLimitations")
            self.assertEqual(len(result["advisories"]), 1)
            self.assertGreater(result["tracks"][0]["truePeakDBTP"], 0)
            levels = [row["lufs"] for row in result["tracks"]]
            self.assertGreater(max(levels) - min(levels), 9.0)
            self.assertFalse(any(item["check"] == "audio" for item in result["delivery"]["limitations"]))
            html = (evidence / "report.html").read_text(encoding="utf-8")
            self.assertIn("音频与视频观察", html)
            self.assertIn("Mix「歌单响度一致化」", html)

            corrupt = folder / package.name
            data = package.read_bytes()
            corrupt.write_bytes(data[:-1] + bytes([data[-1] ^ 1]))
            rejected = self.invoke("ssroutine.py", "submit", corrupt, "--output-dir", batch, "--batch-id", "corrupt",
                                   "--source-limited", succeeds=False)
            self.assertIn("Media payload digest mismatch", rejected.stderr)
            self.assertFalse((batch / "corrupt").exists())

    def test_video_compression_keeps_every_original_audio_stream(self):
        encoders = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"], text=True)
        if "libx265" not in encoders and not (sys.platform == "darwin" and "hevc_videotoolbox" in encoders):
            self.skipTest("FFmpeg HEVC encoder required")
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.mp4"
            compressed = Path(directory) / "compressed.mp4"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error",
                            "-f", "lavfi", "-i", "testsrc2=size=96x96:rate=24:duration=2",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                            "-f", "lavfi", "-i", "sine=frequency=540:duration=2",
                            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-c:v", "mpeg4", "-c:a", "aac",
                            "-metadata:s:a:0", "language=eng", "-metadata:s:a:1", "language=zho", str(source)],
                           check=True, capture_output=True, timeout=30)
            result, metadata = optimize_video(source, compressed, 64, None, "hevc")
            self.assertEqual(metadata["codec"], "hevc")
            self.assertEqual(metadata["height"], 64)
            audio = stream_hashes(result, "a")
            self.assertEqual(len(audio.splitlines()), 2)
            self.assertEqual(audio, stream_hashes(source, "a"))

    def test_default_quality_rejects_h264_fallback_but_explicit_original_keeps_it(self):
        encoders = subprocess.check_output(["ffmpeg", "-hide_banner", "-encoders"], text=True)
        if "libx264" not in encoders:
            self.skipTest("FFmpeg H.264 encoder required")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video, audio = root / "lesson.mp4", root / "T1.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                            "-i", "sine=frequency=410:duration=2", str(audio)], check=True, timeout=20)
            caption = root / "lesson.srt"
            caption.write_text("1\n00:00:00,100 --> 00:00:01,600\nTest caption\n", encoding="utf-8")
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi",
                            "-i", "testsrc2=size=96x96:rate=24:duration=2", "-i", str(audio), "-i", str(caption),
                            "-c:v", "libx264", "-c:a", "aac", "-c:s", "mov_text", str(video)], check=True, timeout=20)
            review = {"plan": {"program": "DEMO", "routine": "2", "revision": 1, "video": str(video),
                               "music": [{"file": str(audio), "section": "T1", "variant": "standard"}]},
                      "tracks": [{"source": identity(audio), "duration": 2}],
                      "video": {"source": identity(video)}}
            options = Namespace(output_dir=root / "result", video_quality="efficient",
                                video_codec=None, video_max_height=None, video_bitrate_kbps=None)
            fallback = {**video_info(video), "compression": "keptOriginal", "reason": "Picture sample failed"}
            with (patch("ssroutine.efficient_video", return_value=(video, fallback)),
                  redirect_stdout(StringIO()), redirect_stderr(StringIO())):
                with self.assertRaisesRegex(ValueError, "成品视频编码与交付要求不符"):
                    build_package(options, review=review)
                self.assertFalse(list(options.output_dir.glob("*.ssroutine")))
                options.video_quality = "original"
                result = build_package(options, review=review)
                with result.open("rb") as stream:
                    _, manifest = read_manifest(stream)
                    self.assertEqual(manifest["video"]["asset"]["sha256"], digest(video))
                    stream.seek(manifest["music"][0]["asset"]["bytes"], 1)
                    self.assertEqual(stream.read(manifest["video"]["asset"]["bytes"]), video.read_bytes())

    def test_multiple_picture_versions_require_a_unique_default(self):
        streams = [
            {"index": 0, "codec_type": "video", "disposition": {"default": 0}},
            {"index": 1, "codec_type": "video", "disposition": {"default": 1}},
            {"index": 2, "codec_type": "video", "disposition": {"default": 1, "attached_pic": 1}},
        ]
        self.assertEqual(picture_stream({"streams": streams})["index"], 1)
        streams[1]["disposition"]["default"] = 0
        with self.assertRaisesRegex(ValueError, "唯一默认轨"):
            picture_stream({"streams": streams})

    def test_subtitle_packet_audit_skips_empty_samples_but_retains_exact_text_timing(self):
        empty = {"pts_time": "1.200000", "data_hash": "SHA256:" + hashlib.sha256(b"\0\0").hexdigest()}
        text = {"pts_time": "1.200000", "duration_time": "0.850000", "data_hash": "SHA256:" + "0" * 64}
        with patch("media_analysis.run", return_value=json.dumps({"packets": [empty, text]}).encode()):
            self.assertEqual(timed_text_packets(Path("video.mp4"), 0), [text])

    def test_legacy_edit_options_reject_before_writing_output(self):
        cases = [
            ("ssroutine.py", ["build", "review.json"], ["--lufs", "-16"]),
            ("ssroutine.py", ["build", "review.json"], ["--balance-video"]),
            ("ssroutine.py", ["finish", "review.json"], ["--repair-music-peaks"]),
            ("ssroutine.py", ["revise", "input.ssroutine", "manifest.json", "--revision", "2"], ["--reduce-peak", "T1"]),
            ("ssroutine.py", ["revise", "input.ssroutine", "manifest.json", "--revision", "2"], ["--rebalance", "T1"]),
            ("ssroutine.py", ["submit", "input.ssroutine"], ["--target-lufs", "-16"]),
            ("qa_preview.py", ["input.ssroutine"], ["--target-lufs", "-16"]),
        ]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            for script, command, option in cases:
                with self.subTest(script=script, option=option):
                    result = self.invoke(script, *command, "--output-dir", output, *option, succeeds=False)
                    self.assertIn(f"{option[0]} 已停用", result.stderr)
                    self.assertIn("Mix「歌单响度一致化」", result.stderr)
                    self.assertFalse(output.exists())


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
