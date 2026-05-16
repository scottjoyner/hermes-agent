# NAS Management

## Trigger Conditions

When the user asks about:
- Setting up or configuring the NAS
- Managing NAS storage and files
- Integrating services with the NAS
- Backing up NAS data
- Monitoring NAS health
- Creating NAS-related Docker services
- Auto-ingest pipeline configuration

## Environment

- NAS path: `/media/scott/NAS/fileserver`
- NAS device: `/dev/sda2` (exfat)
- NAS user: `scott` (uid=1000, gid=1000)
- NAS mount options: `defaults,rw,nosuid,nodev,relatime,uid=1000,gid=1000,fmask=0022,dmask=0022,iocharset=utf8,errors=remount-ro`

## NAS Directory Structure

```
/media/scott/NAS/fileserver/
├── backups/
│   ├── daily/
│   └── weekly/
├── bodycam/
├── dashcam/
├── audio/
├── neo4j/
├── nextcloud/
├── signal-cli/
└── docker-compose/
```

## Key Procedures

### 1. Check NAS Health

```bash
# Run health check
/home/scott/git/hermes-agent/skills/nas-management/scripts/nas-health-check.sh

# Check mount status
mountpoint /media/scott/NAS/fileserver

# Check disk usage
df -h /media/scott/NAS/fileserver
```

### 2. Mount NAS

```bash
# Mount NAS
mount /media/scott/NAS/fileserver

# Verify mount
df -h /media/scott/NAS/fileserver
```

### 3. Configure Docker Services with NAS

```bash
# Edit docker-compose.yml
nano /home/scott/docker-compose/docker-compose.yml

# Add NAS mount to services
# volumes:
#   - nas-data:/mnt/nas:rw

# Restart services
cd /home/scott/docker-compose
docker-compose up -d
```

### 4. Backup NAS Data

```bash
# Run daily backup
/home/scott/git/hermes-agent/skills/nas-management/scripts/nas-daily-backup.sh

# Run weekly backup
/home/scott/git/hermes-agent/skills/nas-management/scripts/nas-weekly-backup.sh
```

### 5. Monitor Services

```bash
# Check all services
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Check specific services
docker logs neo4j
docker logs nextcloud
docker logs signal-cli
docker logs nginx
```

## Integration Patterns

### Neo4j Integration

- Store graph data on NAS for persistence
- Use bind mount for Neo4j data directory
- Backup Neo4j data daily

### Nextcloud Integration

- Store Nextcloud data on NAS
- Use bind mount for Nextcloud data directory
- Sync NAS files to Nextcloud shares

### Signal CLI Integration

- Store Signal CLI data on NAS
- Use bind mount for Signal CLI data directory
- Backup Signal CLI data daily

## Auto-Ingest Pipeline

### File Watcher

- Monitor NAS directories for new files
- Process files based on type (video, audio, images)
- Trigger Neo4j ingestion and Nextcloud sync

### Ingestion Steps

1. Detect new file in watched directory
2. Compute file hash and metadata
3. Determine file type and processing pipeline
4. Trigger appropriate processing (video, audio, image)
5. Ingest into Neo4j graph database
6. Sync to Nextcloud
7. Update ingestion state

## Troubleshooting

### NAS Not Mounting

1. Check /etc/fstab entry
2. Run `mount -a` to test
3. Check dmesg for errors
4. Verify device path (/dev/sda2)

### Docker Services Failing

1. Check logs: `docker logs <service>`
2. Verify NAS is mounted
3. Check permissions
4. Restart service: `docker-compose restart <service>`

### Permission Issues

1. Verify uid/gid match between host and containers
2. Check NAS mount options
3. Fix permissions: `chown -R scott:scott /media/scott/NAS/fileserver`

## Files

- SKILL.md: This file
- references/integration-patterns.md: Integration patterns reference
- templates/nas-config-template.md: Configuration templates
- scripts/nas-health-check.sh: Health check script
- scripts/nas-watcher.py: Auto-ingest watcher script
