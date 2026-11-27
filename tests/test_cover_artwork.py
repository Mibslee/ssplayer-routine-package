"""Cover normalization on a real music container with unchanged sound."""

from io import BytesIO
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

from mutagen import File
from PIL import Image
from zxingcpp import BarcodeFormat, create_barcode, write_barcode_to_image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from cover_artwork import embedded_covers, inspect_music_artwork, music_with_cover, prominent_qr
from media_analysis import audio_packet_hash, run


class CoverArtworkTests(unittest.TestCase):
    def qr_image(self):
        barcode = create_barcode("synthetic cover review", BarcodeFormat.QRCode)
        qr = Image.fromarray(write_barcode_to_image(barcode, scale=5))
        return qr

    def test_prominent_qr_is_detected_but_small_code_is_not(self):
        qr = self.qr_image()
        framed = Image.new("L", (640, 640), 255)
        framed.paste(qr, (20, 20))
        for image, detected in ((qr, True), (framed, False)):
            data = BytesIO()
            image.save(data, format="PNG")
            self.assertEqual(prominent_qr(data.getvalue()), detected)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_all_embedded_art_is_replaced_and_audio_packets_are_identical(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, tagged, corrected = (root / f"{name}.mp3" for name in ("original", "tagged", "corrected"))
            artwork, ad = root / "approved.jpg", root / "advert.png"
            Image.new("RGB", (200, 200), "navy").save(artwork)
            self.qr_image().save(ad)
            run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=2", str(source))
            music_with_cover(source, tagged, ad)
            self.assertEqual(inspect_music_artwork(tagged, artwork.read_bytes()), "qr")
            self.assertEqual(inspect_music_artwork(source, artwork.read_bytes()), "mismatch")
            music_with_cover(tagged, corrected, artwork)
            self.assertEqual(embedded_covers(File(corrected)), [artwork.read_bytes()])
            self.assertIsNone(inspect_music_artwork(corrected, artwork.read_bytes()))
            self.assertEqual(audio_packet_hash(source), audio_packet_hash(corrected))
            self.assertEqual(music_with_cover(corrected, root / "redundant.mp3", artwork), corrected)


if __name__ == "__main__":
    unittest.main()
