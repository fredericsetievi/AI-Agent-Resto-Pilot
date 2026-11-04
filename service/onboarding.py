"""Restaurant selection for WhatsApp senders who have no existing link."""

from collections import Counter


def restaurant_selection(choices, text):
    """Return (selected restaurant or None, reply) without changing any data.

    Names and stable restaurant IDs are accepted. A duplicate name requires
    its displayed ID; a menu reorder cannot silently select another tenant.
    """
    choice = " ".join(str(text or "").casefold().split())
    if choice.startswith("pilih "):
        choice = choice[6:].strip()
    matches = [row for row in choices if choice in {row["id"].casefold(), row["name"].casefold()}]
    if choice and len(matches) == 1:
        selected = matches[0]
        return selected, (f"Nomor WhatsApp ini terhubung ke {selected['name']}. "
                          "Kirim ulang rincian pembelianmu untuk membuat draf, "
                          "atau tanyakan stok. Belum ada pembelian yang dicatat.")
    if not choices:
        return None, "Belum ada restoran tersedia. Hubungi pengelola RestoPilot. Belum ada pembelian yang dicatat."
    counts = Counter(row["name"].casefold() for row in choices)
    lines = [f"- {row['name']}" + (f" (kode: {row['id']})" if counts[row['name'].casefold()] > 1 else "")
             for row in choices]
    return None, ("Selamat datang di RestoPilot. Pilih restoran untuk nomor WhatsApp ini:\n"
                  + "\n".join(lines)
                  + "\nBalas dengan nama restoran (atau kode jika namanya sama). "
                  "Pesan pembelian belum diproses; kirim ulang setelah memilih restoran.")
