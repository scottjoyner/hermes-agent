# USB Drive Auto-Sort for Auto-Ingest

Detect a plugged-in USB drive, scan it for media/transcription files, and route them to the correct auto-ingest pipeline directories on the NAS.

## Prerequisites

The agent's machine must have the NAS mounted. The canonical mount point is:
```
/media/scott/NAS1/
```
If not mounted, the agent should mount it first (see Mounting section below).

## Pre-flight Checklist

Before doing anything, run these checks:

```bash
# 1. Verify NAS is mounted and writable
mount | grep NAS1
df -h /media/scott/NAS1/
ls -la /media/scott/NAS1/fileserver/ | head -5

# 2. Verify USB drive is detected and mounted
lsblk -d -o NAME,TYPE,SIZE,MOUNTPOINT,MODEL | grep usb
mount | grep usb-drive
df -h /media/scott/usb-drive/

# 3. Count files on USB (for the report)
echo "Total files on USB: $(find /media/scott/usb-drive -type f | wc -l)"
echo "Audio: $(find /media/scott/usb-drive -type f \( -iname '*.wav' -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.flac' \) | wc -l)"
echo "Video: $(find /media/scott/usb-drive -type f \( -iname '*.mp4' -o -iname '*.avi' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.flv' -o -iname '*.wmv' \) | wc -l)"
echo "Transcriptions: $(find /media/scott/usb-drive -type f \( -iname '*transcription*' -o -iname '*.srt' -o -iname '*.vtt' \) | wc -l)"
echo "Metadata: $(find /media/scott/usb-drive -type f -iname '*metadata*' | wc -l)"

# 4. Check for existing year folders on NAS (for naming convention)
ls /media/scott/NAS1/fileserver/dashcam/ | grep '^[0-9]\{4\}$' | sort -n | tail -3
ls /media/scott/NAS1/fileserver/audio/ | grep '^[0-9]\{4\}$' | sort -n | tail -3
ls /media/scott/NAS1/fileserver/bodycam/ | grep '^[0-9]\{4\}$' | sort -n | tail -3
```

If any check fails, report the error and stop. Do not proceed without NAS access.

## Step 1: Detect the USB Drive

```bash
# List block devices, look for removable USB drives
lsblk -d -o NAME,TYPE,SIZE,MOUNTPOINT,MODEL | grep -E 'usb|USB|removable'

# Or use blkid to find external drives by filesystem type
blkid | grep -E 'vfat|exfat|ntfs'

# Or check /dev/sd* for removable devices
lsblk -d -o NAME,TYPE,TRAN | grep usb
```

Typical USB drive appears as `/dev/sdb1` or `/dev/sdc1`. Note the device path and filesystem type.

### Mount the USB drive

```bash
# Create mount point
mkdir -p /media/scott/usb-drive

# Mount (use correct filesystem type)
mount -t exfat /dev/sdb1 /media/scott/usb-drive
# or
mount -t vfat /dev/sdb1 /media/scott/usb-drive
# or
mount -t ntfs-3g /dev/sdb1 /media/scott/usb-drive

# Verify
ls -la /media/scott/usb-drive/
```

## Step 2: Scan for Files

```bash
# Count files by type
echo "=== Audio files ==="
find /media/scott/usb-drive -type f \( -iname '*.wav' -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.flac' -o -iname '*.aac' -o -iname '*.ogg' \) | wc -l

echo "=== Video files ==="
find /media/scott/usb-drive -type f \( -iname '*.mp4' -o -iname '*.avi' -o -iname '*.mov' -o -iname '*.mkv' -o -iname '*.flv' -o -iname '*.wmv' \) | wc -l

echo "=== Transcription files ==="
find /media/scott/usb-drive -type f \( -iname '*transcription*' -o -iname '*.srt' -o -iname '*.vtt' -o -iname '*.txt' \) | wc -l

echo "=== Metadata files ==="
find /media/scott/usb-drive -type f -iname '*metadata*' | wc -l

echo "=== All files ==="
find /media/scott/usb-drive -type f | wc -l
```

## Step 3: Classify and Route Files

The USB drive files need to be routed to these NAS directories:

| NAS Directory | File Types | Purpose |
|--------------|-----------|---------|
| `/media/scott/NAS1/fileserver/audio/` | .wav, .mp3, .m4a, .flac, .aac, .ogg | Raw audio recordings |
| `/media/scott/NAS1/fileserver/audio/transcriptions/` | .txt, .srt, .vtt, .csv | Audio transcriptions |
| `/media/scott/NAS1/fileserver/dashcam/` | .avi, .mp4, .mov, .mkv | Dashcam footage |
| `/media/scott/NAS1/fileserver/dashcam/audio/` | .wav, .mp3 | Dashcam audio |
| `/media/scott/NAS1/fileserver/dashcam/transcriptions/` | .txt, .csv, .srt | Dashcam transcriptions |
| `/media/scott/NAS1/fileserver/dashcam/metadata/` | *_metadata.csv | Dashcam metadata |
| `/media/scott/NAS1/fileserver/bodycam/` | .mp4, .mov | Bodycam footage |
| `/media/scott/NAS1/fileserver/headcam/` | .avi, .mp4, .mov | Headcam footage |
| `/media/scott/NAS1/fileserver/joyner/` | Any | Personal/home video |

### Classification logic

```bash
# Check for existing year folders on the USB drive
find /media/scott/usb-drive -maxdepth 1 -type d -name '????' | sort

# Check for dashcam naming patterns (MOVI*, part_*, date-prefixed)
find /media/scott/usb-drive -maxdepth 2 -type f \( -iname 'MOVI*' -o -iname 'part_*' -o -iname '*_metadata.csv' \) | head -20

# Check for bodycam naming patterns (YYYYMMDD_HHMMSS)
find /media/scott/usb-drive -maxdepth 2 -type f -regex '.*[0-9]\{8\}_[0-9]\{6\}\.[mp][4v]' | head -20

# Check for audio naming patterns
find /media/scott/usb-drive -maxdepth 2 -type f -regex '.*[0-9]\{8\}_[0-9]\{6\}\.\(wav\|mp3\|m4a\|flac\)' | head -20
```

**Classification rules:**
1. **Dashcam files**: Files with `MOVI*`, `part_*`, or `_metadata.csv` patterns → `dashcam/`
2. **Bodycam files**: Files named `YYYYMMDD_HHMMSS.ext` in video formats → `bodycam/`
3. **Headcam files**: `.avi` files (existing headcam files are .avi) → `headcam/`
4. **Audio files**: `.wav`, `.mp3`, `.m4a`, `.flac` → `audio/`
5. **Transcriptions**: Files with `transcription` in name, `.srt`, `.vtt` → `audio/transcriptions/`
6. **Metadata**: `*_metadata.csv` files → `dashcam/metadata/`
7. **Everything else**: → `joyner/` (catch-all for personal video)

## Step 4: Move Files (Dry Run First)

### Dry run — preview what would be moved

```bash
# List all files that would be moved, grouped by target
echo "=== TO: audio/ ==="
find /media/scott/usb-drive -type f \( -iname '*.wav' -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.flac' \) -print
echo "=== TO: audio/transcriptions/ ==="
find /media/scott/usb-drive -type f \( -iname '*transcription*' -o -iname '*.srt' -o -iname '*.vtt' \) -print
echo "=== TO: dashcam/ ==="
find /media/scott/usb-drive -type f \( -iname 'MOVI*' -o -iname 'part_*' -o -iname '*.avi' -o -iname '*.mp4' -o -iname '*.mov' -o -iname '*.mkv' \) -print
echo "=== TO: dashcam/metadata/ ==="
find /media/scott/usb-drive -type f -iname '*metadata*' -print
echo "=== TO: bodycam/ ==="
find /media/scott/usb-drive -type f -regex '.*[0-9]\{8\}_[0-9]\{6\}\.[mp][4v]' -print
echo "=== TO: headcam/ ==="
find /media/scott/usb-drive -type f -iname '*.avi' -print
echo "=== TO: joyner/ ==="
find /media/scott/usb-drive -type f \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.flv' -o -iname '*.wmv' \) -print
```

### Move files

```bash
# Ensure target directories exist
mkdir -p /media/scott/NAS1/fileserver/audio/$(date +%Y)
mkdir -p /media/scott/NAS1/fileserver/audio/transcriptions
mkdir -p /media/scott/NAS1/fileserver/dashcam/$(date +%Y)
mkdir -p /media/scott/NAS1/fileserver/dashcam/audio
mkdir -p /media/scott/NAS1/fileserver/dashcam/transcriptions
mkdir -p /media/scott/NAS1/fileserver/dashcam/metadata
mkdir -p /media/scott/NAS1/fileserver/bodycam/$(date +%Y)
mkdir -p /media/scott/NAS1/fileserver/headcam
mkdir -p /media/scott/NAS1/fileserver/joyner/$(date +%Y)

# Move audio files
find /media/scott/usb-drive -type f \( -iname '*.wav' -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.flac' \) \
  -exec mv -t /media/scott/NAS1/fileserver/audio/$(date +%Y)/ {} +

# Move transcriptions
find /media/scott/usb-drive -type f \( -iname '*transcription*' -o -iname '*.srt' -o -iname '*.vtt' \) \
  -exec mv -t /media/scott/NAS1/fileserver/audio/transcriptions/ {} +

# Move dashcam footage (MOVI*, part_*, avi/mp4/mov/mkv)
find /media/scott/usb-drive -type f \( -iname 'MOVI*' -o -iname 'part_*' \) \
  -exec mv -t /media/scott/NAS1/fileserver/dashcam/$(date +%Y)/ {} +

# Move dashcam metadata
find /media/scott/usb-drive -type f -iname '*metadata*' \
  -exec mv -t /media/scott/NAS1/fileserver/dashcam/metadata/ {} +

# Move bodycam footage
find /media/scott/usb-drive -type f -regex '.*[0-9]\{8\}_[0-9]\{6\}\.[mp][4v]' \
  -exec mv -t /media/scott/NAS1/fileserver/bodycam/$(date +%Y)/ {} +

# Move headcam footage
find /media/scott/usb-drive -type f -iname '*.avi' \
  -exec mv -t /media/scott/NAS1/fileserver/headcam/ {} +

# Move remaining video to joyner
find /media/scott/usb-drive -type f \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.flv' -o -iname '*.wmv' \) \
  -exec mv -t /media/scott/NAS1/fileserver/joyner/$(date +%Y)/ {} +
```

## Step 5: Verify and Report

```bash
# Report what was moved
echo "=== Audio files moved ==="
find /media/scott/NAS1/fileserver/audio/$(date +%Y)/ -newer /media/scott/usb-drive -type f 2>/dev/null | wc -l

echo "=== Dashcam files moved ==="
find /media/scott/NAS1/fileserver/dashcam/$(date +%Y)/ -newer /media/scott/usb-drive -type f 2>/dev/null | wc -l

echo "=== Bodycam files moved ==="
find /media/scott/NAS1/fileserver/bodycam/$(date +%Y)/ -newer /media/scott/usb-drive -type f 2>/dev/null | wc -l

echo "=== Transcription files moved ==="
find /media/scott/NAS1/fileserver/audio/transcriptions/ -newer /media/scott/usb-drive -type f 2>/dev/null | wc -l

echo "=== Remaining on USB (should be empty) ==="
find /media/scott/usb-drive -type f | wc -l

# Unmount the USB drive
umount /media/scott/usb-drive
rmdir /media/scott/usb-drive
```

## Step 6: Trigger Ingestion

After files are moved, trigger the auto-ingest pipeline:

```bash
cd /home/scott/git/auto-ingest
./run_ingest_all.sh
# Or via Docker:
docker compose restart ingest-service
```

## Mounting the NAS (if not already mounted)

```bash
# Check if NAS1 is mounted
mount | grep NAS1

# If not mounted, mount it
mkdir -p /media/scott/NAS1
mount -t exfat /dev/sda2 /media/scott/NAS1
# Or if it's a network mount, use the appropriate method for that agent's setup
```

## Important Notes

- **Always dry-run first** — use the preview commands before moving files
- **Files are MOVED, not copied** — once moved, they're gone from the USB drive
- **Year folders** are created automatically for audio, dashcam, bodycam, and joyner
- **Dashcam metadata CSV** (`*_metadata.csv`) is critical for the pipeline — ensure it stays with its video files
- **Transcription pairing**: transcriptions must be named to match their audio file (e.g., `20231226131615_transcription.txt` for `20231226131615.WAV`)
- **Path correction**: if the agent's code references `/media/scott/NAS/fileserver/`, fix it to `/media/scott/NAS1/fileserver/` (see path fix commands in auto-ingest-new-data skill)

## Handling Subdirectories on USB Drive

USB drives often have nested folders (e.g., `DCIM/`, `Recordings/`, `2024-01-15/`). The `find` commands above handle this because they search recursively. However, be aware:

```bash
# If files are in deep subdirectories, find still works:
find /media/scott/usb-drive -type f -iname '*.wav'  # finds .wav anywhere in tree

# But if you want to preserve folder structure, use cp -r instead of mv:
# find /media/scott/usb-drive -type d | while read dir; do
#   mkdir -p "/media/scott/NAS1/fileserver/audio/$(date +%Y)/$(echo $dir | sed 's|/media/scott/usb-drive/||')"
# done
# find /media/scott/usb-drive -type f -iname '*.wav' -exec cp -r {} /media/scott/NAS1/fileserver/audio/$(date +%Y)/ {} +
```

## Edge Cases

1. **Files already on NAS**: If files exist at the target path, `mv` will overwrite. Use `-n` flag to skip:
   ```bash
   find ... -exec mv -n -t /target/ {} +  # -n = no clobber
   ```

2. **Case sensitivity**: File extensions may be uppercase (`.WAV`, `.MP4`). All `find` commands use `-iname` for case-insensitive matching.

3. **Large files**: If the USB drive has very large video files, `mv` is instantaneous (same filesystem) but the NAS mount may be slow. Monitor progress:
   ```bash
   du -sh /media/scott/usb-drive/  # check total size before moving
   df -h /media/scott/NAS1/fileserver/  # check available space
   ```

4. **Empty USB drive**: If no files match any category, report it:
   ```bash
   remaining=$(find /media/scott/usb-drive -type f | wc -l)
   if [ "$remaining" -gt 0 ]; then
     echo "WARNING: $remaining files did not match any category. They were left on the USB drive."
     find /media/scott/usb-drive -type f
   fi
   ```

5. **Permission errors**: If the NAS is mounted read-only or permissions are wrong:
   ```bash
   touch /media/scott/NAS1/fileserver/.write-test && rm /media/scott/NAS1/fileserver/.write-test
   # If this fails, the mount is read-only — check mount options
   ```

## Error Handling

```bash
# Check for permission errors
ls -la /media/scott/NAS1/fileserver/  # ensure writable

# Check disk space before moving large batches
df -h /media/scott/NAS1/fileserver/

# If mount fails, check dmesg for USB errors
dmesg | tail -20

# If USB is busy, find and kill processes using it
lsof /media/scott/usb-drive  # find what's using it
fuser -km /media/scott/usb-drive  # kill them
```
