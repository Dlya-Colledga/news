"""Optional local PostgreSQL without Docker: uv run --with pgserver python scripts/dev_postgres.py.
Only prints a connection string; does not overwrite .env. Data is kept under .local/.
"""

from pathlib import Path
import argparse
import pgserver

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["start", "stop"], nargs="?", default="start")
args = parser.parse_args()
root = Path(__file__).resolve().parents[1] / ".local"
root.mkdir(exist_ok=True)
server = pgserver.get_server(root / "postgres", cleanup_mode="stop" if args.action == "stop" else None)
if args.action == "stop":
    server.cleanup()
else:
    print("DATABASE_URL=" + server.get_uri().replace("postgresql://", "postgresql+psycopg://"))
