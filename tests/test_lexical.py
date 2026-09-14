import numpy as np

from vault_mcp.lexical import IndexBM25, tokens


def test_tokens_garde_identifiants_et_sous_parties() -> None:
    t = tokens("Voir ORA-01555 et llm-gateway.service, Économie")
    assert "ora-01555" in t
    assert "01555" in t
    assert "llm-gateway.service" in t
    assert "gateway" in t
    assert "economie" in t
    assert "et" not in t


def test_bm25_identifiant_rare_devant_mot_frequent() -> None:
    textes = [
        "vps vps vps configuration generale",
        "vps note sur ORA-01555 apres un long texte",
        "autre chose sans rapport",
    ]
    index = IndexBM25.construire(textes)
    s = index.scores("vps ORA-01555")
    assert int(np.argmax(s)) == 1
    assert s[2] == 0


def test_bm25_vide() -> None:
    index = IndexBM25.construire([])
    assert index.scores("rien").shape == (0,)
