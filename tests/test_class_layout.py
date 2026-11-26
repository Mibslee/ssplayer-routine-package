from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch

from test_mixed_pdf import write_pdf
from grit_evidence import grit_source_evidence
from media_analysis import class_format_suggestions
from qa_preview import class_recording_conditions
from ssroutine import check_class_template_sources


def section_order(template):
    return [slot.get("sectionCodes") or [f"T{number}" for number in slot["sectionNumbers"]]
            for slot in template["slots"]]


class ClassLayoutTests(unittest.TestCase):
    def test_grit_name_is_reusable_and_existing_qualified_titles_keep_exact_source_checks(self):
        songs = [{"section": "T1", "title": "First Song", "variant": "standard"},
                 {"section": "T3", "title": "Second Song", "variant": "standard"}]
        with tempfile.TemporaryDirectory() as directory:
            for routine in ("57", "58"):
                with self.subTest(routine=routine):
                    score = Path(directory) / f"grit-{routine}.pdf"
                    write_pdf(score, [
                        [(100, f"LES MILLS GRIT {routine}")],
                        [(100, "GRIT STRENGTH"), (125, "MUSIC"),
                         (150, "01. WARM-UP"), (175, "02. STRENGTH"), (220, "DECLARATION OF INTENT")],
                        [(90, "01. WARM-UP"), (150, "30-minute HIIT workout"), (200, "EXERCISE")],
                        [(100, "01. First Song 3:20 mins"), (130, "TECHNIQUE AND COACHING")],
                        [(90, "02. STRENGTH"), (150, "EXERCISE")],
                        [(100, "02. Second Song 3:20 mins"), (130, "TECHNIQUE AND COACHING")],
                        [(100, "GRIT ATHLETIC FORMAT"),
                         (125, f"1 GRIT STRENGTH {routine} (TRACK 2) Strength"),
                         (150, f"2 GRIT STRENGTH {routine} (TRACK 1) Warm-up"),
                         (175, "Total time: 29:41")],
                    ])
                    evidence = grit_source_evidence(score, songs, routine)
                    expected = evidence["templates"]
                    self.assertEqual([item["name"] for item in expected], ["GRIT Strength", "GRIT Athletic"])
                    self.assertEqual([item["key"] for item in expected],
                                     [f"grit-strength-{routine}", f"grit-athletic-{routine}"])
                    self.assertEqual([section_order(item) for item in expected],
                                     [[["T1"], ["T3"]], [["T3"], ["T1"]]])
                    self.assertEqual([item["page"] for item in expected], [2, 7])
                    self.assertEqual(check_class_template_sources(score, expected, include_omissions=True,
                                                                  source_evidence=evidence), [])
                    old_titles = [{**item, "name": f"{item['name']} {routine}"} for item in expected]
                    self.assertEqual(check_class_template_sources(score, old_titles, include_omissions=True,
                                                                  source_evidence=evidence), [])
                    bad = {**old_titles[0], "slots": list(reversed(old_titles[0]["slots"]))}
                    with self.assertRaisesRegex(ValueError, "原谱不符"):
                        check_class_template_sources(score, [bad], source_evidence=evidence)

    def test_asymmetric_columns_keep_four_formats_and_real_ab_order(self):
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "class-formats.pdf"
            write_pdf(score, [[
                (105, "EXPRESS FORMATS", 28), (105, "KEY", 370),
                (152, "30-MINUTE STRENGTH", 28), (152, "30-MINUTE FLEXIBILITY", 179),
                (174, "Track 01 Warm-up", 28), (174, "Track 03B Mobility", 179),
                (196, "Track 03A Strength", 28), (196, "Track 04 Balance", 179),
                (174, "Track 77 Legend entry", 370), (196, "Track 78 Legend entry", 370),
                (218, "Track 79 Legend entry", 370),
                (218, "Track 03B Strength", 28), (218, "Track 10 Cooldown", 179),
                (290, "45-MINUTE STRENGTH", 28), (290, "45-MINUTE FLEXIBILITY", 179),
                (312, "Track 03A&B Strength", 28), (312, "Track 04 Balance", 179),
                (334, "Track 07 Strength", 28), (334, "Track 10 Cooldown", 179),
            ]])
            templates = class_format_suggestions(score)
            self.assertEqual({row["name"]: section_order(row) for row in templates}, {
                "30 分钟 Strength": [["T1"], ["T3A"], ["T3B"]],
                "30 分钟 Flexibility": [["T3B"], ["T4"], ["T10"]],
                "45 分钟 Strength": [["T3A", "T3B"], ["T7"]],
                "45 分钟 Flexibility": [["T4"], ["T10"]],
            })
            songs = [{"section": section, "variant": "standard"} for section in
                     ("T1", "T3A", "T3B", "T4", "T7", "T10")]
            self.assertTrue(all(not class_recording_conditions(row, songs) for row in templates))
            self.assertEqual(check_class_template_sources(score, templates, include_omissions=True), [])

    def test_column_flow_and_translated_duplicate_share_physical_source(self):
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "two-editions.pdf"
            write_pdf(score, [
                [(600, "FORMATOS EXPRES"), (729, "30-MINUTE STRENGTH"),
                 (750, "Track 01 Warm-up"), (130, "Track 03A&B Strength", 302),
                 (153, "Track 06 Cooldown", 302), (175, "Total Time 28:00", 302),
                 (195, "Please note", 302)],
                [(100, "EXPRESS FORMATS"), (130, "FUERZA 30 MINUTOS"),
                 (151, "Track 01 Calentamiento"), (172, "Track 03A&B Fuerza"),
                 (193, "Track 06 Relajación"), (214, "Tiempo total 28:00")],
            ])
            template, = class_format_suggestions(score)
            self.assertEqual(template["sourcePages"], [1, 2])
            self.assertEqual(section_order(template), [["T1"], ["T3A", "T3B"], ["T6"]])
            self.assertEqual(check_class_template_sources(score, [{**template, "page": 2}],
                                                          include_omissions=True), [])

    def test_unnumbered_role_must_match_an_independent_numbered_source(self):
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "source-role.pdf"
            page = [(105, "FORMATOS EXPRES"), (136, "FUERZA 30 MINUTOS"),
                    (155, "Track 02 Movilidad"), (176, "Final del movimiento", 108),
                    (197, "Tiempo total 30:00"), (218, "Track 06 Relajación")]
            heading = [(102, "04. FIN. DEL MOVIMIENTO"), (166, "TRACK FOCUS"),
                       (190, "Follow the flow of the warm-up."),
                       (205, "Choose a comfortable starting posture."),
                       (220, "Use steady and comfortable breathing.")]
            write_pdf(score, [page, heading])
            template, = class_format_suggestions(score, [1])
            self.assertEqual(section_order(template), [["T2"], ["T4"], ["T6"]])
            write_pdf(score, [page, heading, [(y, text.replace("04.", "05.")) for y, text in heading]])
            template, = class_format_suggestions(score, [1])
            self.assertEqual(section_order(template), [["T2"], ["T6"]])
            self.assertIn("未标曲号", template["sourceReview"][0])

    def test_number_column_ocr_recheck_must_keep_independent_letter_evidence(self):
        import pdfplumber

        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "scan-fixture.pdf"
            write_pdf(score, [[(110, "30-MINUTE STRENGTH"),
                              (137, "Track"), (137, "01", 71), (137, "Warm-up", 115),
                              (162, "Track"), (162, "02A&B", 71), (162, "Strength", 115),
                              (185, "Track"), (185, "03", 71), (185, "Balance", 115),
                              (210, "Total Time 29:00")]])
            with pdfplumber.open(score) as pdf:
                words = [{**word, "text": word["text"].replace("02A&B", "O2A&B"), "ocr": True}
                         for word in pdf.pages[0].extract_words()]
            with patch("score_evidence.ScorePageReader.words", return_value=words):
                with patch("class_templates.subprocess.run",
                           return_value=CompletedProcess([], 0, stdout=b"02A&B\n")) as tesseract:
                    template, = class_format_suggestions(score)
                    self.assertIn("tessedit_char_whitelist=0123456789AB&+/", tesseract.call_args.args[0])
                    self.assertEqual(section_order(template), [["T1"], ["T2A", "T2B"], ["T3"]])
                with patch("class_templates.subprocess.run",
                           return_value=CompletedProcess([], 0, stdout=b"02A\n")):
                    template, = class_format_suggestions(score)
                    self.assertEqual(section_order(template), [["T1"], ["T3"]])
                    self.assertIn("O2A&B", template["sourceReview"][0])


if __name__ == "__main__":
    unittest.main()
