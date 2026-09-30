"""The station token file, and what it refuses at startup. S-08.

An empty token is a station anyone can claim; a token shared by two stations
lets the second silently take the first's identity. Both must fail where the
operator is looking - at startup - rather than at an upgrade months later.
Each refusal is paired with the file that is accepted, so a loader that
refused everything could not pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.__main__ import FileAuthenticator


def tokens_file(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "gateway.tokens"
    path.write_text(text, encoding="utf-8")
    return path


async def test_a_well_formed_file_resolves_each_token_to_its_station(
    tmp_path: Path,
) -> None:
    authenticator = FileAuthenticator(
        tokens_file(
            tmp_path,
            "# stations\ntbilisi-base-1: token-one\n\nkutaisi-base-1:   token-two  \n",
        )
    )

    assert await authenticator.station_for_token("token-one") == "tbilisi-base-1"
    assert await authenticator.station_for_token("token-two") == "kutaisi-base-1"
    assert await authenticator.station_for_token("token-three") is None


def test_an_empty_token_is_refused(tmp_path: Path) -> None:
    with pytest.raises(
        ValueError, match=r"gateway.tokens:2: the token for 'kutaisi-base-1' is empty"
    ):
        FileAuthenticator(
            tokens_file(tmp_path, "tbilisi-base-1: token-one\nkutaisi-base-1:\n")
        )


def test_an_empty_station_id_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"gateway.tokens:1: the station_id is empty"):
        FileAuthenticator(tokens_file(tmp_path, ": token-one\n"))


def test_a_token_shared_by_two_stations_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"already assigned to 'tbilisi-base-1'"):
        FileAuthenticator(
            tokens_file(
                tmp_path, "tbilisi-base-1: token-one\nkutaisi-base-1: token-one\n"
            )
        )


def test_a_line_without_a_separator_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="expected 'station_id: token'"):
        FileAuthenticator(tokens_file(tmp_path, "tbilisi-base-1 token-one\n"))


def test_a_file_with_no_tokens_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="defines no tokens"):
        FileAuthenticator(tokens_file(tmp_path, "# nothing yet\n"))
