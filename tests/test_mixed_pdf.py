from pathlib import Path
from io import BytesIO
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from media_analysis import extract_cues, inspect_section_functions
from score_evidence import ScorePageReader

def write_pdf(path, document):
    """Write a small extractable PDF with no test dependencies beyond the skill."""
    page_numbers = list(range(4, 4 + 2 * len(document), 2))
    objects = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: f"<< /Type /Pages /Kids [{' '.join(f'{i} 0 R' for i in page_numbers)}] /Count {len(document)} >>".encode(),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    for index, rows in zip(page_numbers, document, strict=True):
        runs = []
        for y, line, *positions in rows:
            x = positions[0] if positions else 34
            encoded = "".join(chr(byte) if 32 <= byte <= 126 and chr(byte) not in "()\\"
                              else f"\\{byte:03o}" for byte in line.encode("cp1252"))
            runs.append(f"BT /F1 12 Tf {x} {1000 - y} Td ({encoded}) Tj ET")
        text = "\n".join(runs).encode()
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
             (205, "For the next 5 minutes, choose a comfortable posture."),
             (220, "Use steady and comfortable breathing.")]

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

    def test_localized_action_header_preserves_pdf_title_and_exercise_text(self):
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "spanish.pdf"
            write_pdf(score, [[(101, "01. EQUILIBRIO"), (183, "MÚSICA", 75),
                              (183, "SECUENCIA/EJERCICIO", 215), (183, "CTS", 440),
                              (183, "REPETICIONES", 474), (215, "0:25", 34),
                              (215, "Postura de árbol", 215), (240, "0:50", 34),
                              (240, "Torsión", 215)]])
            song = {"section": "T1", "variant": "standard", "duration": 90,
                    "sectionFunction": {"page": 1, "name": "EQUILIBRIO"}}
            _, proposals, _ = extract_cues(score, [song])
            self.assertEqual([(row["time"], row["action"]) for row in proposals[0]["rows"]],
                             [(25.0, "Postura de árbol"), (50.0, "Torsión")])
            self.assertEqual(inspect_section_functions(score, [song]), [("matched", None)])

    def test_packaged_physical_pages_preserve_selected_translation_in_qa(self):
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "parallel-languages.pdf"
            write_pdf(score, [[(101, "01. BALANCE"), (183, "MUSIC", 75),
                              (183, "EXERCISE", 215), (183, "REPS", 474),
                              (215, "0:10", 34), (215, "Balance", 215),
                              (240, "0:45", 34), (240, "Change legs", 215)],
                             [(101, "01. EQUILIBRIO"), (183, "MÚSICA", 75),
                              (183, "EJERCICIO", 215), (183, "REPETICIONES", 474),
                              (215, "0:15", 34), (215, "Equilibrio", 215),
                              (240, "0:40", 34), (240, "Cambiar de pierna", 215)]])
            song = {"section": "T1", "variant": "standard", "duration": 90}
            page_count, draft, warnings = extract_cues(score, [song], page_numbers=[2])
            self.assertEqual((page_count, draft[0]["pages"], warnings), (2, [2], []))
            packaged = {**song, "pdfPage": 2, "sectionFunction": {"page": 2, "name": "EQUILIBRIO"},
                        "cues": [draft[0]["rows"][0]]}
            _, checked, _ = extract_cues(BytesIO(score.read_bytes()), [packaged])
            self.assertEqual(checked[0]["sectionFunctionCheck"], ("matched", None))
            self.assertEqual([row["time"] for row in checked[0]["rows"]], [15.0, 40.0])

    def test_ocr_evidence_is_reused_for_a_packaged_copy_of_a_scan(self):
        class OutlinedPage:
            curves, rects, images = [None] * 100, [], []
            width, height = 595, 842

            def extract_words(self):
                return [{"text": "watermark", "top": 20, "x0": 300}]

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "score.pdf"
            original = b"test scan bytes"
            source.write_bytes(original)
            page = OutlinedPage()
            words = [{"text": "01.", "top": 80, "x0": 90, "ocr": True},
                     {"text": "BALANCE", "top": 80, "x0": 124, "ocr": True}]
            with (patch("score_evidence.shutil.which", return_value="tesseract"),
                  patch("score_evidence.read_ocr_words", return_value=words) as ocr):
                self.assertEqual(ScorePageReader(source).words(page, 2), words)
                packaged = BytesIO(original)
                packaged.seek(5)
                self.assertEqual(ScorePageReader(packaged).words(page, 2), words)
                self.assertEqual(packaged.tell(), 5)
                ocr.assert_called_once_with(original, 2, page.width, page.height)

    def test_interleaved_lettered_tracks_do_not_merge_two_editions(self):
        def page(label, first):
            return [(101, label), (183, "MUSIC", 75), (183, "EXERCISE", 215), (183, "REPS", 474),
                    (215, first, 34), (215, "Begin", 215), (240, "0:46", 34), (240, "Change legs", 215)]

        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "parallel-parts.pdf"
            write_pdf(score, [page("03A. BALANCE", "0:10"), page("03B. BALANCE", "0:12"),
                              page("03A. EQUILIBRIO", "0:15"), page("03B. EQUILIBRIO", "0:17")])
            song = {"section": "T3A", "variant": "standard", "duration": 90, "pdfPage": 3}
            _, checked, _ = extract_cues(score, [song])
            self.assertEqual(checked[0]["pages"], [3])
            self.assertEqual([row["time"] for row in checked[0]["rows"]], [15.0, 46.0])

    def test_pinned_translation_without_numbered_text_needs_the_complete_matching_clock(self):
        rows = [(183, "MUSIC", 75), (183, "EXERCISE", 215), (183, "REPS", 474),
                (215, "0:10", 34), (215, "Begin", 215), (240, "0:30", 34),
                (240, "Switch", 215), (265, "0:45", 34), (265, "Finish", 215)]
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "parallel-unreadable-heading.pdf"
            write_pdf(score, [[(101, "01. BALANCE"), *rows], rows, rows[:-2]])
            song = {"section": "T1", "variant": "standard", "duration": 90}
            _, checked, _ = extract_cues(score, [{**song, "cues": [{"page": 2}]},
                                                {**song, "cues": [{"page": 3}]}])
            self.assertEqual([entry["pages"] for entry in checked], [[2], [1]])

if __name__ == "__main__":
    unittest.main()
