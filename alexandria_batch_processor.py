#!/usr/bin/env python3
"""
Alexandria Batch Processor - Process multiple audiobooks sequentially
Processes one audio file at a time, creating complete datasets for each
"""

import os
import sys
import time
import shutil
import argparse
import subprocess
import json
import logging
import torch
from datetime import datetime, timedelta
from pathlib import Path

# Setup logging
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"alexandria_batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logger = logging.getLogger("alexandria_batch")
logger.setLevel(logging.DEBUG)

# File handler (detailed)
fh = logging.FileHandler(log_file)
fh.setLevel(logging.DEBUG)
file_format = logging.Formatter('[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] %(message)s')
fh.setFormatter(file_format)

# Console handler (info and above)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
console_format = logging.Formatter('[%(levelname)s] %(message)s')
ch.setFormatter(console_format)

logger.addHandler(fh)
logger.addHandler(ch)

logger.info("=" * 70)
logger.info("Alexandria Batch Processor - Sequential Audiobook Processing")
logger.info("=" * 70)
logger.info(f"Log file: {log_file}")

def get_gpu_stats():
    """Get current GPU memory and utilization stats."""
    if not torch.cuda.is_available():
        return None

    stats = {}
    try:
        # Memory stats (works for both NVIDIA and AMD ROCm)
        allocated = torch.cuda.memory_allocated() / 1e9  # GB
        reserved = torch.cuda.memory_reserved() / 1e9    # GB
        total = torch.cuda.get_device_properties(0).total_memory / 1e9  # GB

        stats['allocated_gb'] = allocated
        stats['reserved_gb'] = reserved
        stats['total_gb'] = total
        stats['allocated_percent'] = (allocated / total * 100) if total > 0 else 0

        # Try to get utilization via rocm-smi for AMD GPUs
        try:
            result = subprocess.run(
                ['/opt/rocm/bin/rocm-smi', '--showuse', '--json'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Filter out warning lines and parse JSON
                json_lines = [line for line in result.stdout.split('\n') if line.strip().startswith('{')]
                if json_lines:
                    data = json.loads(json_lines[0])
                    # rocm-smi format: {"card0": {"GPU use (%)": "value"}}
                    for card_key, card_data in data.items():
                        gpu_use_str = card_data.get('GPU use (%)', 'N/A')
                        if gpu_use_str != 'N/A':
                            stats['utilization_percent'] = float(gpu_use_str)
                        break  # Just get first GPU
            else:
                logger.debug(f"rocm-smi returned error: {result.returncode}")
                stats['utilization_percent'] = None
        except FileNotFoundError:
            stats['utilization_percent'] = None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError):
            stats['utilization_percent'] = None
        except Exception as e:
            logger.debug(f"rocm-smi error: {e}")
            stats['utilization_percent'] = None

    except Exception as e:
        logger.debug(f"Could not get GPU stats: {e}")
        return None

    return stats

def log_gpu_stats(label=""):
    """Log GPU memory and utilization statistics."""
    stats = get_gpu_stats()
    if not stats:
        return

    label_str = f" ({label})" if label else ""
    logger.info(f"GPU Usage{label_str}:")
    # Note: Parent process memory will show 0 since main script runs in subprocess
    if stats.get('utilization_percent') is not None:
        logger.info(f"  └─ GPU Utilization: {stats['utilization_percent']:.1f}%")
    else:
        logger.info(f"  └─ GPU Utilization: (rocm-smi unavailable)")

def format_duration(seconds):
    """Format seconds as Xh Ym Zs or Ym Zs."""
    hours = int(seconds // 3600)
    mins = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    if hours > 0:
        return f"{hours}h {mins}m {secs}s"
    elif mins > 0:
        return f"{mins}m {secs}s"
    else:
        return f"{secs}s"

def check_disk_space(path, required_gb_per_file, num_files):
    """Check if disk has enough space for batch processing."""
    try:
        stat = shutil.disk_usage(path)
        free_gb = stat.free / (1024 ** 3)
        required_gb = required_gb_per_file * num_files

        logger.info(f"▶ Disk space check:")
        logger.info(f"  ├─ Available: {free_gb:.1f} GB")
        logger.info(f"  ├─ Estimated needed: ~{required_gb:.1f} GB ({required_gb_per_file} GB/file × {num_files} files)")

        if free_gb < required_gb:
            logger.warning(f"  └─ ⚠ Low disk space - may fill up during processing")
            return False
        else:
            logger.info(f"  └─ ✓ Sufficient disk space")
            return True
    except Exception as e:
        logger.debug(f"Disk space check failed: {e}")
        return True  # Don't block on check failure

class BatchProcessor:
    SUPPORTED_FORMATS = {'.wav', '.mp3', '.m4a', '.flac', '.ogg'}

    def __init__(self, model_path, chunk_size=10.0, language="en", force=False):
        self.model_path = model_path
        self.chunk_size = chunk_size
        self.language = language
        self.force = force  # If True, reprocess even if output exists
        self.results = {
            "succeeded": [],
            "failed": [],
            "skipped": []
        }
        self.total_time = 0
        self.batch_start_time = None

    def validate_files(self, audio_files):
        """Validate all audio files and skip already-processed ones."""
        logger.info("▶ Validating input files...")
        valid_files = []

        for audio_file in audio_files:
            audio_path = Path(audio_file)

            if not audio_path.exists():
                logger.error(f"  ✗ File not found: {audio_file}")
                self.results["skipped"].append({
                    "file": audio_file,
                    "reason": "File not found"
                })
                continue

            if audio_path.suffix.lower() not in self.SUPPORTED_FORMATS:
                logger.warning(f"  ⚠ Unsupported format: {audio_file}")
                self.results["skipped"].append({
                    "file": audio_file,
                    "reason": "Unsupported audio format"
                })
                continue

            # Check if output already exists (resume capability)
            expected_output = f"alexandria_dataset_{audio_path.stem}.zip"
            if os.path.exists(expected_output) and not self.force:
                output_size_mb = os.path.getsize(expected_output) / (1024 * 1024)
                logger.info(f"  ⊘ {audio_path.name} → already processed: {expected_output} ({output_size_mb:.1f} MB)")
                self.results["skipped"].append({
                    "file": audio_file,
                    "output": expected_output,
                    "output_size_mb": output_size_mb,
                    "reason": "Already processed (use --force to reprocess)"
                })
                continue

            file_size_mb = audio_path.stat().st_size / (1024 * 1024)
            logger.info(f"  ✓ {audio_path.name} ({file_size_mb:.1f} MB)")
            valid_files.append(audio_file)

        if not self.model_path or not os.path.exists(self.model_path):
            logger.error(f"Model file not found: {self.model_path}")
            logger.error("Cannot proceed without model")
            sys.exit(1)

        logger.info(f"  ├─ Model: {Path(self.model_path).name}")
        logger.info(f"  └─ Files to process: {len(valid_files)}/{len(audio_files)} (skipped: {len(self.results['skipped'])})")

        return valid_files

    def process_file(self, audio_file, file_index, total_files):
        """Process a single audio file with real-time output streaming."""
        file_name = Path(audio_file).stem
        file_size = os.path.getsize(audio_file) / (1024 * 1024)

        logger.info("=" * 70)
        logger.info(f"▶ Processing [{file_index}/{total_files}] {Path(audio_file).name}")
        logger.info(f"  ├─ Size: {file_size:.1f} MB")
        logger.info(f"  ├─ Model: {Path(self.model_path).name}")
        logger.info(f"  ├─ Chunk size: {self.chunk_size}s")

        # Show overall batch ETA based on completed files
        if file_index > 1 and len(self.results["succeeded"]) > 0:
            avg_time = self.total_time / len(self.results["succeeded"])
            remaining_files = total_files - file_index + 1
            eta_secs = avg_time * remaining_files
            logger.info(f"  └─ Batch ETA: ~{format_duration(eta_secs)} ({remaining_files} files remaining)")
        else:
            logger.info(f"  └─ Batch ETA: calculating after first file completes...")
        logger.info("=" * 70)

        output_name = f"alexandria_dataset_{file_name}.zip"
        cmd = [
            sys.executable,
            "-u",  # Unbuffered output for real-time streaming
            "alexandria_preparer_rocm_compatible.py",
            "--audio", audio_file,
            "--model", self.model_path,
            "--chunk-size", str(self.chunk_size),
            "--lang", self.language,
            "--output", output_name,
        ]
        # Pass --resume unless --force was set; the preparer's source-marker check
        # ensures we won't accidentally resume into a different file's partial work.
        if not self.force:
            cmd.append("--resume")

        start_time = time.monotonic()
        logger.info(f"Starting subprocess at {datetime.now().strftime('%H:%M:%S')}...")
        logger.info("─" * 70 + " [subprocess output begins]")

        process = None
        last_stderr_lines = []
        try:
            # Use Popen for real-time output streaming
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,  # Merge stderr into stdout for ordering
                text=True,
                bufsize=1,  # Line-buffered
            )

            # Stream output line by line in real-time
            timeout_at = time.monotonic() + (3600 * 24)  # 24 hour timeout
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    print(line, flush=True)  # Real-time display
                    # Keep last 20 lines for error context
                    last_stderr_lines.append(line)
                    if len(last_stderr_lines) > 20:
                        last_stderr_lines.pop(0)

                if time.monotonic() > timeout_at:
                    process.kill()
                    raise subprocess.TimeoutExpired(cmd, 3600 * 24)

            process.wait()
            returncode = process.returncode

            logger.info("─" * 70 + " [subprocess output ends]")

            elapsed_secs = time.monotonic() - start_time
            time_str = format_duration(elapsed_secs)

            if returncode == 0:
                logger.info(f"✓ SUCCESS: {Path(audio_file).name} processed ({time_str})")

                if os.path.exists(output_name):
                    output_size = os.path.getsize(output_name) / (1024 * 1024)
                    logger.info(f"  ├─ Output: {output_name} ({output_size:.1f} MB)")
                    logger.info(f"  └─ Time: {time_str}")

                    self.results["succeeded"].append({
                        "file": audio_file,
                        "output": output_name,
                        "output_size_mb": output_size,
                        "time": time_str,
                        "time_seconds": elapsed_secs
                    })
                else:
                    logger.warning(f"⚠ Output file not created: {output_name}")
                    self.results["failed"].append({
                        "file": audio_file,
                        "reason": "Output file not created",
                        "time": time_str
                    })

            else:
                logger.error(f"✗ FAILED: {Path(audio_file).name} (return code: {returncode})")
                if last_stderr_lines:
                    logger.error(f"  Last output lines:")
                    for line in last_stderr_lines[-10:]:
                        logger.error(f"    {line}")

                self.results["failed"].append({
                    "file": audio_file,
                    "return_code": returncode,
                    "time": time_str
                })

            self.total_time += elapsed_secs

        except subprocess.TimeoutExpired:
            logger.error(f"✗ TIMEOUT: {Path(audio_file).name} exceeded 24 hours")
            if process:
                process.kill()
                process.wait()
            self.results["failed"].append({
                "file": audio_file,
                "reason": "Timeout (>24 hours)"
            })
        except KeyboardInterrupt:
            logger.warning(f"⚠ INTERRUPTED: User cancelled processing of {Path(audio_file).name}")
            if process:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
            self.results["failed"].append({
                "file": audio_file,
                "reason": "User interrupted (KeyboardInterrupt)"
            })
            raise  # Re-raise to stop the batch
        except Exception as e:
            logger.error(f"✗ ERROR: {Path(audio_file).name} - {e}")
            if process:
                process.kill()
            self.results["failed"].append({
                "file": audio_file,
                "reason": str(e)
            })

    def run(self, audio_files):
        """Process all audio files sequentially."""
        self.batch_start_time = time.monotonic()
        logger.info(f"\n▶ Starting batch processing: {len(audio_files)} files")
        log_gpu_stats("batch start")

        # Validate all files first (also skips already-processed)
        valid_files = self.validate_files(audio_files)

        if not valid_files:
            if self.results["skipped"]:
                logger.info(f"All {len(self.results['skipped'])} files already processed (use --force to reprocess)")
                self.print_summary()
                return True
            logger.error("No valid files to process")
            return False

        # Estimate disk space needs (~250MB per audiobook for dataset)
        check_disk_space(".", required_gb_per_file=0.5, num_files=len(valid_files))

        # Process each file
        try:
            for idx, audio_file in enumerate(valid_files, 1):
                self.process_file(audio_file, idx, len(valid_files))

                # Show overall batch progress after each file
                if len(valid_files) > 1:
                    completed = len(self.results["succeeded"]) + len(self.results["failed"])
                    progress_pct = (completed / len(valid_files)) * 100
                    elapsed = time.monotonic() - self.batch_start_time
                    logger.info(f"\n📊 Batch progress: {completed}/{len(valid_files)} files ({progress_pct:.1f}%) | Total elapsed: {format_duration(elapsed)}")

                # Small pause between files
                if idx < len(valid_files):
                    logger.info(f"⏳ Waiting before next file...\n")
                    log_gpu_stats(f"between files {idx}/{len(valid_files)}")
                    time.sleep(2)
        except KeyboardInterrupt:
            logger.warning("\n⚠ Batch processing interrupted by user")
            logger.info("Completed files retained - rerun batch to resume from interruption point")

        # Summary
        self.print_summary()
        return len(self.results["failed"]) == 0

    def print_summary(self):
        """Print processing summary."""
        logger.info("\n" + "=" * 70)
        logger.info("BATCH PROCESSING SUMMARY")
        logger.info("=" * 70)
        log_gpu_stats("batch complete")

        # Overall stats
        total_files = len(self.results["succeeded"]) + len(self.results["failed"]) + len(self.results["skipped"])
        total_time_str = format_duration(self.total_time)

        # Wall-clock time (includes pauses between files)
        wall_time = time.monotonic() - self.batch_start_time if self.batch_start_time else self.total_time
        wall_time_str = format_duration(wall_time)

        logger.info(f"Total files processed: {total_files}")
        logger.info(f"Total processing time: {total_time_str}")
        if wall_time > self.total_time + 5:
            logger.info(f"Total wall-clock time: {wall_time_str}")
        logger.info("")

        # Succeeded
        if self.results["succeeded"]:
            logger.info(f"✓ SUCCEEDED: {len(self.results['succeeded'])}")
            total_output_size = 0
            for item in self.results["succeeded"]:
                logger.info(f"  ├─ {Path(item['file']).name}")
                logger.info(f"  │  ├─ Output: {item['output']} ({item['output_size_mb']:.1f} MB)")
                logger.info(f"  │  └─ Time: {item['time']}")
                total_output_size += item['output_size_mb']
            logger.info(f"  └─ Total output size: {total_output_size:.1f} MB\n")
        else:
            logger.info("✓ SUCCEEDED: 0\n")

        # Failed
        if self.results["failed"]:
            logger.warning(f"✗ FAILED: {len(self.results['failed'])}")
            for item in self.results["failed"]:
                logger.warning(f"  ├─ {Path(item['file']).name}")
                if "reason" in item:
                    logger.warning(f"  │  └─ Reason: {item['reason']}")
                else:
                    logger.warning(f"  │  └─ Return code: {item.get('return_code', 'Unknown')}")
            logger.warning("")
        else:
            logger.info("✗ FAILED: 0\n")

        # Skipped
        if self.results["skipped"]:
            logger.info(f"⊘ SKIPPED: {len(self.results['skipped'])}")
            for item in self.results["skipped"]:
                logger.info(f"  ├─ {Path(item['file']).name}")
                logger.info(f"  │  └─ Reason: {item['reason']}")
            logger.info("")

        # Save results to JSON
        results_file = f"batch_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(results_file, 'w') as f:
            json.dump({
                "timestamp": datetime.now().isoformat(),
                "total_time_seconds": self.total_time,
                "results": self.results
            }, f, indent=2)
        logger.info(f"Results saved to: {results_file}")
        logger.info("=" * 70)

def main():
    parser = argparse.ArgumentParser(
        description="Alexandria Batch Processor - Process multiple audiobooks sequentially"
    )

    parser.add_argument(
        "audio_files",
        nargs="+",
        help="Audio files to process"
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Gemma GGUF model path"
    )
    parser.add_argument(
        "--chunk-size",
        type=float,
        default=10.0,
        help="Target chunk size in seconds (default: 10.0)"
    )
    parser.add_argument(
        "--lang",
        default="en",
        help="Language code (default: en)"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocess files even if alexandria_dataset_<name>.zip already exists"
    )

    args = parser.parse_args()

    processor = BatchProcessor(
        model_path=args.model,
        chunk_size=args.chunk_size,
        language=args.lang,
        force=args.force
    )

    success = processor.run(args.audio_files)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
