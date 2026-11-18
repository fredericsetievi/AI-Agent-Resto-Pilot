"""Business analysis agent: read-only tool-use for open owner questions.

When the deterministic agents (opname, menu facts, stock facts, supplier,
purchase parser) cannot answer an owner question, this agent may call
READ-ONLY store tools to compose an answer with real numbers. The
contract, enforced here rather than by prompting alone:

- Tools are tenant-locked server-side: the restaurant id is bound when
  the tools are built and never comes from the model.
- Tools are read-only SELECT-path store methods. Writes stay behind the
  owner approval gates and are never exposed to this module.
- Budget: at most MAX_TOOL_CALLS tool calls per question; each tool
  result is truncated; the model must answer with numbers taken from
  tool outputs, never invented ones.
- Fail-open: any error raises BizAgentError and the caller keeps the
  existing deterministic behavior (help reply + intent_unknown log).
"""
import asyncio
import json
import re
import httpx

MAX_TOOL_CALLS = 4
TOOL_RESULT_CHARS = 1500
ANSWER_CHARS = 2500
QUESTION_RE = re.compile(r"(?i)\?|\b(berapa|kenapa|gimana|bagaimana|apakah|kapan|analisis|bandingkan|proyeksi|mana)\b")

DEFAULT_MODEL = "stepfun-2-16k"
DEFAULT_BASE_URL = "https://api.stepfun.com/v1"


class BizAgentError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


def question_shaped(text):
    """True when a message looks like an open question worth the agent.

    Deliberately narrow: purchases, schedule changes, and commands must
    keep flowing through the deterministic agents even when they read
    like questions at a glance.
    """
    text = str(text or "").strip()
    if not text or len(text) > 300:
        return False
    return bool(QUESTION_RE.search(text))


def clamp_days(value):
    """Clamp a model-supplied window to the supported 1..90 days."""
    try:
        return max(1, min(int(value), 90))
    except (TypeError, ValueError):
        return 7


def build_tools(store, restaurant_id):
    """Bind the read-only tools to one tenant. The restaurant id is bound
    here and never comes from the model."""
    return {
        "sales_insights": {
            "description": "Penjualan menu dalam periode: porsi, omzet, peringkat menu, dan perubahan vs periode sebelumnya.",
            "args": {"days": "jumlah hari 1-90 (opsional, default 7)"},
            "run": lambda args: store.sales_insights(restaurant_id, clamp_days(args.get("days"))),
        },
        "supplier_best_prices": {
            "description": "Kutipan harga terbaru per bahan dari semua supplier, beserta daftar supplier terdaftar.",
            "args": {},
            "run": lambda args: store.supplier_overview(restaurant_id),
        },
        "low_stock_items": {
            "description": "Bahan yang stoknya di bawah ambang pesan ulang beserta saran jumlah beli.",
            "args": {},
            "run": lambda args: {"candidates": store.low_stock_candidates(restaurant_id)},
        },
        "cash_status": {
            "description": "Amplop kas hari ini: budget yang dibawa, uang kembali, total pembelian tercatat, dan sisa yang belum terjelaskan.",
            "args": {},
            "run": lambda args: store.cash_envelope_status(restaurant_id),
        },
    }


def _system_prompt(tool_catalog):
    return (
        "Anda analis bisnis untuk pemilik warung kecil di Indonesia. Jawab selalu dalam Bahasa Indonesia "
        "yang sederhana dan ramah.\n"
        "Anda menjawab pertanyaan BACA-SAJA tentang data bisnis resto ini. Untuk mendapatkan angka, "
        "panggil alat yang tersedia; angka hanya boleh berasal dari hasil alat — tidak boleh mengarang.\n"
        "Aturan:\n"
        "1. Panggil alat seperlunya (maksimal beberapa kali), lalu jawab.\n"
        "2. Sebutkan periode setiap angka (mis. '7 hari terakhir').\n"
        "3. Jika tidak ada data atau alat tidak menjawab pertanyaan, katakan jujur dan sarankan perintah "
        "chat yang relevan (mis. 'tanya harga cabai ke supplier').\n"
        "4. Anda tidak bisa mencatat pembelian, mengubah stok, atau menulis apa pun — jika diminta, "
        "sarankan perintah chat yang sesuai tanpa mencoba.\n"
        "5. Jawaban maksimal beberapa kalimat atau poin singkat.\n\n"
        "Alat yang tersedia (panggil dengan JSON):\n" + json.dumps(tool_catalog, ensure_ascii=False)
    )


def _parse_action(content):
    """Extract the action JSON from a model reply; anything else fails."""
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise BizAgentError("The business agent returned no JSON object.")
    try:
        parsed = json.loads(match.group(0))
    except ValueError:
        raise BizAgentError("The business agent returned invalid JSON.")
    if not isinstance(parsed, dict) or "action" not in parsed:
        raise BizAgentError("The business agent returned no action.")
    return parsed


async def _chat(messages, *, api_key, model, base_url, client):
    payload = {"model": model, "temperature": 0, "messages": messages}
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)
    try:
        response = await client.post(
            base_url + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
    except Exception as exc:  # transport, DNS, timeouts — the caller fails open
        raise BizAgentError(f"The business agent could not be reached: {str(exc)[:120]}.")
    finally:
        if owns_client:
            await client.aclose()
    if response.status_code != 200:
        status = 429 if response.status_code == 429 else 502
        raise BizAgentError(f"The business agent rejected the request (HTTP {response.status_code}).", status)
    try:
        content = response.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        raise BizAgentError("The business agent returned an unexpected response.")
    if not isinstance(content, str) or not content.strip():
        raise BizAgentError("The business agent returned an empty result.")
    return content


async def _run_tool(tool, args):
    return await asyncio.to_thread(tool["run"], args)


async def answer_business_question(text, *, store, restaurant_id, api_key,
                                   model=None, base_url=None, client=None,
                                   max_tool_calls=MAX_TOOL_CALLS):
    """Answer one open owner question with read-only tools; fail-open."""
    if not api_key or not api_key.strip():
        raise BizAgentError("The business agent is not configured.", 503)
    tools = build_tools(store, restaurant_id)
    tool_catalog = [{"name": name, "description": tool["description"], "args": tool["args"]}
                    for name, tool in tools.items()]
    messages = [
        {"role": "system", "content": _system_prompt(tool_catalog)},
        {"role": "user", "content": str(text).strip()[:600]},
    ]
    model = model or DEFAULT_MODEL
    base_url = base_url or DEFAULT_BASE_URL
    calls = 0
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60.0, follow_redirects=False)
    try:
        for _ in range(max_tool_calls):
            content = await _chat(messages, api_key=api_key, model=model, base_url=base_url, client=client)
            action = _parse_action(content)
            if action.get("action") == "final":
                answer = str(action.get("answer") or "").strip()
                return answer[:ANSWER_CHARS] or "Saya belum menemukan datanya."
            name = action.get("name")
            args = action.get("args") if isinstance(action.get("args"), dict) else {}
            tool = tools.get(name)
            if not tool:
                messages.append({"role": "assistant", "content": content})
                messages.append({"role": "user", "content": f"Alat '{name}' tidak tersedia. "
                                                            "Gunakan alat yang ada atau jawab final."})
                continue
            calls += 1
            try:
                result = await _run_tool(tool, args)
                body = json.dumps(result, ensure_ascii=False, default=str)[:TOOL_RESULT_CHARS]
            except Exception as exc:
                body = json.dumps({"error": str(exc)[:200]}, ensure_ascii=False)
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content": f"Hasil alat {name}:\n{body}"})
        # Budget exhausted: one final forced answer from what was collected.
        messages.append({"role": "user", "content": "Batas jumlah alat tercapai. Jawab sekarang dengan data yang sudah ada."})
        content = await _chat(messages, api_key=api_key, model=model, base_url=base_url, client=client)
        action = _parse_action(content)
        answer = str(action.get("answer") or "").strip()
        return answer[:ANSWER_CHARS] or "Saya belum menemukan datanya."
    finally:
        if owns_client:
            await client.aclose()
