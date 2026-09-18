"""Vocabulary for auditing per-line Emotion / Style instructs.

Ported from the buddies fork of Finrandojin/alexandria-audiobook
(app/instruct_utils.py, MIT, Xiao Zhang, 2026-09-16..18), whose rules rest on
one measurement made there: on the CustomVoice path an explicit rate
instruction lengthened takes by +17.9% on average, while a scene note with no
acoustic target moved nothing consistently. The lists mirror
VOICE_REFERENCE.md Sections I-III: Section I (timbre / register / identity)
belongs in the constant character style, not in a per-line instruct; II-III
(emotion, delivery, pacing) are what a per-line instruct is for; actions and
scene meaning belong nowhere. Kept verbatim so a finding here means the same
thing it meant in their measurement; this project has not yet reproduced it
(instruct_audit.py is the first step - what our pass 3 actually writes).
"""
import re

TIMBRE_TERMS = [
    # English (Section I headline terms)
    "timbre", "gravelly", "raspy", "husky", "scratchy", "smoky", "guttural", "coarse",
    "hoarse", "throaty", "gruff", "silky", "velvety", "honeyed", "creamy", "mellow",
    "buttery", "booming", "chesty", "sonorous", "rumbling", "hollow", "cavernous",
    "bassy", "resonant", "breathy", "airy", "feathery", "reedy", "tinny", "shrill",
    "nasal", "twangy", "whiny", "brittle", "metallic", "falsetto", "vocal fry",
    "sibilant", "tremulous", "register", "baritone", "tenor", "bass", "alto",
    "soprano", "mezzo", "pitch range", "high-pitched", "low-pitched", "vocal cords",
    # Chinese
    "音色", "音区", "音质", "嗓音", "声线", "共鸣", "共振", "磁性", "沙哑", "嘶哑",
    "浑厚", "厚重", "厚实", "低沉", "明亮", "清脆", "清亮", "圆润", "尖细", "细弱",
    "鼻音", "气声", "干涩", "粗糙", "苍老", "低音", "中音", "高音", "男中音", "男高音",
    "女中音", "男声", "女声", "少女", "儿童", "声带", "喉咙", "喉音",
    # identity words that belong in the anchor, not per line
    "年轻", "年迈", "中年", "十几岁", "少年", "老妇", "老者", "岁",
]

EMOTION_TERMS = [
    "angry", "sad", "happy", "afraid", "anxious", "nervous", "tense", "calm",
    "warm", "cold", "weary", "tired", "excited", "amused", "wry", "bitter",
    "resigned", "smug", "urgent", "gentle", "stern", "tender", "defeated",
    "grieving", "startled", "sarcastic", "desperate", "curious", "guarded",
    # the sanctioned neutral-narration vocabulary must not read as "no direction"
    "neutral", "even", "somber", "sombre", "quiet", "quietly", "composed",
    "steady", "matter-of-fact", "deadpan", "wryly", "light", "grave",
    "疲惫", "愤怒", "生气", "紧张", "平静", "冷淡", "犹豫", "惊讶", "恐惧", "悲伤",
    "喜悦", "无奈", "嘲讽", "轻蔑", "轻慢", "坚定", "恭敬", "急切", "克制", "冷静",
    "焦虑", "压抑", "警惕", "温柔", "严厉", "兴奋", "茫然", "绝望", "心虚", "坦然",
    "凝重", "沉重", "戏谑", "慵懒", "震惊", "困惑", "笃定", "诚恳", "威严", "深意",
    "中性", "客观", "平稳", "平和", "沉静", "从容", "凝重", "专注", "生硬",
    "炫耀", "掌控", "试探", "示弱", "尴尬", "如释重负", "悬疑", "锐利", "深澱",
    "坚定", "郑重", "轻快", "从容不迫", "好奇", "体谅", "亲昵", "怀疑",
]

DELIVERY_TERMS = [
    "pace", "slowly", "quickly", "rapidly", "measured", "halting", "staccato",
    "legato", "drawl", "monotone", "flat", "clipped", "whisper", "murmur", "mutter",
    "shout", "softly", "loudly", "emphasis", "pause", "breathless", "articulate",
    "narration", "narrating", "deadpan", "hushed", "brisk", "clipped", "staccato",
    "语速", "缓慢", "快速", "急促", "平稳", "停顿", "拖长", "断断续续", "喃喃",
    "低语", "耳语", "低声", "轻声", "小声", "喊", "吼", "大声", "高喊", "咬字",
    "重音", "放慢", "加快", "平铺直叙", "拖腔", "一字一顿", "叹息", "喘息", "颤抖",
    "语气", "口吻", "调子", "念", "叙述", "播报", "朗读", "平缓", "语调", "声调",
    "干脆", "强调", "咬字清晰", "含蓄", "简洁", "利落", "停顿感",
]

ACTION_TERMS = [
    "nod", "shake his head", "shake her head", "smile", "smiles", "laugh", "sigh",
    "glance", "stare", "frown", "shrug", "gesture", "turn away", "reaches",
    "笑了笑", "笑", "点头", "摇头", "转身", "拿起", "放下", "看着", "盯着", "皱眉",
    "叹气", "耸肩", "挥手", "站起来", "坐下", "走向", "停下", "动作", "表情", "眼神",
    "手势", "沉默",
]

SCENE_TERMS = [
    "scene", "describes", "description", "narrating the", "the room", "setting",
    "场景", "描述", "画面", "周围", "环境", "背景", "氛围", "体现场", "突显", "渲染",
    "体现", "强调事件", "事件", "情节", "弦外之音", "暗示",
]

MAX_CLAUSES = 3
WARN_WORDS_EN = 12
WARN_CHARS_EN = 90
WARN_CHARS_ZH = 24
_BRACKETS = "「」『』“”\"'‘’（）()【】〔〕[]《》〈〉"
_CLAUSE_SPLIT = re.compile(r"[,，;；。.!！?？、]")


def has_cjk(text):
    return bool(re.search(r"[㐀-䶿一-鿿]", text))


def hits(text, terms):
    low = text.lower()
    found = []
    for term in terms:
        if term.isascii():
            if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", low):
                found.append(term)
        elif term in text:
            found.append(term)
    return found


def count_clauses(text):
    return len([p for p in _CLAUSE_SPLIT.split(text or "") if p.strip()])


def audit_instruct(text, speaker=""):
    """-> [{"code", "detail"}], empty when the instruct conforms. The codes:
    timbre / action / scene (vocabulary that does not belong), no_delivery
    (nothing the voice can act on), over_specified (> MAX_CLAUSES clauses),
    long, punctuation, speaker_name, empty."""
    instruction = (text or "").strip()
    findings = []
    if not instruction:
        return [{"code": "empty", "detail": "no direction at all"}]
    zh = has_cjk(instruction)
    for code, terms, why in (
            ("timbre", TIMBRE_TERMS, "acoustic identity - belongs in Character Style"),
            ("action", ACTION_TERMS, "an action or gesture, not a voice direction"),
            ("scene", SCENE_TERMS, "scene/meta wording the voice cannot act on")):
        found = hits(instruction, terms)
        if found:
            findings.append({"code": code, "detail": f"{', '.join(found[:4])} ({why})"})
    if not (hits(instruction, EMOTION_TERMS) or hits(instruction, DELIVERY_TERMS)):
        findings.append({"code": "no_delivery",
                         "detail": "no emotion/delivery/pacing word - nothing for the voice to act on"})
    clauses = count_clauses(instruction)
    if clauses > MAX_CLAUSES:
        findings.append({"code": "over_specified", "detail": f"{clauses} clauses (max {MAX_CLAUSES})"})
    words = len(instruction.split())
    if zh and len(instruction) > WARN_CHARS_ZH:
        findings.append({"code": "long", "detail": f"{len(instruction)} chars (aim <= {WARN_CHARS_ZH})"})
    elif not zh and (words > WARN_WORDS_EN or len(instruction) > WARN_CHARS_EN):
        findings.append({"code": "long", "detail": f"{words} words / {len(instruction)} chars"})
    if any(ch in instruction for ch in _BRACKETS):
        findings.append({"code": "punctuation", "detail": "quotes/brackets (stage-direction style)"})
    if re.search(r"[!！?？~～…]{2,}", instruction) or "..." in instruction:
        findings.append({"code": "punctuation", "detail": "repeated/ellipsis punctuation"})
    if speaker and speaker.strip() and speaker.strip() in instruction:
        findings.append({"code": "speaker_name", "detail": f"mentions '{speaker.strip()}'"})
    return findings
