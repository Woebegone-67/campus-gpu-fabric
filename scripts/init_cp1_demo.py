#!/usr/bin/env python3
"""Create local-only identities; never publish or bundle the generated credentials."""
import argparse
import json
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    auth_path = args.directory / "auth.json"
    client_path = args.directory / "client-identities.json"
    if auth_path.exists() or client_path.exists():
        raise SystemExit("Existing identities preserved. Use another directory to create new ones.")
    tokens, clients = {}, {}
    for role, owner in (("member", "alice"), ("member", "bob"),
                        ("member", "supplier"), ("administrator", None)):
        name = owner or "admin"
        token = secrets.token_urlsafe(32)
        tokens[token] = {"actor_id": name, "role": role, "owner_id": owner}
        clients[name] = {"Authorization": "Bearer " + token}
        if owner:
            clients[name]["X-Owner"] = owner
    for path, data in ((auth_path, tokens), (client_path, clients)):
        with path.open("x") as handle:
            path.chmod(0o600)
            json.dump(data, handle, indent=2)
    print(f"Created local credentials: {auth_path.resolve()}")
    print(f"Client headers (keep local): {client_path.resolve()}")
    print("No test key was generated or sent to the browser.")


if __name__ == "__main__":
    main()
