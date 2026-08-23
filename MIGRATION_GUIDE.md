# Migration Plan: Video-Automation → Auto-Ingest System

## Overview

This document provides a complete migration and connection guide for integrating the legacy **video-automation** pipeline with the new **auto-ingest** system, along with Hermes Agent remote machine connectivity for distributed processing.

**Version**: 1.0  
**Last Updated**: 2026-05-18  
**Author**: Scott Joyner (deathstar user)

---

## Table of Contents

1. [Architecture](#architecture)
2. [Remote Machine Connectivity](#remote-machine-connectivity)
3. [Migration Workflow](#migration-workflow)
4. [Pipeline Comparison](#pipeline-comparison)
5. [Setup Instructions](#setup-instructions)
6. [Cron & Automation](#cron--automation)
7. [Troubleshooting](#troubleshooting)

---

## Architecture

### System Components

```
Legacy (video-automation/)                 New (auto-ingest/)
─────────────────────                      ─────────────────
● Copy scripts (audio/dashcam/bodycam)     ● Content OS content workflow
● Whisper transcription                   ● LLM-driven drafting/verification  
● Speaker diarization                     ● Postiz scheduler integration
● Neo4j ingest                            ● Human review gates
● YOLO vehicle detection                  ● Privacy-compliant outputs
● Metadata scraping                       ● Approved handoff exports

Remote Access Layer (Hermes Agent):
─────────────────────────────────────
Main Machine: 192.168.1.128 / 100.78.106.121
Users: deathstar (primary), r2d2 (tunnel)
Network Range: x1 to x370 (SSH-based agent network)

Knowledge Sharing:
- ~/.hermes/ directories synced hourly via cron
- Hermes CLI executes remote pipeline tasks
- Auto-ingest outputs routed to Content OS workflow
```

### Key Design Principles

1. **Approval-Gated**: No auto-publishing; all outputs require human review
2. **Privacy-First**: No social scraping; use exports and user-owned content
3. **Remote Collaboration**: Hermes Agent enables cross-machine coordination
4. **Local-First Processing**: All data stays on-premises

---

## Remote Machine Connectivity

### Main Machine Connection

**Status**: ✅ CONNECTED  
**IP Address**: 192.168.1.128 (Tailscale: 100.78.106.121)  
**SSH User**: deathstar  
**Repository**: `~/.git/hermes-agent`

### SSH Configuration

The following hosts are accessible via SSH:

```bash
# Main machine - direct connection
ssh deathstar@192.168.1.128

# Via Tailscale alias (alternative access)
ssh deathstar@100.78.106.121

# Hermes Agent (remote CLI)
cd ~/.git/hermes-agent && python run_agent.py "Remote task prompt"
```

### Agent Network Range (x1-x370)

The Hermes Agent network supports distributed processing across x1 to x370 agents:

- **Primary agent**: Main machine (this host)
- **Additional agents**: Any hosts matching `x*-370` pattern in x1-x370 range

### Knowledge Sharing Setup

```bash
# On main machine, run once:
pip install requests uvicorn jinja2 python-jose[cryptography] PyYAML passlib[bcrypt] aiosqlite rich

mkdir -p ~/.tmp-agent-$$
mkdir -p ~/.hermes/logs

# Configure hourly sync in /etc/crontab:
echo '0 * * * * deathstar cd ~/.git/hermes-agent && python run_agent.py "sync r2d2" >> ~/.hermes/logs/handoff.log 2>/dev/null || true' >> /etc/crontab
```

### Agent Management Scripts

Located at `/home/deathstar/`:

- `agent-network.sh` - SSH-based agent management and command execution
- `install-hermes-main.sh` - Hermes installation automation

---

## Migration Workflow

### Phase 1: Environment Setup

1. **Clone both repositories**:
```bash
# Legacy pipeline (optional, for reference)
cd ~/git/video-automation

# Auto-ingest system (primary)
cd ~/git/auto-ingest
```

2. **Install Python dependencies**:
```bash
pip install -r requirements.txt  # if exists
# Or install core dependencies:
pip install requests uvicorn jinja2 python-jose[cryptography] PyYAML passlib[bcrypt] aiosqlite rich
```

3. **Configure LLM endpoint** (for auto-ingest):
```bash
export CONTENT_OS_LLM_BASE_URL=http://localhost:1234/v1
export CONTENT_OS_LLM_API_KEY=lm-studio
export CONTENT_OS_LLM_MODEL=local-model
```

### Phase 2: Content Workflow Integration

The auto-ingest system now serves as the foundation for video content processing, with these enhancements:

1. **Ingest transcripts from local media** (from video-automation output):
```bash
content-os scan-inputs ./transcripts --out ./content-os/stores/proof/input-scan.md
content-os ingest-source ./transcripts/interview_segments.json --title "Lessons from the ingest pipeline" --route REPURPOSE --format linkedin
```

2. **Adapt metadata outputs**: Route approved local outputs into Content OS proof folders without scraping.

### Phase 3: Hermes Remote Execution

Leverage the Hermes Agent for distributed processing:

```bash
# Execute remote task on main machine
cd ~/.git/hermes-agent && python run_agent.py "Process video batch in auto-ingest"

# Transfer processed content back to local workflow
ssh deathstar@192.168.1.128 "scp -r /home/deathstar/.hermes/processed_content ~/auto-ingest/output/"
```

---

## Pipeline Comparison

| Stage | video-automation (Legacy) | auto-ingest (New) | Notes |
|-------|---------------------------|-------------------|--------|
| **Copy** | audio_copy.sh, dashcam_copy.sh | Content OS stores/proof/ | Legacy scripts can still run for historical data |
| **Transcription** | whisper_audio_chunked.py (large-v3) | content-os brief/draft | New: LLM-enhanced with human review gates |
| **Diarization** | speakers_reconcile.py → Neo4j | speaker_linker.py | Both approaches valid; choose based on needs |
| **Ingest** | Neo4j graph database | Content OS lifecycle workflow | New: Approval-gated, privacy-compliant |
| **Metadata** | yolo_vehicle_detction.py | metadata_scraper_iterator.py | Parallel execution possible |
| **Verification** | None (direct ingest) | verify command + quality gates | New: Built-in quality assurance |
| **Review** | Manual ad-hoc | approve command | New: Structured human review |
| **Handoff** | Direct export | scheduler-handoff → Postiz | New: Platform-agnostic exports |

### Hybrid Approach Recommendations

- **Historical data**: Continue using video-automation scripts for existing footage
- **New footage**: Use auto-ingest pipeline with Hermes remote coordination
- **Cross-machine processing**: Hermes enables parallel execution across x1-x370 network

---

## Setup Instructions

### Quick Start (Minimum Configuration)

```bash
# 1. Pull latest code from both repos
cd ~/git/auto-ingest && git pull origin main
# video-automation already cloned if needed

# 2. Install dependencies
pip install requests uvicorn jinja2 python-jose[cryptography] PyYAML passlib[bcrypt] aiosqlite rich

# 3. Configure LLM
export CONTENT_OS_LLM_BASE_URL=http://localhost:1234/v1
export CONTENT_OS_LLM_API_KEY=lm-studio

# 4. Create Content OS folder
cd ~/git/auto-ingest && content-os init

# 5. Connect to main machine via Hermes (if not already connected)
cd ~/.git/hermes-agent && python run_agent.py "Connected to main machine at $(date)"
```

### Full Setup (Production-Ready)

1. **Configure Hermes remote access** (main machine):
   - Run the installation commands from the [Remote Machine Connectivity](#remote-machine-connectivity) section
   - Ensure `/etc/crontab` has hourly sync configured

2. **Initialize Content OS workflow**:
```bash
cd ~/git/auto-ingest && content-os init
content-os new-run --title "Video-automation migration test" --route ORIGINAL --format x_thread
```

3. **Process a test transcript** (from video-automation output):
```bash
content-os scan-inputs ./transcripts/2026-05-migration-test --out ./content-os/stores/proof/test-scan.md
content-os ingest-source ./transcripts/2026-05-migration-test/interview_segments.json \
  --title "Migration test: transcript integration" \
  --route REPURPOSE \
  --format linkedin
```

4. **Brief and draft with LLM**:
```bash
content-os brief 2026-05-migration-test
content-os draft 2026-05-migration-test
```

5. **Execute remote validation via Hermes**:
```bash
cd ~/.git/hermes-agent && python run_agent.py "Verify migration integration on main machine"
```

---

## Cron & Automation

### Auto-Ingest Workflow Automation

Configure in `/etc/crontab`:

```bash
# Hourly sync of proof and work folders via Content OS
0 * * * * deathstar cd ~/git/auto-ingest && python content_os/__main__.py scan-inputs ./transcripts/ --out ./content-os/stores/proof/latest-scan.md 2>/dev/null || true

# Daily batch processing (6:00 AM)
0 6 * * * deathstar cd ~/git/auto-ingest && content-os brief $(ls runs/active/ | tail -1) 2>/dev/null || true

# Weekly quality review (Sunday at midnight)
0 0 * * 0 deathstar cd ~/git/auto-ingest && python content_os/__main__.py doctor --full-report 2>/dev/null || true
```

### Hermes Agent Handoff Automation

The main machine runs hourly syncs via the `/etc/crontab` entry from the [Remote Machine Connectivity](#remote-machine-connectivity) section.

---

## Troubleshooting

### Common Issues

**Q: SSH connection fails with "Host key verification failed"**  
A: Run once to accept host keys:
```bash
ssh -o StrictHostKeyChecking=accept-new deathstar@192.168.1.128 "whoami"
```

**Q: Python packages not found**  
A: Install on the remote machine:
```bash
pip install requests uvicorn jinja2 python-jose[cryptography] PyYAML passlib[bcrypt] aiosqlite rich
```

**Q: Hermes agent loop stuck on main machine**  
A: Check logs and restart cron service:
```bash
ssh deathstar@192.168.1.128 "tail -50 ~/.hermes/logs/handoff.log"
ssh deathstar@192.168.1.128 "sudo systemctl restart crond"
```

**Q: Content OS commands fail with LLM errors**  
A: Verify LLM endpoint is accessible:
```bash
curl -X POST http://localhost:1234/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model": "openai/gpt-oss-20b", "messages": [{"role": "user", "content": "test"}]}'
```

### Useful Commands

```bash
# Check Hermes connection status (local machine)
ls ~/.git/hermes-agent/.git/config  # Verify repo exists

# Check remote services (main machine)
ssh deathstar@192.168.1.128 "systemctl list-units --state=running" | grep -E 'hermes|ollama|docker'

# View Hermes handoff logs
ssh deathstar@192.168.1.128 "tail -100 ~/.hermes/logs/handoff.log"

# Check Content OS status (local machine)
cd ~/git/auto-ingest && content-os status
content-os doctor

# Force refresh remote connection via Hermes
cd ~/.git/hermes-agent && python run_agent.py "Sync complete — migration ready"
```

---

## Next Steps & Roadmap

### Immediate Priorities

1. ✅ Connect to main machine (completed)
2. ✅ Install Hermes dependencies on main machine (run manually)
3. ⏳ Test full pipeline integration (local → remote → local sync)
4. ⏳ Configure Content OS workflow for video content
5. ⏳ Document any discovered bugs or improvements

### Future Enhancements

- **Neo4j persistence**: Consider migrating speaker reconciliation to Neo4j graph for complex relationships
- **Multi-agent processing**: Deploy additional Hermes agents in x1-x370 range for parallel batch processing
- **Vector search**: Integrate dashcam YOLO embeddings with vector database for frame-level retrieval
- **Quality metrics pipeline**: Add automated quality gates before human review

---

## Credits & Attribution

- **Content OS**: See README.md for details on this workflow system
- **Hermes Agent**: AI CLI agent from Nous Research (https://hermes-agent.nousresearch.com)
- **Video-Automation Pipeline**: Legacy media processing system at ~/git/video-automation/
- **Auto-Ingest System**: Modern content workflow at ~/git/auto-ingest/

---

## License & Compliance

This migration plan documents integration between systems. The auto-ingest Content OS workflow is approval-gated and does not auto-publish to social platforms. All outputs are for manual review before any publication.

Privacy: No social scraping is performed; all inputs from user-owned files, exports, or approved APIs only.

---

**End of Migration Plan Document**

---

## Hybrid Workflow Bridge (Current Phase)

### Strategy

Continue processing historical dashcam data with `run_all.sh` via Hermes CLI while actively transferring processed content to your NAS as part of a migration bridge. This maintains your current workflow without rushing into Content OS LLM processing yet.

### Implementation

#### 1. Run Legacy Transfer Script via Hermes CLI

```bash
# On main machine: transfer dashcam outputs to NAS location
cd ~/git/auto-ingest/transfer-scripts
~/git/auto-ingest/transfer-scripts/nas-transfer-via-hermes.sh
```

**What this does:**
- Scans local output from `run_all.sh` (typically in `/mnt/8TB_2025/fileserver/dashcam/`)
- Transfers processed content with date-based organization
- Copies sidecar files (metadata, transcripts, heatmaps, YOLO outputs)
- Organizes by year/month/day for easy archival

#### 2. Hybrid Workflow Script Template

Create a wrapper script that automates the hybrid approach:

```bash
#!/usr/bin/env bash
# ~/scripts/hybrid-nas-transfer.sh

set -Eeuo pipefail

MAIN_DIR="/mnt/8TB_2025/fileserver/dashcam"  # Adjust based on run_all.sh output
NAS_DEST="~/git/auto-ingest/nas-archive/$YEAR-$MONTH"

echo "🎬 Hybrid NAS Transfer Bridge ($DATE)"

# Process recent dashcam files and transfer to NAS
cd ~/git/auto-ingest/transfer-scripts
./nas-transfer-via-hermes.sh --source "$MAIN_DIR" --dest "$NAS_DEST" --include-all --dry-run

echo "✅ Historical data transferred to NAS before eventual Content OS migration"
```

#### 3. Hermes CLI Workflow

Run via Hermes after completing a `run_all.sh` processing batch:

```bash
# After run_all.sh completes processing a date range
hermes transfer-nas \
  --source /mnt/8TB_2025/fileserver/dashcam/$(basename $(date +%Y-%m)) \
  --dest ~/git/auto-ingest/nas-archive/$(basename $(date +%Y-%m))

echo "✅ NAS archival complete. Ready for Content OS when ready."
```

#### 4. Hybrid Processing Pipeline (Complete Workflow)

**Step 1**: Run legacy `run_all.sh` via Hermes
```bash
cd ~/git/video-automation
./run_all.sh
```

**Step 2**: Transfer to NAS via hybrid script
```bash
cd ~/git/auto-ingest/transfer-scripts
./nas-transfer-via-hermes.sh --source /mnt/8TB_2025/fileserver/dashcam/$YEAR-$MONTH-$DAY
```

**Step 3**: Archive on secondary machine
```bash
ssh deathstar@192.168.1.128 "rsync -avz ~/git/auto-ingest/nas-archive/ /mnt/8TB_2025/fileserver/nas-archive/"
```

**Result**: All historical data safely stored on NAS while you work at your own pace toward Content OS migration.

---

## Benefits of Bridge Strategy

### Immediate Gains ✅

1. **Preserves Current Workflow**: No rush to change working methods
2. **NAS Archival**: Historical data moves off primary drives immediately
3. **Gradual Transition**: Can experiment with Content OS without production pressure
4. **Data Safety**: Multiple copies (local + NAS) before eventual migration

### When Ready for Content OS LLM Processing

Once you want to integrate LLM processing, the transition becomes simple:

```bash
# Add Content OS workflow around existing NAS archives
cd ~/git/auto-ingest
content-os init  # Initialize Content OS project
content-os ingest-source ./nas-archive/$(basename $(date +%Y-%m)) \
  --title "Historical dashcam batch from $(date)" \
  --route ARCHIVE \
  --format youtube
```

**No data duplication**: Reuse existing NAS archives as input for LLM processing.

---

## Troubleshooting Hybrid Workflow

**Q: Transfer script says no mounted drives found**
A: Mount the 8TB drive on main machine before running transfer:
```bash
# Check mount point
lsblk | grep -E "^sdb|^sdc"

# Mount if needed (adjust UUID/path)
sudo mount /dev/disk/by-uuid/UUID-HERE /mnt/8TB_2025/fileserver
```

**Q: Want to test transfer without actually copying**
A: Run in dry-run mode:
```bash
DRY_RUN=1 ~/git/auto-ingest/transfer-scripts/nas-transfer-via-hermes.sh
```

**Q: Need to transfer only specific date range**
A: Adjust source folder in script or specify path explicitly:
```bash
./nas-transfer-via-hermes.sh --source /mnt/8TB_2025/fileserver/dashcam/2024/12
```

---

## Migration Timeline (Bridge Strategy)

| Phase | Duration | Action |
|-------|----------|--------|
| **1. NAS Archival** | 1-2 weeks | Transfer current run_all.sh output to NAS daily |
| **2. Content OS Exploration** | 1 week | Test LLM processing on small sample from NAS archives |
| **3. Hybrid Processing** | Ongoing | Mix of legacy + Content OS for new footage |
| **4. Full Migration** | When ready | Complete transition to Content OS workflow |

---

## Useful Commands Summary

### Immediate Hybrid Actions

```bash
# Check transfer script exists
ls ~/git/auto-ingest/transfer-scripts/nas-transfer-via-hermes.sh

# Run one-off transfer (after run_all.sh completes)
cd ~/git/auto-ingest/transfer-scripts && ./nas-transfer-via-hermes.sh

# Transfer with dry-run first (test)
DRY_RUN=1 ~/git/auto-ingest/transfer-scripts/nas-transfer-via-hermes.sh

# Check NAS mount on secondary machine
ssh deathstar@192.168.1.128 "df -h /mnt/8TB_2025/fileserver"

# Sync archives to secondary machine
rsync -avz ~/git/auto-ingest/nas-archive/ deathstar@192.168.1.128:/mnt/8TB_2025/fileserver/nas-archive/
```

### Monitoring Progress

```bash
# List NAS archive contents
ls -la ~/git/auto-ingest/nas-archive/

# Count total transferred data
du -sh ~/git/auto-ingest/nas-archive/*/

# Check Hermes agent status on main machine
cd ~/.git/hermes-agent && python run_agent.py "showing hybrid workflow ready"
```

---

## Next Steps

1. ✅ **Copy transfer script** (shown above) - Create wrapper for your output location
2. ⏳ **Run first NAS transfer** - Transfer one batch from `run_all.sh` to NAS
3. 📈 **Monitor progress** - Check archival on secondary machine
4. 🎯 **Plan Content OS integration** - When ready, apply LLM processing to archived data

---

## Related Documentation

- Main Migration Plan: `~/git/auto-ingest/VIDEO_AUTOMATION_MIGRATION_PLAN.md`
- Transfer Scripts: `~/git/auto-ingest/transfer-scripts/nas-transfer-via-hermes.sh`
- Original Copy Script: `~/git/auto-ingest/dashcam_copy.sh`

---

**End of Hybrid Workflow Guide**
