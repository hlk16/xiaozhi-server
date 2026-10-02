"""验证 LLM provider 是否支持结构化输出，并测量对首 token 延迟的影响。

对应 DESIGN_STRUCTURED_OUTPUT.md §10.4 的前置验证。
上线前必须先跑通本脚本，确认：
  1. provider 接受 response_format 参数（不报 400）
  2. 输出是合法 JSON，且包含 voice / actions 字段
  3. 开启 JSON 模式后首 token 延迟劣化 < 200ms

用法：
    python performance_tester/verify_structured_output.py
    python performance_tester/verify_structured_output.py --provider DeepSeekLLM --runs 5
    python performance_tester/verify_structured_output.py --only json_object

注意：脚本会真实调用 LLM API，产生少量费用。
"""

import sys
import os
import json
import time
import argparse
import statistics
from typing import Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openai
from tabulate import tabulate
from config.settings import load_config

# 动作码从项目代码读取，保证与实现同步（DESIGN §4.1.1 的单一数据源）
try:
    from core.utils.textUtils import ACTION_CODES
except Exception:  # 容错：textUtils 尚未改造时用当前白名单
    ACTION_CODES = frozenset({
        "NOACTION", "WAVE", "CLAP", "THINK",
        "WELCOME", "POINT_LEFT", "POINT_RIGHT", "SALUTE",
    })

MAX_ACTIONS = 3
MAX_DELAY_MS = 5000

# 目标契约（DESIGN §4.1）
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "voice": {"type": "string", "description": "要朗读的纯口语文本"},
        "actions": {
            "type": "array",
            "maxItems": MAX_ACTIONS,
            "description": "按执行顺序排列的肢体动作，没有合适的动作时空数组",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "enum": sorted(ACTION_CODES)},
                    "delay_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_DELAY_MS,
                    },
                },
                "required": ["code", "delay_ms"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["voice", "actions"],
    "additionalProperties": False,
}

# 测试用系统提示词。
# 注意：部分 provider（含 DeepSeek）的 JSON 模式要求 prompt 里出现 "json" 字样。
SYSTEM_PROMPT = f"""你是小智，一台人形家用服务机器人。讲话自然、简短、口语化。

【机器人输出格式】
你的每次回复必须是一个 json 对象，包含两个字段：
- voice: 要朗读的纯口语文本
- actions: 按执行顺序排列的肢体动作数组，每个动作为 {{"code": "动作码", "delay_ms": 延迟毫秒}}
  没有合适的动作时用空数组 []

可选动作码：{", ".join(sorted(ACTION_CODES))}

示例：
{{"voice": "你好呀，我是小智", "actions": [{{"code": "WAVE", "delay_ms": 0}}]}}
{{"voice": "让我想想", "actions": [{{"code": "THINK", "delay_ms": 0}}]}}
{{"voice": "今天天气不错", "actions": []}}
"""

TEST_QUESTIONS = [
    "你好呀",
    "帮我算一下 12 乘 8 等于多少",
    "给我鼓个掌",
]
# 不使用工具调用，避免 function calling 干扰 response_format 的测试
FUNCTIONS = None


def build_client(provider_config: dict) -> Tuple[openai.OpenAI, str]:
    """按项目配置构造 openai 客户端"""
    base_url = provider_config.get("base_url") or provider_config.get("url")
    api_key = provider_config.get("api_key")
    model = provider_config.get("model_name")
    if not api_key or not base_url or not model:
        raise ValueError(
            f"provider 配置不完整：需要 api_key / base_url(或 url) / model_name，"
            f"当前为 {list(provider_config.keys())}"
        )
    return openai.OpenAI(api_key=api_key, base_url=base_url, timeout=60), model


def run_once(
    client: openai.OpenAI,
    model: str,
    question: str,
    mode: str,
) -> dict:
    """单次请求。返回 {ok, ttft, total, raw, error}"""
    kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "stream": True,
        "temperature": 0.7,
        "max_tokens": 200,
    }

    if mode == "json_object":
        kwargs["response_format"] = {"type": "json_object"}
    elif mode == "json_schema":
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "robot_reply", "strict": True, "schema": REPLY_SCHEMA},
        }

    start = time.monotonic()
    ttft = None
    chunks = []
    try:
        stream = client.chat.completions.create(**kwargs)
        for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                if ttft is None:
                    ttft = time.monotonic() - start
                chunks.append(content)
        total = time.monotonic() - start
        stream.close()
        return {
            "ok": True,
            "ttft": ttft,
            "total": total,
            "raw": "".join(chunks),
            "error": None,
        }
    except Exception as e:
        return {
            "ok": False,
            "ttft": None,
            "total": time.monotonic() - start,
            "raw": "",
            "error": f"{type(e).__name__}: {str(e)[:200]}",
        }


def validate(raw: str) -> Tuple[bool, str]:
    """校验输出是否符合契约。返回 (是否通过, 说明)"""
    if not raw:
        return False, "输出为空"

    # JSON 模式可能被 markdown 代码块包裹，先剥离
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        obj = json.loads(text)
    except json.JSONDecodeError as e:
        return False, f"不是合法 JSON: {e}"

    if not isinstance(obj, dict):
        return False, f"顶层不是对象: {type(obj).__name__}"

    if not isinstance(obj.get("voice"), str) or not obj["voice"].strip():
        return False, "voice 缺失或为空"

    actions = obj.get("actions")
    if not isinstance(actions, list):
        return False, f"actions 不是数组: {type(actions).__name__}"

    if len(actions) > MAX_ACTIONS:
        return False, f"动作数 {len(actions)} 超过上限 {MAX_ACTIONS}"

    for i, act in enumerate(actions):
        if not isinstance(act, dict):
            return False, f"actions[{i}] 不是对象"
        if act.get("code") not in ACTION_CODES:
            return False, f"actions[{i}].code 非法: {act.get('code')!r}"
        delay = act.get("delay_ms")
        if not isinstance(delay, int) or not (0 <= delay <= MAX_DELAY_MS):
            return False, f"actions[{i}].delay_ms 非法: {delay!r}"

    return True, f"voice={obj['voice'][:20]!r} actions={[a['code'] for a in actions]}"


def probe_modes(client, model, runs: int, only: Optional[str]) -> dict:
    """逐个模式跑 N 轮，返回统计结果"""
    modes = ["baseline", "json_object", "json_schema"]
    if only:
        modes = [only]

    results = {}
    for mode in modes:
        print(f"\n>>> 测试模式: {mode}")
        ttfts, ok_cnt, valid_cnt, samples = [], 0, 0, []

        for i, q in enumerate((TEST_QUESTIONS * runs)[:runs]):
            r = run_once(client, model, q, mode)
            if not r["ok"]:
                print(f"  [{i+1}/{runs}] ❌ 请求失败: {r['error']}")
                samples.append(("请求失败", r["error"]))
                continue

            ok_cnt += 1
            if r["ttft"]:
                ttfts.append(r["ttft"])
            passed, msg = validate(r["raw"])
            if passed:
                valid_cnt += 1
            samples.append((("✅" if passed else "⚠️"), msg))
            print(f"  [{i+1}/{runs}] {'✅' if passed else '⚠️'} "
                  f"ttft={r['ttft']*1000:.0f}ms total={r['total']*1000:.0f}ms  {msg}")

        results[mode] = {
            "requests_ok": ok_cnt,
            "schema_valid": valid_cnt,
            "runs": runs,
            "ttft_p50": statistics.median(ttfts) if ttfts else None,
            "ttft_min": min(ttfts) if ttfts else None,
            "samples": samples,
        }

    return results


def report(results: dict):
    """输出对比表 + 结论"""
    print("\n" + "=" * 78)
    print("汇总")
    print("=" * 78)

    rows = []
    for mode, r in results.items():
        ttft = f"{r['ttft_p50']*1000:.0f} ms" if r["ttft_p50"] else "—"
        rows.append([
            mode,
            f"{r['requests_ok']}/{r['runs']}",
            f"{r['schema_valid']}/{r['runs']}",
            ttft,
        ])
    print(tabulate(rows, headers=["模式", "请求成功", "符合契约", "首token中位"], tablefmt="github"))

    # 延迟劣化对比
    base = results.get("baseline", {})
    if base.get("ttft_p50"):
        print("\n首 token 延迟劣化（相对 baseline）：")
        for mode in ("json_object", "json_schema"):
            r = results.get(mode)
            if not r or not r.get("ttft_p50"):
                continue
            delta = (r["ttft_p50"] - base["ttft_p50"]) * 1000
            flag = "✅ 可接受" if delta < 200 else "⚠️ 超过 200ms 阈值"
            print(f"  {mode:<14} {delta:+.0f} ms   {flag}")

    # 结论
    print("\n" + "-" * 78)
    print("结论")
    print("-" * 78)

    jo = results.get("json_object", {})
    js = results.get("json_schema", {})

    if jo.get("requests_ok", 0) > 0 and jo.get("schema_valid", 0) == jo.get("requests_ok"):
        print("  ✅ json_object 可用，且输出完全符合契约 → 按默认模式实施")
    elif jo.get("requests_ok", 0) > 0:
        print(f"  ⚠️ json_object 请求成功但契约符合率 "
              f"{jo['schema_valid']}/{jo['requests_ok']}，需要加强 prompt 约束")
    else:
        print("  ❌ json_object 不可用 → 检查 provider 文档或改用其他模式")

    if js.get("requests_ok", 0) > 0 and js.get("schema_valid", 0) == js.get("requests_ok"):
        print("  ✅ json_schema 也可用 → 可选用更强保证（mode: json_schema）")
    else:
        print("  ℹ️ json_schema 不可用或不完全符合，使用 json_object 即可")

    print("\n  提醒：本脚本只验证了 JSON 模式本身，未验证【流式增量解析 voice 字段】。")
    print("        那部分需要写完 stream_json.py 后单独测。")


def main():
    parser = argparse.ArgumentParser(description="验证 LLM provider 的结构化输出支持")
    parser.add_argument("--provider", default="DeepSeekLLM", help="config.yaml 中 LLM 段的配置名")
    parser.add_argument("--runs", type=int, default=3, help="每个模式跑几轮")
    parser.add_argument("--only", choices=["baseline", "json_object", "json_schema"],
                        help="只测某一个模式")
    args = parser.parse_args()

    config = load_config()
    llm_configs = config.get("LLM", {})
    if args.provider not in llm_configs:
        print(f"❌ config 里找不到 provider: {args.provider}")
        print(f"   可选：{', '.join(llm_configs.keys())}")
        sys.exit(1)

    provider_config = llm_configs[args.provider]
    client, model = build_client(provider_config)
    base_url = provider_config.get("base_url") or provider_config.get("url")

    print("=" * 78)
    print(f"provider : {args.provider}")
    print(f"model    : {model}")
    print(f"base_url : {base_url}")
    print(f"每模式轮数: {args.runs}")
    print("=" * 78)

    results = probe_modes(client, model, args.runs, args.only)
    report(results)


if __name__ == "__main__":
    main()
