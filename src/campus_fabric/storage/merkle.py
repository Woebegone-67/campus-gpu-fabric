"""Transaction-local, persistent deterministic Merkle treap for canonical JSON cells.

Keys, values and child digests are domain-separated. Updates only traverse a
search path; no full-state hash, cross-transaction cache or XOR accumulator is used.
SQLite owns atomicity. Callers commit this index with their cells and journal.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from campus_fabric.storage.journal import encode

EMPTY_HASH = hashlib.sha256(b"fabric-state-empty-v2").hexdigest()


def priority(key: str) -> str:
    return hashlib.sha256(b"fabric-state-priority-v2\0" + key.encode()).hexdigest()


def value_hash(payload: str) -> str:
    return hashlib.sha256(b"fabric-state-value-v2\0" + payload.encode()).hexdigest()


def node_hash(key: str, value_digest: str, left_hash: str, right_hash: str) -> str:
    body = encode([key, value_digest, left_hash, right_hash]).encode()
    return hashlib.sha256(b"fabric-state-node-v2\0" + body).hexdigest()


def create_schema(connection) -> None:
    connection.execute(
        "CREATE TABLE fabric_tree (key TEXT PRIMARY KEY, priority TEXT NOT NULL, "
        "value_hash TEXT NOT NULL, left_key TEXT, right_key TEXT, root_hash TEXT NOT NULL)"
    )


@dataclass(frozen=True)
class Node:
    key: str
    priority: str
    value_hash: str
    left_key: str | None
    right_key: str | None
    root_hash: str

    @property
    def rank(self):
        return self.priority, self.key


class MerkleTree:
    def __init__(self, connection, root_key: str | None = None):
        self.connection = connection
        self.root_key = root_key
        self._cache: dict[str, Node] = {}
        self.loaded_nodes = 0
        self.written_nodes = 0

    def _node(self, key: str) -> Node:
        if key not in self._cache:
            row = self.connection.execute(
                "SELECT key,priority,value_hash,left_key,right_key,root_hash "
                "FROM fabric_tree WHERE key=?", (key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("Merkle index references a missing node")
            self._cache[key] = Node(*row)
            self.loaded_nodes += 1
        return self._cache[key]

    def _hash(self, key: str | None) -> str:
        return EMPTY_HASH if key is None else self._node(key).root_hash

    @property
    def root_hash(self) -> str:
        return self._hash(self.root_key)

    def _put(self, key, value_digest, left, right):
        record = Node(key, priority(key), value_digest, left, right,
                      node_hash(key, value_digest, self._hash(left), self._hash(right)))
        if self._cache.get(key) == record:
            return key
        self.connection.execute(
            "INSERT INTO fabric_tree(key,priority,value_hash,left_key,right_key,root_hash) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
            "priority=excluded.priority,value_hash=excluded.value_hash,"
            "left_key=excluded.left_key,right_key=excluded.right_key,root_hash=excluded.root_hash",
            (record.key, record.priority, record.value_hash, record.left_key,
             record.right_key, record.root_hash),
        )
        self._cache[key] = record
        self.written_nodes += 1
        return key

    def _insert(self, root, key, digest):
        if root is None:
            return self._put(key, digest, None, None)
        current = self._node(root)
        if key == root:
            return self._put(root, digest, current.left_key, current.right_key)
        if key < root:
            left = self._insert(current.left_key, key, digest)
            child = self._node(left)
            if child.rank < current.rank:
                self._put(root, current.value_hash, child.right_key, current.right_key)
                return self._put(left, child.value_hash, child.left_key, root)
            return self._put(root, current.value_hash, left, current.right_key)
        right = self._insert(current.right_key, key, digest)
        child = self._node(right)
        if child.rank < current.rank:
            self._put(root, current.value_hash, current.left_key, child.left_key)
            return self._put(right, child.value_hash, root, child.right_key)
        return self._put(root, current.value_hash, current.left_key, right)

    def set(self, key: str, payload_text: str) -> None:
        if not self.connection.in_transaction:
            raise RuntimeError("Merkle writes require an active transaction")
        if not isinstance(key, str) or not isinstance(payload_text, str):
            raise TypeError("Merkle keys and canonical payloads must be strings")
        self.root_key = self._insert(self.root_key, key, value_hash(payload_text))

    def _merge(self, left, right):
        if left is None or right is None:
            return right if left is None else left
        a, b = self._node(left), self._node(right)
        if a.rank < b.rank:
            merged = self._merge(a.right_key, right)
            return self._put(left, a.value_hash, a.left_key, merged)
        merged = self._merge(left, b.left_key)
        return self._put(right, b.value_hash, merged, b.right_key)

    def _delete(self, root, key):
        if root is None:
            raise KeyError(key)
        current = self._node(root)
        if root == key:
            merged = self._merge(current.left_key, current.right_key)
            self.connection.execute("DELETE FROM fabric_tree WHERE key=?", (key,))
            self._cache.pop(key, None)
            self.written_nodes += 1
            return merged
        if key < root:
            return self._put(root, current.value_hash,
                             self._delete(current.left_key, key), current.right_key)
        return self._put(root, current.value_hash, current.left_key,
                         self._delete(current.right_key, key))

    def delete(self, key: str) -> None:
        if not self.connection.in_transaction:
            raise RuntimeError("Merkle writes require an active transaction")
        self.root_key = self._delete(self.root_key, key)

    def verify(self, cells: Mapping[str, str]) -> str:
        """Full startup/offline check, not the hot write path or independent oracle."""
        # Re-read SQL so this check cannot conceal external changes behind the cache.
        rows = {row[0]: Node(*row) for row in self.connection.execute(
            "SELECT key,priority,value_hash,left_key,right_key,root_hash FROM fabric_tree"
        )}
        if rows.keys() != cells.keys():
            raise RuntimeError("Merkle index does not cover exactly the persisted cells")
        visited = set()

        def walk(key, lower=None, upper=None, parent_rank=None):
            if key is None:
                return EMPTY_HASH
            if key in visited or key not in rows:
                raise RuntimeError("Merkle index contains a cycle or missing child")
            item = rows[key]
            if ((lower is not None and key <= lower) or
                    (upper is not None and key >= upper) or
                    item.priority != priority(key) or
                    (parent_rank is not None and item.rank < parent_rank)):
                raise RuntimeError("Merkle search/priority order is invalid")
            visited.add(key)
            left = walk(item.left_key, lower, key, item.rank)
            right = walk(item.right_key, key, upper, item.rank)
            digest = value_hash(cells[key])
            expected = node_hash(key, digest, left, right)
            if item.value_hash != digest or item.root_hash != expected:
                raise RuntimeError("Merkle node digest differs from persisted cells")
            return expected

        result = walk(self.root_key)
        if visited != rows.keys():
            raise RuntimeError("Merkle index has detached nodes")
        return result
