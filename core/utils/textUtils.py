import re
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

logger = logging.getLogger(__name__)

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
    """获取文本内的情绪消息"""
    emoji = "🙂"
    emotion = "happy"
    for char in text:
        if char in EMOJI_MAP:
            emoji = char
            emotion = EMOJI_MAP[char]
            break
    try:
        await conn.websocket.send(
            json.dumps(
                {
                    "type": "llm",
                    "text": emoji,
                    "emotion": emotion,
                    "session_id": conn.session_id,
                }
            )
        )
    except Exception as e:
        conn.logger.bind(tag=TAG).warning(f"发送情绪表情失败，错误:{e}")
    return


def is_emoji(char):
    """检查字符是否为emoji表情"""
    code_point = ord(char)
    return any(start <= code_point <= end for start, end in EMOJI_RANGES)


def check_emoji(text):
    """去除文本中的所有emoji表情"""
    return "".join(char for char in text if not is_emoji(char) and char != "\n")
