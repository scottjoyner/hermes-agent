# Auto-Ingest: New Data Ingestion

Manage the auto-ingest pipeline for ingesting new files into the Neo4j knowledge graph. This skill covers file organization, path fixes, and ingestion workflow.

## Critical Path Fix

**The real data mount is `/media/scott/NAS1/fileserver/`** (81TB exfat). All code currently references `/media/scott/NAS/fileserver/` which is a stale empty directory.

### Files that need path correction

In every file below, replace `/media/scott/NAS/fileserver/` with `/media/scott/NAS1/fileserver/`:

1. **`/home/scott/git/auto-ingest/ingest_transcriptsv5_3.py`** (lines 42-55, 71, 90-94)
   - `DEFAULT_SCAN_ROOTS` array
   - `DASHCAM_ROOT` default
   - `AUDIO_BASE` path
   - `DEFAULT_RTTM_DIRS` array
2. **`/home/scott/git/auto-ingest/run_ingest_all.sh`** (lines 33, 36)
   - `SCAN_ROOTS` export
   - `DASHCAM_ROOT` export
3. **`/home/scott/git/auto-ingest/deploy/path_profiles.env`** (create from example, then fix)
   - All `*_ROOT` paths
   - `SCAN_ROOTS` line
4. **Docker compose services** — check mounted paths in `docker-compose.yml`

### Quick path fix command

```bash
cd /home/scott/git/auto-ingest
# Fix the Python script
sed -i 's|/media/scott/NAS/fileserver/|/media/scott/NAS1/fileserver/|g' ingest_transcriptsv5_3.py
# Fix the shell script
sed -i 's|/media/scott/NAS/fileserver/|/media/scott/NAS1/fileserver/|g' run_ingest_all.sh
# Fix the env file
cp deploy/path_profiles.env.example deploy/path_profiles.env
sed -i 's|/nas/fileserver/|/media/scott/NAS1/fileserver/|g' deploy/path_profiles.env
```

## File Organization Structure

Drop new files into `/media/scott/NAS1/fileserver/` organized by type. The pipeline scans these roots:

| Type | Target Directory | File Formats |
|------|-----------------|--------------|
| Audio recordings | `/media/scott/NAS1/fileserver/audio/` | .wav, .mp3, .m4a, .flac |
| Audio transcriptions | `/media/scott/NAS1/fileserver/audio/transcriptions/` | .txt, .csv (paired with audio) |
| Dashcam footage | `/media/scott/NAS1/fileserver/dashcam/` | .avi, .mp4, .mov, .mkv + `_metadata.csv` |
| Dashcam audio | `/media/scott/NAS1/fileserver/dashcam/audio/` | .wav, .mp3 |
| Dashcam transcriptions | `/media/scott/NAS1/fileserver/dashcam/transcriptions/` | .txt, .csv |
| Bodycam footage | `/media/scott/NAS1/fileserver/bodycam/` | .mp4, .mov |
| Headcam footage | `/media/scott/NAS1/fileserver/headcam/` | .avi, .mp4 |
| Personal/home video | `/media/scott/NAS1/fileserver/joyner/` | Any video/audio |

### Directory naming convention

- **Audio:** `/media/scott/NAS1/fileserver/audio/YYYYMMDD_HHMMSS.description.wav`
- **Dashcam:** `/media/scott/NAS1/fileserver/dashcam/YYYY/` subfolders (existing structure)
- **Bodycam:** `/media/scott/NAS1/fileserver/bodycam/YYYYMMDD_HHMMSS.description.mp4`
- **Transcriptions:** Must be paired with media file using `_transcription.txt` or `_transcription.csv` naming

## Workflow: Loading New Data

### Step 1: Drop files

```bash
# For audio
mkdir -p /media/scott/NAS1/fileserver/audio/2026
cp /path/to/new/audio.wav /media/scott/NAS1/fileserver/audio/2026/

# For dashcam
mkdir -p /media/scott/NAS1/fileserver/dashcam/2026
cp /path/to/new/dashcam.mp4 /media/scott/NAS1/fileserver/dashcam/2026/
cp /path/to/metadata.csv /media/scott/NAS1/fileserver/dashcam/2026/

# For bodycam
mkdir -p /media/scott/NAS1/fileserver/bodycam/2026
cp /path/to/new/bodycam.mp4 /media/scott/NAS1/fileserver/bodycam/2026/
```

### Step 2: Verify paths are fixed

```bash
# Confirm the code points to the right place
grep "NAS1/fileserver" /home/scott/git/auto-ingest/ingest_transcriptsv5_3.py | head -5
grep "NAS1/fileserver" /home/scott/git/auto-ingest/run_ingest_all.sh | head -5
```

### Step 3: Trigger ingestion

```bash
cd /home/scott/git/auto-ingest

# Dry run to preview what would be ingested
DRY_RUN=1 ./run_ingest_all.sh

# Actual ingestion
./run_ingest_all.sh

# Or via Docker (if services are running)
docker compose restart ingest-service
```

### Step 4: Monitor

```bash
# Watch logs
docker compose logs -f ingest-service
tail -f /home/scott/git/auto-ingest/logs/ingest_*.log | tail -50

# Check Neo4j for new nodes
docker exec neo4j cypher-shell -u neo4j -p knowledge_graph_2026 \
  "MATCH (n) RETURN labels(n) AS label, count(*) AS cnt ORDER BY cnt DESC LIMIT 10"
```

## Docker Pipeline Services

| Service | Poll | Purpose |
|---------|------|---------|
| `ingest-service` | 5 min | Runs `run_ingest_all.sh` continuously |
| `ingest-worker` | 30s | Claims `.job` files from `/nas/drop/` |
| `sync-service` | 10 min | Legacy drop sync from deathstar |
| `neo4j` | — | Graph database (port 7474/7687) |

## Common Issues

### Pipeline not picking up new files
1. Check paths are fixed (step 2 above)
2. Check Docker services are running: `docker compose ps`
3. Check the ingest service log: `docker compose logs --tail=50 ingest-service`
4. Verify file permissions: files must be readable by the container user

### Neo4j connection failures
- Password in code is `knowledge_graph_2026`
- Docker compose may use different password — align before running
- Check: `docker exec neo4j cypher-shell -u neo4j -p knowledge_graph_2026 "RETURN 1"`

### Container can't see NAS1
- The Docker compose volume mounts must include `/media/scott/NAS1`
- Check `docker-compose.yml` for volume definitions
- If missing, add: `- /media/scott/NAS1:/media/scott/NAS1`

## Key Files Reference

| File | Purpose |
|------|---------|
| `ingest_transcriptsv5_3.py` | Main Python ingest engine |
| `run_ingest_all.sh` | Shell wrapper with env vars, runs the Python script |
| `deploy/path_profiles.env` | Host-specific path configuration |
| `deploy/sync_from_legacy_drop.sh` | Legacy deathstar drop sync |
| `deploy/worker_ingest.sh` | Distributed job queue worker |
| `deploy/job_trigger_api.py` | HTTP API for enqueueing jobs (port 8766) |
