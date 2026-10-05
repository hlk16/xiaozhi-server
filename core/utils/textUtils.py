import re
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

logger = logging.getLogger(__name__)

# ====================================================================
# 机器人动作码 —— 结构化输出方案（DESIGN_STRUCTURED_OUTPUT.md §4.1.4）
#
# ACTION_DESCRIPTIONS 是**唯一数据源**：schema、prompt 说明、校验白名单
# 全部由它派生。新增一个动作只改这里，不需要改第二处。
# ====================================================================

ACTION_DESCRIPTIONS = {
    "WAVE": "挥手打招呼",
    "CLAP": "拍手鼓掌",
    "THINK": "思考动作（手托下巴）",
    "WELCOME": "欢迎手势（双臂张开）",
    "POINT_LEFT": "指向左",
    "POINT_RIGHT": "指向右",
    "SALUTE": "敬礼",
}

# 合法动作码（不含 NOACTION —— 无动作统一用空数组表示）
ROBOT_ACTION_CODES = frozenset(ACTION_DESCRIPTIONS)

# 单次回复的动作数量上限（固定值，不做成配置）
MAX_ACTIONS = 3

# 单个动作延迟上限（毫秒），防止模型给出离谱数值
MAX_DELAY_MS = 5000

# ---- 遗留：旧的方括号方案白名单，含 NOACTION ----
# 结构化输出版本稳定前保留，供 parse_robot_response 与 connection.py 使用。
# 待旧路径下线后一并删除（DESIGN_STRUCTURED_OUTPUT.md 决策 D6）。
ACTION_CODES = frozenset(
    {
        "NOACTION",
        "WAVE",
        "CLAP",
        "THINK",
        "WELCOME",
        "POINT_LEFT",
        "POINT_RIGHT",
        "SALUTE",
    }
)

VOICE_REPLY_PATTERN = re.compile(r"\[语音回复:(.*?)\]")
ACTION_PATTERN = re.compile(r"\[动作指令:(.*?)\]")


def build_reply_schema() -> dict:
    """构建 LLM 输出契约的 JSON Schema。

    同时服务两种模式（DESIGN_STRUCTURED_OUTPUT.md §4.5）：
      - json_schema 模式：整个 dict 作为 response_format 参数传给 API
      - json_object  模式：由 render_action_spec() 渲染成人话写进 prompt

    因为两个模式共用同一份定义，将来 provider 支持 strict 模式时
    只需改配置，不需要动代码。
    """
    return {
        "type": "object",
        "properties": {
            "voice": {
                "type": "string",
                "description": "要朗读的纯口语文本。不要包含任何标记或格式符号。",
            },
            "actions": {
                "type": "array",
                "maxItems": MAX_ACTIONS,
                "description": "按执行顺序排列的肢体动作，没有合适的动作时空数组 []",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": sorted(ROBOT_ACTION_CODES),
                            "description": "肢体动作代码",
                        },
                        "delay_ms": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": MAX_DELAY_MS,
                            "description": "相对上一条动作的延迟毫秒数；第一条通常填 0",
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


def render_action_spec() -> str:
    """把动作清单渲染成给 LLM 看的人话，供 prompt 模板使用。

    对应 agent-base-prompt.txt 里的 {{action_spec}} 占位符。
    """
    return "\n".join(f"- {code}: {desc}" for code, desc in ACTION_DESCRIPTIONS.items())


def parse_robot_response(text: str) -> tuple:
    """
    解析机器人回复格式，提取语音文本和动作指令

    Args:
        text: LLM原始输出文本

    Returns:
        tuple: (voice_text, action_code)
            - voice_text: 提取的语音文本，若无格式则返回原文本
            - action_code: 动作代码，默认"NULL"

    Examples:
        >>> parse_robot_response("[语音回复:你好呀][动作指令:WAVE]")
        ("你好呀", "WAVE")
        >>> parse_robot_response("普通文本回复")
        ("普通文本回复", "NULL")
    """
    voice_match = VOICE_REPLY_PATTERN.search(text)
    action_match = ACTION_PATTERN.search(text)

    logger.info(f"[RobotParser] 原始文本: {repr(text)}")
    logger.info(f"[RobotParser] voice_match: {voice_match.group(0) if voice_match else None}")
    logger.info(f"[RobotParser] action_match: {action_match.group(0) if action_match else None}")

    # 如果找到语音回复标记，提取内容
    if voice_match:
        voice_text = voice_match.group(1).strip()
    else:
        # 容错：如果没有语音回复标记，但找到动作指令标记
        # 尝试提取动作指令后的内容作为语音文本
        if action_match:
            action_end = action_match.end()
            remaining = text[action_end:].strip()
            # 如果动作指令后面还有内容，作为语音
            if remaining:
                voice_text = remaining
            else:
                # 动作指令前面可能有语音内容
                action_start = action_match.start()
                voice_text = text[:action_start].strip()
                # 清理可能残留的 [ 或 ]
                voice_text = voice_text.strip("[] ")
        else:
            voice_text = text

    action_code = action_match.group(1).strip() if action_match else "NOACTION"

    if action_code not in ACTION_CODES:
        action_code = "NOACTION"

    logger.info(f"[RobotParser] 解析结果: voice_text={repr(voice_text)}, action_code={action_code}")

    return voice_text, action_code

TAG = __name__
EMOJI_MAP = {
    "😂": "funny",
    "😭": "crying",
    "😠": "angry",
    "😔": "sad",
    "😍": "loving",
    "😲": "surprised",
    "😱": "shocked",
    "🤔": "thinking",
    "😌": "relaxed",
    "😴": "sleepy",
    "😜": "silly",
    "🙄": "confused",
    "😶": "neutral",
    "🙂": "happy",
    "😆": "laughing",
    "😳": "embarrassed",
    "😉": "winking",
    "😎": "cool",
    "🤤": "delicious",
    "😘": "kissy",
    "😏": "confident",
}

# 模型没给出白名单内的 emoji 时的兜底值
DEFAULT_EMOJI = "🙂"
DEFAULT_EMOTION = "happy"

EMOJI_RANGES = [
    (0x1F600, 0x1F64F),
    (0x1F300, 0x1F5FF),
    (0x1F680, 0x1F6FF),
    (0x1F900, 0x1F9FF),
    (0x1FA70, 0x1FAFF),
    (0x2600, 0x26FF),
    (0x2700, 0x27BF),
]


def get_string_no_punctuation_or_emoji(s):
    """去除字符串首尾的空格、标点符号和表情符号"""
    chars = list(s)
    # 处理开头的字符
    start = 0
    while start < len(chars) and is_punctuation_or_emoji(chars[start]):
        start += 1
    # 处理结尾的字符
    end = len(chars) - 1
    while end >= start and is_punctuation_or_emoji(chars[end]):
        end -= 1
    return "".join(chars[start : end + 1])


def is_punctuation_or_emoji(char):
    """检查字符是否为空格、指定标点或表情符号"""
    # 定义需要去除的中英文标点（包括全角/半角）
    punctuation_set = {
        "，",
        ",",  # 中文逗号 + 英文逗号
        "。",
        ".",  # 中文句号 + 英文句号
        "！",
        "!",  # 中文感叹号 + 英文感叹号
        "“",
        "”",
        '"',  # 中文双引号 + 英文引号
        "：",
        ":",  # 中文冒号 + 英文冒号
        "-",
        "－",  # 英文连字符 + 中文全角横线
        "、",  # 中文顿号
        "[",
        "]",  # 方括号
        "【",
        "】",  # 中文方括号
    }
    if char.isspace() or char in punctuation_set:
        return True
    return is_emoji(char)


async def get_emotion(conn: "ConnectionHandler", text):
    """从 LLM 回复文本中提取情绪，下发给客户端。

    注意这里取的是**模型自己**的情绪（它在回复开头插入的 emoji），
    不是用户的情绪——用户情绪由 ASR 声学模型识别，走另一条链路
    （见 core/providers/asr/base.py 的「识别情绪」日志）。

    本函数以前成功时不打任何日志，导致整条链路不可观测、出问题无法排查。
    现在无论成功失败都会记录：命中的 emoji、映射出的情绪、实际下发的报文。
    """
    emoji = DEFAULT_EMOJI
    emotion = DEFAULT_EMOTION
    matched = False

    for char in text:
        if char in EMOJI_MAP:
            emoji = char
            emotion = EMOJI_MAP[char]
            matched = True
            break

    payload = {
        "type": "llm",
        "text": emoji,
        "emotion": emotion,
        "session_id": conn.session_id,
    }
    # 实际下发的报文。json.dumps 默认 ensure_ascii=True，emoji 会被转义成
    # 😆 这类代理对——两种写法等价，前端解析结果相同。
    message = json.dumps(payload)
    # 日志里用可读形式，否则 emoji 全是转义码没法看
    message_readable = json.dumps(payload, ensure_ascii=False)

    # 没命中白名单说明模型这次没按 prompt 插 emoji（或插了表外的），
    # 前端拿到的永远是默认笑脸——这条日志用于发现这种情况。
    if not matched:
        conn.logger.bind(tag=TAG).warning(
            f"[EMOTION] 未命中白名单 emoji，使用默认值 "
            f"| emoji={emoji} emotion={emotion} | 回复片段={text[:30]!r}"
        )

    try:
        await conn.websocket.send(message)
        conn.logger.bind(tag=TAG).info(
            f"[EMOTION] 下发成功 "
            f"| matched={matched} emoji={emoji} emotion={emotion} "
            f"| bytes={len(message)} | 报文={message_readable}"
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).error(
            f"[EMOTION] 下发失败 "
            f"| emoji={emoji} emotion={emotion} "
            f"| 错误={type(e).__name__}: {e} | 报文={message_readable}"
        )
    return


def is_emoji(char):
    """检查字符是否为emoji表情"""
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in EMOJI_RANGES)


def check_emoji(text):
    """去除文本中的所有emoji表情"""
    return "".join(char for char in text if not is_emoji(char) and char != "\n")
