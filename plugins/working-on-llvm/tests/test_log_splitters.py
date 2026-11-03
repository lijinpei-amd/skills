#!/usr/bin/env python3
"""Run the bundled splitters' regression tests with python3 <this file>."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "working-on-llvm" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import sched_log_split
import split_ir_dump


class SplitIrDumpTests(unittest.TestCase):
    SAMPLE = (
        "; *** IR Dump After InstCombinePass on [module] ***\n"
        "define void @foo() { ret void }\n"
    )

    def test_stages_and_publishes_into_new_directory(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source = temp / "dump.txt"
            output = temp / "split"
            source.write_text(self.SAMPLE, encoding="utf-8")

            count = split_ir_dump.split_file(str(source), str(output))

            self.assertEqual(count, 1)
            self.assertTrue((output / "0000_InstCombinePass.ll").is_file())
            self.assertIn("0000_InstCombinePass.ll",
                          (output / "index.txt").read_text(encoding="utf-8"))

    def test_publishes_over_existing_empty_directory(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source = temp / "dump.txt"
            output = temp / "split"
            source.write_text(self.SAMPLE, encoding="utf-8")
            output.mkdir()
            output.chmod(0o775)
            original_stat = output.stat()

            count = split_ir_dump.split_file(str(source), str(output))

            self.assertEqual(count, 1)
            self.assertTrue((output / "index.txt").is_file())
            published_stat = output.stat()
            self.assertEqual(published_stat.st_ino, original_stat.st_ino)
            self.assertEqual(published_stat.st_mode & 0o777, 0o775)

    def test_new_output_uses_normal_umask_directory_mode(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source = temp / "dump.txt"
            output = temp / "split"
            source.write_text(self.SAMPLE, encoding="utf-8")

            old_umask = os.umask(0o022)
            try:
                split_ir_dump.split_file(str(source), str(output))
            finally:
                os.umask(old_umask)

            self.assertEqual(output.stat().st_mode & 0o777, 0o755)

    def test_rejects_input_inside_output_without_modifying_it(self):
        with tempfile.TemporaryDirectory() as temp_name:
            output = Path(temp_name) / "split"
            output.mkdir()
            source = output / "index.txt"
            source.write_text(self.SAMPLE, encoding="utf-8")

            with self.assertRaises(ValueError):
                split_ir_dump.split_file(str(source), str(output))

            self.assertEqual(source.read_text(encoding="utf-8"), self.SAMPLE)
            self.assertEqual(list(output.iterdir()), [source])

    def test_refuses_to_overwrite_existing_generated_output(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source = temp / "dump.txt"
            source.write_text(self.SAMPLE, encoding="utf-8")
            output = temp / "split"
            output.mkdir()
            existing = output / "0000_InstCombinePass.ll"
            existing.write_text("keep\n", encoding="utf-8")

            with self.assertRaises(ValueError):
                split_ir_dump.split_file(str(source), str(output))
            self.assertEqual(existing.read_text(encoding="utf-8"), "keep\n")

    def test_sanitize_uses_pass_slugs(self):
        cases = {
            "InstCombinePass on foo": "InstCombinePass",
            "Machine Scheduler (machine-scheduler)": "machine_scheduler",
            "CSE (cse) ('func.func' operation: @f)": "cse",
            "CanonicalizerPass: canonicalize{max-iterations=10 x=(y)}": "canonicalize",
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(split_ir_dump.sanitize(header), expected)


class SchedLogSplitTests(unittest.TestCase):
    def test_splits_into_new_collision_safe_chunks(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source = temp / "schedule.log"
            source.write_text("preamble\nBEGIN one\nbody\nBEGIN two\n", encoding="utf-8")
            output = temp / "chunks"

            chunks = sched_log_split.split_file(source, r"^BEGIN", output)

            self.assertEqual(
                [chunk.name for chunk in chunks],
                ["schedule_1.log", "schedule_2.log", "schedule_3.log"],
            )
            self.assertEqual(chunks[0].read_text(encoding="utf-8"), "preamble\n")
            self.assertEqual(
                chunks[1].read_text(encoding="utf-8"), "BEGIN one\nbody\n"
            )
            self.assertEqual(chunks[2].read_text(encoding="utf-8"), "BEGIN two\n")

    def test_refuses_existing_or_stale_chunks_without_modifying_them(self):
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source = temp / "schedule.log"
            source.write_text("BEGIN replacement\n", encoding="utf-8")
            output = temp / "chunks"
            output.mkdir()
            stale = output / "schedule_9.log"
            stale.write_text("keep\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                sched_log_split.split_file(source, r"^BEGIN", output)

            self.assertEqual(stale.read_text(encoding="utf-8"), "keep\n")
            self.assertEqual(list(output.iterdir()), [stale])


if __name__ == "__main__":
    unittest.main()
