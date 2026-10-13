"""Execute the seasonal helper offline so an unordered palette cannot pass check."""

from pathlib import Path
import shutil
import subprocess

import pytest

from applicant_scout.rio_score_tiers import (
    RIO_SCORE_BUCKETS,
    RIO_SCORE_SEASON,
    RIO_SCORE_SEASON_NAME,
)


@pytest.mark.parametrize("corruption", [None, "order", "source", "colour"])
def test_palette_check_preserves_runtime_order_and_source(tmp_path, corruption):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is needed to execute the seasonal helper")
    root = Path(__file__).resolve().parents[1]
    helper = tmp_path / "scripts/seasonal/refresh-rio-score-tiers.ps1"
    helper.parent.mkdir(parents=True)
    shutil.copyfile(root / helper.relative_to(tmp_path), helper)
    palette = tmp_path / "src/applicant_scout/rio_score_tiers.py"
    palette.parent.mkdir(parents=True)
    text = (root / palette.relative_to(tmp_path)).read_text(encoding="utf-8")
    if corruption == "order":
        lines = text.splitlines()
        indexes = [i for i, line in enumerate(lines) if line.startswith("    (")]
        lines[indexes[0]], lines[indexes[-1]] = lines[indexes[-1]], lines[indexes[0]]
        text = "\n".join(lines) + "\n"
    elif corruption == "source":
        text = text.replace("https://raider.io/api/", "https://example.invalid/api/")
    elif corruption == "colour":
        text = text.replace(RIO_SCORE_BUCKETS[0][1], "#000000", 1)
    palette.write_text(text, encoding="utf-8")
    mock = "\n".join(
        f"    [pscustomobject]@{{score={score}; rgbHex='{colour}'}}"
        for score, colour in RIO_SCORE_BUCKETS
    )
    wrapper = tmp_path / "check.ps1"
    wrapper.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "function Invoke-RestMethod { param($Uri, $TimeoutSec)\n"
        + mock
        + "\n}\n"
        + ". (Join-Path $PSScriptRoot 'scripts/seasonal/refresh-rio-score-tiers.ps1') "
        + f"-Season '{RIO_SCORE_SEASON}' -SeasonName '{RIO_SCORE_SEASON_NAME}' -Check\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(wrapper)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if corruption is None:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Season palette matches" in result.stdout
    else:
        assert result.returncode != 0
        assert "Bundled palette differs" in result.stderr
    assert palette.read_text(encoding="utf-8") == text
