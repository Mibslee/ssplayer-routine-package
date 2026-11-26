from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from media_analysis import extract_cues, inspect_section_functions

def write_pdf(path, document):
    """Write a small extractable PDF with no test dependencies beyond the skill."""
    page_numbers = list(range(4, 4 + 2 * len(document), 2))
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_numbers)}] /Count {len(document)} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for index, rows in zip(page_numbers, document, strict=True):
        text = "\n".join(f"BT /F1 12 Tf 34 {1000 - y} Td ({line}) Tj ET" for y, line in rows).encode()
        objects[index] = f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 560 1000] /Resources << /Font << /F1 3 0 R >> >> /Contents {index + 1} 0 R >>".encode()
        objects[index + 1] = f"<< /Length {len(text)} >>\nstream\n".encode() + text + b"\nendstream"
    data = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for i, body in sorted(objects.items()):
        offsets.append(len(data))
        data.extend(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    data.extend("".join(f"{o:010d} 00000 n \n" for o in offsets[1:]).encode())
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(data)

NARRATIVE = [(135, "5:57mins"), (170, "TRACK FOCUS"), (190, "Follow the flow of the warm-up."),
             (205, "Choose a comfortable starting posture."), (220, "Use steady and comfortable breathing.")]

class MixedEvidenceTests(unittest.TestCase):
    def test_distinguishes_recording_length_from_action_times(self):
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "score.pdf"
            write_pdf(score, [[(101, "01. BREATHING"), *NARRATIVE],
                              [(101, "02. BALANCE"), *NARRATIVE, (241, "0:25 WIDE STANCE")]])
            songs = [
                {"section": "T1", "variant": "standard", "pdfPage": 1, "duration": 357,
                 "sectionFunction": {"page": 1, "name": "BREATHING"}},
                {"section": "T2", "variant": "standard", "pdfPage": 2, "duration": 357,
                 "sectionFunction": {"page": 2, "name": "BALANCE"}},
            ]
            _, proposals, _ = extract_cues(score, songs)
            self.assertEqual(proposals[0].get("untimedScorePage"), 1)
            self.assertNotIn("untimedScorePage", proposals[1])
            self.assertEqual(inspect_section_functions(score, songs), [("matched", None), ("matched", None)])
            wrong = {**songs[0], "sectionFunction": {"page": 1, "name": "STRENGTH"}}
            self.assertEqual(inspect_section_functions(score, [wrong])[0][0], "contradiction")

    def test_clock_on_unnumbered_continuation_prevents_source_limitation(self):
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "score.pdf"
            write_pdf(score, [[(101, "01. BREATHING"), *NARRATIVE],
                              [(210, "0:25 WIDE STANCE")], [(101, "02. BALANCE"), *NARRATIVE]])
            song = {"section": "T1", "variant": "standard", "pdfPage": 1, "duration": 357,
                    "sectionFunction": {"page": 1, "name": "BREATHING"}}
            _, proposals, _ = extract_cues(score, [song])
            self.assertNotIn("untimedScorePage", proposals[0])

if __name__ == "__main__":
    unittest.main()
