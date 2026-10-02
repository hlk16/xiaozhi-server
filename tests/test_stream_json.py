"""core/utils/stream_json.py 的单元测试。

项目没有测试框架，所以用可直接运行的脚本形式（python tests/test_stream_json.py）。
每个用例都是纯函数调用，不依赖网络或配置。

覆盖重点：
  - 分片边界（逐字符喂入必须和一次性喂入结果一致）
  - JSON 转义（\\" \\\\ \\n \\uXXXX，以及被切半的 \\u）
  - 动作校验（非法码 / 延迟钳制 / 数量截断 / 类型容错）
  - 降级路径（畸形 JSON、无前缀、纯文本）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.utils.stream_json import RobotReplyParser, VoiceStreamExtractor, parse_complete

CODES = frozenset({"WAVE", "CLAP", "THINK", "WELCOME", "POINT_LEFT", "POINT_RIGHT", "SALUTE"})

_passed = 0
_failed = 0


def check(name, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}\n         期望: {want!r}\n         实际: {got!r}")


def check_true(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  [OK]   {name}")
    else:
        _failed += 1
        print(f"  [FAIL] {name}  {detail}")


# ---------------------------------------------------------------- 流式提取

def test_extractor_basic():
    print("\n== VoiceStreamExtractor 基本提取 ==")
    e = VoiceStreamExtractor()
    out = e.feed('{"voice":"你好呀","actions":[]}')
    check("一次性喂入", out, "你好呀")
    check_true("状态转为 DONE", e.state == VoiceStreamExtractor.DONE)


def test_extractor_char_by_char():
    print("\n== 逐字符喂入（最严苛的切分）==")
    text = '{"voice":"哈喽！我是小智，今天想聊点啥呢？","actions":[]}'
    e = VoiceStreamExtractor()
    out = "".join(e.feed(c) for c in text)
    check("逐字符结果一致", out, "哈喽！我是小智，今天想聊点啥呢？")


def test_extractor_spaces():
    print("\n== 键名与值的空白容错 ==")
    for raw, want in [
        ('{"voice":"x","actions":[]}', "x"),
        ('{"voice": "x","actions":[]}', "x"),
        ('{ "voice" : "x" ,"actions":[]}', "x"),
        ('{\n  "voice":\n  "x",\n  "actions":[]\n}', "x"),
    ]:
        e = VoiceStreamExtractor()
        check(f"容忍写法 {raw[:24]}...", e.feed(raw), want)


def test_extractor_escapes():
    print("\n== JSON 转义 ==")
    cases = [
        (r'{"voice":"他说\"你好\"","actions":[]}', '他说"你好"'),
        (r'{"voice":"反斜杠\\测试","actions":[]}', "反斜杠\\测试"),
        (r'{"voice":"第一行\n第二行","actions":[]}', "第一行\n第二行"),
        (r'{"voice":"制表\t符","actions":[]}', "制表\t符"),
        (r'{"voice":"你好","actions":[]}', "你好"),
    ]
    for raw, want in cases:
        e = VoiceStreamExtractor()
        check(f"转义 {raw[11:26]}", e.feed(raw), want)


def test_extractor_split_escape():
    print("\n== 转义序列被分片切断 ==")
    raw = r'{"voice":"你好你世界","actions":[]}'
    # 逐字符喂入，\u 和它的 4 位十六进制会被拆到不同分片
    e = VoiceStreamExtractor()
    out = "".join(e.feed(c) for c in raw)
    check("\\uXXXX 逐字符喂入", out, "你好你世界")

    # 反斜杠单独成片
    e2 = VoiceStreamExtractor()
    parts = ['{"voice":"a', "\\", 'nb",', '"actions":[]}']
    out2 = "".join(e2.feed(p) for p in parts)
    check("反斜杠单独成片", out2, "a\nb")


def test_extractor_prefix_split():
    print("\n== 前缀被分片切断 ==")
    e = VoiceStreamExtractor()
    parts = ['{"vo', 'ice"', ': ', '"内容', '在此"', ',"actions":[]}']
    check("前缀跨分片", "".join(e.feed(p) for p in parts), "内容在此")


def test_extractor_no_prefix():
    print("\n== 找不到 voice 前缀 ==")
    e = VoiceStreamExtractor()
    out = e.feed("这是一段完全不符合格式的文本" * 40)  # 超过 SEEK_LIMIT
    check("无前缀时返回空", out, "")
    check_true("标记为 FAILED", e.state == VoiceStreamExtractor.FAILED)


# ---------------------------------------------------------------- 完整解析器

def _run(raw, **kw):
    p = RobotReplyParser(CODES, **kw)
    p.feed(raw)
    return p.finish(), p


def test_parser_normal():
    print("\n== RobotReplyParser 正常路径 ==")
    raw = '{"voice":"你好呀","actions":[{"code":"WAVE","delay_ms":0}]}'
    (voice, actions), p = _run(raw)
    check("voice", voice, "你好呀")
    check("actions", actions, [{"code": "WAVE", "delay_ms": 0}])
    check_true("ok=True", p.ok)


def test_parser_empty_actions():
    print("\n== 无动作场景 ==")
    (voice, actions), p = _run('{"voice":"今天天气不错","actions":[]}')
    check("voice", voice, "今天天气不错")
    check("actions 为空数组", actions, [])
    check_true("ok=True", p.ok)


def test_parser_multi_actions():
    print("\n== 多动作 ==")
    raw = ('{"voice":"好耶","actions":['
           '{"code":"CLAP","delay_ms":0},'
           '{"code":"SALUTE","delay_ms":1000},'
           '{"code":"POINT_LEFT","delay_ms":1200}]}')
    (voice, actions), p = _run(raw)
    check("动作数", len(actions), 3)
    check("顺序保持", [a["code"] for a in actions], ["CLAP", "SALUTE", "POINT_LEFT"])
    check("延迟保持", [a["delay_ms"] for a in actions], [0, 1000, 1200])


def test_parser_validation():
    print("\n== 动作校验与容错 ==")
    cases = [
        # (说明, 原始 JSON, 期望 actions, 期望 stats 检查)
        ("非法码被跳过",
         '{"voice":"x","actions":[{"code":"FLY","delay_ms":0}]}', []),
        ("非法码不拖累合法码",
         '{"voice":"x","actions":[{"code":"WAVE","delay_ms":0},{"code":"FLY","delay_ms":0}]}',
         [{"code": "WAVE", "delay_ms": 0}]),
        ("延迟超上限被钳制",
         '{"voice":"x","actions":[{"code":"WAVE","delay_ms":99999}]}',
         [{"code": "WAVE", "delay_ms": 5000}]),
        ("负延迟归零",
         '{"voice":"x","actions":[{"code":"WAVE","delay_ms":-500}]}',
         [{"code": "WAVE", "delay_ms": 0}]),
        ("延迟类型错归零",
         '{"voice":"x","actions":[{"code":"WAVE","delay_ms":"abc"}]}',
         [{"code": "WAVE", "delay_ms": 0}]),
        ("缺 delay_ms 补 0",
         '{"voice":"x","actions":[{"code":"WAVE"}]}',
         [{"code": "WAVE", "delay_ms": 0}]),
        ("动作是裸字符串",
         '{"voice":"x","actions":["WAVE"]}',
         [{"code": "WAVE", "delay_ms": 0}]),
        ("actions 是单个对象",
         '{"voice":"x","actions":{"code":"WAVE","delay_ms":0}}',
         [{"code": "WAVE", "delay_ms": 0}]),
        ("actions 是 null",
         '{"voice":"x","actions":null}', []),
        ("actions 缺失", '{"voice":"x"}', []),
        ("条目类型错被跳过",
         '{"voice":"x","actions":[123,{"code":"WAVE","delay_ms":0}]}',
         [{"code": "WAVE", "delay_ms": 0}]),
    ]
    for name, raw, want in cases:
        (_, actions), _ = _run(raw)
        check(name, actions, want)


def test_parser_truncate():
    print("\n== 动作数量截断 ==")
    raw = ('{"voice":"x","actions":['
           '{"code":"WAVE","delay_ms":0},{"code":"CLAP","delay_ms":0},'
           '{"code":"THINK","delay_ms":0},{"code":"SALUTE","delay_ms":0}]}')
    (_, actions), p = _run(raw)
    check("截断到 3 个", len(actions), 3)
    check("统计截断数", p.stats.truncated, 1)


def test_parser_malformed():
    print("\n== 畸形 JSON 的降级 ==")
    # voice 完整但 actions 畸形 → 应保留 voice
    raw = '{"voice":"我还能说话","actions":[{"code":"WAVE"'
    (voice, actions), p = _run(raw)
    check("保留已提取的 voice", voice, "我还能说话")
    check("动作为空", actions, [])
    check_true("ok=False", not p.ok)

    # 完全不是 JSON
    (voice2, actions2), p2 = _run("就是一句普通的话")
    check("纯文本 voice 为空", voice2, "")
    check("纯文本 actions 为空", actions2, [])
    check_true("ok=False", not p2.ok)


def test_parser_stream_equals_oneshot():
    print("\n== 流式与一次性结果必须一致 ==")
    raw = ('{"voice":"哈喽！我是小智，今天想聊点啥呢？",'
           '"actions":[{"code":"WAVE","delay_ms":0},{"code":"THINK","delay_ms":800}]}')

    # 一次性
    (v1, a1), p1 = _run(raw)
    # 逐字符
    p2 = RobotReplyParser(CODES)
    streamed = "".join(p2.feed(c) for c in raw)
    v2, a2 = p2.finish()

    check("voice 一致", v2, v1)
    check("actions 一致", a2, a1)
    check("流式吐出的文本 = voice", streamed, v1)

    # 随机二分片
    p3 = RobotReplyParser(CODES)
    i, parts = 0, []
    step = 3
    while i < len(raw):
        parts.append(raw[i:i + step])
        i += step
    got3 = "".join(p3.feed(x) for x in parts)
    v3, a3 = p3.finish()
    check("分片喂入 voice 一致", v3, v1)
    check("分片喂入流式输出一致", got3, v1)


def test_stream_progressive():
    print("\n== 流式提取的渐进性 ==")
    p = RobotReplyParser(CODES)
    # 模拟逐 token 到达。
    # 注意：第 1 片 + 第 2 片拼起来是 `{"voice":"哈喽！我`，前缀已完整，
    # 所以第 2 片就应该开始吐内容——JSON 里越过值的开引号后，
    # 直到未转义 `"` 之前都是安全的字符串内容。
    tokens = ['{"voice"', ':"哈喽！我', '是小智，今天', '想聊点啥呢？"', ',"actions":[]}']
    emitted = [p.feed(t) for t in tokens]

    check("第 1 片：前缀不完整，无输出", emitted[0], "")
    check("第 2 片：前缀补齐，吐出值内容", emitted[1], "哈喽！我")
    check("第 3 片：继续吐", emitted[2], "是小智，今天")
    check("第 4 片：遇到结束引号，吐出剩余", emitted[3], "想聊点啥呢？")
    check("第 5 片：voice 已完成，不再吐", emitted[4], "")

    check("拼接结果 = 完整 voice",
          "".join(emitted), "哈喽！我是小智，今天想聊点啥呢？")
    check_true("总字符数 = 16", sum(len(x) for x in emitted) == 16)

    # 关键指标：第 2 片就吐了 4 个字，此时 actions 还完全没到
    check_true("第 2 片即已下发音声（早于 actions 4 片）", len(emitted[1]) > 0)


def test_parse_complete():
    print("\n== parse_complete 便捷接口 ==")
    voice, actions, p = parse_complete(
        '{"voice":"你好","actions":[{"code":"WAVE","delay_ms":0}]}', CODES)
    check("voice", voice, "你好")
    check("actions", actions, [{"code": "WAVE", "delay_ms": 0}])
    check_true("ok", p.ok)


def test_max_actions_param():
    print("\n== max_actions 可配置 ==")
    raw = ('{"voice":"x","actions":['
           '{"code":"WAVE","delay_ms":0},{"code":"CLAP","delay_ms":0},'
           '{"code":"THINK","delay_ms":0}]}')
    (_, actions), _ = _run(raw, max_actions=1)
    check("限制为 1", len(actions), 1)


# ---------------------------------------------------------------- 主入口

def main():
    for fn in [
        test_extractor_basic,
        test_extractor_char_by_char,
        test_extractor_spaces,
        test_extractor_escapes,
        test_extractor_split_escape,
        test_extractor_prefix_split,
        test_extractor_no_prefix,
        test_parser_normal,
        test_parser_empty_actions,
        test_parser_multi_actions,
        test_parser_validation,
        test_parser_truncate,
        test_parser_malformed,
        test_parser_stream_equals_oneshot,
        test_stream_progressive,
        test_parse_complete,
        test_max_actions_param,
    ]:
        fn()

    print("\n" + "=" * 60)
    print(f"通过 {_passed} / 失败 {_failed}")
    print("=" * 60)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
