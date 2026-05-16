#!/usr/bin/env python3
"""
Alexandria Batch Processor - Process multiple audiobooks sequentially
Processes one audio file at a time, creating complete datasets for each
"""

import os
import sys
import argparse
import subprocess
import json
import logging
import gc
import torch
from datetime import datetime
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
    logger.info(f"  ├─ Memory: {stats['allocated_gb']:.2f}GB / {stats['total_gb']:.2f}GB ({stats['allocated_percent']:.1f}%)")
    if stats.get('utilization_percent') is not None:
        logger.info(f"  └─ Utilization: {stats['utilization_percent']:.1f}%")
    else:
        logger.info(f"  └─ Utilization: (rocm-smi unavailable)")

class BatchProcessor:
    def __init__(self, model_path, chunk_size=10.0, language="en", skip_validation=False):
        self.model_path = model_path
        self.chunk_size = chunk_size
        self.language = language
        self.skip_validation = skip_validation
        self.results = {
            "succeeded": [],
            "failed": [],
            "skipped": []
        }
        self.total_time = 0

    def validate_files(self, audio_files):
        """Validate all audio files before processing."""
        logger.info("▶ Validating input files...")
        valid_files = []

        for audio_file in audio_files:
            if not os.path.exists(audio_file):
                logger.error(f"  ✗ File not found: {audio_file}")
                self.results["skipped"].append({
                    "file": audio_file,
                    "reason": "File not found"
                })
                continue

            if not audio_file.lower().endswith(('.wav', '.mp3', '.m4a', '.flac', '.ogg')):
                logger.warning(f"  ⚠ Unsupported format: {audio_file}")
                self.results["skipped"].append({
                    "file": audio_file,
                    "reason": "Unsupported audio format"
                })
                continue

            file_size_mb = os.path.getsize(audio_file) / (1024 * 1024)
            logger.info(f"  ✓ {Path(audio_file).name} ({file_size_mb:.1f} MB)")
            valid_files.append(audio_file)

        if not self.model_path or not os.path.exists(self.model_path):
            logger.error(f"Model file not found: {self.model_path}")
            logger.error("Cannot proceed without model")
            sys.exit(1)

        logger.info(f"  ├─ Model: {Path(self.model_path).name}")
        logger.info(f"  └─ Valid files to process: {len(valid_files)}/{len(audio_files)}")

        return valid_files

    def process_file(self, audio_file, file_index, total_files):
        """Process a single audio file."""
        file_name = Path(audio_file).stem
        file_size = os.path.getsize(audio_file) / (1024 * 1024)

        logger.info("=" * 70)
        logger.info(f"▶ Processing [{file_index}/{total_files}] {Path(audio_file).name}")
        logger.info(f"  ├─ Size: {file_size:.1f} MB")
        logger.info(f"  ├─ Model: {Path(self.model_path).name}")
        logger.info(f"  └─ Chunk size: {self.chunk_size}s")
        logger.info("=" * 70)

        # Build command to run the main script
        cmd = [
            sys.executable,
            "alexandria_preparer_rocm_compatible.py",
            "--audio", audio_file,
            "--model", self.model_path,
            "--chunk-size", str(self.chunk_size),
            "--lang", self.language
        ]

        try:
            start_time = datetime.now()
            logger.info(f"Starting subprocess at {start_time.strftime('%H:%M:%S')}...")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600 * 24  # 24 hour timeout per file
            )

            elapsed_time = datetime.now() - start_time
            elapsed_secs = elapsed_time.total_seconds()
            elapsed_mins = int(elapsed_secs // 60)
            elapsed_hours = elapsed_mins // 60
            elapsed_mins = elapsed_mins % 60

            if elapsed_hours > 0:
                time_str = f"{elapsed_hours}h {elapsed_mins}m"
            else:
                time_str = f"{elapsed_mins}m {int(elapsed_secs % 60)}s"

            if result.returncode == 0:
                logger.info(f"✓ SUCCESS: {Path(audio_file).name} processed ({time_str})")

                # Check if output file was created
                expected_output = "alexandria_dataset.zip"
                if os.path.exists(expected_output):
                    output_size = os.path.getsize(expected_output) / (1024 * 1024)
                    logger.info(f"  ├─ Output: {expected_output} ({output_size:.1f} MB)")

                    # Rename output to include original filename
                    output_name = f"alexandria_dataset_{file_name}.zip"
                    os.rename(expected_output, output_name)
                    logger.info(f"  └─ Renamed to: {output_name}")

                    self.results["succeeded"].append({
                        "file": audio_file,
                        "output": output_name,
                        "output_size_mb": output_size,
                        "time": time_str,
                        "time_seconds": elapsed_secs
                    })
                else:
                    logger.warning(f"⚠ Output file not created: {expected_output}")
                    self.results["failed"].append({
                        "file": audio_file,
                        "reason": "Output file not created",
                        "time": time_str
                    })

            else:
                logger.error(f"✗ FAILED: {Path(audio_file).name} (return code: {result.returncode})")
                if result.stderr:
                    logger.error(f"  Error: {result.stderr[-500:]}")  # Last 500 chars of error

                self.results["failed"].append({
                    "file": audio_file,
                    "return_code": result.returncode,
                    "time": time_str
                })

            self.total_time += elapsed_secs

        except subprocess.TimeoutExpired:
            logger.error(f"✗ TIMEOUT: {Path(audio_file).name} exceeded 24 hours")
            self.results["failed"].append({
                "file": audio_file,
                "reason": "Timeout (>24 hours)"
            })
        except Exception as e:
            logger.error(f"✗ ERROR: {Path(audio_file).name} - {e}")
            self.results["failed"].append({
                "file": audio_file,
                "reason": str(e)
            })

    def run(self, audio_files):
        """Process all audio files sequentially."""
        logger.info(f"\n▶ Starting batch processing: {len(audio_files)} files")
        log_gpu_stats("batch start")

        # Validate all files first
        valid_files = self.validate_files(audio_files)

        if not valid_files:
            logger.error("No valid files to process")
            return False

        # Process each file
        for idx, audio_file in enumerate(valid_files, 1):
            self.process_file(audio_file, idx, len(valid_files))

            # Small pause between files
            if idx < len(valid_files):
                logger.info(f"\n⏳ Waiting before next file...\n")
                log_gpu_stats(f"between files {idx}/{len(valid_files)}")
                import time
                time.sleep(2)

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

        total_hours = int(self.total_time // 3600)
        total_mins = int((self.total_time % 3600) // 60)
        total_secs = int(self.total_time % 60)

        if total_hours > 0:
            total_time_str = f"{total_hours}h {total_mins}m {total_secs}s"
        else:
            total_time_str = f"{total_mins}m {total_secs}s"

        logger.info(f"Total files processed: {total_files}")
        logger.info(f"Total processing time: {total_time_str}")
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

    args = parser.parse_args()

    processor = BatchProcessor(
        model_path=args.model,
        chunk_size=args.chunk_size,
        language=args.lang
    )

    success = processor.run(args.audio_files)
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()
