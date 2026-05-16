# NAS Configuration Template

## Overview

This template provides configuration examples for integrating the NAS with various services.

## Docker Compose Configuration

### Basic NAS Mount

```yaml
version: '3.8'

volumes:
  nas-data:
    driver: local
    driver_opts:
      type: none
      device: /media/scott/NAS/fileserver
      o: bind

services:
  nextcloud:
    image: nextcloud:latest
    volumes:
      - nextcloud-data:/var/www/html
      - nas-data:/mnt/nas:rw
    environment:
      - NEXTCLOUD_TRUSTED_DOMAINS=nextcloud.local
    ports:
      - "8081:80"

volumes:
  nextcloud-data:
    driver: local
```

### Full Stack Configuration

```yaml
version: '3.8'

volumes:
  neo4j-data:
    driver: local
  nextcloud-data:
    driver: local
  nas-data:
    driver: local
    driver_opts:
      type: none
      device: /media/scott/NAS/fileserver
      o: bind

services:
  # Neo4j graph database
  neo4j:
    image: neo4j:latest
    volumes:
      - neo4j-data:/data
      - nas-data:/mnt/nas:rw
    environment:
      - NEO4J_AUTH=neo4j/knowledge_graph_2026
      - NEO4J_dbms_memory_heap_max__size=2G
    ports:
      - "7474:7474"
      - "7687:7687"

  # Nextcloud file sharing
  nextcloud:
    image: nextcloud:latest
    volumes:
      - nextcloud-data:/var/www/html
      - nas-data:/mnt/nas:rw
    environment:
      - NEXTCLOUD_TRUSTED_DOMAINS=nextcloud.local
    ports:
      - "8081:80"

  # Signal CLI REST API
  signal-cli:
    image: pterodactyl/signal-cli-rest-api:latest
    volumes:
      - signal-cli-data:/home/.local/share/signal-cli
      - nas-data:/mnt/nas:rw
    ports:
      - "8400:8400"

  # Redis cache
  redis:
    image: redis:latest
    volumes:
      - redis-data:/data

  # Database
  db:
    image: postgres:latest
    environment:
      - POSTGRES_PASSWORD=knowledge_graph_2026
    volumes:
      - db-data:/var/lib/postgresql/data

  # Nginx reverse proxy
  nginx:
    image: nginx:latest
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - nas-data:/mnt/nas:rw
    ports:
      - "80:80"
      - "443:443"

volumes:
  neo4j-data:
    driver: local
  nextcloud-data:
    driver: local
  signal-cli-data:
    driver: local
  redis-data:
    driver: local
  db-data:
    driver: local
```

## Nginx Reverse Proxy Configuration

```nginx
# nginx.conf
events {
    worker_connections 1024;
}

http {
    upstream neo4j {
        server neo4j:7474;
    }

    upstream nextcloud {
        server nextcloud:80;
    }

    server {
        listen 80;
        server_name nextcloud.local;

        location / {
            proxy_pass http://nextcloud;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }

    server {
        listen 443 ssl;
        server_name nextcloud.local;

        ssl_certificate /etc/ssl/certs/nextcloud.crt;
        ssl_certificate_key /etc/ssl/private/nextcloud.key;

        location / {
            proxy_pass http://nextcloud;
            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;
        }
    }
}
```

## System Configuration

### /etc/fstab Entry

```
# NAS mount
/dev/sda2 /media/scott/NAS/fileserver exfat defaults,rw,nosuid,nodev,relatime,uid=1000,gid=1000,fmask=0022,dmask=0022,iocharset=utf8,errors=remount-ro 0 0
```

### systemd Service for NAS Mount

```ini
# /etc/systemd/system/nas-mount.service
[Unit]
Description=Mount NAS filesystem
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/mount /media/scott/NAS/fileserver
ExecStop=/bin/umount /media/scott/NAS/fileserver

[Install]
WantedBy=multi-user.target
```

### systemd Timer for Health Check

```ini
# /etc/systemd/system/nas-health-check.timer
[Unit]
Description=Run NAS health check every hour

[Timer]
OnBootSec=5min
OnUnitActiveSec=1h
Unit=nas-health-check.service

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/nas-health-check.service
[Unit]
Description=NAS health check

[Service]
Type=oneshot
ExecStart=/home/scott/git/hermes-agent/skills/nas-management/scripts/nas-health-check.sh
```

## Auto-Ingest Configuration

### File Watcher Service

```ini
# /etc/systemd/system/nas-watcher.service
[Unit]
Description=NAS file watcher for auto-ingest
After=network-online.target

[Service]
Type=simple
User=scott
Group=scott
ExecStart=/usr/bin/python3 /home/scott/git/hermes-agent/skills/nas-management/scripts/nas-watcher.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Auto-Ingest Pipeline Configuration

```yaml
# nas-ingest-config.yaml
nas:
  path: /media/scott/NAS/fileserver
  watch_dirs:
    - bodycam
    - dashcam
    - audio
  backup_dir: backups/daily

ingest:
  neo4j:
    uri: bolt://localhost:7687
    user: neo4j
    password: knowledge_graph_2026
    batch_size: 100

  nextcloud:
    url: http://localhost:8081
    user: admin
    password: admin_password_2026
    share_path: /NAS

backup:
  neo4j:
    enabled: true
    schedule: "0 2 * * *"
    retention_days: 30

  signal_cli:
    enabled: true
    schedule: "0 3 * * *"
    retention_days: 30

  nextcloud:
    enabled: true
    schedule: "0 4 * * *"
    retention_days: 30
