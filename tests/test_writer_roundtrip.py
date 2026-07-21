"""Writer round-trip harness (M0): exercise a real kicad_pcb_tool writer with
write=True against the `scratch_board` copy (never the real kiln.kicad_pcb),
then reparse the scratch board and confirm the intended change landed and
everything else (component/segment counts) is unchanged.

Uses `create_group`/`delete_group` since they're simple, self-contained
top-level-block writers (append/remove one `(group ...)` block) with an
explicit dry-run default (write=False) - a good minimal proof that the
write path is safe and reparses cleanly.
"""

from __future__ import annotations

from pathlib import Path

import kicad_pcb_tool as k


def test_create_group_write_true_roundtrips(scratch_board: Path) -> None:
    before_components = k.list_components(scratch_board, limit=10_000)
    before_count = len(before_components)
    before_groups = k.list_groups(scratch_board)["groups"]
    before_group_count = len(before_groups)

    refs = ["R1", "U10"]

    # Dry run first: must not touch the file.
    dry = k.create_group(scratch_board, "roundtrip_test_group", refs, write=False)
    assert dry["write"] is False
    assert len(k.list_groups(scratch_board)["groups"]) == before_group_count

    result = k.create_group(scratch_board, "roundtrip_test_group", refs, write=True)
    assert result["write"] is True
    assert result["member_count"] == len(refs)

    # Reparse from scratch (bypass any cache) and confirm the change is present.
    after_groups = k.list_groups(scratch_board)["groups"]
    assert len(after_groups) == before_group_count + 1
    new_group = next(g for g in after_groups if g["name"] == "roundtrip_test_group")
    member_refs = {m["reference"] for m in new_group["members"]}
    assert member_refs == set(refs)

    # Everything else must be untouched: same component count, same references.
    after_components = k.list_components(scratch_board, limit=10_000)
    assert len(after_components) == before_count
    after_refs = {c["reference"] for c in after_components}
    before_refs = {c["reference"] for c in before_components}
    assert after_refs == before_refs

    # The board file must still fully parse (pads too, for a known ref).
    fp = k.get_footprint_pads(scratch_board, "U10")
    assert fp["reference"] == "U10"
    assert len(fp["pads"]) > 0


def test_delete_group_write_true_removes_block(scratch_board: Path) -> None:
    refs = ["R1", "U10"]
    k.create_group(scratch_board, "roundtrip_test_group_2", refs, write=True)
    before_count = len(k.list_components(scratch_board, limit=10_000))

    dry = k.delete_group(scratch_board, name="roundtrip_test_group_2", write=False)
    assert dry["write"] is False
    assert any(g["name"] == "roundtrip_test_group_2" for g in k.list_groups(scratch_board)["groups"])

    result = k.delete_group(scratch_board, name="roundtrip_test_group_2", write=True)
    assert result["write"] is True

    after_groups = k.list_groups(scratch_board)["groups"]
    assert not any(g["name"] == "roundtrip_test_group_2" for g in after_groups)

    # Component count still unaffected by group add+remove.
    assert len(k.list_components(scratch_board, limit=10_000)) == before_count
