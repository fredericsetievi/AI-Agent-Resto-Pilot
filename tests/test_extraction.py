from service.extraction import extract_purchase, is_chatter_item, match_catalog_detail, match_catalog_name, normalize_item


# A real pre-order forward from the cake-shop pilot: five shops, ~50 cakes,
# bullet lines, shop-name headers, and four different price notations in one
# message. The owner forwards this to know how much cash to bring.
CAKE_ORDER = "\n".join([
    "Pre-order bolu buat kamis ya Bu:",
    "Dari Toko Melati:",
    "- bolu pandan 2 pcs Rp25.000, bolu coklat 1 pcs 20rb, bolu keju 1 pcs 30000, bolu srikaya 1 pcs 22rb, bolu mawar 1 pcs 25 ribu, bolu mesis 1 pcs 27.500, bolu kukus 1 pcs 18rb, bolu gula 1 pcs 21000, bolu legit 1 pcs 30rb, bolu coklat 1 pcs Rp20.000",
    "Dari Bu Ratna:",
    "- bolu coklat 2 pcs 20rb, bolu keju 1 pcs 28rb, bolu pandan 1 pcs 24rb, bolu srikaya 1 pcs 21rb, bolu mawar 1 pcs 26rb, bolu mesis 1 pcs 27.500, bolu kukus 1 pcs 17rb, bolu gula 1 pcs 20rb, bolu legit 1 pcs 29rb, bolu keju 1 pcs Rp28.000",
    "Dari Hj. Nia:",
    "- bolu kukus 2 pcs 15000, bolu pandan 1 pcs 23rb, bolu coklat 1 pcs 19rb, bolu keju 1 pcs 27rb, bolu srikaya 1 pcs 20rb, bolu gula 1 pcs 18rb, bolu mesis 1 pcs 26rb, bolu mawar 1 pcs 24rb, bolu legit 1 pcs 28rb, bolu kukus 1 pcs Rp15.000",
    "Dari Toko Lima:",
    "- bolu pandan 2 pcs 25rb, bolu keju 1 pcs 30rb, bolu coklat 1 pcs 21rb, bolu srikaya 1 pcs 22rb, bolu mawar 1 pcs 25rb, bolu kukus 1 pcs 16rb, bolu gula 1 pcs 19rb, bolu mesis 1 pcs 28rb, bolu legit 1 pcs 31rb, bolu pandan 1 pcs Rp25.000",
    "Dari Bu Ani:",
    "- bolu srikaya 2 pcs 21rb, bolu coklat 1 pcs 20rb, bolu pandan 1 pcs 24rb, bolu keju 1 pcs 29rb, bolu kukus 1 pcs 15rb, bolu mawar 1 pcs 23rb, bolu gula 1 pcs 18rb, bolu mesis 1 pcs 27rb, bolu legit 1 pcs 30rb, bolu srikaya 1 pcs Rp21.000",
])


def test_forwarded_cake_preorder_from_five_shops_parses_every_cake():
    result = extract_purchase(CAKE_ORDER)
    chatter = [item for item in result["items"] if is_chatter_item(item)]
    cakes = [item for item in result["items"] if not is_chatter_item(item)]
    # The greeting sentence parses as a pseudo-item and is detected as chatter
    # so the store layer drops it instead of drafting a phantom product.
    assert [item["raw_name"] for item in chatter] == ["pre-order bolu buat kamis ya bu"]
    assert all(warning.startswith("Line 1:") for warning in result["warnings"])

    # All 50 cake clauses across the five shops survive as reviewable items.
    assert len(cakes) == 50
    assert sum(int(item["quantity"]) for item in cakes) == 55
    # Bullet markers never leak into the item name.
    assert all(item["raw_name"].startswith("bolu") for item in cakes)
    assert all(item["unit"] == "pcs" and item["line_total_idr"] is not None for item in cakes)
    # The demo catalog has no bolu flavors: items stay unmapped for the owner
    # to confirm (auto-created on approval) instead of being force-matched.
    assert all(item["ingredient_id"] is None for item in cakes)
    assert all(item.get("matched_from") is None for item in cakes)

    # Rp-prefixed and rb/ribu-suffixed amounts are lot totals; a bare number
    # after a quantity is a per-piece price (2 × Rp15.000 = Rp30.000).
    assert cakes[0]["line_total_idr"] == 25000  # 2 pcs Rp25.000
    assert cakes[1]["line_total_idr"] == 20000  # 1 pcs 20rb
    assert cakes[20]["line_total_idr"] == 30000  # 2 pcs 15000, per-piece

    # Known limitation: only the first shop header is kept as the supplier.
    assert result["supplier"] == "Toko Melati"


def test_preorder_list_without_prices_still_drafts_the_quantities():
    # Owners forward order lists before prices exist — they just need the
    # cake counts for the cash envelope. Quantities draft; totals warn.
    result = extract_purchase("Dari Bu Ratna:\n- bolu pandan 3 pcs, bolu coklat 2 pcs\n- bolu keju 5 pcs")
    assert [item["raw_name"] for item in result["items"]] == ["bolu pandan", "bolu coklat", "bolu keju"]
    assert [item["quantity"] for item in result["items"]] == ["3", "2", "5"]
    assert all(item["line_total_idr"] is None for item in result["items"])
    assert result["supplier"] == "Bu Ratna"
    assert len(result["warnings"]) == 3  # one missing-total warning per line


def test_one_item_per_line_order_sheet_exceeds_the_20_line_limit():
    # A fully formatted order sheet (one line per cake) cannot fit the
    # 20-line intake; the parser refuses everything instead of truncating
    # silently, and tells the owner to split it into parts.
    order = "\n".join(["dari Toko Lima:"] + [f"- bolu pandan {index % 9 + 1} pcs Rp25.000" for index in range(25)])
    result = extract_purchase(order)
    assert result["items"] == []
    assert any("20-item limit" in warning and "split" in warning for warning in result["warnings"])


def test_x_quantity_notation_is_not_parsed_and_needs_dashboard_correction():
    # The "x5" count notation from WhatsApp order forms is unsupported: the
    # item survives with its name intact but no quantity, so the draft asks
    # for correction rather than guessing a number.
    result = extract_purchase("bolu pandan x5")
    item = result["items"][0]
    assert item["raw_name"] == "bolu pandan x5"
    assert item["quantity"] is None


def test_x_multiplier_quotes_a_per_unit_price():
    # "minyak 2L x 38rb" means two liters at Rp38.000 each (pilot corpus).
    result = extract_purchase("beli minyak 2L x 38rb")
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["raw_name"].startswith("minyak")
    assert item["quantity"] == "2" and item["unit"] == "l"
    assert item["line_total_idr"] == 76000
    assert result["warnings"] == []


def test_x_multiplier_without_scale_still_reads_as_a_price():
    result = extract_purchase("gula 2 kg x 15000")
    item = result["items"][0]
    assert item["quantity"] == "2" and item["unit"] == "kg"
    assert item["line_total_idr"] == 30000


def test_trailing_per_unit_annotation_does_not_reject_the_line():
    # "per kg" restates the price basis; it must not be treated as instruction
    # language that discards the whole line (pilot finding).
    result = extract_purchase("beli telur 1 kg 30000 per kg")
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["quantity"] == "1" and item["unit"] == "kg"
    assert item["line_total_idr"] == 30000
    assert result["warnings"] == []

    result = extract_purchase("telur 1kg 30000/kg")
    assert result["items"][0]["line_total_idr"] == 30000


def test_harga_clause_is_a_price_not_an_item():
    # "beli 3 kg cabai merah, harga 150000" must price the cabai, not draft a
    # phantom "Harga" product (pilot finding).
    result = extract_purchase("beli 3 kg cabai merah, harga 150000")
    assert [item["raw_name"] for item in result["items"]] == ["cabai merah"]
    item = result["items"][0]
    assert item["quantity"] == "3" and item["unit"] == "kg"
    assert item["line_total_idr"] == 150000


def test_keyword_only_total_clause_attaches_to_a_lone_item():
    result = extract_purchase("cabai merah 3kg, total 150rb")
    assert [item["raw_name"] for item in result["items"]] == ["cabai merah"]
    assert result["items"][0]["line_total_idr"] == 150000
    assert result["warnings"] == []


def test_shared_total_is_never_misattributed_to_the_last_item():
    # "bayar 250rb" pays for BOTH items: the amount is dropped from gula
    # merah and flagged instead of pricing it at the whole order total.
    result = extract_purchase("Beli beras 10kg dan gula merah 3kg bayar 250rb")
    items = result["items"]
    assert [item["raw_name"] for item in items] == ["beras", "gula merah"]
    assert all(item["quantity"] is not None for item in items)
    assert all(item["line_total_idr"] is None for item in items)
    assert any("250000" in warning and "total" in warning.casefold() for warning in result["warnings"])

    # "total 66rb" must not become a phantom third product either.
    result = extract_purchase("tadi pagi beli telur 2kg, kecombrang 1 bungkus, total 66rb")
    # V34.4 strips the "tadi pagi beli" chat opener so the item matches the
    # catalog instead of drafting a phantom product with a chat name.
    assert [item["raw_name"] for item in result["items"]] == ["telur", "kecombrang"]
    assert all(item["line_total_idr"] is None for item in result["items"])
    assert any("66000" in warning for warning in result["warnings"])


def test_embedded_total_still_prices_its_own_clause():
    # A "total" amount inside a single clause prices that clause only; a
    # shared total is announced by its own clause or with "bayar".
    result = extract_purchase("cabai 2kg total 30000, telur 1kg 33000")
    assert result["items"][0]["line_total_idr"] == 30000
    assert result["items"][1]["line_total_idr"] == 33000
    assert result["warnings"] == []


def test_keyword_only_amount_alone_yields_no_phantom_item():
    result = extract_purchase("total 66rb")
    assert result["items"] == []
    assert result["warnings"]


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
    # Letter slips with the same opening word are recovered.
    assert match_catalog_name("kombrang")[1] == "kecombrang"
    assert match_catalog_name("bwang merah")[1] == "bawang"
    assert match_catalog_name("cabay")[1] == "cabai"


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


def test_trailing_dari_clause_becomes_supplier():
    result = extract_purchase("Beli cabai 3 kg 150rb dari Bu Sari")
    assert result["items"] == [
        {"ingredient_id": "cabai", "raw_name": "cabai", "quantity": "3", "unit": "kg", "line_total_idr": 150000}
    ]
    assert result["warnings"] == []
    assert result["supplier"] == "Bu Sari"

    # Trailing punctuation is trimmed from the name.
    assert extract_purchase("beli cabai 3 kg 150rb dari Bu Sari.")["supplier"] == "Bu Sari"

    # No supplier clause: the result stays exactly as before.
    assert "supplier" not in extract_purchase("beli cabai 3 kg 150rb")

    # The clause is stripped per line; the first named supplier wins.
    multi = extract_purchase("beli cabai 3 kg 150rb dari Bu Sari\nbeli telur 1 kg 30rb")
    assert [item["ingredient_id"] for item in multi["items"]] == ["cabai", "telur"]
    assert multi["supplier"] == "Bu Sari"

    # An item without quantity or price still parses; the supplier is kept.
    sparse = extract_purchase("beli cabai dari Bu Sari")
    assert sparse["items"][0]["ingredient_id"] == "cabai"
    assert sparse["supplier"] == "Bu Sari"
    assert any("quantity" in warning for warning in sparse["warnings"])


def test_supplier_header_line_names_the_supplier_without_adding_an_item():
    result = extract_purchase("Beli dari Dika:\nkembung 25 ekor Rp40000\nmas 6 ekor Rp30000")
    assert [item["raw_name"] for item in result["items"]] == ["kembung", "mas"]
    assert result["supplier"] == "Dika"
    assert result["warnings"] == []

    # A bare "dari <name>" header works too, and trailing punctuation is trimmed.
    assert extract_purchase("dari Pasar Pagi.\ntelur 2 kg Rp56000")["supplier"] == "Pasar Pagi"

    # A "dari" line with items on it is still a purchase line, not a header.
    inline = extract_purchase("beli cabai 3 kg 150rb dari Bu Sari")
    assert [item["ingredient_id"] for item in inline["items"]] == ["cabai"]
    assert inline["supplier"] == "Bu Sari"


def test_comma_and_dan_lists_split_into_separate_items():
    result = extract_purchase("beli kembung 25 ekor, mas 6 ekor")
    assert [item["raw_name"] for item in result["items"]] == ["kembung", "mas"]
    assert [item["quantity"] for item in result["items"]] == ["25", "6"]

    dan = extract_purchase("beli gula 5 kg dan telur 2 kg Rp56000")
    assert [item["raw_name"] for item in dan["items"]] == ["gula", "telur"]
    assert dan["items"][1]["line_total_idr"] == 56000

    # Approval prefixes and digit-less clauses never split.
    assert len(extract_purchase("setuju, beli cabai 3 kg 150rb")["items"]) == 1

    # Dish-style names and digit-less conjunction lists keep the legacy
    # single-clause rejection with its ambiguity warning.
    dish = extract_purchase("bumbu dan rempah 3 kg Rp60000")
    assert dish["items"] == []
    assert any("multiple item clauses" in warning for warning in dish["warnings"])

    # Decimal commas stay inside the price: the split never touches them.
    price = extract_purchase("gula 2 kg Rp7,5rb")
    assert price["items"][0]["raw_name"] == "gula"
    assert price["items"][0]["line_total_idr"] == 7500

    # Clauses that fail to parse fall back to the single-clause behavior.
    fallback = extract_purchase("beli gula dan garam")
    assert fallback["items"] == []
    assert any("multiple item clauses" in warning for warning in fallback["warnings"])


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
    # Fully specified "dan" lists now parse as separate items.
    combined = extract_purchase("beli cabai 1 kg 20rb dan beras 2 kg 30rb")
    assert [item["ingredient_id"] for item in combined["items"]] == ["cabai", "beras"]
    assert combined["warnings"] == []

    # Clauses without a name still fall back to the ambiguity warning.
    nameless = extract_purchase("beli gula dan garam")
    assert nameless["items"] == []
    assert any("multiple item clauses" in warning for warning in nameless["warnings"])

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


def test_split_message_chunks_respects_the_line_cap():
    from service.extraction import split_message_chunks

    lines = [f"{index}kg bahan nomor {index}" for index in range(1, 26)]
    chunks = split_message_chunks("\n".join(lines))
    assert len(chunks) == 2
    assert len(chunks[0].splitlines()) == 20
    assert len(chunks[1].splitlines()) == 5
    assert chunks[0].splitlines()[0] == "1kg bahan nomor 1"
    assert chunks[1].splitlines()[-1] == "25kg bahan nomor 25"
    assert split_message_chunks("beli cabai 3 kg") == ["beli cabai 3 kg"]
    assert split_message_chunks("") == []
    assert split_message_chunks(None) == []


def test_has_itemized_purchase_signal_needs_digit_lines():
    from service.extraction import has_itemized_purchase_signal

    listed = "\n".join(["#2 Sayuran:", "2kg daun bawang", "5kg wortel", "6kg kol"])
    assert has_itemized_purchase_signal(listed)
    assert not has_itemized_purchase_signal("\n".join(["halo mas", "pagi ini cerah", "nanti kami ke pasar"]))
    assert not has_itemized_purchase_signal("2kg daun bawang")
    assert not has_itemized_purchase_signal(None)


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


def test_unprefixed_whole_number_price_is_per_unit():
    # A bare trailing number reads as a per-unit price: owners quote prices
    # per piece ("banana cake 10 2500" meant ten pieces at Rp2.500 each —
    # the old total reading produced a nonsensical Rp250 piece, Nov 2026
    # pilot finding). Quantity 1 is unchanged by the reading.
    priced = extract_purchase("beli cabai 1 kg 25000")
    assert priced["items"][0] == {
        "ingredient_id": "cabai",
        "raw_name": "cabai",
        "quantity": "1",
        "unit": "kg",
        "line_total_idr": 25000,
    }
    assert priced["warnings"] == []

    cakes = extract_purchase("banana cake 10 2500")
    assert cakes["items"][0]["quantity"] == "10"
    assert cakes["items"][0]["line_total_idr"] == 25000  # 10 × Rp2.500

    # The word "total" before a bare amount flips to a lot price.
    lot = extract_purchase("beli cabai 2 kg total 30000")
    assert lot["items"][0]["line_total_idr"] == 30000
    assert lot["warnings"] == []

    # A bare amount without any quantity never reaches the per-unit reading:
    # it is flagged as missing price/quantity for the draft to clarify.
    orphan = extract_purchase("banana cake 2500")
    assert orphan["items"][0]["line_total_idr"] is None
    assert orphan["warnings"]

    # Small bare numbers stay ambiguous — "5" is more likely another quantity.
    small = extract_purchase("beli cabai 1 kg 5")
    assert small["items"][0]["line_total_idr"] is None
    assert any("Unprefixed whole-number price" in warning for warning in small["warnings"])


def test_normalization_warns_when_quantity_is_missing():
    result = extract_purchase("beli beras")["items"][0]
    normalized = normalize_item(result)
    assert any("Quantity is missing" in warning for warning in normalized["warnings"])


def test_spelling_recoveries_are_marked_for_confirmation():
    assert match_catalog_detail("bwang putih") == ("bwang putih", "bawang_putih", True)
    assert match_catalog_detail("kombrang")[2] is True
    # Trusted alias and prefix matches stay unmarked.
    assert match_catalog_detail("cabe")[2] is False
    assert match_catalog_detail("bawang putih kupas")[2] is False
    assert match_catalog_detail("daun bawang")[1] is None

    result = extract_purchase("beli bwang putih 2 kg 30rb")
    item = result["items"][0]
    assert item["ingredient_id"] == "bawang_putih"
    assert item["matched_from"] == "bwang putih"
    assert result["warnings"] == []
    exact = extract_purchase("beli cabe 3 kg 150rb")["items"][0]
    assert "matched_from" not in exact
    # The annotation survives normalization so the confirmation reply can show it.
    assert normalize_item(dict(item))["matched_from"] == "bwang putih"


# Real supplier messages from the warung pilot (Nov 2026), verbatim except
# that supplier names are anonymized. Owners forward what they sent to the
# supplier (and what the supplier quoted) so the draft shows what cash to
# bring; the bot must eat the jargon, PO grammar, and package sizes as-is.
SUPPLIER_DELIVERY_NOTE = (
    "Nanti siang, kirim 2 gas, 3 melon, 8 isul, 2 aqua.\n"
    "Isul = air isi ulang utk customer"
)
SUPPLIER_PO_SUNCO = "Pagi mas. Selasa ini, sy PO Sunco @2ltr 10 dus ya🙏"
SUPPLIER_PO_TEH = (
    "Siang mas. Dr warung tetangga, mau PO 10pcs teh racik merah kemasan 500gr, "
    "20pcs teh sereh 500gr, 5 pcs teh lemon 500 gr. Bisa ya 🙏"
)
SUPPLIER_PO_PACKAGING = "\n".join([
    "Pagi ci. Bisa pesan:",
    "1. Sedotan plastik bungkus kertas 1 pak",
    "2. Tusuk gigi bungkus kertas 2 pak",
    "3. Kantong plastik Tiger no.15, 1 ikat.",
    "4. Kantong plastik Tiger no.24, 4 ikat",
    "5. Kantong plastik Tiger no.28, 4 ikat",
    "6. Kantong plastik Tiger no.35, 1 ikat",
    "7. ATP 12x25 6 ikat.",
    "8. Kertas bungkus nasi Suparma pink 2 ikat",
    "9. Gelas plastik 18oz + tutup cembung, 2 slop",
    "10. Karet gelang kuning 10 pak",
    "11. Sendok plastik 500pcs",
    "12. Plastik sampah Laris 90x120 3 pak",
    "Kabari ya ci. Usahakan bisa diantar siang/sore nanti y🙏",
])


def test_delivery_note_with_jargon_items_and_inline_definition():
    result = extract_purchase(SUPPLIER_DELIVERY_NOTE)
    chatter = [item for item in result["items"] if is_chatter_item(item)]
    items = [item for item in result["items"] if not is_chatter_item(item)]
    # The greeting prefix "Nanti siang" is dropped with the comma clauses,
    # the jargon names survive verbatim, and the inline "Isul = ..." gloss is
    # junk chat: it drafts no phantom product (V34.4).
    assert [item["raw_name"] for item in items] == ["gas", "melon", "isul", "aqua"]
    assert [item["quantity"] for item in items] == ["2", "3", "8", "2"]
    assert all(item["unit"] is None and item["line_total_idr"] is None for item in items)
    assert chatter == []


def test_po_forward_with_at_size_marker_and_politeness_tail():
    result = extract_purchase(SUPPLIER_PO_SUNCO)
    items = result["items"]
    assert len(items) == 1
    # The "@2ltr" pack size is lifted out of the name into pack fields (V34.4).
    assert items[0]["raw_name"] == "sunco"
    assert items[0]["quantity"] == "10"
    assert items[0]["unit"] == "dus"
    assert items[0]["pack_size"] == "2"
    assert items[0]["pack_unit"] == "l"
    assert items[0]["line_total_idr"] is None
    assert result["warnings"] == ["Line 1: line total is missing or unsupported."]


def test_po_at_price_per_unit_multiplies_quantity():
    priced = extract_purchase("Sunco 10 dus @15rb")
    assert priced["items"][0]["raw_name"] == "sunco"
    assert priced["items"][0]["quantity"] == "10"
    assert priced["items"][0]["line_total_idr"] == 150000
    assert priced["warnings"] == []
    rp = extract_purchase("beli minyak 2 dus @Rp15.000")
    assert rp["items"][0]["line_total_idr"] == 30000
    assert rp["warnings"] == []
    # "10 dus @15rb" order works the same as "@15rb 10 dus".
    trailing = extract_purchase("beli kardus 5 ikat @2rb")
    assert trailing["items"][0]["line_total_idr"] == 10000


def test_warung_po_list_parses_leading_counts_and_package_sizes():
    result = extract_purchase(SUPPLIER_PO_TEH)
    items = result["items"]
    assert [item["raw_name"] for item in items] == [
        "teh racik merah kemasan 500gr",
        "teh sereh 500gr",
        "teh lemon 500 gr",
    ]
    assert [item["quantity"] for item in items] == ["10", "20", "5"]
    assert all(item["unit"] == "pcs" for item in items)


def test_package_size_qualifiers_are_not_quantities():
    guarded = extract_purchase("beli teh racik merah kemasan 500gr")
    assert guarded["items"][0]["raw_name"] == "teh racik merah kemasan 500gr"
    assert guarded["items"][0]["quantity"] is None
    assert any("quantity is missing" in warning for warning in guarded["warnings"])
    # A lone "@2ltr" is the package contents, not a count: the name stays
    # clean and the pack size rides into its own fields.
    sized = extract_purchase("beli sunco @2ltr")
    assert sized["items"][0]["raw_name"] == "sunco"
    assert sized["items"][0]["quantity"] is None
    assert sized["items"][0]["pack_size"] == "2"
    assert sized["items"][0]["pack_unit"] == "l"


def test_numbered_packaging_po_parses_every_line():
    result = extract_purchase(SUPPLIER_PO_PACKAGING)
    chatter = [item for item in result["items"] if is_chatter_item(item)]
    items = [item for item in result["items"] if not is_chatter_item(item)]
    assert [item["raw_name"] for item in chatter] == [
        "pagi ci. bisa pesan",
        "kabari ya ci. usahakan bisa diantar siang/sore nanti",
    ]
    assert [item["raw_name"] for item in items] == [
        "sedotan plastik bungkus kertas",
        "tusuk gigi bungkus kertas",
        "kantong plastik tiger no.15",
        "kantong plastik tiger no.24",
        "kantong plastik tiger no.28",
        "kantong plastik tiger no.35",
        "atp 12x25",
        "kertas bungkus nasi suparma pink",
        "gelas plastik 18oz + tutup cembung",
        "karet gelang kuning",
        "sendok plastik",
        "plastik sampah laris 90x120",
    ]
    assert [item["quantity"] for item in items] == ["1", "2", "1", "4", "4", "1", "6", "2", "2", "10", "500", "3"]


def test_order_verb_prefixes_are_stripped_from_item_names():
    for line in ["kirim 3 telur", "po 3 telur", "mau pesan 3 telur", "sy pesan 3 telur", "beli 3 telur"]:
        result = extract_purchase(line)
        assert result["items"][0]["raw_name"] == "telur", line
        assert result["items"][0]["quantity"] == "3", line


# ---- V34.4: glued quantities, k-scale prices, junk lines, question tails,
# pack-size markers, and estimated quantity ranges (owner replay findings).

def test_glued_quantity_splits_from_the_name():
    for line, name, qty, unit in [
        ("telur2kg", "telur", "2", "kg"),
        ("beli telur2kg", "telur", "2", "kg"),
        ("beli beras10kg 120rb", "beras", "10", "kg"),
        ("telur2,5kg", "telur", "2.5", "kg"),
        ("minyak goreng2l", "minyak goreng", "2", "l"),
    ]:
        result = extract_purchase(line)
        assert result["items"][0]["raw_name"] == name, line
        assert result["items"][0]["quantity"] == qty, line
        assert result["items"][0]["unit"] == unit, line
    # A price scale glued to the name is not a quantity unit.
    assert extract_purchase("kopi25rb")["items"][0]["raw_name"] == "kopi25rb"


def test_k_suffix_reads_as_a_price():
    priced = extract_purchase("buy 2 kg sugar 25k")
    assert priced["items"][0]["raw_name"] == "sugar"
    assert priced["items"][0]["quantity"] == "2"
    assert priced["items"][0]["line_total_idr"] == 25000
    assert priced["warnings"] == []
    bare = extract_purchase("gula 25k")
    assert bare["items"][0]["raw_name"] == "gula"
    assert bare["items"][0]["line_total_idr"] == 25000


def test_junk_lines_never_become_phantom_items():
    for line in [
        "Bon jika barang tersedia",
        "Fwd: pesanan",
        "FWD: pesanan",
        "hubungi agus 0812xx",
        "wa: 081234567890",
        "agus 081234567890",
        "Isul = air isi ulang utk customer",
    ]:
        assert extract_purchase(line)["items"] == [], line


def test_question_tail_after_a_purchase_is_dropped():
    result = extract_purchase("beli kecombrang 1 kg 25rb. btw stok beras masih berapa?")
    assert result["items"][0]["raw_name"] == "kecombrang"
    assert result["items"][0]["quantity"] == "1"
    assert result["items"][0]["unit"] == "kg"
    assert result["items"][0]["line_total_idr"] == 25000
    assert result["warnings"] == []


def test_greeting_and_question_lines_classify_as_chatter_not_stock():
    # Owners mix greetings and questions into a purchase message; those lines
    # parse as chatter items — visible in the thread, never catalog products.
    result = extract_purchase(
        "Papa pagi ini beli\n"
        "3 gas 12kg\n"
        "4 gas 3kg\n"
        "8 galon air isi ulang\n"
        "bagaimana isinya, ci?"
    )
    stock = [item for item in result["items"] if not is_chatter_item(item)]
    chatter = [item for item in result["items"] if is_chatter_item(item)]
    assert [item["raw_name"] for item in stock] == ["gas 12kg", "gas 3kg", "galon air isi ulang"]
    assert [item["quantity"] for item in stock] == ["3", "4", "8"]
    assert "bagaimana isinya" in " ".join(item["raw_name"] or "" for item in chatter)


def test_chatter_filter_keeps_real_priceless_items_and_priced_lines():
    # Priceless-but-real lines still draft to ask for prices; greeting-embedded
    # priced lines are unaffected; digit-less stock questions are chatter.
    result = extract_purchase("kecap manis\nCimory 2 15000\nsisa beras masih berapa?")
    stock = [item for item in result["items"] if not is_chatter_item(item)]
    chatter = [item for item in result["items"] if is_chatter_item(item)]
    assert [item["raw_name"] for item in stock] == ["kecap manis", "cimory"]
    assert stock[1]["line_total_idr"] == 30000
    assert len(chatter) == 1


def test_chat_only_message_classifies_as_chatter():
    result = extract_purchase("halo pagi pa, gimana kabarnya?")
    assert result["items"] and all(is_chatter_item(item) for item in result["items"])


def test_pack_size_marker_lands_in_pack_fields_and_multiplies():
    result = extract_purchase("PO Sunco @2ltr 10 dus ya🙏")
    item = result["items"][0]
    assert item["raw_name"] == "sunco"
    assert item["quantity"] == "10"
    assert item["unit"] == "dus"
    assert item["pack_size"] == "2"
    assert item["pack_unit"] == "l"
    # Normalization multiplies the pack contents into the stock movement.
    normalized = normalize_item(dict(item))
    assert normalized["canonical_quantity"] == "20"
    assert normalized["canonical_unit"] == "l"
    # A known volume ingredient still converts cleanly.
    minyak = normalize_item({**item, "ingredient_id": "minyak", "raw_name": "minyak", "line_total_idr": 380000})
    assert minyak["canonical_quantity"] == "20"
    assert minyak["warnings"] == []
    # A dimension mismatch (ltr pack on a kg ingredient) keeps the warning.
    mismatch = normalize_item({**item, "ingredient_id": "beras", "raw_name": "beras"})
    assert mismatch["canonical_quantity"] is None
    assert mismatch["warnings"]
    # "@15rb" is a per-unit price, never a pack size.
    priced = extract_purchase("Sunco 10 dus @15rb")
    assert priced["items"][0].get("pack_size") is None
    assert priced["items"][0]["line_total_idr"] == 150000


def test_quantity_range_uses_the_lower_bound_with_a_note():
    result = extract_purchase("beli cabai sekitar 3-4 kg 150rb")
    item = result["items"][0]
    assert item["raw_name"] == "cabai sekitar"
    assert item["quantity"] == "3"
    assert item["unit"] == "kg"
    assert item["line_total_idr"] == 150000
    assert any("quantity range 3-4 kg" in warning for warning in result["warnings"])
    leading = extract_purchase("beli 3-4 kg cabai")
    assert leading["items"][0]["quantity"] == "3"
    assert leading["items"][0]["unit"] == "kg"
    spaced = extract_purchase("3 s/d 4 kg cabai")
    assert spaced["items"][0]["quantity"] == "3"
    assert spaced["items"][0]["unit"] == "kg"
    kata = extract_purchase("2 sampai 3 dus telur")
    assert kata["items"][0]["quantity"] == "2"
    assert kata["items"][0]["unit"] == "dus"


def test_new_ingredient_announcement_is_a_clean_purchase_line():
    result = extract_purchase("pengen nambah bahan baru: kecap manis 500ml")
    assert result["items"][0]["raw_name"] == "kecap manis"
    assert result["items"][0]["quantity"] == "500"
    assert result["items"][0]["unit"] == "ml"
    priced = extract_purchase("tambah bahan baru: saos sambal 3 botol 45rb")
    assert priced["items"][0]["raw_name"] == "saos sambal"
    assert priced["items"][0]["line_total_idr"] == 45000
    assert priced["warnings"] == []


def test_time_prefix_does_not_pollute_the_item_name():
    result = extract_purchase("tadi pagi beli telur 2kg, 1 bungkus kecombrang, total 66rb")
    names = [item["raw_name"] for item in result["items"]]
    assert "tadi pagi beli telur" not in names
    assert names[0] == "telur"
    assert result["items"][0]["ingredient_id"] == "telur"
    assert result["items"][0]["quantity"] == "2"
    assert names[1] == "kecombrang"
    yesterday = extract_purchase("kemarin beli telur 1 kg 33rb")
    assert yesterday["items"][0]["ingredient_id"] == "telur"
    assert yesterday["items"][0]["line_total_idr"] == 33000
