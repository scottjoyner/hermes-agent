# Handoff: Paperclip Integration for Hermes Agent Messaging

## Status: ACTIVE - Paperclip Running in Docker

**Last Updated:** 2026-05-14 15:30 UTC
**Owner:** scott (primary), Hermes Agent (current worker)
**Priority:** CRITICAL - This is the messaging layer for agent-to-agent communication

---

## What's Done

### 1. Paperclip Deployed in Docker
- **Container:** `docker-paperclip-1` (running, healthy)
- **Image:** `docker-paperclip:latest` (built from paperclip/Dockerfile)
- **Port:** 3100 (exposed on 0.0.0.0)
- **Mode:** `authenticated` + `private`
- **Bind:** `lan` (0.0.0.0)
- **Database:** Embedded PostgreSQL (persistent in bind mount)
- **Data Directory:** `paperclip/data/docker-paperclip`
- **Bootstrap:** COMPLETE (`bootstrapInviteActive: true`)

**Health Check:**
```bash
curl http://localhost:3100/api/health
# Returns: {"status":"ok","deploymentMode":"authenticated","bootstrapStatus":"bootstrap_complete","bootstrapInviteActive":true}
```

### 2. Repos Cloned
- `/home/scott/git/hermes-agent/paperclip` - Paperclip orchestration platform
- `/home/scott/git/hermes-agent/hermes-paperclip-adapter` - Hermes adapter for Paperclip
- `/home/scott/git/hermes-agent/hermes-agent-self-evolution` - Self-improvement pipeline

### 3. Hermes Paperclip Adapter
- Located at: `/home/scott/git/hermes-agent/hermes-paperclip-adapter/`
- Implements `ServerAdapterModule` interface
- Provides 8 inference providers (Anthropic, OpenRouter, OpenAI, Nous, etc.)
- Skills integration (syncs Paperclip + Hermes skills)
- Structured transcript parsing (raw Hermes stdout → typed `TranscriptEntry`)
- Session codec for persistence across heartbeats

**Adapter Config Example:**
```json
{
  "name": "Hermes Engineer",
  "adapterType": "hermes_local",
  "adapterConfig": {
    "model": "anthropic/claude-sonnet-4",
    "maxIterations": 50,
    "timeoutSec": 300,
    "persistSession": true,
    "enabledToolsets": ["terminal", "file", "web"]
  }
}
```

### 4. Tailscale Network
- **Local IP:** 100.64.43.123
- **Status:** Active, connected to kipnerter tailnet
- **Reachability:** Paperclip bound to `lan` (0.0.0.0), accessible via Tailscale

---

## What's Blocked

### 1. Hermes Adapter Not Yet Registered
- The `hermes-paperclip-adapter` repo is cloned but NOT yet registered as a Paperclip plugin
- **Next Step:** Install adapter as Paperclip plugin via `~/.paperclip/adapter-plugins.json`
- **Path:** Use `file:` entry pointing to `/home/scott/git/hermes-agent/hermes-paperclip-adapter/`

### 2. Hermes Agent Not Yet Hired
- No Hermes agent created in Paperclip board yet
- **Next Step:** Create agent in Paperclip UI or via API with `adapterType: "hermes_local"`

### 3. Git Push Not Yet Done
- hermes-agent repo has NO remotes configured
- **Next Step:** Add remote and push updates so other agents know what's coming

---

## Docker Management Commands

### Start Paperclip
```bash
cd /home/scott/git/hermes-agent
BETTER_AUTH_SECRET=$(openssl rand -hex 32) \
  docker compose -f paperclip/docker/docker-compose.quickstart.yml up --build -d
```

### Stop Paperclip
```bash
cd /home/scott/git/hermes-agent
docker compose -f paperclip/docker/docker-compose.quickstart.yml down
```

### Check Status
```bash
docker ps --filter name=paperclip
curl http://localhost:3100/api/health
docker logs docker-paperclip-1 --tail 50
```

### View Logs
```bash
docker logs -f docker-paperclip-1
```

### Restart
```bash
docker restart docker-paperclip-1
```

---

## Paperclip Onboarding (If Rebuilt)

If Paperclip needs to be rebuilt or reset:

1. **Run onboard:**
   ```bash
   sudo docker exec docker-paperclip-1 pnpm paperclipai onboard -y --bind lan
   ```

2. **Bootstrap CEO (if needed):**
   ```bash
   sudo docker exec docker-paperclip-1 pnpm paperclipai auth bootstrap-ceo
   ```

3. **Check bootstrap status:**
   ```bash
   curl http://localhost:3100/api/health
   # Look for: "bootstrapStatus":"bootstrap_complete","bootstrapInviteActive":true
   ```

---

## Hermes Adapter Installation

### Option 1: Plugin Manager (Recommended)
1. Create `~/.paperclip/adapter-plugins.json`:
   ```json
   {
     "plugins": [
       {
         "name": "hermes-paperclip-adapter",
         "type": "hermes_local",
         "source": "file",
         "path": "/home/scott/git/hermes-agent/hermes-paperclip-adapter/"
       }
     ]
   }
   ```

2. Restart Paperclip:
   ```bash
   docker restart docker-paperclip-1
   ```

3. Verify adapter loads:
   ```bash
   curl http://localhost:3100/api/plugins
   ```

### Option 2: Direct Registry (For Development)
Edit `paperclip/server/src/adapters/registry.ts`:
```typescript
import * as hermesLocal from "/home/scott/git/hermes-agent/hermes-paperclip-adapter";
import { execute, testEnvironment, detectModel, listSkills, syncSkills, sessionCodec } from "hermes-paperclip-adapter/server";

registry.set("hermes_local", {
  ...hermesLocal,
  execute,
  testEnvironment,
  detectModel,
  listSkills,
  syncSkills,
  sessionCodec,
});
```

---

## Creating a Hermes Agent in Paperclip

### Via API
```bash
curl -X POST http://localhost:3100/api/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Hermes Engineer",
    "adapterType": "hermes_local",
    "adapterConfig": {
      "model": "anthropic/claude-sonnet-4",
      "maxIterations": 50,
      "timeoutSec": 300,
      "persistSession": true,
      "enabledToolsets": ["terminal", "file", "web"]
    }
  }'
```

### Via UI
1. Open Paperclip board: `http://localhost:3100`
2. Go to Agents → Create Agent
3. Select adapter type: `hermes_local`
4. Fill in config (see above)

---

## Architecture

```
Tailscale Network (100.64.43.123)
    │
    ├─ Paperclip Server (port 3100) [DOCKER RUNNING]
    │   ├─ Board UI (management)
    │   ├─ API (agent coordination)
    │   ├─ Heartbeat system (30s intervals)
    │   └─ Embedded PostgreSQL
    │
    ├─ Hermes Agent [NOT YET HIRED]
    │   ├─ Receives tasks from Paperclip
    │   ├─ Executes with full tool suite
    │   └─ Reports results back
    │
    └─ Other Agents (OpenClaw, Claude, etc.) [FUTURE]
```

---

## Known Issues & Pitfalls

### 1. Docker Permission Denied
- **Symptom:** `unable to get image: permission denied while trying to connect to the docker API`
- **Fix:** Add user to docker group: `sudo usermod -aG docker scott`
- **Note:** Requires `newgrp docker` or re-login to take effect

### 2. BETTER_AUTH_SECRET Required
- **Symptom:** `required variable BETTER_AUTH_SECRET is missing a value`
- **Fix:** Set `BETTER_AUTH_SECRET` env var or use `--env-file`

### 3. Bootstrap Pending
- **Symptom:** Health check shows `"bootstrapStatus":"bootstrap_pending"`
- **Fix:** Run `pnpm paperclipai onboard -y` or `pnpm paperclipai auth bootstrap-ceo`

### 4. Adapter Not Loading
- **Symptom:** `hermes_local` adapter not available in Paperclip UI
- **Fix:** Verify adapter path in `~/.paperclip/adapter-plugins.json` is correct

### 5. Paperclip Hanging on Startup
- **Symptom:** Process tree shows node processes but no listening ports
- **Fix:** Check if port 3100 is in use: `ss -tlnp | grep 3100`
- **Note:** Kill existing processes: `pkill -9 -f paperclip`

---

## Next Steps (For Continuation)

1. **Install Hermes Adapter** as Paperclip plugin
2. **Create Hermes Agent** in Paperclip board
3. **Test Heartbeat** - verify Hermes wakes on schedule and receives tasks
4. **Push hermes-agent Updates** - add remote and push so other agents know what's coming
5. **Document Agent Capabilities** - create agent profile for Paperclip board

---

## File Locations

- Paperclip Source: `/home/scott/git/hermes-agent/paperclip/`
- Paperclip Docker: `docker-paperclip-1`
- Paperclip Data: `/home/scott/git/hermes-agent/paperclip/data/docker-paperclip/`
- Hermes Adapter: `/home/scott/git/hermes-agent/hermes-paperclip-adapter/`
- Self-Evolution: `/home/scott/git/hermes-agent/hermes-agent-self-evolution/`
- Hermes Agent: `/home/scott/git/hermes-agent/`

---

## Contact

- **Primary:** scott (scottjoyner)
- **Current Worker:** Hermes Agent (this session)
- **Other Agents:** Check this document first before working on Paperclip integration

---

*This document is the source of truth for Paperclip integration status. Update it as work progresses.*
