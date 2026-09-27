"""Display behavior that previously prevented practical object weighing."""

import unittest
from dataclasses import replace

from trackpad_scale.live_readout import LiveReadout
from trackpad_scale.models import RawContact, RawFrame, TargetTouchState


def frame(sequence, pressure=20, *, path=1, state=TargetTouchState.TOUCHING):
    contact = RawContact(
        path_index=path, state=state, finger_code=1, hand_code=0,
        normalized_x=0.5, normalized_y=0.5, z_total_raw=1,
        pressure_candidate_raw=pressure, z_density_raw=1,
        normalized_x_bits=0x3F000000, normalized_y_bits=0x3F000000,
        z_total_bits=0x3F800000, pressure_candidate_bits=0,
        z_density_bits=0x3F800000,
    )
    return RawFrame(sequence, sequence, sequence / 123,
                    round(sequence * 1e9 / 123), (contact,))


class LiveReadoutTests(unittest.TestCase):
    def test_contact_load_zero_and_unload_without_stability_gate(self):
        scale = LiveReadout()
        first = scale.process(frame(1, 20, state=TargetTouchState.MAKE_TOUCH))
        self.assertEqual(first.estimated_grams, 20)
        self.assertFalse(first.zeroed)
        zero = scale.zero(first.contact_epoch)
        self.assertEqual(zero.estimated_grams, 0)
        self.assertTrue(zero.zeroed)
        for i in range(2, 12):
            loaded = scale.process(frame(i, 120))
        self.assertEqual(loaded.estimated_grams, 100)
        self.assertEqual(loaded.pressure_raw, 120)
        for i in range(12, 22):
            unloaded = scale.process(frame(i, 20))
        self.assertEqual(unloaded.estimated_grams, 0)
        for i in range(22, 32):
            lighter = scale.process(frame(i, 15))
        self.assertEqual(lighter.estimated_grams, -5)  # Do not hide baseline drift.

    def test_smoothing_is_bounded_and_zero_discards_previous_load_history(self):
        scale = LiveReadout()
        scale.process(frame(1, 10))
        reading = scale.process(frame(2, 30))
        self.assertEqual(reading.estimated_grams, 20)
        zero = scale.zero(reading.contact_epoch)
        self.assertEqual(zero.zero_offset_raw, 30)
        self.assertEqual(scale.process(frame(3, 30)).estimated_grams, 0)
        for i in range(4, 14):
            reading = scale.process(frame(i, 130))
        self.assertEqual(reading.estimated_grams, 100)

    def test_release_clears_zero_and_ignores_terminal_pressure(self):
        for state in (TargetTouchState.NOT_TRACKING, TargetTouchState.BREAK_TOUCH,
                      TargetTouchState.LINGER_IN_RANGE, TargetTouchState.OUT_OF_RANGE):
            with self.subTest(state=state):
                scale = LiveReadout()
                reading = scale.process(frame(1))
                scale.zero(reading.contact_epoch)
                self.assertIsNone(scale.process(frame(2, 999, state=state)))
                reading = scale.process(frame(3, 8))
                self.assertEqual(reading.estimated_grams, 8)
                self.assertFalse(reading.zeroed)

    def test_object_contacts_sum_without_resetting_reference_zero(self):
        scale = LiveReadout()
        first = scale.process(frame(1))
        scale.zero(first.contact_epoch)
        for sequence in range(2, 12):
            one = frame(sequence)
            second = replace(one.contacts[0], path_index=2, pressure_candidate_raw=100)
            # The object may be first in the array; it cannot replace reference 1.
            loaded = scale.process(replace(one, contacts=(second,) + one.contacts))
            self.assertEqual(loaded.path_index, 1)
            self.assertEqual(loaded.contact_epoch, first.contact_epoch)
            self.assertTrue(loaded.zeroed)
        self.assertEqual(loaded.pressure_raw, 120)
        self.assertEqual(loaded.estimated_grams, 100)
        self.assertEqual(loaded.contributing_paths, (1, 2))
        for sequence in range(12, 22):
            one = frame(sequence)
            second = replace(second, state=TargetTouchState.LINGER_IN_RANGE)
            unloaded = scale.process(replace(one, contacts=(second,) + one.contacts))
        self.assertEqual(unloaded.estimated_grams, 0)
        self.assertTrue(unloaded.zeroed)

    def test_hover_pressure_updates_the_combined_reading_and_preserves_zero(self):
        # Reproduces the live recording: finger at zero, other contacts in
        # state 2 carrying changing pressure. Those values cannot be ignored.
        for state in (TargetTouchState.START_IN_RANGE, TargetTouchState.HOVER_IN_RANGE):
            with self.subTest(state=state):
                scale = LiveReadout()
                initial = scale.process(frame(1, 10))
                scale.zero(initial.contact_epoch)
                for sequence, pressure in ((2, 23), (3, 124), (4, 363)):
                    one = frame(sequence, 0)
                    extra = replace(one.contacts[0], path_index=2, state=state,
                                    pressure_candidate_raw=pressure)
                    reading = scale.process(replace(one, contacts=one.contacts + (extra,)))
                    self.assertEqual(reading.pressure_raw, pressure)
                    self.assertTrue(reading.zeroed)
                    self.assertEqual(reading.zero_offset_raw, 10)
                    self.assertEqual(reading.contact_epoch, initial.contact_epoch)
                    self.assertIsNone(scale.issue)
                recovered = scale.process(frame(5, 10))
                self.assertIsNone(scale.issue)
                self.assertTrue(recovered.zeroed)
                self.assertEqual(scale.zero(initial.contact_epoch).estimated_grams, 0)

    def test_force_redistribution_does_not_change_total_or_contact_epoch(self):
        scale = LiveReadout()
        initial = scale.process(frame(1, 7))
        scale.zero(initial.contact_epoch)
        # Captured sequence 1835–1837: finger/hover force splits 7+0, 6+1, 5+2.
        for sequence, pressure in ((2, 0), (3, 1), (4, 2)):
            one = frame(sequence, 7 - pressure)
            extra = replace(one.contacts[0], path_index=2,
                            state=TargetTouchState.HOVER_IN_RANGE,
                            pressure_candidate_raw=pressure)
            reading = scale.process(replace(one, contacts=(extra,) + one.contacts))
            self.assertEqual(reading.pressure_raw, 7)
            self.assertEqual(reading.estimated_grams, 0)
            self.assertEqual(reading.contact_epoch, initial.contact_epoch)

    def test_reference_reclassification_keeps_zero_and_departure_clears_it(self):
        scale = LiveReadout()
        initial = scale.process(frame(1, 10))
        scale.zero(initial.contact_epoch)
        hover = scale.process(frame(2, 30, state=TargetTouchState.HOVER_IN_RANGE))
        self.assertTrue(hover.zeroed)
        self.assertEqual(hover.contact_epoch, initial.contact_epoch)
        one = frame(3, 30, state=TargetTouchState.BREAK_TOUCH)
        extra = replace(one.contacts[0], path_index=2, state=TargetTouchState.TOUCHING,
                        pressure_candidate_raw=8)
        new = scale.process(replace(one, contacts=one.contacts + (extra,)))
        self.assertEqual(new.estimated_grams, 8)
        self.assertFalse(new.zeroed)
        with self.assertRaises(ValueError):
            scale.zero(initial.contact_epoch)

    def test_hover_only_frames_can_carry_live_pressure(self):
        for state in (TargetTouchState.START_IN_RANGE, TargetTouchState.HOVER_IN_RANGE):
            reading = LiveReadout().process(frame(1, 25, state=state))
            self.assertEqual(reading.estimated_grams, 25)

    def test_zero_pressure_hover_does_not_block_a_stationary_finger(self):
        scale = LiveReadout()
        for sequence, pressure in ((1, 10), (2, 110)):
            one = frame(sequence, pressure)  # Identical X/Y throughout.
            hover = replace(one.contacts[0], path_index=2,
                            state=TargetTouchState.HOVER_IN_RANGE, pressure_candidate_raw=0)
            reading = scale.process(replace(one, contacts=(hover,) + one.contacts))
            self.assertIsNone(scale.issue)
            self.assertEqual(reading.pressure_raw, pressure)
        self.assertEqual(reading.estimated_grams, 60)

    def test_contact_and_stream_changes_reset_zero_and_filter(self):
        cases = (
            frame(2, 8, path=2), frame(3, 8),
            replace(frame(2, 8), frame_number=9),
            replace(frame(2, 8), device_timestamp=2),
            replace(frame(2, 8), host_monotonic_ns=2_000_000_000),
            replace(frame(2, 8), device_timestamp=0),
            replace(frame(2, 8), host_monotonic_ns=0),
        )
        for next_frame in cases:
            with self.subTest(frame=next_frame):
                scale = LiveReadout()
                initial = scale.process(frame(1))
                scale.zero(initial.contact_epoch)
                changed = scale.process(next_frame)
                self.assertEqual(changed.estimated_grams, 8)
                self.assertFalse(changed.zeroed)
                with self.assertRaisesRegex(ValueError, "Contact changed"):
                    scale.zero(initial.contact_epoch)

    def test_stale_stream_and_reused_path_cannot_accept_a_queued_zero(self):
        scale = LiveReadout()
        first = scale.process(frame(1))
        scale.reset()
        scale.process(frame(2, 40))
        with self.assertRaises(ValueError):
            scale.zero(first.contact_epoch)
        scale.process(replace(frame(3), contacts=()))
        with self.assertRaises(ValueError):
            scale.zero(first.contact_epoch)

    def test_invalid_pressure_cannot_be_displayed(self):
        for pressure in (float('nan'), float('inf'), 43690):
            self.assertIsNone(LiveReadout().process(frame(1, pressure)))

    def test_bad_extra_record_invalidates_aggregate_and_zero(self):
        for pressure in (float('nan'), float('inf'), 43690):
            scale = LiveReadout()
            initial = scale.process(frame(1))
            scale.zero(initial.contact_epoch)
            one = frame(2)
            extra = replace(one.contacts[0], path_index=2,
                            state=TargetTouchState.HOVER_IN_RANGE,
                            pressure_candidate_raw=pressure)
            self.assertIsNone(scale.process(replace(one, contacts=one.contacts + (extra,))))
            self.assertEqual(scale.issue, "invalid_pressure")
            with self.assertRaises(ValueError):
                scale.zero(initial.contact_epoch)

    def test_duplicate_paths_cannot_double_count_pressure(self):
        scale = LiveReadout()
        one = frame(1)
        self.assertIsNone(scale.process(replace(one, contacts=one.contacts * 2)))
        self.assertEqual(scale.issue, "invalid_pressure")


if __name__ == '__main__':
    unittest.main()
