"""Start an isolated, loopback-only PostgreSQL instance for this workspace."""

import shutil
import shlex
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
directory = root / ".postgres"
data = directory / "data"
directory.mkdir(exist_ok=True)


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


for executable in ("initdb", "pg_ctl", "psql", "createdb"):
    if not shutil.which(executable):
        raise SystemExit("Install PostgreSQL and add its bin directory to PATH first.")

if not (data / "PG_VERSION").exists():
    run("initdb", "-D", str(data), "-U", "deepsearchagent", "--auth=trust", "--encoding=UTF8", "--no-locale")
if subprocess.run(["pg_ctl", "-D", str(data), "status"], capture_output=True).returncode:
    run("pg_ctl", "-D", str(data), "-l", str(directory / "server.log"),
        "-o", f"-h 127.0.0.1 -p 55432 -k {shlex.quote(str(directory))}", "-w", "start")
connection = ["-h", "127.0.0.1", "-p", "55432", "-U", "deepsearchagent"]
exists = run("psql", *connection, "-d", "postgres", "-tAc",
             "SELECT 1 FROM pg_database WHERE datname='deepsearchagent'", capture_output=True, text=True)
if not exists.stdout.strip():
    run("createdb", *connection, "deepsearchagent")
print("PostgreSQL ready: postgresql://deepsearchagent@127.0.0.1:55432/deepsearchagent")
print(f"Stop with: pg_ctl -D {data} stop")
