from service.extraction import extract_purchase, match_catalog_name, normalize_item


def test_market_names_map_by_prefix_but_prepared_products_stay_unknown():
    assert match_catalog_name("Bawang Merah Kupas")[1] == "bawang"
    assert match_catalog_name("bawang putih kupas")[1] == "bawang_putih"
    assert match_catalog_name("b merah kapos")[1] == "bawang"
    assert match_catalog_name("combrang")[1] == "kecombrang"
    assert match_catalog_name("gula merah batok")[1] == "gula_merah"
    # A different ingredient that merely contains a catalog word.
    assert match_catalog_name("daun bawang")[1] is None
    # A prepared product is not the raw ingredient.
    assert match_catalog_name("cabe kering 1kg")[1] is None
    assert match_catalog_name("ultima @ 10kg")[1] is None


def test_requested_examples_and_price_does_not_become_quantity():
    first = extract_purchase("beli cabai 3 kg 150rb")
    assert first["items"] == [
        {"ingredient_id": "cabai", "raw_name": "cabai", "quantity": "3", "unit": "kg", "line_total_idr": 150000}
    ]
    assert first["warnings"] == []

    second = extract_purchase("beli cabai 500 g Rp25.000")
    assert second["items"] == [
        {"ingredient_id": "cabai", "raw_name": "cabai", "quantity": "500", "unit": "g", "line_total_idr": 25000}
    ]

    third = extract_purchase("beli beras")
    assert third["items"][0]["quantity"] is None
    assert third["items"][0]["line_total_idr"] is None
    assert any("quantity" in warning for warning in third["warnings"])


def test_aliases_decimal_comma_and_supported_line_separators():
    result = extract_purchase("beli cabe 0,5 kg 20rb; beli telur ayam 1 kg 30 ribu\nbeli minyak goreng 2 l 50rb")
    assert [item["ingredient_id"] for item in result["items"]] == ["cabai", "telur", "minyak"]
    assert [item["quantity"] for item in result["items"]] == ["0.5", "1", "2"]
    assert [item["line_total_idr"] for item in result["items"]] == [20000, 30000, 50000]
    assert result["warnings"] == []


def test_unknown_item_and_pack_are_preserved_for_manual_selection():
    result = extract_purchase("beli daun bawang 2 ikat 10rb")
    assert result["items"][0]["ingredient_id"] is None
    assert result["items"][0]["raw_name"] == "daun bawang"
    assert result["items"][0]["quantity"] == "2"
    assert result["items"][0]["unit"] == "ikat"
    normalized = normalize_item(result["items"][0])
    assert normalized["canonical_quantity"] is None
    assert normalized["canonical_unit"] is None
    assert normalized["unit_price_idr"] is None
    assert normalized["warnings"]

    unknown_tag = extract_purchase("beli kentang 2 sack 10rb")["items"][0]
    assert unknown_tag["unit"] == "sack"
    assert normalize_item(unknown_tag)["unit"] == "sack"


def test_normalization_uses_decimal_conversions_and_exact_unit_price():
    parsed = extract_purchase("beli cabai 500 g Rp25.000")["items"][0]
    normalized = normalize_item(parsed)
    assert normalized["canonical_quantity"] == "0.5"
    assert normalized["canonical_unit"] == "kg"
    assert normalized["unit_price_idr"] == "50000"
    assert normalized["warnings"] == []

    liquid = normalize_item(
        {"ingredient_id": "minyak", "raw_name": "minyak", "quantity": "250", "unit": "ml", "line_total_idr": 12500}
    )
    assert liquid["canonical_quantity"] == "0.25"
    assert liquid["canonical_unit"] == "l"
    assert liquid["unit_price_idr"] == "50000"


def test_ambiguous_or_instruction_like_source_is_rejected():
    combined = extract_purchase("beli cabai 1 kg 20rb dan beras 2 kg 30rb")
    assert combined["items"] == []
    assert any("multiple item clauses" in warning for warning in combined["warnings"])

    for source in ("jangan beli cabai 1 kg 20rb", "rencana beli cabai 1 kg 20rb", "beli cabai 1 kg per kg 20rb", "beli cabai 1 kg diskon 20rb", "beli cabai 1 l 20rb"):
        result = extract_purchase(source)
        if "per kg" in source or "diskon" in source or "jangan" in source or "rencana" in source:
            assert result["warnings"]
            assert result["items"] == []
        if "1 l" in source:
            normalized = normalize_item(result["items"][0])
            assert normalized["canonical_quantity"] is None
            assert normalized["warnings"]


def test_numeric_bounds_precision_and_input_line_limit():
    for quantity in ("0", "-1", "1000001", "1,1234567"):
        result = extract_purchase(f"beli cabai {quantity} kg 10rb")
        assert result["items"][0]["quantity"] is None
        assert result["warnings"]

    result = extract_purchase("\n".join("beli beras 1 kg 10rb" for _ in range(21)))
    assert result["items"] == []
    assert any("20-item limit" in warning and "split" in warning for warning in result["warnings"])


def test_tiny_decimal_total_that_does_not_resolve_to_whole_rupiah_is_rejected():
    result = extract_purchase("beli cabai 1 kg 0,0001rb")
    assert result["items"][0]["line_total_idr"] is None
    assert result["warnings"]


def test_oversized_rp_amount_and_normalization_total_are_rejected_without_exception():
    huge = "9" * 10000
    extracted = extract_purchase(f"beli cabai 1 kg Rp{huge}")
    assert len(extracted["items"]) == 1
    assert extracted["items"][0]["quantity"] == "1"
    assert extracted["items"][0]["line_total_idr"] is None
    assert extracted["warnings"]

    normalized = normalize_item(
        {"ingredient_id": "cabai", "raw_name": "cabai", "quantity": "1", "unit": "kg", "line_total_idr": huge}
    )
    assert normalized["unit_price_idr"] is None
    assert normalized["warnings"]


def test_unprefixed_whole_number_price_is_not_assumed():
    result = extract_purchase("beli cabai 1 kg 25000")
    assert result["items"][0] == {
        "ingredient_id": "cabai",
        "raw_name": "cabai",
        "quantity": "1",
        "unit": "kg",
        "line_total_idr": None,
    }
    assert any("Unprefixed whole-number price" in warning for warning in result["warnings"])


def test_normalization_warns_when_quantity_is_missing():
    result = extract_purchase("beli beras")["items"][0]
    normalized = normalize_item(result)
    assert any("Quantity is missing" in warning for warning in normalized["warnings"])
