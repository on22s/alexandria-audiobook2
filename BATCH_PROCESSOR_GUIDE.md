# Alexandria Batch Processor Guide

Process multiple audiobook files sequentially with a single command.

## Overview

The batch processor allows you to queue multiple audio files for processing. Each file is processed one after another (sequentially), creating a complete annotated dataset for each.

**Key Features:**
- Process multiple files with one command
- Sequential processing (one at a time, no parallel overhead)
- Automatic output file naming (includes original filename)
- Comprehensive logging and progress tracking
- JSON results summary with statistics
- GPU acceleration for all files
- Time estimates for each file

## Usage

### Single File (Original Script)
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### Multiple Files (Batch Processor)
```bash
python alexandria_batch_processor.py \
  audiobook1.wav audiobook2.wav audiobook3.wav \
  --model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

## Command Line Options

### Positional Arguments
- `audio_files` - One or more audio files to process (required, space-separated)

### Optional Arguments
- `--model PATH` - Path to Gemma GGUF model file (required)
- `--chunk-size SIZE` - Target duration per chunk in seconds (default: 10.0)
- `--lang CODE` - Language code for transcription (default: en)
- `--force` - Reprocess files even if `alexandria_dataset_<name>.zip` already exists (default: skip)

## Resume Capability

The batch processor supports **two layers of resume**:

**1. File-level skip** — Files whose output zip (`alexandria_dataset_<name>.zip`) already exists are skipped on subsequent runs.

**2. Mid-file resume** — If a file crashed mid-annotation (e.g., 50 hours into a 60-hour run), the batch processor passes `--resume` to the preparer automatically. The preparer reads its checkpoint and continues from the last completed segment.

The preparer's `.source` marker in `dataset_temp/` ensures we never accidentally resume into a different file's partial work — if the marker doesn't match the current audio file, `dataset_temp/` is wiped and processing starts fresh.

```bash
# First run - processes all 3 files
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf
# (book1 completes, book2 crashes 50h into annotation)

# Second run:
#  - book1.wav skipped (zip exists)
#  - book2.wav resumes from last checkpoint (continues from segment ~4000)
#  - book3.wav processed fresh
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf

# Force reprocess everything (no skip, no resume)
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf --force
```

## Real-Time Output Streaming

The batch processor streams the main script's output **in real-time** so you can monitor:
- Transcription progress (Wav2Vec2 chunks being processed)
- GPU utilization percentage
- Annotation progress with elapsed/remaining time estimates
- Any warnings or errors as they happen

No more waiting hours wondering if processing is stuck!

## Examples

### Process 3 audiobooks
```bash
python alexandria_batch_processor.py \
  book1.wav book2.wav book3.wav \
  --model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
```

### Process with custom chunk size
```bash
python alexandria_batch_processor.py \
  book1.wav book2.wav \
  --model model.gguf \
  --chunk-size 15.0
```

### Process in different language
```bash
python alexandria_batch_processor.py \
  spanish_book.wav french_book.wav \
  --model model.gguf \
  --lang es
```

## Output Files

For each input file, the batch processor creates:
- `alexandria_dataset_<filename>.zip` - Annotated dataset
- Automatically renamed from `alexandria_dataset.zip` to avoid conflicts

Example:
```
Input:  audiobook1.wav
Output: alexandria_dataset_audiobook1.zip

Input:  audiobook2.wav  
Output: alexandria_dataset_audiobook2.zip
```

## Logging

The batch processor creates detailed logs in the `logs/` directory:
- `logs/alexandria_batch_YYYYMMDD_HHMMSS.log` - Full processing log

## Results Summary

After processing completes, a JSON summary is created:
- `batch_results_YYYYMMDD_HHMMSS.json` - Processing results

Contains:
- Individual file results (success/failure)
- Output file paths and sizes
- Processing times for each file
- Total processing time
- Timestamp

Example:
```json
{
  "timestamp": "2026-05-16T16:30:00",
  "total_time_seconds": 14400,
  "results": {
    "succeeded": [
      {
        "file": "book1.wav",
        "output": "alexandria_dataset_book1.zip",
        "output_size_mb": 250.5,
        "time": "2h 15m",
        "time_seconds": 8100
      }
    ],
    "failed": [],
    "skipped": []
  }
}
```

## Processing Flow

1. **Validation** - Checks all files exist and are valid audio formats
2. **Model Check** - Verifies Gemma model is accessible
3. **GPU Monitoring** - Logs GPU memory and utilization at start
4. **Sequential Processing** - Processes each file one by one:
   - Loads audio (16kHz + 24kHz)
   - **Transcribes with Wav2Vec2** (GPU primary, 30s overlapping chunks for context preservation)
     - Falls back to Insanely Fast Whisper if needed
     - Falls back to WhisperX-CPU if needed
   - **Annotates chunks with Gemma** (GPU acceleration)
     - GPU memory and utilization monitored every 10 segments
     - Elapsed time shown in hours/minutes format
     - Remaining time estimated with real-time updates
   - Creates audio segments with annotations
   - Packages as ZIP file
   - Renames output to avoid conflicts
   - GPU stats logged between files
5. **Summary** - Generates processing report with batch statistics

## GPU Monitoring & Progress Tracking

### Real-Time Monitoring
- **GPU Memory**: Tracks allocated/total memory and percentage during:
  - After Wav2Vec2 model loads
  - Before/during Wav2Vec2 chunk processing (every 50 chunks)
  - After Gemma model loads
  - During annotation (every 10 segments)
  - Between files during batch processing

- **GPU Utilization**: Shows actual GPU workload percentage via rocm-smi
  - Helps verify GPU is actually being used
  - Detects false positives where GPU claims to be used but isn't
  - Example: `Utilization: 45.3%`

### Progress Tracking
- **Time Format**: All time estimates shown in hours/minutes format
  - Estimated total: `~60h 13m` instead of just seconds
  - Elapsed time: `Elapsed: 7m 40s`
  - Remaining time: `Remaining: ~60h 12m`
  - Updated every 10 annotation segments

### Example Progress Output
```
GPU Usage (chunk 50/725):
  ├─ Memory: 4.52GB / 16.00GB (28.3%)
  └─ Utilization: 87.5%

Progress: 10/4713 chunks | Elapsed: 7m 40s | Remaining: ~60h 12m
```

## Performance Tips

- **GPU Acceleration** - All transcription and annotation uses GPU with real-time monitoring
- **Sequential Processing** - No parallel overhead, each file gets full GPU access
- **Disk Space** - Ensure ~5-10 GB free space per audiobook
- **Monitor Memory** - GPU cache is cleared between files, memory usage logged

## Time Estimates

Based on ~5.5 hours per 19,500-second audiobook:
- Transcription: ~15-20 minutes (GPU accelerated)
- Annotation: ~4-5 hours (GPU accelerated, ~46s per chunk)

Total = ~4.5-5.5 hours per audiobook

For 3 audiobooks: ~13-16 hours total

## Troubleshooting

### File Not Found
```
Error: File not found: audiobook.wav
```
- Check file path is correct
- Use absolute paths if file is in different directory

### Unsupported Format
```
Warning: Unsupported format: audiobook.aac
```
- Supported formats: .wav, .mp3, .m4a, .flac, .ogg
- Convert file using ffmpeg or other audio tool

### Model Not Found
```
Error: Model file not found
```
- Ensure model path is correct
- Download Gemma-4 model if missing

### GPU Not Available
```
Warning: GPU not available - running on CPU (slower)
```
- Check CUDA/ROCm installation
- Verify GPU drivers are installed
- Single file will take much longer on CPU

### Out of Space
```
Error: No space left on device
```
- Clean up old datasets
- Free up disk space (need ~10GB per file)
- Check `du -sh alexandria_dataset*.zip` for old files

## Advanced Usage

### Process Files from Directory
```bash
python alexandria_batch_processor.py $(ls /path/to/audiobooks/*.wav) \
  --model model.gguf
```

### Process with Error Recovery
The batch processor automatically:
- Validates all files before starting
- Continues if one file fails
- Reports which files succeeded/failed
- Saves results to JSON

### Monitor Progress
```bash
# In another terminal
tail -f logs/alexandria_batch_*.log
```

## Development Notes

The batch processor:
- Wraps `alexandria_preparer_rocm_compatible.py`
- Handles file naming conflicts
- Tracks timing and results
- Logs all operations
- Cleans up temporary files

To modify behavior, edit `alexandria_batch_processor.py` directly.
