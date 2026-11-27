from pathlib import Path
import tempfile
import unittest

from test_mixed_pdf import write_pdf
from media_analysis import class_format_suggestions, expand_sequence, extract_cues, inspect_section_functions
from ssroutine import check_class_template_sources, section_function_checks, validate_section_function_review
from delivery_policy import class_recording_conditions


class BodyattackTests(unittest.TestCase):
    def test_wrapped_express_header_and_track_label_keep_three_distinct_formats(self):
        page = [
            (100, "55-MINUTE FORMAT"),
            (130, "Track 01 Warm-up"),
            (155, "Track Circuit/Aerobic"),
            (168, "03A/3B"),
            (190, "Track 05 Strength"),
            (215, "Approx Time 55:00"),
            (250, "45-MINUTE FORMAT"),
            (280, "Track 01 Warm-up"),
            (310, "Track 05 Strength"),
            (340, "Approx Time 42:10"),
            (370, "30-MINUTE EXPRESS"),
            (386, "FORMAT OPTION 1"),
            (420, "Track 01 Warm-up"),
            (450, "Express 05 Strength"),
            (480, "Approx Time 27:50"),
        ]
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "score.pdf"
            write_pdf(score, [page])
            formats = class_format_suggestions(score)
            self.assertEqual([item["minutes"] for item in formats], [55, 45, 30])
            self.assertEqual(formats[0]["slots"][1]["sectionCodes"], ["T3A", "T3B"])
            self.assertEqual([len(item["slots"]) for item in formats], [3, 2, 2])
            self.assertEqual(formats[2]["slots"][-1]["requiredDurationMinutes"], 30)
            self.assertNotIn("requiredDurationMinutes", formats[1]["slots"][-1])
            songs = [
                {"section": "T1", "variant": "standard"},
                {"section": "T3B", "variant": "standard"},
                {"section": "T5", "variant": "standard"},
                {"section": "T5", "variant": "alternative", "classDurationMinutes": 30},
            ]
            self.assertFalse(any(class_recording_conditions(item, songs) for item in formats))
            self.assertTrue(class_recording_conditions(formats[2], songs[:-1]))
            self.assertEqual(check_class_template_sources(score, formats, include_omissions=True), [])

    def test_named_replacement_and_express_have_separate_score_evidence(self):
        actions = [(200, "MUSIC", 80), (200, "EXERCISE", 215),
                   (200, "CTS", 440), (200, "REPS", 474),
                   (222, "0:10", 34), (222, "Move left", 215),
                   (245, "0:15", 34), (245, "Move right", 215)]
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "score.pdf"
            write_pdf(score, [
                [(103, "02. MIXED IMPACT"),
                 (140, "Please use Second Mix with the same choreography."),
                 *actions],
                [(103, "05. STRENGTH"), *actions],
                [(83, "Exp"), (103, "05. STRENGTH"),
                 *[(y, word.replace("Move", "Step"), x) for y, word, x in actions]],
            ])
            replacement = {"section": "T2", "variant": "alternative", "duration": 25,
                           "title": "Second Mix", "sectionFunction": {"page": 1, "name": "MIXED IMPACT"}}
            stranger = {**replacement, "title": "Different Mix"}
            short = {"section": "T5", "variant": "alternative", "duration": 25,
                     "sectionFunction": {"page": 3, "name": "STRENGTH"}}
            regular = {"section": "T5", "variant": "standard", "duration": 25}
            claims = inspect_section_functions(score, [replacement, stranger, short])
            self.assertEqual([claim[0] for claim in claims], ["matched", "manual", "matched"])
            _, drafts, _ = extract_cues(score, [replacement, stranger, short, regular])
            self.assertEqual([draft["pages"] for draft in drafts], [[1], [], [3], [2]])
            self.assertEqual(drafts[2]["rows"][0]["action"], "Step left")
            self.assertEqual(drafts[3]["rows"][0]["action"], "Move left")

    def test_manual_score_review_retains_contradiction_gate(self):
        function = {"page": 1, "name": "MIXED IMPACT"}
        evidence = {"page": 1, "observedHeading": "02. MIXED IMPACT",
                    "recordingBasis": "The source notes name this mix and prescribe this score."}
        with tempfile.TemporaryDirectory() as directory:
            score = Path(directory) / "score.pdf"
            write_pdf(score, [[(102, "02. MIXED IMPACT"), (125, "TRACK FOCUS"),
                               (150, "Choose a comfortable starting posture."),
                               (168, "Maintain a stable pulse for this section."),
                               (186, "Return to centre and set up the next step.")]])
            song = {"section": "T2", "variant": "alternative", "sectionFunction": function,
                    "sectionFunctionReview": evidence}
            self.assertEqual(section_function_checks(score, [song]), [("sourceReviewed", None)])
            status, _ = section_function_checks(score, [{**song, "section": "T3"}])[0]
            self.assertEqual(status, "contradiction")
            with self.assertRaises(ValueError):
                validate_section_function_review({**evidence, "page": 2}, function, "T2")

    def test_printed_repeat_expands_only_defined_sequence_ids(self):
        definitions = {"B": "Single knee", "B1": "Double knee", "G": "Jack"}
        self.assertEqual(expand_sequence("Seq uences B, B1 and G", definitions),
                         ("Single knee\nDouble knee\nJack", ""))
        self.assertIsNone(expand_sequence("Sequences B, H", definitions))
