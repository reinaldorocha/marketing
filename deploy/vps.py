#!/usr/bin/env python3
"""Root-only deployment to an existing Docker/PostgreSQL/NPM VPS; stdlib only.

Secrets live outside the checkout. Commands and API error bodies are never
echoed: both may contain database passwords, signing keys or provider tokens.
"""

import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
STATE = Path("/etc/marketing")
ENV_FILE = STATE / "marketing.env"
BACKUPS = Path("/var/backups/marketing")


def run(args, *, data=None, timeout=120, log=None):
    result = subprocess.run(args, input=data, capture_output=True, timeout=timeout, cwd=ROOT)
    if log:
        with log.open("ab") as handle:
            handle.write(result.stdout + result.stderr)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}); inspect the protected deployment log")
    return result.stdout.decode()


def inspect(name):
    return json.loads(run(["docker", "inspect", name]))[0]


def read_env():
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def save_env(values):
    # Our generated values contain no shell substitutions; never source this file.
    temporary = ENV_FILE.with_suffix(".tmp")
    with temporary.open("w") as handle:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise RuntimeError(f"Multiline environment value unsupported: {key}")
            handle.write(f"{key}={value}\n")
    temporary.chmod(0o600)
    temporary.replace(ENV_FILE)


def compose(*args, log=None, timeout=120, data=None):
    return run(
        ["docker", "compose", "--project-name", "marketing", "--env-file", str(ENV_FILE),
         "-f", str(ROOT / "deploy/docker-compose.vps.yml"), *args],
        log=log, timeout=timeout, data=data,
    )


def validate_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,50}", value):
        raise RuntimeError("Database and role names must be lowercase SQL identifiers")
    return value


def literal(value):
    return "'" + value.replace("'", "''") + "'"


def pg(env, query, database="postgres"):
    return run(
        ["docker", "exec", "-i", "-u", "postgres", env["DB_CONTAINER"], "psql", "-X",
         "-v", "ON_ERROR_STOP=1", "-U", env["DB_ADMIN_USER"], "-d", database, "-At"],
        data=query.encode(),
    ).strip()


def provision_database(env):
    role = validate_name(env["POSTGRES_USER"])
    database = validate_name(env["POSTGRES_DB"])
    exists = pg(env, f"SELECT 1 FROM pg_roles WHERE rolname={literal(role)}")
    if not exists:
        pg(env, f"CREATE ROLE {role} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
           f"PASSWORD {literal(env['POSTGRES_PASSWORD'])}")
    privileges = pg(env, f"SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication "
                    f"FROM pg_roles WHERE rolname={literal(role)}")
    if privileges != "f":
        raise RuntimeError("Refusing to use a privileged application database role")
    owner = pg(env, f"SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname={literal(database)}")
    if not owner:
        pg(env, f"CREATE DATABASE {database} OWNER {role}")
    elif owner != role:
        raise RuntimeError("Database already exists with a different owner; refusing to change it")
    print("Dedicated database and restricted role ready", flush=True)


def create_backup(env, revision):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    folder = BACKUPS / stamp
    folder.mkdir(parents=True, mode=0o700)
    shutil.copy2(ENV_FILE, folder / "marketing.env")
    shutil.copy2(ROOT / "deploy/docker-compose.vps.yml", folder / "docker-compose.vps.yml")
    (folder / "revision-before.txt").write_text(revision + "\n")
    destination = folder / "database.dump"
    with destination.open("wb") as handle:
        result = subprocess.run(
            ["docker", "exec", "-u", "postgres", env["DB_CONTAINER"], "pg_dump", "-U",
             env["DB_ADMIN_USER"], "-Fc", "-d", env["POSTGRES_DB"]],
            stdout=handle, stderr=subprocess.PIPE, timeout=600,
        )
    if result.returncode or destination.read_bytes()[:5] != b"PGDMP":
        raise RuntimeError(f"Database backup failed: {folder}")
    # pg_restore -l checks the custom archive directory without changing the DB.
    run(["docker", "exec", "-i", env["DB_CONTAINER"], "pg_restore", "--list"],
        data=destination.read_bytes(), log=folder / "backup-validation.log")
    probe = subprocess.run(["docker", "volume", "inspect", "marketing_media"], capture_output=True)
    if probe.returncode == 0:
        source = json.loads(probe.stdout)[0]["Mountpoint"]
        with tarfile.open(folder / "media.tar.gz", "w:gz") as archive:
            archive.add(source, arcname="media")
    print(f"Backup: {folder}", flush=True)
    return folder


def npm_client(env, backup):
    npm = inspect(env["NPM_CONTAINER"])
    data_mount = next(m["Source"] for m in npm["Mounts"] if m["Destination"] == "/data")
    # This deployment targets the existing SQLite-based NPM installation.
    db_path = Path(data_mount) / "database.sqlite"
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as connection:
        admins = connection.execute(
            "SELECT id,email,roles FROM user WHERE is_deleted=0 AND is_disabled=0 ORDER BY id"
        ).fetchall()
        admin = next(row for row in admins if "admin" in json.loads(row[2]))
        with sqlite3.connect(backup / "npm-before.sqlite") as target:
            connection.backup(target)
    # Root already controls this NPM instance. Issue a short-lived token through
    # its installed JWT library/key, without changing passwords or exposing keys.
    code = (
        'import fs from "node:fs"; import jwt from "jsonwebtoken"; '
        'const key=JSON.parse(fs.readFileSync("/data/keys.json")).key; '
        f'console.log(jwt.sign({{iss:"api",attrs:{{id:{admin[0]}}},scope:["user"]}},key,'
        '{algorithm:"RS256",expiresIn:"10m"}));'
    )
    token = run(["docker", "exec", "-w", "/app", env["NPM_CONTAINER"],
                 "node", "--input-type=module", "-e", code]).strip()
    ip = npm["NetworkSettings"]["Networks"][env["EXTERNAL_DOCKER_NETWORK"]]["IPAddress"]

    def api(path, payload=None, method=None):
        request = urllib.request.Request(
            f"http://{ip}:81/api{path}",
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=300) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # NPM error bodies may contain credentials; keep them root-only.
            (backup / "npm-api-error.json").write_bytes(exc.read())
            raise RuntimeError(f"NPM API failed ({exc.code}); see {backup}/npm-api-error.json") from None

    return api, admin[1]


def configure_proxy(env, backup):
    api, contact = npm_client(env, backup)
    domain = env["APP_DOMAIN"]
    hosts = api("/nginx/proxy-hosts")
    matches = [h for h in hosts if domain in h["domain_names"]]
    if matches and (matches[0]["forward_host"] != "marketing-web" or matches[0]["domain_names"] != [domain]):
        raise RuntimeError("Domain belongs to another NPM proxy; refusing to overwrite it")
    certificates = api("/nginx/certificates")
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = next((c for c in certificates if c["domain_names"] == [domain]
                 and datetime.datetime.fromisoformat(c["expires_on"].replace("Z", "+00:00")).replace(
                     tzinfo=datetime.timezone.utc) > now + datetime.timedelta(days=2)), None)
    if not cert:
        print("Requesting HTTPS certificate from Let's Encrypt", flush=True)
        cert = api("/nginx/certificates", {
            "provider": "letsencrypt", "domain_names": [domain],
            "meta": {"letsencrypt_email": env.get("ACME_EMAIL") or contact,
                     "letsencrypt_agree": True, "dns_challenge": False},
        })
    payload = {
        "domain_names": [domain], "forward_scheme": "http", "forward_host": "marketing-web",
        "forward_port": 8000, "certificate_id": cert["id"], "ssl_forced": True,
        "hsts_enabled": True, "hsts_subdomains": False, "http2_support": True,
        "block_exploits": True, "caching_enabled": False, "allow_websocket_upgrade": True,
        "access_list_id": 0, "advanced_config": "client_max_body_size 210m;\nproxy_read_timeout 130s;",
        "enabled": True, "locations": [], "meta": {},
    }
    if matches:
        result = api(f"/nginx/proxy-hosts/{matches[0]['id']}", payload, "PUT")
    else:
        result = api("/nginx/proxy-hosts", payload)
    if not result.get("meta", {}).get("nginx_online", True):
        raise RuntimeError("NPM rejected the generated nginx configuration")
    run(["docker", "exec", env["NPM_CONTAINER"], "nginx", "-t"], log=backup / "deploy.log")
    (backup / "proxy-result.json").write_text(json.dumps({"proxy_id": result["id"], "certificate_id": cert["id"]}))
    print("Nginx proxy and HTTPS configured", flush=True)


def wait_healthy():
    for _ in range(90):
        app_id = compose("ps", "-q", "app").strip()
        worker_id = compose("ps", "-q", "worker").strip()
        if app_id and worker_id:
            app, worker = inspect(app_id), inspect(worker_id)
            if app["State"].get("Health", {}).get("Status") == "healthy" and worker["State"]["Running"]:
                if worker["RestartCount"]:
                    raise RuntimeError("Worker restarted during deployment; inspect its log")
                return
        time.sleep(2)
    raise RuntimeError("Application/worker did not become healthy within 180 seconds")


def verify_public(env):
    domain = env["APP_DOMAIN"]
    # Verify TLS hostname/chain and redirect using the real local nginx listener,
    # even when public DNS is behind a CDN. Nothing bypasses TLS verification.
    headers = run(["curl", "--fail", "--silent", "--show-error", "--max-time", "30",
                   "--resolve", f"{domain}:443:127.0.0.1", "-D", "-", f"https://{domain}/healthz"])
    if '"database": "ok"' not in headers:
        raise RuntimeError("Public health endpoint did not confirm the database")
    for name in ["strict-transport-security:", "x-content-type-options:", "x-frame-options:", "referrer-policy:"]:
        if name not in headers.lower():
            raise RuntimeError(f"Missing security header: {name}")
    code = run(["curl", "--silent", "--show-error", "--max-time", "20", "--resolve",
                f"{domain}:80:127.0.0.1", "-o", "/dev/null", "-w", "%{http_code}", f"http://{domain}/"])
    if code not in ["301", "302", "307", "308"]:
        raise RuntimeError("Nginx is not redirecting HTTP to HTTPS")


def create_admin(email, backup):
    if not email:
        return
    password = secrets.token_urlsafe(24)
    code = '''import json, sys
from django.contrib.auth import get_user_model
from django.db import transaction
from allauth.account.models import EmailAddress
from apps.accounts.services import ensure_provisioned
data=json.load(sys.stdin)
with transaction.atomic():
    user=get_user_model().objects.filter(email__iexact=data["email"]).first()
    created=user is None
    if created:
        user=get_user_model().objects.create_superuser(email=data["email"], password=data["password"])
        EmailAddress.objects.get_or_create(user=user, email=user.email, defaults={"primary":True,"verified":True})
        ensure_provisioned(user)
print("ADMIN_CREATED" if created else "ADMIN_ALREADY_EXISTS_UNCHANGED")
'''
    output = compose("run", "--rm", "--no-deps", "-T", "app", "python", "manage.py", "shell", "-c", code,
                     data=json.dumps({"email": email, "password": password}).encode(), log=backup / "deploy.log")
    if "ADMIN_CREATED" in output:
        credentials = STATE / "initial-admin.txt"
        credentials.write_text(f"Email: {email}\nPassword: {password}\nChange this password after the first login.\n")
        credentials.chmod(0o600)
        print(f"Initial administrator created. Credentials: {credentials}", flush=True)
    else:
        print("Existing account left unchanged; no password or privilege reset", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "update"])
    parser.add_argument("--domain")
    parser.add_argument("--admin-email")
    parser.add_argument("--acme-email")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root (sudo bash scripts/install.sh or scripts/update.sh)")
    os.umask(0o077)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (STATE / "deployment.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error("Another marketing deployment is already running")
    for command in ["git", "docker", "curl"]:
        if not shutil.which(command):
            parser.error(f"Install prerequisite first: {command}")
    run(["docker", "compose", "version"])
    if run(["git", "status", "--porcelain", "--untracked-files=normal"]).strip():
        parser.error("Commit or save checkout changes before deployment")
    if run(["git", "branch", "--show-current"]).strip() != "main":
        parser.error("Use a dedicated main checkout for this deployment")
    previous_revision = run(["git", "rev-parse", "HEAD"]).strip()
    if args.action == "update":
        if not ENV_FILE.exists():
            parser.error("Run install first")
        run(["git", "fetch", "origin", "main"], timeout=180)
        run(["git", "merge", "--ff-only", "origin/main"])
    revision = run(["git", "rev-parse", "HEAD"]).strip()
    if not ENV_FILE.exists():
        if not args.domain or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.domain):
            parser.error("Install requires --domain with a DNS hostname, without http://")
        password = secrets.token_urlsafe(36)
        env = {
            "APP_DOMAIN": args.domain, "DB_CONTAINER": "stack-postgres", "DB_ADMIN_USER": "postgres",
            "POSTGRES_USER": "marketing", "POSTGRES_DB": "marketing", "POSTGRES_PASSWORD": password,
            "DATABASE_URL": f"postgres://marketing:{password}@stack-postgres:5432/marketing",
            "SECRET_KEY": secrets.token_urlsafe(64), "ENCRYPTION_KEY_SALT": secrets.token_urlsafe(64),
            "EXTERNAL_DOCKER_NETWORK": "stack_default", "NPM_CONTAINER": "root-app-1",
            "TRUSTED_PROXIES": "", "IMAGE_TAG": revision,
            "WEB_WORKERS": "2", "WEB_THREADS": "2", "STORAGE_BACKEND": "local",
            "ACME_EMAIL": args.acme_email or args.admin_email or "",
        }
        save_env(env)
    env = read_env()
    if args.domain and args.domain != env["APP_DOMAIN"]:
        parser.error("Domain differs from saved installation; edit the protected environment deliberately")
    socket.getaddrinfo(env["APP_DOMAIN"], 443)
    network = env["EXTERNAL_DOCKER_NETWORK"]
    db, npm = inspect(env["DB_CONTAINER"]), inspect(env["NPM_CONTAINER"])
    for container in [db, npm]:
        if not container["State"]["Running"] or network not in container["NetworkSettings"]["Networks"]:
            parser.error("Existing PostgreSQL and NPM must be running on the configured Docker network")
    # Trust the existing proxy itself, rather than every RFC1918 network.
    proxy_ip = npm["NetworkSettings"]["Networks"][network]["IPAddress"]
    env["TRUSTED_PROXIES"] = proxy_ip + "/32"
    if args.action == "install":
        provision_database(env)
    backup = create_backup(env, previous_revision)
    old_env = ENV_FILE.read_bytes()
    env["IMAGE_TAG"] = revision
    save_env(env)
    log = backup / "deploy.log"
    downtime = False
    migrations_started = False
    try:
        compose("config", "--quiet", log=log)
        print("Building application image (existing services stay running)", flush=True)
        compose("build", "app", timeout=1800, log=log)
        compose("run", "--rm", "--no-deps", "-T", "app", "python", "manage.py", "check", "--deploy", log=log)
        print("Stopping app/worker, backing up the quiesced data and applying migrations", flush=True)
        compose("stop", "app", "worker", timeout=150, log=log)
        downtime = True
        # The backup above also protects build/config failures. This second one
        # is consistent with media and queue state once both writers are stopped.
        backup = create_backup(env, previous_revision)
        log = backup / "deploy.log"
        migrations_started = True
        compose("run", "--rm", "--no-deps", "-T", "app", "python", "manage.py", "migrate", "--noinput",
                timeout=600, log=log)
        create_admin(args.admin_email, backup)
        compose("up", "-d", "--no-build", "--remove-orphans", "app", "worker", timeout=180, log=log)
        wait_healthy()
        configure_proxy(env, backup)
        verify_public(env)
        (STATE / "deployment.json").write_text(json.dumps({
            "revision": revision, "domain": env["APP_DOMAIN"], "last_backup": str(backup),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }, indent=2))
        print(f"Deployment verified: https://{env['APP_DOMAIN']} ({revision[:12]})", flush=True)
    except Exception:
        if not migrations_started:
            ENV_FILE.write_bytes(old_env)
            if downtime:
                compose("up", "-d", "--no-build", "app", "worker", log=log)
        print(f"Deployment failed. Protected backup/logs: {backup}", file=sys.stderr)
        if migrations_started:
            print("Do not downgrade images blindly after schema changes. See docs/vps-deployment.md.", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
