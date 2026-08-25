# NAS Integration Patterns

## Overview

This document provides integration patterns for connecting the NAS to various services and applications.

## Docker Integration

### Bind Mount (Recommended)

```yaml
volumes:
  nas-data:
    driver: local
    driver_opts:
      type: none
      device: /media/scott/NAS/fileserver
      o: bind
```

### NFS Mount

```yaml
volumes:
  nas-data:
    type: nfs
    o: addr=192.168.1.100,rw
    device: ":/fileserver"
```

## Service Integration

### Neo4j Integration

```bash
# Connect to Neo4j with NAS data
neo4j-admin import --database=knowledge_graph --nodes=/media/scott/NAS/fileserver/neo4j/nodes.csv --relationships=/media/scott/NAS/fileserver/neo4j/rels.csv
```

### Nextcloud Integration

```bash
# Sync NAS data to Nextcloud
nextcloud-occ files:copy /media/scott/NAS/fileserver /NAS
```

### Signal CLI Integration

```bash
# Backup Signal CLI data to NAS
rsync -avz /home/scott/.local/share/signal-cli/ /media/scott/NAS/fileserver/backups/signal-cli/
```

## Backup Strategies

### Daily Backup Script

```bash
#!/bin/bash
# Daily backup script for NAS data

NAS_PATH="/media/scott/NAS/fileserver"
BACKUP_DIR="/media/scott/NAS/fileserver/backups/daily"
DATE=$(date +%Y%m%d)

# Create backup directory
mkdir -p "$BACKUP_DIR/$DATE"

# Backup Neo4j data
docker exec neo4j neo4j-admin dump --to=$BACKUP_DIR/$DATE/neo4j.dump

# Backup Signal CLI data
rsync -avz /home/scott/.local/share/signal-cli/ $BACKUP_DIR/$DATE/signal-cli/

# Backup Nextcloud data
rsync -avz /var/lib/docker/volumes/nextcloud-data/_data/ $BACKUP_DIR/$DATE/nextcloud/

# Clean up old backups (keep last 30 days)
find $BACKUP_DIR -maxdepth 1 -type d -mtime +30 -exec rm -rf {} \;
```

### Weekly Full Backup

```bash
#!/bin/bash
# Weekly full backup script for NAS data

NAS_PATH="/media/scott/NAS/fileserver"
BACKUP_DIR="/media/scott/NAS/fileserver/backups/weekly"
DATE=$(date +%Y%m%d)

# Create backup directory
mkdir -p "$BACKUP_DIR/$DATE"

# Full system backup
tar -czf $BACKUP_DIR/$DATE/full-backup.tar.gz     /etc/systemd     /etc/docker     /home/scott/docker-compose     /media/scott/NAS/fileserver/backups/daily
```

## Monitoring

### Health Check Integration

```bash
# Run health check
/home/scott/git/hermes-agent/skills/nas-management/scripts/nas-health-check.sh

# Check logs
tail -f /var/log/nas-health-check.log
```

### Service Status

```bash
# Check all services
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# Check specific services
docker ps --filter "name=neo4j" --filter "name=nextcloud" --filter "name=signal-cli" --filter "name=nginx"
```

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

## Troubleshooting

### Common Issues

1. **NAS not mounting**: Check /etc/fstab and run `mount -a`
2. **Docker services failing**: Check logs with `docker logs <service>`
3. **Permission issues**: Verify user/group IDs match between host and containers
4. **Disk space**: Run `df -h` to check available space

### Recovery Procedures

1. **Recover Neo4j data**: `neo4j-admin load --from=neo4j.dump --database=knowledge_graph --force`
2. **Recover Signal CLI data**: Copy from backup to `/home/scott/.local/share/signal-cli/`
3. **Recover Nextcloud data**: Copy from backup to `/var/lib/docker/volumes/nextcloud-data/_data/`
