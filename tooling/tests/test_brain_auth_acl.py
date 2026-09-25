"""L'ACL du dossier d'etat ne doit jamais passer par un etat vide (cf. brain_auth._restrict_token_acl)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import brain_auth  # noqa: E402


def test_heritage_retire_et_droit_octroye_dans_le_meme_appel(monkeypatch, tmp_path):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        class R:
            stdout = b"DOMAIN-user\r\n"
        return R()

    monkeypatch.setattr(brain_auth.os, "name", "nt")
    monkeypatch.setattr(subprocess, "run", fake_run)
    brain_auth._restrict_token_acl(tmp_path, directory=True)

    icacls = [c for c in calls if c[0] == "icacls"]
    for call in icacls:
        if "/inheritance:r" in call:
            assert "/grant:r" in call, f"heritage retire sans octroi dans le meme appel : {call}"
    assert any("/inheritance:r" in c and "DOMAIN-user:(OI)(CI)F" in c for c in icacls)
