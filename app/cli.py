import argparse
import asyncio
import getpass
import json
import logging
import sys
from sqlalchemy import select
from app.db.session import SessionLocal
from app.db.models import User
from app.core.security import hasher


def models_command(args):
    try:
        import argostranslate.package as package
    except ImportError:
        print("Install the engine first: uv sync --extra translation", file=sys.stderr)
        return 1
    installed = {(p.from_code, p.to_code) for p in package.get_installed_packages()}
    if args.action == "list":
        print(json.dumps({"installed": sorted(installed)}, ensure_ascii=False, indent=2))
        return 0
    try:
        package.update_package_index()
        available = package.get_available_packages()
        if args.action == "catalog":
            print(
                json.dumps(
                    {
                        "available": [
                            {"from": p.from_code, "to": p.to_code, "version": p.package_version}
                            for p in available
                            if p.from_code in ("en", "ru", "ky") and p.to_code in ("en", "ru", "ky")
                        ]
                    },
                    indent=2,
                )
            )
            return 0
        failed = False
        for pair in args.pairs:
            parts = pair.split(":")
            if len(parts) != 2:
                raise ValueError("Use source:target, e.g. en:ru")
            source, target = parts
            if (source, target) in installed:
                print(f"Already installed: {source} → {target}")
                continue
            model = next((p for p in available if p.from_code == source and p.to_code == target), None)
            if model is None:
                print(f"Unavailable in catalog: {source} → {target}", file=sys.stderr)
                failed = True
                continue
            try:
                print(f"Installing {source} → {target} ({model.package_version})", flush=True)
                package.install_from_path(model.download())
                print(f"Installed {source} → {target}", flush=True)
            except Exception as exc:
                print(f"Installation failed for {source} → {target}: {exc}", file=sys.stderr)
                failed = True
        return 1 if failed else 0
    except Exception as exc:
        print(f"Model catalog unavailable: {exc}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(prog="news", description="Frame newsroom management")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed", help="Create source configuration and category taxonomy; no news fixtures")
    user = sub.add_parser("create-user", help="Create an editor/admin with an interactive password prompt")
    user.add_argument("username")
    user.add_argument("--role", choices=["admin", "editor"], default="admin")
    sub.add_parser("worker", help="Run the independent scheduler")
    sub.add_parser("ingest", help="Run one import + translation cycle")
    models = sub.add_parser("models")
    models.add_argument("action", choices=["list", "catalog", "install"])
    models.add_argument("pairs", nargs="*", default=["en:ru", "en:ky", "ru:en", "ky:en"])
    tmdb = sub.add_parser("tmdb")
    tmdb_sub = tmdb.add_subparsers(dest="action", required=True)
    search = tmdb_sub.add_parser("search")
    search.add_argument("title")
    cache = tmdb_sub.add_parser("cache")
    cache.add_argument("kind", choices=["movie", "tv"])
    cache.add_argument("id", type=int)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.command == "models":
        raise SystemExit(models_command(args))
    if args.command == "seed":
        from app.db.seed import seed

        with SessionLocal() as db:
            seed(db)
        print("Sources and categories initialized. No news content was fabricated.")
    elif args.command == "create-user":
        password = getpass.getpass("Password (at least 12 characters): ")
        if len(password) < 12 or password != getpass.getpass("Repeat password: "):
            parser.error("Passwords must match and contain at least 12 characters")
        with SessionLocal() as db:
            if db.scalar(select(User).where(User.username == args.username)):
                parser.error("User already exists")
            db.add(User(username=args.username, password_hash=hasher.hash(password), role=args.role))
            db.commit()
        print("User created")
    elif args.command in ("worker", "ingest"):
        from app.workers.runner import run

        asyncio.run(run(once=args.command == "ingest"))
    elif args.command == "tmdb":
        from app.services.tmdb import TMDBService
        from app.core.config import get_settings

        if not get_settings().tmdb_api_key:
            parser.error("TMDB_API_KEY is not configured; news aggregation remains available")
        service = TMDBService()
        if args.action == "search":
            print(json.dumps(asyncio.run(service.search(args.title)), ensure_ascii=False, indent=2))
        else:
            with SessionLocal() as db:
                work = asyncio.run(service.cache_work(db, args.id, args.kind))
                db.commit()
                print(f"Cached #{work.id}: {work.title}")


if __name__ == "__main__":
    main()
