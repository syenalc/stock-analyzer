"""
エージェント型チャット
Claude API を tool_use モードで呼び、必要に応じて agent.tools のツールを実行する
"""
import os
import anthropic

from agent.tools import TOOL_DEFINITIONS, execute_tool
from utils.config import get_secret
from utils.logging_config import get_logger

logger = get_logger(__name__)

AVAILABLE_MODELS = {
    "Sonnet 4.6 (高速・低コスト)": "claude-sonnet-4-6",
    "Opus 4.7 (高精度・高コスト)": "claude-opus-4-7",
}
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOOL_TURNS = 8  # 暴走防止

_BASE_SYSTEM_PROMPT = """あなたは米国株の投資アシスタントです。
ユーザーの自然言語の質問に答えるため、以下のツールを必要に応じて呼び出してください。

利用方針:
- 質問に答える前に、必ず関連データを `query_tweets` や `get_price_history` で集めること
- X APIから新規取得（fetch_user_tweets）は**ユーザーが明示的に指示した時のみ**実行。お金がかかる
- 「今〜どう思う？」「分析して」と言われた場合は、まずSupabaseの保存済みデータ（query_tweets, get_price_history, get_manual_notes）を使う
- 株価リアルタイム取得（get_stock_price）も外部API呼び出しなので、必要な時だけ使う
- 引用するときは「@username が〜と言及」のように誰の発言か明示する
- 確証がない部分は「材料不足」と明確に伝える
- 推奨ではなく判断材料の提供に徹する
- 投資はユーザー自身の責任。最終判断はユーザーに委ねる
- マークダウンで整形（見出し・箇条書き・引用）

回答は日本語で。
"""


def _load_user_context() -> str:
    """CLAUDE.md (ローカル) または USER_CONTEXT secret (クラウド) からユーザーコンテキストを読む"""
    path = os.path.join(os.path.dirname(__file__), "..", "CLAUDE.md")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            logger.warning("CLAUDE.md読み込み失敗: %s", e)
    return (get_secret("USER_CONTEXT", "") or "").strip()


def _build_system_prompt() -> str:
    ctx = _load_user_context()
    if ctx:
        return _BASE_SYSTEM_PROMPT + f"\n\n---\n## ユーザーコンテキスト（最優先で参照）\n\n{ctx}"
    return _BASE_SYSTEM_PROMPT

_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
    return _client


def run_agent(user_message: str, history: list[dict] | None = None, model: str = DEFAULT_MODEL) -> tuple[str, list[dict], list[str]]:
    """エージェントを1ターン実行

    Returns:
        (assistant_text, new_history, tool_trace)
        - new_history: 次回のhistoryとして渡せる完全なmessages配列
        - tool_trace: 実行したツール名のログ
    """
    client = _get_client()
    messages = list(history or [])
    messages.append({"role": "user", "content": user_message})

    tool_trace = []
    final_text = ""
    system_prompt = _build_system_prompt()

    for turn in range(MAX_TOOL_TURNS):
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            system=system_prompt,
            tools=TOOL_DEFINITIONS,
            messages=messages,
        )
        logger.info(
            "agent turn=%d stop=%s in=%d out=%d",
            turn, resp.stop_reason, resp.usage.input_tokens, resp.usage.output_tokens,
        )

        # アシスタント応答をhistoryへ（SDKオブジェクトをJSON保存可能なdictに変換）
        assistant_content = []
        for block in resp.content:
            if block.type == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif block.type == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        messages.append({"role": "assistant", "content": assistant_content})

        # ツール呼び出しがなければ終了
        if resp.stop_reason != "tool_use":
            text_blocks = [b.text for b in resp.content if b.type == "text"]
            final_text = "\n".join(text_blocks).strip()
            break

        # ツール実行 → tool_result を返す
        tool_results = []
        for block in resp.content:
            if block.type == "tool_use":
                tool_trace.append(block.name)
                logger.info("tool call: %s(%s)", block.name, block.input)
                result_str = execute_tool(block.name, block.input)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })
        messages.append({"role": "user", "content": tool_results})
    else:
        final_text = "（ツール呼び出しが上限に達しました。質問を分割してください）"

    return final_text, messages, tool_trace
