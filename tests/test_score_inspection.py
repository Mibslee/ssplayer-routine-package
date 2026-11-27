from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from PIL import Image

from test_mixed_pdf import write_pdf


class ScoreInspectionTests(unittest.TestCase):
    def inspect(self, source, pages, output):
        script = Path(__file__).resolve().parents[1] / "scripts" / "ssroutine.py"
        return subprocess.run([sys.executable, str(script), "inspect-score", str(source),
                               "--pages", pages, "--output-dir", str(output)],
                              capture_output=True, text=True)

    def test_selected_pages_export_visual_and_positioned_evidence(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "layout.pdf"
            output = Path(work) / "observations"
            write_pdf(source, [
                [(100, "Opening notes")],
                [(100, "Circuit", 34), (100, "55-minute", 310),
                 (130, "Track 03A", 34), (130, "Express", 310)],
            ])
            original = source.read_bytes()

            completed = self.inspect(source, "2", output)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            index = json.loads((output / "index.json").read_text())
            self.assertEqual(index["sourceSHA256"], sha256(original).hexdigest())
            self.assertEqual(index["pageCount"], 2)
            [evidence] = index["pages"]
            self.assertEqual(evidence["page"], 2)
            self.assertEqual(evidence["textSource"], "pdfText")
            text = (output / evidence["text"]).read_text()
            first_row = text.splitlines()[0]
            self.assertIn("Circuit", first_row)
            self.assertIn("[x=310.0] 55-minute", first_row)
            self.assertNotIn("Opening notes", text)
            with Image.open(output / evidence["image"]) as rendered:
                self.assertEqual(rendered.format, "PNG")
                self.assertGreater(rendered.width, 560)
            self.assertEqual(source.read_bytes(), original)

    def test_out_of_range_selection_leaves_no_evidence_folder(self):
        with tempfile.TemporaryDirectory() as work:
            source = Path(work) / "score.pdf"
            output = Path(work) / "observations"
            write_pdf(source, [[(100, "First page")]])

            completed = self.inspect(source, "1-2", output)

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(output.exists())
