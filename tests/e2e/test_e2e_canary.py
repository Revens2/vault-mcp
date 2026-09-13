"""Canaries tiny / realistic : 50 conversations traitees par le workflow reel,
decompte exact des appels. Aucun LLM : l'analyse est un texte synthetique."""

from __future__ import annotations

import pytest

from tests.e2e import canary
from tests.e2e.harness import Bench, RawClient

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("profile", ["tiny", "realistic"])
def test_50_conversations_un_run(bench: Bench, client: RawClient, profile: str) -> None:
    convs = canary.tiny(50) if profile == "tiny" else canary.realistic(50, cap=400_000)
    for conv in convs:
        bench.add_conversation(canary.SOURCE, conv.name, conv.body)
    client.call("convia_scan")
    client.calls.clear()
    listed = client.call("convia_list_pending_analysis", limit=50)
    assert listed["returned"] == 50
    confirmed = 0
    for i, item in enumerate(listed["items"]):
        read = client.call("convia_read_for_analysis", path=item["path"])
        res = client.call("convia_write_analysis", source_path=read["path"],
                          source_hash=read["source_sha256"],
                          analysis_version=read["analysis_version"],
                          markdown=canary.analysis_markdown(i))
        confirmed += 0 if res["duplicate"] else 1
    assert confirmed == 50
    assert len(client.calls) == 101
    assert client.call("convia_list_pending_analysis", limit=50)["pending_total"] == 0
    assert all(p.name.lower().find("canary-e2e") >= 0
               for p in bench.convia_root.rglob("*.md")), "aucune donnee non canary"
