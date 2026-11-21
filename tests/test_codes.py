from datafordeler_mcp.codes import FIELD_LISTS, code_lists, translate


def test_every_mapped_list_is_in_codes_json():
    missing = set(FIELD_LISTS.values()) - set(code_lists())
    assert not missing
    assert all(code_lists()[name] for name in FIELD_LISTS.values())


def test_translate_known_code():
    assert translate("byg056Varmeinstallation", "1") == {"code": "1", "text": "Fjernvarme/blokvarme"}
    assert translate("byg021BygningensAnvendelse", 120)["text"] == "Fritliggende enfamiliehus"


def test_translate_unknown_code_and_plain_fields():
    assert translate("byg056Varmeinstallation", "424242") == {"code": "424242", "text": "unknown code"}
    assert translate("byg026Opfoerelsesaar", 1971) == 1971
    assert translate("byg056Varmeinstallation", None) is None
