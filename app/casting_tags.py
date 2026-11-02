"""Gender, age, tone and texture tags read from a voice description (#1532).

Ported, with permission, from REFLEXGAMING007's Alexandria Casting Studio concept
(deriveGender, deriveAges, TONE_VOCAB, TEXTURE_VOCAB). The rules are kept as-is, so
the casting library's counts match the concept's on the same voices, apart from
the one fix marked in _AGE_RULES. They are
keyword rules over the persona description, not a model: a tag means the word is
there, nothing more.
"""
import re

GENDER_ORDER = ('Male', 'Female', 'Ambiguous / mixed', 'Not specified')
AGE_ORDER = ('Child', 'Teen', 'Young Adult', 'Adult', 'Older / Elderly', 'Ancient / Ageless', 'Not specified')

_FEMALE = re.compile(r'\bfemale\b|\bwoman\b|\bwomen\b|\bgirl\b|\bmaiden\b|\bshe\b|\bher\b|\bherself\b|\bwives\b'
                     r'|\bwife\b|\bfeminine\b|\bmother\b|\bmatriarch\b|\bqueen\b|\bprincess\b|\bsister\b|\bdaughter\b')
_MALE = re.compile(r'\bmale\b|\bman\b|\bmen\b|\bboy\b|\bhe\b|\bhim\b|\bhis\b|\bhimself\b|\bgentleman\b|\bmasculine\b'
                   r'|\bfather\b|\bking\b|\bprince\b|\bbrother\b|\bson\b')
_AMBIGUOUS = re.compile(r'\bandrogynous\b|\bgenderless\b|\bnon[- ]?binary\b|\bgender[- ]?neutral\b'
                        r'|\bneither male nor female\b|\bsexless\b|\bambiguous (?:gender|voice|timbre)\b')
_AGE_RULES = (
    ('Ancient / Ageless', re.compile(r'\b(ancient|ageless|timeless|primordial|eternal|immortal|godlike|deity|divine'
                                     r'|cosmic|centuries old|millennia)\b')),
    # One deliberate change from the concept: "middle-aged" normalises to "middle aged", whose "aged"
    # tagged every middle-aged voice as elderly too. "aged" after "middle " no longer counts.
    ('Older / Elderly', re.compile(r'\b(elderly|old man|old woman|old male|old female|older man|older woman|older male'
                                   r'|older female|(?<!middle )aged|senior|grandmother|grandfather|grandma|grandpa|crone'
                                   r'|gray haired|grey haired|grey-haired|gray-haired'
                                   r'|in his (sixties|seventies|eighties|90s)|in her (sixties|seventies|eighties|90s))\b')),
    ('Child', re.compile(r'\b(child|childlike|children|kid|little boy|little girl|young boy|young girl|baby|infant'
                         r'|toddler)\b')),
    ('Teen', re.compile(r'\b(teen|teenager|teenaged|adolescent|in (his|her) teens)\b')),
    ('Young Adult', re.compile(r'\b(young adult|young man|young woman|young male|young female|youthful|early twenties'
                               r'|mid twenties|late twenties|in (his|her) twenties|20s|20 something|twenties)\b')),
    ('Adult', re.compile(r'\b(adult|middle aged|middle-aged|mature|grown|in (his|her) thirties|in (his|her) forties'
                         r'|30s|40s|thirties|forties)\b')),
)
TONE_VOCAB = (
    "academic", "aggressive", "agitated", "alluring", "aloof", "amused", "angry", "announcing", "anxious",
    "aristocratic", "arrogant", "assertive", "authoritative", "bellowing", "bewildered", "blunt", "boastful",
    "breathless", "brisk", "businesslike", "calm", "casual", "chant", "chanting", "cheerful", "childish", "chilling",
    "chuckling", "coaxing", "cold", "commanding", "composed", "concerned", "condescending", "contemptuous",
    "conversational", "cooperative", "crying", "curious", "dazed", "deadpan", "decisive", "deferential", "deliberate",
    "derisive", "despair", "desperate", "detached", "dignified", "direct", "domineering", "dramatic", "dreamy",
    "drunken", "eerie", "emotionless", "enigmatic", "erratic", "explanatory", "fast-paced", "fawning", "fearful",
    "firm", "formal", "frank", "friendly", "frightened", "furious", "gentle", "gloating", "grief-stricken", "grieving",
    "growling", "harsh", "hesitant", "horrified", "humorous", "hurried", "hypnotic", "hysterical", "imperious",
    "informative", "innocent", "inquisitive", "instructional", "intoxicated", "inviting", "jocular", "jovial",
    "languid", "laughing", "lighthearted", "loud", "maniacal", "matter-of-fact", "matter-of-factly", "measured",
    "melancholic", "melodious", "menacing", "mocking", "monotone", "mumbling", "muttering", "mysterious", "narrative",
    "nervous", "noble", "obedient", "ominous", "panicked", "persuasive", "philosophical", "pious", "playful",
    "pleading", "poetic", "polite", "praying", "preaching", "professional", "proud", "quiet", "quivering", "reciting",
    "refined", "reflective", "regal", "regretful", "relaxed", "reporting", "respectful", "reverent", "sad",
    "sarcastic", "sassy", "screaming", "seductive", "serene", "serious", "sharp", "shocked", "shouting", "sighing",
    "singing", "sinister", "slow", "slurred", "smug", "snarky", "soft-spoken", "solemn", "somber", "sorrowful",
    "stammering", "steady", "stern", "stoic", "stuttering", "subdued", "subservient", "succinct", "suggestive",
    "supercilious", "surprised", "suspicious", "sweet", "sycophantic", "taciturn", "taunting", "teacher-like",
    "tearful", "terrified", "terse", "theatrical", "thoughtful", "threatening", "trembling", "uncanny", "unexpressive",
    "unhinged", "unhurried", "unsettling", "urgent", "volatile", "warm", "welcoming", "whispering",
)
TEXTURE_VOCAB = (
    "booming", "breathy", "cadence", "clear", "coarse", "crisp", "deep", "disembodied", "distant", "echoing",
    "ethereal", "faint", "feeble", "gravelly", "gruff", "guttural", "heavy", "heavy voice", "high pitched",
    "high-pitched", "hoarse", "hollow", "hushed", "illusory", "jarring", "layered", "low", "low voice", "low-pitched",
    "low-sounding", "magnetic", "mechanical", "melodic", "melodious", "metal", "muffled", "muted", "natural",
    "natural audiobook voice", "phlegm", "plain", "raspy", "resonant", "resonating", "resounding", "resounds",
    "robust", "rough", "sharp", "shrill", "singing voice", "smooth", "soft", "stiff", "thin", "throaty",
    "thunderous", "unmellow", "weak", "weathered", "whispering",
)


def _matchers(vocab):
    return tuple((term, re.compile(r'(^|[^a-z0-9-])' + re.escape(term) + r'([^a-z0-9-]|$)')) for term in vocab)


_TONE_MATCHERS, _TEXTURE_MATCHERS = _matchers(TONE_VOCAB), _matchers(TEXTURE_VOCAB)


def _words(description, keep):
    return ' ' + re.sub(f'[^{keep}]+', ' ', str(description or '').lower()) + ' '


def get_gender_tag(description):
    text = _words(description, 'a-z0-9/')
    female, male = bool(_FEMALE.search(text)), bool(_MALE.search(text))
    if _AMBIGUOUS.search(text) or (female and male):
        return 'Ambiguous / mixed'
    return 'Female' if female else 'Male' if male else 'Not specified'


def get_age_tags(description):
    text = _words(description, 'a-z0-9')
    found = [label for label, rule in _AGE_RULES if rule.search(text)]
    return sorted(found, key=AGE_ORDER.index) if found else ['Not specified']


def _scan(description, matchers):
    text = _words(description, 'a-z0-9-')
    found = []
    for term, rule in matchers:
        match = rule.search(text)
        if match:
            found.append((match.start() + len(match.group(1)), term))
    return [term for _, term in sorted(found)]


def get_voice_tags(description):
    """-> {'gender', 'ages', 'tones', 'textures'}; tones/textures in the order they appear."""
    return {'gender': get_gender_tag(description), 'ages': get_age_tags(description),
            'tones': _scan(description, _TONE_MATCHERS), 'textures': _scan(description, _TEXTURE_MATCHERS)}
