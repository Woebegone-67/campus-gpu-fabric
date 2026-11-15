#!/usr/bin/env python3
"""Create local-only identities; never publish or bundle the generated credentials."""
import argparse
import json
import secrets
from pathlib import Path

from campus_fabric.auth.sessions import hash_password


def ensure_accounts(directory: Path):
    """Add password login without renaming any persisted owner or bearer identity."""
    account_path = directory / "accounts.json"
    if account_path.exists():
        account_path.chmod(0o600)
        return
    password_path = directory / "login-accounts.txt"
    if password_path.exists():
        raise SystemExit("Incomplete account setup; existing password record preserved.")
    tokens = json.loads((directory / "auth.json").read_text())
    accounts = {}
    lines = ["织算 · 初始登录账户", "请妥善保存此文件；不要提交到 Git 或展示在投影中。", ""]
    for actor in tokens.values():
        username = "chen" if actor["actor_id"] == "supplier" else actor["actor_id"]
        if username in accounts:
            raise SystemExit("Duplicate account name; existing identities preserved.")
        password = secrets.token_urlsafe(12)
        accounts[username] = {"password_hash": hash_password(password), "actor": actor}
        label = "管理员" if actor["role"] == "administrator" else "用户"
        lines.extend([f"{username}（{label}）", f"密码：{password}", ""])
    # Neither passwords nor browser sessions enter the bearer credential files.
    with password_path.open("x") as handle:
        password_path.chmod(0o600)
        handle.write("\n".join(lines))
    with account_path.open("x") as handle:
        account_path.chmod(0o600)
        json.dump(accounts, handle, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--ensure-accounts", action="store_true",
                        help="Add browser accounts to existing identities without replacing them")
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    auth_path = args.directory / "auth.json"
    client_path = args.directory / "client-identities.json"
    if args.ensure_accounts:
        if not auth_path.exists() or not client_path.exists():
            raise SystemExit("An existing complete identity pair is required.")
        ensure_accounts(args.directory)
        print(f"Login accounts ready: {(args.directory / 'login-accounts.txt').resolve()}")
        return
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
    ensure_accounts(args.directory)
    print(f"Created local credentials: {auth_path.resolve()}")
    print(f"Client headers (keep local): {client_path.resolve()}")
    print(f"Login accounts (keep local): {(args.directory / 'login-accounts.txt').resolve()}")
    print("No test key was generated or sent to the browser.")


if __name__ == "__main__":
    main()
