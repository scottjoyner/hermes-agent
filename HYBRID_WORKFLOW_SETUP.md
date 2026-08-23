# Hybrid Workflow Setup: Legacy → Auto-Ingest Bridge Migration

## Overview

This guide shows how to run the legacy `run_all.sh` script via Hermes CLI while transferring processed dashcam content to your NAS for archival, as a bridge strategy before eventually moving to Content OS LLM processing.

### Current Situation ✅

- **Processing**: Running `run_all.sh` via Hermes CLI on main machine
- **Storage**: Data currently stored locally in video-automation repo
- **NAS Location**: `/media/scott/NAS/fileserver/dashcam` (mounted on secondary machine)
- **Goal**: Continue hybrid workflow - transfer historical data to NAS, then gradually migrate to Content OS

---

## Setup Required

### 1. Create Hybrid Transfer Script

Copy the dashcam transfer script and create an optimized version for your current output location:

<tool_call>
<function=terminal>
<parameter=command>
mkdir -p ~/git/auto-ingest/transfer-scripts && cp ~/git/auto-ingest/dashcam_copy.sh ~/git/auto-ingest/transfer-scripts/nas-transfer-via-hermes.sh 2>&1