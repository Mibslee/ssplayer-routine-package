import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from bodypump import expand_blocks, phrase_size


class BodyPumpRepeatTests(unittest.TestCase):
    @staticmethod
    def cue(second, counts, action, **fields):
        return dict(time=second, page=3, counts=counts, action=action, **fields)

    def test_single_block_keeps_rows_and_only_mirrors_named_exercise(self):
        rows = [self.cue(3, '2x8', 'TRANSITION: KICKBACK R, L hand on bench', block=1),
                self.cue(11, '4x8', 'KICKBACK R', repetitions='4x'),
                self.cue(27, '2x8', 'REPEAT BLOCK 1 with KICKBACK on L side', block=2)]
        bad, evidence = expand_blocks(rows, 56)
        self.assertEqual((bad, evidence), (rows, []))
        rows[-1]['counts'] = '6x8'
        rows[-1]['music'] = 'Target lyric'
        result, evidence = expand_blocks(rows, 56)
        self.assertEqual(result[-2]['action'], 'TRANSITION: KICKBACK L, R hand on bench')
        self.assertEqual(result[-1]['repetitions'], '4x')
        self.assertEqual(result[-2]['music'], 'Target lyric')
        self.assertEqual(result[-2]['repeatsBlock'], 1)
        self.assertEqual(evidence[0]['times'], [27, 35])

    def test_two_blocks_keep_order_and_do_not_repeat_an_excluded_hold(self):
        rows = [self.cue(4, '4x8', 'LUNGE L', block=1),
                self.cue(20, '2x8', 'PRESS', block=2),
                self.cue(28, '½x8', 'HOLD'),
                self.cue(30, '6x8', 'REPEAT BLOCKS 1 and 2 on R side', block=3)]
        result, evidence = expand_blocks(rows, 55)
        self.assertEqual([r['action'] for r in result[-2:]], ['LUNGE R', 'PRESS'])
        self.assertEqual(evidence[0]['sourceBlocks'], [1, 2])
        self.assertEqual(evidence[0]['times'], [30, 46])

    def test_missing_source_and_unknown_qualifier_stay_visible(self):
        rows = [self.cue(2, '4x8', 'LUNGE L', block=1),
                self.cue(18, '4x8', 'REPEAT BLOCK 9', block=2),
                self.cue(34, '4x8', 'REPEAT BLOCK 1 with a different sequence', block=3)]
        self.assertEqual(expand_blocks(rows, 51), (rows, []))
        self.assertEqual(phrase_size('2¼x8') * 8, 18)


class SourceVideoCoverageTests(unittest.TestCase):
    def test_visual_evidence_covers_distinct_regions_and_matches_the_clock(self):
        from ssroutine import confirmed_visual_clock

        chapter = {"start": 30, "end": 330, "evidence": "visuallyConfirmed"}
        sightings = [{"videoSecond": second + 30.5, "trackSecond": second} for second in (10, 120, 250)]
        self.assertTrue(confirmed_visual_clock(chapter, sightings, 300))
        self.assertFalse(confirmed_visual_clock(chapter, sightings[:2], 300))
        sightings[-1]["videoSecond"] += 3
        self.assertFalse(confirmed_visual_clock(chapter, sightings, 300))

    def test_audio_only_status_is_specific_to_a_reviewed_variant(self):
        from ssroutine import audio_only_score_variant

        alternate = {"sourceVariantWithoutVideo": True, "sectionFunction": {"approved": True}}
        self.assertTrue(audio_only_score_variant({"variant": "alternative"}, alternate))
        self.assertFalse(audio_only_score_variant({"variant": "standard"}, alternate))
        self.assertFalse(audio_only_score_variant({"variant": "alternative"},
                                                  {**alternate, "chapter": {"approved": True}}))
