# NAS Management Skill

## Overview

This skill provides comprehensive management capabilities for the NAS (Network Attached Storage) system, including health monitoring, backup automation, service integration, and auto-ingest pipeline configuration.

## Features

- Health monitoring and diagnostics
- Automated backup strategies (daily/weekly)
- Docker service integration patterns
- Auto-ingest pipeline for file processing
- Neo4j graph database integration
- Nextcloud file sharing integration
- Signal CLI data management
- Security hardening guidelines

## Quick Start

### 1. Check NAS Health

```bash
# Run health check
/home/scott/git/hermes-agent/skills/nas-management/scripts/nas-health-check.sh

# Check logs
tail -f /var/log/nas-health-check.log
```

### 2. Mount NAS

```bash
# Mount NAS
mount /media/scott/NAS/fileserver

# Verify mount
df -h /media/scott/NAS/fileserver
```

### 3. Configure Docker Services

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

## Directory Structure

```
skills/nas-management/
├── SKILL.md              # Main skill documentation
├── README.md             # This file
├── references/
│   └── integration-patterns.md  # Integration patterns reference
├── templates/
│   └── nas-config-template.md   # Configuration templates
└── scripts/
    ├── nas-health-check.sh  # Health check script
    └── nas-watcher.py       # Auto-ingest watcher script
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

## Backup Strategies

### Daily Backup

- Backup Neo4j data
- Backup Signal CLI data
- Backup Nextcloud data
- Clean up old backups (keep last 30 days)

### Weekly Backup

- Full system backup
- Archive daily backups
- Verify backup integrity

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

## Security

### Firewall Configuration

```bash
# Allow necessary ports
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 7474/tcp
ufw allow 7687/tcp
ufw allow 8400/tcp
ufw allow 8081/tcp
```

### Docker Security

```yaml
# docker-compose.yml security configuration
services:
  neo4j:
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp
    volumes:
      - neo4j-data:/data
      - nas-data:/mnt/nas:rw

  nextcloud:
    security_opt:
      - no-new-privileges:true
    read_only: true
    tmpfs:
      - /tmp
    volumes:
      - nextcloud-data:/var/www/html
      - nas-data:/mnt/nas:rw
```

## Contributing

When adding new NAS-related skills or integrations:

1. Update this README with new patterns
2. Add integration patterns to references/
3. Add configuration templates to templates/
4. Add scripts to scripts/
5. Update SKILL.md with new trigger conditions

## Maintenance

### Regular Tasks

- Weekly: Verify backup integrity
- Monthly: Review disk usage and clean up
- Quarterly: Update security configurations
- Annually: Review and update integration patterns

### Monitoring

- Set up systemd timers for health checks
- Configure email alerts for critical issues
- Monitor Docker service status
- Track NAS disk usage trends
