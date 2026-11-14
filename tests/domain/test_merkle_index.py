"""Persistent index checked against a separate whole-set reference construction."""

import hashlib
import json
import random
import sqlite3

import pytest

from campus_fabric.storage.merkle import MerkleTree, create_schema


def full_reference(cells):
    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def canonical(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()

    def build(keys):
        if not keys:
            return digest(b"fabric-state-empty-v2")
        root = min(keys, key=lambda k: (digest(b"fabric-state-priority-v2\0" + k.encode()), k))
        at = keys.index(root)
        value = digest(b"fabric-state-value-v2\0" + cells[root].encode())
        return digest(b"fabric-state-node-v2\0" + canonical(
            [root, value, build(keys[:at]), build(keys[at + 1:])]))

    return build(sorted(cells))


@pytest.mark.parametrize("seed", [0, 17, 983])
def test_every_update_delete_and_insertion_order_agrees_with_full_reference(tmp_path, seed):
    connection = sqlite3.connect(tmp_path / "tree.sqlite", isolation_level=None)
    create_schema(connection)
    connection.execute("BEGIN IMMEDIATE")
    tree, cells = MerkleTree(connection), {}
    keys = [f"key-{i}" for i in range(80)] + ["中文", "a/b", "[1]", "", "0"]
    random.Random(seed).shuffle(keys)
    for number, key in enumerate(keys):
        cells[key] = json.dumps({"value": number, "nested": [False, None, -0.0]},
                               sort_keys=True, separators=(",", ":"))
        tree.set(key, cells[key])
        assert tree.root_hash == full_reference(cells)
    root = tree.root_key
    connection.commit()
    reopened = MerkleTree(connection, root)
    assert reopened.verify(cells) == full_reference(cells)
    connection.execute("BEGIN IMMEDIATE")
    for key in keys[::3]:
        cells[key] = '"changed"'
        reopened.set(key, cells[key])
        assert reopened.root_hash == full_reference(cells)
    for key in reversed(keys):
        del cells[key]
        reopened.delete(key)
        assert reopened.root_hash == full_reference(cells)
    assert reopened.root_key is None
    assert reopened.verify(cells) == full_reference({})
    connection.commit()
    connection.close()


def test_large_history_single_change_touches_search_path_only(tmp_path):
    connection = sqlite3.connect(tmp_path / "tree.sqlite", isolation_level=None)
    create_schema(connection)
    cells = {f"historical-{i:05d}": str(i) for i in range(10000)}
    connection.execute("BEGIN IMMEDIATE")
    tree = MerkleTree(connection)
    for key, value in cells.items():
        tree.set(key, value)
    root = tree.root_key
    expected = full_reference(cells)
    assert tree.root_hash == expected
    connection.commit()
    connection.execute("BEGIN IMMEDIATE")
    update = MerkleTree(connection, root)
    update.set("historical-05000", "-0.0")
    cells["historical-05000"] = "-0.0"
    assert update.root_hash == full_reference(cells)
    assert update.loaded_nodes < 100
    assert update.written_nodes < 60
    connection.rollback()
    cells["historical-05000"] = "5000"
    assert MerkleTree(connection, root).verify(cells) == expected
    connection.close()


@pytest.mark.parametrize("damage", ["payload", "digest", "priority", "cycle", "detached"])
def test_full_verification_rejects_cell_or_index_damage(tmp_path, damage):
    connection = sqlite3.connect(tmp_path / "tree.sqlite", isolation_level=None)
    create_schema(connection)
    connection.execute("BEGIN IMMEDIATE")
    cells = {"a": "0", "b": "false", "c": "0.0"}
    tree = MerkleTree(connection)
    for key, value in cells.items():
        tree.set(key, value)
    if damage == "payload":
        cells["a"] = "1"
    elif damage == "digest":
        connection.execute("UPDATE fabric_tree SET root_hash='forged' WHERE key=?", (tree.root_key,))
    elif damage == "priority":
        connection.execute("UPDATE fabric_tree SET priority='forged' WHERE key=?", (tree.root_key,))
    elif damage == "cycle":
        connection.execute("UPDATE fabric_tree SET left_key=key WHERE key=?", (tree.root_key,))
    else:
        tree.set("orphan", "null")
        # Supply the old root and complete cell set; an unlinked subtree must fail.
        cells["orphan"] = "null"
        tree.root_key = "a"
    with pytest.raises(RuntimeError, match="Merkle"):
        tree.verify(cells)
    connection.rollback()
    connection.close()


def test_nontransactional_mutation_is_rejected(tmp_path):
    connection = sqlite3.connect(tmp_path / "tree.sqlite", isolation_level=None)
    create_schema(connection)
    tree = MerkleTree(connection)
    with pytest.raises(RuntimeError, match="active transaction"):
        tree.set("a", "1")
    with pytest.raises(RuntimeError, match="active transaction"):
        tree.delete("a")
    connection.close()
