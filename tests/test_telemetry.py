"""Journal des appels MCP : ce qu il extrait, et surtout ce qu il n extrait jamais."""

from __future__ import annotations

import json

from vault_mcp import telemetry


def test_parse_outil_et_ref_sans_contenu():
    body = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
        "name": "convia_write_analysis",
        "arguments": {"source_path": "raw/assets/ConvIA/x/secret-nom.md",
                      "markdown": "CONTENU PRIVE"}}}).encode()
    method, tool, ref = telemetry.parse_request(body)
    assert (method, tool) == ("tools/call", "convia_write_analysis")
    assert ref.startswith("p:") and "secret" not in ref and "PRIVE" not in ref


def test_parse_job_id_tronque():
    body = json.dumps({"method": "tools/call", "params": {
        "name": "wiki_ingest_read",
        "arguments": {"job_id": "abcdef0123456789abcdef", "lease_id": "l"}}}).encode()
    assert telemetry.parse_request(body)[2] == "job:abcdef012345"


def test_parse_corps_invalide_ne_leve_pas():
    assert telemetry.parse_request(b"{pas du json")[0] == "?"
    assert telemetry.parse_request(b"")[0] == ""


def test_sniff_refus_structure_et_texte_echappe():
    structured = (b'{"result":{"structuredContent":'
                  b'{"etat":"refuse","message":"ERREUR: hash perime"}}}')
    assert "hash perime" in (telemetry.sniff_error(structured) or "")
    echappe = b'{"content":[{"text":"{\\"etat\\": \\"refuse\\", \\"message\\": \\"ERREUR: x\\"}"}]}'
    assert telemetry.sniff_error(echappe) is not None
    assert telemetry.sniff_error(b'{"result":{"etat":"en_attente"}}') is None
