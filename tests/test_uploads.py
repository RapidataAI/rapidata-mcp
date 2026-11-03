import pytest

from rapidata_mcp.uploads import check_reference

GUID = "3f2a9c1e-5b7d-4e2a-9c1f-0d8e7a6b5c4d"


@pytest.mark.parametrize(
    "reference",
    [
        f"{GUID}.png",
        f"{GUID.upper()}.PNG",
        f"{GUID}.webp",
        f"{GUID}.mp4",
        f"{GUID}.jpeg2000",
        GUID,
        f"{GUID.replace('-', '')}.png",
    ],
)
def test_any_guid_with_any_extension_is_a_reference(reference):
    assert check_reference(f" {reference} ", "r") == reference


@pytest.mark.parametrize(
    "value",
    [
        "cat.png",
        f"{GUID[:13]}{GUID[14:]}.png",
        f"{GUID}.tar.gz",
        f"/tmp/{GUID}.png",
        f"{GUID}.png/x",
        f"x{GUID}.png",
        "",
    ],
)
def test_anything_else_is_not(value):
    with pytest.raises(ValueError, match=r"not an asset reference.*upload_media\(\)"):
        check_reference(value, "r")
