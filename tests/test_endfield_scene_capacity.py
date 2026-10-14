from __future__ import annotations

import unittest
import os
import subprocess
import sys

from endfield_scene_capacity import (ActiveMemoryReservation, ProcessMemorySnapshot, SystemMemorySnapshot,
                                     available_physical_memory_bytes, memory_admission,
                                     process_memory_snapshot, system_memory_snapshot)


class SceneCapacityTests(unittest.TestCase):
    def test_available_physical_memory_is_a_positive_byte_count(self) -> None:
        available = available_physical_memory_bytes()
        self.assertIs(type(available), int)
        self.assertGreater(available, 0)

    def test_admission_reserves_unconsumed_growth_in_both_memory_domains(self) -> None:
        active = (ActiveMemoryReservation(800, ProcessMemorySnapshot(500, 600, 400, 500)),)
        admitted = memory_admission(300, active, SystemMemorySnapshot(750, 2000, 850, 3000), 100)
        self.assertTrue(admitted.allowed)
        self.assertEqual(admitted.physical_required_bytes, 700)
        self.assertEqual(admitted.commit_required_bytes, 800)
        self.assertFalse(memory_admission(300, active, SystemMemorySnapshot(750, 2000, 799, 3000), 100).allowed)

    def test_admission_stops_when_an_active_child_has_outgrown_its_reservation(self) -> None:
        active = (ActiveMemoryReservation(800, ProcessMemorySnapshot(801, 900, 400, 500)),)
        self.assertFalse(memory_admission(100, active, SystemMemorySnapshot(5000, 6000, 5000, 6000), 100).allowed)
        reduced = (ActiveMemoryReservation(800, ProcessMemorySnapshot(500, 900, 400, 500)),)
        self.assertFalse(memory_admission(100, reduced, SystemMemorySnapshot(5000, 6000, 5000, 6000), 100).allowed)

    @unittest.skipUnless(os.name == "nt", "Windows process handle integration")
    def test_native_counters_capture_a_real_owned_child_and_keep_lifetime_peak_after_exit(self) -> None:
        command = [sys._base_executable, "-c", "import sys; data=bytearray(32*1024*1024); print('allocated',flush=True); sys.stdin.readline()"]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        try:
            self.assertIsNotNone(process.stdout)
            self.assertEqual(process.stdout.readline().strip(), b"allocated")
            live = process_memory_snapshot(int(process._handle))
            self.assertGreaterEqual(live.private_commit_bytes, 32 * 1024 ** 2)
            host = system_memory_snapshot()
            self.assertGreater(host.total_physical_bytes, host.available_physical_bytes)
            self.assertGreaterEqual(host.total_commit_bytes, host.available_commit_bytes)
            process.communicate(b"\n", timeout=5)
            exited = process_memory_snapshot(int(process._handle))
            self.assertGreaterEqual(exited.peak_working_set_bytes, live.working_set_bytes)
            self.assertGreaterEqual(exited.peak_commit_bytes, live.private_commit_bytes)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
