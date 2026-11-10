[English](VOICE_REFERENCE.md) | [中文](VOICE_REFERENCE_CN.md) | [日本語](VOICE_REFERENCE_JA.md)

# 声音参考

一份综合性的声音表演术语表，汇编自专业表演术语词典和配音指导数据库，并结合了 Qwen3-TTS VoiceDesign 测试的实证发现。

## 本文档怎么用（从这里开始）

本文档分两部分：

### 第一部分：导演术语表——描述声音的词

这是一本描述声音听感的专业术语词典。在以下场合使用这些词：

1. **设计新声音**（Voice Designer 页）——从 **Section I: Texture, Timbre & Quality** 里选词
   - 例子：不要写 "a nice old man voice"，而写 "male baritone, warm chest resonance, hint of gravelly texture"

2. **指导角色的演绎**（Editor 页、`instruct` 字段）——从 **Sections II-IV: Emotion, Delivery, Archetypes** 里选词
   - 例子：不要写 "say it sadly"，而写 "mournful, breathy, slow pace, trembling"

### 第二部分：VoiceDesign 发现——什么才真正有效

这一部分记录了测试 AI 对各类声音描述跟随程度的实验。核心发现是：

> **先讲解剖结构的描述效果最好。** 永远从声音的音区开始（男声用 bass/baritone/tenor，女声用 contralto/mezzo/soprano），然后加 2-3 个质感词。绝不要把"听起来怎么样"和"这个人举止如何"混在一起——AI 会糊涂。

### 快速示例：坏的声音描述 vs 好的声音描述

**坏：** "A nice grandpa who reads slowly"
- 太模糊，没有任何声学信息

**好：** "Male baritone, rich chest resonance, warm smooth timbre, hint of gravelly texture, slow deliberate pacing"
- 明确的音区（baritone）
- 物理品质（胸腔共鸣、沙砾质感）
- 演绎风格（缓慢从容的节奏）

**坏：** "An energetic young woman who's always excited"
- 是行为描述，不是声学描述

**好：** "Female mezzo-soprano, bright clear timbre, light breathy quality, upbeat rhyth delivery"
- 明确的音区（mezzo-soprano）
- 物理品质（明亮、带气声）
- 演绎风格（轻快有节奏）

### 一句话规则

1. **永远先给音区：** 男声 bass/baritone/tenor，女声 contralto/mezzo/soprano
2. **再加 2-3 个质感词：** smooth、gravelly、breathy、rich、thin 等
3. **再加 1 个演绎词：** slow、fast、deliberate、rhythmic 等
4. **绝不要把声学术语和行为术语混在一起：** "warm gravelly baritone" ✓ 对 "friendly confident baritone" ✗ 错
5. **情绪词写进 `instruct`，不要写进 `description`**

---

# 第一部分：导演声音术语表

数百个用于微调声音表演的行业标准术语，按功能组织。

## I. 质地、音色与品质（"乐器"）

声音的物理听感或"颗粒感"。**在 VoiceDesign 的 `description` 里使用这些词。**

| 类别 | 描述词 |
|----------|------------|
| **Rough/Gritty** | Gravelly, Raspy, Husky, Scratchy, Smoky, Guttural, Coarse, Croaking, Grating, Dry, Cracked, Fractured, Sandpaper, Whiskey-soaked, Growling, Gruff, Hoarse, Ragged, Raw, Rusty, Strained, Throaty |
| **Smooth/Rich** | Silky, Velvety, Honeyed, Creamy, Liquid, Orotund, Dulcet, Mellifluous, Polished, Lush, Mellow, Buttery, Soothing, Fluid, Flowing, Seamless, Satiny, Suave, Unctuous |
| **Resonance** | Booming, Chesty, Full-bodied, Deep, Sonorous, Thundering, Rumbling, Hollow, Echoing, Cavernous, Round, Heavy, Grounded, Bassy, Resonant, Bell-like |
| **Light/High** | Airy, Breathy, Feathery, Thin, Reedy, Tinny, Piercing, Shrill, Piping, Wispy, Fluting, Chirpy, Squeaky, Falsetto, Head-voice, Boyish, Girlish, Metallic, Brittle |
| **Nasal/Sharp** | Nasal, Twangy, Whiny, Pinched, Brass, Strident, Cutting, Piercing, Biting, Sharp, Acidic, Astringent, Clamorous, Ear-splitting, Harsh, Jarring, Penetrating, Stingy |
| **Vocal Defects** | Fry (Vocal Fry), Sibilant (whistling 'S'), Plosive (popping 'P'), Tremulous (shaking), Wobbling, Quavering, Warbling, Slurred, Mumbled, Incoherent, Thick |

## II. 情绪、态度与语气（"潜台词"）

用来调定心理状态的形容词。**在 `instruct` 里使用这些词，不要用在 `description` 里。**

| 类别 | 描述词 |
|----------|------------|
| **Aggressive/Hostile** | Acerbic, Antagonistic, Belligerent, Biting, Brusque, Caustic, Combative, Confrontational, Contemptuous, Cutting, Derisive, Dictatorial, Disdainful, Dogmatic, Domineering, Explosive, Fierce, Hard, Imperious, Incendiary, Indignant, Intimidating, Malevolent, Militant, Pugnacious, Savage, Scornful, Seething, Sharp, Spiteful, Stern, Threatening, Truculent, Vengeful, Venomous, Vitriolic, Warlike |
| **Sad/Depressed** | Bereft, Bleak, Broken, Crestfallen, Dejected, Desolate, Despondent, Disheartened, Dismal, Doleful, Dour, Forlorn, Gloomy, Grief-stricken, Heartbroken, Heavy-hearted, Hollow, Hopeless, Lachrymose, Melancholy, Miserable, Morose, Mournful, Pathetic, Pessimistic, Plaintive, Regretful, Remorseful, Resigned, Somber, Sorrowful, Tearful, Tragic, Weepy, Wistful, Woe-begone |
| **Happy/Positive** | Blissful, Buoyant, Celebratory, Cheerful, Chipper, Contented, Delightful, Ebullient, Ecstatic, Elated, Encouraging, Energetic, Enthusiastic, Euphoric, Exhilarated, Exuberant, Festive, Genial, Giddy, Gleeful, Gratified, Hearty, Hopeful, Jaunty, Jolly, Jovial, Joyous, Jubilant, Lighthearted, Merry, Optimistic, Overjoyed, Peppy, Radiant, Rapturous, Reassured, Sunny, Upbeat, Vibrant, Vivacious, Zestful |
| **Fearful/Anxious** | Agitated, Alarmed, Apprehensive, Cautious, Concerned, Cowardly, Distressed, Edgy, Faint, Frightened, Guarded, Hesitant, Hysterical, Insecure, Jittery, Nervous, Panic-stricken, Paralyzed, Paranoid, Petrified, Quaking, Restless, Scared, Shaky, Skittish, Spooked, Startled, Tense, Terrified, Timid, Trembling, Uneasy, Unsettled, Wary, Worried |
| **Calm/Neutral** | Ambivalent, Apathetic, Balanced, Bland, Blas&eacute;, Bored, Casual, Collected, Composed, Cool, Detached, Dispassionate, Dull, Easygoing, Equable, Even-tempered, Flat, Imperturbable, Indifferent, Laid-back, Listless, Matter-of-fact, Measured, Mediocre, Mild, Moderate, Monotone, Nonchalant, Objective, Pacifistic, Passive, Peaceful, Phlegmatic, Placid, Relaxed, Serene, Steady, Stoic, Tranquil, Unemotional, Unflappable |
| **Affectionate/Intimate** | Adoring, Amorous, Caring, Cherishing, Comforting, Compassionate, Coquettish, Cuddly, Doting, Endearing, Fond, Gentle, Intimate, Loving, Maternal, Nurturing, Paternal, Protective, Romantic, Seductive, Sensual, Soft, Sultry, Sweet, Sympathetic, Tender, Touching, Warm, Yearning |
| **Intellectual/Superior** | Analytical, Arrogant, Authoritative, Condescending, Critical, Cynical, Didactic, Dignified, Dry, Educated, Erudite, Haughty, Informative, Inquisitive, Instructional, Knowledgeable, Lofty, Logical, Patronizing, Pedantic, Philosophical, Pompous, Practical, Pragmatic, Precise, Pretentious, Professional, Rational, Sagacious, Sarcastic, Sardonic, Satirical, Scholarly, Skeptical, Smart, Snobbish, Sophisticated, Supercilious, Superior, Thoughtful, Wise, Wry |
| **Playful/Quirky** | Bantering, Cheeky, Childish, Clownish, Droll, Eccentric, Facetious, Fanciful, Flippant, Flirty, Foolish, Goofy, Humorous, Impish, Ironic, Irreverent, Jocular, Kooky, Mischievous, Mocking, Offbeat, Playful, Prankish, Quirky, Rascally, Ridiculous, Sassy, Saucy, Silly, Snarky, Teasing, Tongue-in-cheek, Whimsical, Witty, Zany |

## III. 演绎、语速与节奏（"机械部分"）

速度、流畅度和清晰度的控制。**在 `instruct` 里使用这些词，不要用在 `description` 里。**

| 风格 | 说明 |
|-------|------------|
| **Staccato** | Short, detached, punchy, clipped, abrupt, machine-gun style |
| **Legato** | Smooth, connected, flowing, gliding, continuous, elongated vowels |
| **Rapid-fire** | Blistering speed, urgent, manic, auctioneer-style, frantic, breathless |
| **Measured** | Deliberate, slow, thoughtful, spacing out every word, ponderous |
| **Halting** | Uncertain, pausing frequently, stammering, disjointed, stumbling |
| **Lilting** | Rhythmic rise and fall, musical, singsong, melodic, bouncing |
| **Drawl** | Lazily extended vowels, slow, relaxed, languid, stretching words |
| **Punchy** | High energy emphasis on key words, dynamic, hitting the consonants hard |
| **Deadpan** | Zero inflection, flat pitch, dry comedy, poker-faced delivery |
| **Cadenced** | Highly rhythmic, almost poetic or Shakespearean, metered |

## IV. 行业规格与原型（"角色"）

选角资料和广告脚本里用的行话。

| 原型 | 说明 |
|-----------|------------|
| **Authentic** | Real person, non-actor, documentary style, raw, unpolished |
| **Announcer (Classic)** | "Voice of God" (VOG), presentational, booming, polished, radio DJ |
| **Announcer (Non-Announcery)** | Anti-sell, conversational, throwing it away, indifferent |
| **Conversational** | Just talking to a friend, informal, slurred edges, natural breaths |
| **Girl/Guy Next Door** | Relatable, friendly, middle-class, trustworthy, average, safe |
| **Trusted Advisor** | Doctor, lawyer, parent, financial planner, knowledgeable, calm |
| **Cool Parent** | Modern, hip, not strict, understanding, "Disney Mom/Dad" |
| **The Millennial/Gen Z** | Vocal fry, uptalk, casual, cynical, tech-savvy, fast, ironic |
| **Promo Voice** | Gritty, deep, growly, intense, whispering, high-drama, movie trailer |
| **Corporate/Industrial** | Clear, articulate, professional, neutral, informative, confident |
| **Hard Sell** | High volume, high energy, urgent, shouting, "Car Dealer," aggressive |
| **Soft Sell** | Intimate, whispery, luxury, expensive, comforting, seductive |
| **Characters** | Villain, Hero, Sidekick, Creature, Monster, Robot, Alien, Witch, Old Crone, Sage |

## V. 技术性现场指令（"工作流"）

关于录音过程的指令。

| 指令 | 含义 |
|---------|---------|
| **A-B-C (Three-in-a-row)** | Read the same line three times with three different attitudes |
| **Billboard** | Emphasize a specific name or product ("Hit the brand name harder") |
| **Button** | A clean, definitive end to the sentence/take; sticking the landing |
| **Clean Up** | Re-doing a line to fix a mouth noise or stumble |
| **Cold Read** | Reading the script immediately without rehearsal |
| **Color** | Add more emotion or personality to a specific word |
| **Compression** | Reduce dynamic range (louder in quiet parts, softer in loud parts) |
| **Donut** | Voice at beginning and end, hole in the middle for music/clips |
| **Eat the Mic** | Very close to microphone for proximity effect (deep/intimate bass) |
| **Guide Track** | Matching timing of a pre-recorded audio/video reference |
| **Lay Out** | Pause or stop speaking for a section |
| **Lead-in** | A sentence said silently to get into character before the first line |
| **Level** | Reading at performance volume to set microphone gain |
| **Lift** | Brighten or raise pitch slightly, often at end of a spot |
| **Pick-up** | Recording just one specific sentence or phrase |
| **Popping** | Excessive air on "P"/"B" sounds (fix: "Turn slightly off-axis") |
| **Projection** | Throwing the voice to the back of the room vs. keeping it internal |
| **Punch-in** | Recording a small snippet to insert into an existing take |
| **Safety** | One last take in case the previous good one had a technical glitch |
| **Slate** | Stating name and take number before performing ("John Doe, Take 1") |
| **Smile** | Physically smile while reading (changes tone to brighter/warmer) |
| **Sotto Voce** | A stage whisper; speaking under one's breath |
| **Throw Away** | De-emphasize a line; make it sound unimportant or casual |
| **Time** | Speeding up or slowing down to hit a strict duration (e.g. exactly 29.5s) |
| **Wild Line** | A line recorded separately from script flow (shouts, laughs, grunts) |
| **Walla** | Background crowd noise/murmuring performed by a group |

## VI. 扩展维度参考

涵盖五个关键领域的 150+ 个额外专业术语。

### Dimensions（物理性与氛围）

| 维度 | 术语 |
|-----------|-------|
| **Presence** | Close-mic, Distant, Intimate, Expansive, Internalized, Projection |
| **Texture** | Silky, Velvety, Honeyed, Mellifluous, Buttery, Creamy, Lush, Liquid, Orotund, Resonant |
| **Grit** | Gravelly, Raspy, Smoky, Husky, Guttural, Scratchy, Coarse, Croaking, Grating, Dry, Cracked, Sandpaper, Whiskey-soaked, Growling, Gruff, Hoarse, Ragged, Raw, Rusty, Strained, Throaty |
| **Brightness/Weight** | Airy, Breathy, Feathery, Thin, Reedy, Tinny, Piercing, Shrill, Piping, Wispy, Fluting, Chirpy, Squeaky, Metallic, Brittle |
| **Temperature** | Warm, Sunny, Nurturing, Genial, Toast-like, Cool, Detached, Clinical, Crisp, Distant, Icy, Steel |

### Emotions（心理意图）

| 类别 | 术语 |
|----------|-------|
| **High Energy/Positive** | Jubilant, Exuberant, Ebullient, Cheerful, Playful, Whimsical, Contented, Blissful, Optimistic, Encouraging, Radiant, Rapturous, Peppy, Jaunty, Jolly, Jovial |
| **High Energy/Negative** | Indignant, Seething, Acerbic, Strident, Sharp, Biting, Vitriolic, Threatening, Savage, Belligerent, Antagonistic, Caustic, Combative, Confrontational, Contemptuous, Derisive |
| **Low Energy/Negative** | Melancholy, Despondent, Wistful, Morose, Tearful, Heavy-hearted, Hollow, Defeated, Grief-stricken, Bereft, Bleak, Broken, Crestfallen, Dejected, Desolate, Disheartened, Dismal, Doleful, Dour, Forlorn |
| **Vulnerability** | Jittery, Tremulous, Quavering, Shaky, Hysterical, Panic-stricken, Hesitant, Guarded, Tense, Agitated, Alarmed, Apprehensive, Faint, Frightened, Skittish, Uneasy |
| **Authority/Power** | Commanding, Imperious, Dictatorial, Regal, Stately, Assertive, Dogmatic, Unyielding, Matter-of-fact, Dignified, Pompous, Supercilious, Sagacious |
| **Irony/Wit** | Sarcastic, Ironic, Wry, Cynical, Sardonic, Satirical, Facetious, Flippant, Sassy, Saucy, Cheeky, Bantering, Droll |

### Pitch & Musicality（旋律）

| 方面 | 术语 |
|--------|-------|
| **Range** | Bass-heavy, Mid-range, High-frequency, Falsetto, Head-voice, Chest-voice |
| **Movement** | Uptalk (rising pitch, uncertainty), Down-talk (dropping pitch, finality/authority), Singsong (nursery-rhyme rise and fall), Flat/Monotone (zero variation), Modulated (controlled varied pitch) |
| **Register** | Vocal Fry (creaky), Nasal, Twangy, Resonance, Vibrato, Tremolo |

### Delivery & Mechanics（语速与清晰度）

| 方面 | 术语 |
|--------|-------|
| **Tempo** | Staccato (clipped), Legato (flowing), Rapid-fire, Measured, Deliberate, Halting, Drawling, Languid, Ponderous |
| **Articulation** | Crisp, Glassy, Over-enunciated, Slurred, Lazy, Mumbled, Thick, Sibilant (hissing 'S'), Plosive (popping 'P'/'B') |
| **Emphasis** | Billboard (punching a key word), Throwaway (de-emphasizing), Punchy (high-energy consonants) |
| **Rhythm** | Cadenced, Syncopated, Naturalistic, Flowing, Breathless, Erratic |

### Specs & Archetypes（角色模型）

| 类别 | 原型 |
|----------|-----------|
| **Commercial** | Hard-sell, Soft-sell, Everyman/Everywoman, Girl/Boy Next Door, Hip/Gen-Z, Millennial, Sophisticated/Luxury |
| **Narrative** | The Trusted Advisor, The Sage/Professor, The Villain/Antagonist, The Sidekick, The Reluctant Hero |
| **Technical/Industry** | "Announcery" (classic radio), "Voice of God" (deep/booming/omniscient), "Corporate/Industrial" (polished/professional), "Sotto Voce" (stage whisper), "Walla" (background crowd noise) |

---

# 第二部分：Qwen3-TTS VoiceDesign 实验发现

测试 VoiceDesign 模型如何理解声音描述提示词得到的实证结果。它把上面的专业术语表映射到模型的实际行为。

## 方法

- 所有测试在所有声音上都使用同样 4 个中性句子
- `instruct` 字段留空，以隔离声音描述的效果
- `seed: -1`（随机），以测试自然的稳定性
- 评估维度：跨行的声音一致性、对描述的遵循程度、质量
- 测试脚本存放在 `scripts/voice_design_strategies*.json`

## 第一轮：策略对比

测试了 5 种根本不同的描述思路（男女各一，共 40 个样本）。

### 测试的策略

| 策略 | 思路 | 示例 |
|----------|----------|---------|
| **Anatomy** | Physical/acoustic traits | "A deep male baritone with resonant chest voice, smooth timbre, slight vocal fry" |
| **Persona** | Character identity | "A kind elderly grandfather who speaks with patience and gentle authority" |
| **Technical** | Audio engineering terms | "A well-projected male voice with crisp diction, moderate pace, controlled dynamic range" |
| **Emotion** | Emotional qualities only | "A calm, reassuring male voice that sounds confident, trustworthy, and steady" |
| **Context** | Scene/situation | "A male narrator reading a fantasy novel aloud in a quiet bookshop" |

### 结果

| 策略 | 一致性 | 关键观察 |
|----------|------------|-----------------|
| **Anatomy** | Best (M), Poor (F) | Physical descriptions give the model concrete acoustic targets. Female "light soprano, airy" pushed too high/thin. |
| **Persona** | Variable | Works when archetype strongly implies a vocal type (young journalist -> fast squeaky). Fails when ambiguous (grandfather -> gender switching). |
| **Technical** | Low-Medium | Model partially understands audio engineering terms but not reliably. |
| **Emotion** | Low | Too abstract. No physical anchor for what "calm" or "warm" sounds like. |
| **Context** | None | Different voice every line. Model treats description as scene direction, not voice identity. |

### 第一轮结论

**先讲解剖结构的描述胜出。** 模型需要具体的声学目标（音高、音区、音色、质地）才能产出稳定的声音。抽象的品质（情绪、人设、场景）给了它太多解释空间。

## 第二轮：解剖优先的混合写法

所有描述都改写为：音区 + 音色 + 语气形容词。不含演绎/表演类的词（那些属于 `instruct`）。

### 公式

> **[register] + [timbre/texture descriptors] + [tonal character adjectives]**
>
> Never include: delivery verbs (speaking, reading), pacing words, scene/context

### 结果

| 声音 | 描述 | 一致性 | 质量 | 备注 |
|-------|------------|------------|---------|-------|
| **WARM_BARITONE** | "deep male baritone, rich chest resonance, warm smooth timbre, hint of gravelly texture" | Med-High | High | Voice as described, very good deep quality, some variation |
| **GROUNDED_ALTO** | "female alto, full grounded mid-range, slightly husky, natural warmth" | Med-High | High | Follows description well, less variable than baritone |
| **BRIGHT_TENOR** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | Very Low | High | Wild emotional delivery, "unhinged" quality — see Bright Tenor Investigation |
| **SILKY_MEZZO** | "female mezzo-soprano, silky even tone, soft rounded edges, gentle clarity" | Very High | Very High | Best overall. Extremely easy to listen to. |
| **COMMANDING_BASS** | "male bass, dark authoritative rumble, heavy low-end, firm tone" | High | High | Cowboy/western gruff quality. A keeper. |
| **CRISP_SOPRANO** | "female soprano, precise crystalline tone, balanced brightness, confident projection" | Very High | High | More tonal range but still consistent. Studio-quality feel. |

### 一致性的驱动因素（来自第 I 节的术语）

**高一致性**——模型能可靠抓住的词：
- 平滑/均匀：`silky`、`even`、`soft`、`rounded`（来自 Smooth/Rich）
- 精确：`precise`、`crystalline`、`balanced`（暗示受控）
- 权威/坚定：`dark`、`authoritative`、`firm`、`commanding`（来自 Resonance）
- 扎实：`grounded`、`full`、`husky`（来自 Rough/Gritty + Resonance）

**高波动**——会让结果不稳定的词：
- 能量：`bright`、`youthful energy`（来自 Brightness/Weight——见下方调查）
- 单纯的模糊暖意：`warm`、`deep`，没有锚定的质感词

**观察：** 在所有描述下，女声的一致性都优于男声。

## Bright Tenor 调查

通过系统性消融（8 个变体，32 个样本），隔离出 BRIGHT_TENOR 不稳定的原因。

### 测试矩阵

| 变体 | 描述 | 改了什么 |
|---------|------------|-------------|
| **CONTROL** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | Original — baseline |
| **NO_BRIGHT** | "male tenor, clear tone, light nasal resonance, youthful energy" | Removed "bright" |
| **NO_ENERGY** | "male tenor, bright clear tone, light nasal resonance" | Removed "youthful energy" |
| **NEITHER** | "male tenor, clear tone, light nasal resonance" | Removed both |
| **BRIGHT_ONLY** | "A bright male tenor voice" | Minimal — just "bright" |
| **SHARP_REPLACE** | "male tenor, sharp forward-placed tone, light nasal resonance, crisp articulation" | Replaced "bright" with "sharp" |
| **HIGH_HARMONICS** | "male tenor, strong upper harmonics, clear forward projection, light nasal resonance" | Technical acoustic equivalent |
| **STABILIZED** | "male tenor, steady clean tone, light nasal resonance, even delivery" | Applied consistency formula |

### 结果

| 变体 | 一致性 | 角色感 | 发现 |
|---------|------------|-----------|---------|
| **CONTROL** | Very Low | Unhinged, wild emotional swings | Confirms R2 behavior |
| **NO_BRIGHT** | Medium | Expressive but text-guided, less unhinged | "Bright" contributes emotional volatility |
| **NO_ENERGY** | Medium | Similar to NO_BRIGHT, one auctioneer pacing spike on dramatic text | "Energy" contributes pacing bursts |
| **NEITHER** | High | Follows text cues, stable | Bare tenor base is inherently stable |
| **BRIGHT_ONLY** | High | Stable, text-responsive | "Bright" alone is fine |
| **SHARP_REPLACE** | High | Annoyed/irritable quality | "Sharp/forward-placed" maps to irritated tone |
| **HIGH_HARMONICS** | Medium | Raised voice, near-shouting, very clear | Useful niche: alertness/wake-up effect |
| **STABILIZED** | High | Narrator-like, low emotion | Consistency formula works but flattens expression |

### 关键发现：交互效应

**"Bright" 和 "youthful energy" 单独用都稳定，合在一起却会互相破坏。** 这个组合给了模型互相矛盾的信号——音色上的明亮（声学）+ 行为上的活力（表演）——于是它每次生成都在两种解释之间摇摆。

这很可能是条通用原则：**在声音描述里把声学描述词（第 I 节）和行为/能量描述词（第 III 节）混在一起，会产生歧义。** 声音描述要纯粹待在第 I 节的范围内；第 II-IV 节的品质放到 `instruct` 里。

### 逐词映射

| 词 | 节 | 模型的解释 | 有用程度 |
|------|---------|---------------------|------------|
| `bright` (alone) | I. Brightness/Weight | Stable tonal brightness | Good in isolation |
| `bright` + energy terms | I + III | Emotional chaos | Avoid combination |
| `sharp` | I. Nasal/Sharp | Annoyed/irritable tone | Use carefully |
| `upper harmonics` | I. (technical) | Raised volume, near-shout | Niche: alertness effect |
| `steady`, `even` | I. Smooth/Rich | Narrator-like stability | Flattens expression |
| `silky`, `rounded` | I. Smooth/Rich | High consistency + listenability | Best for narration |
| `dark`, `authoritative` | I. Resonance | Gruff/western quality, consistent | Good for male authority |
| `crystalline`, `precise` | I. Smooth/Rich | Consistent with expressive range | Best balance |
| `hollow` (in instruct) | I. Resonance | Whisper/sigh hybrid, breathy emptiness | Avoid in instruct — model reads it as acoustic, not emotional. Use "defeated" instead. |

## 声音描述提示词的工作规则

1. **先给音区**（bass、baritone、tenor、alto、mezzo-soprano、soprano）
2. **再加 2-3 个只来自第 I 节的质感/音色词**
3. **绝不要在 description 字段里把声学描述词（第 I 节）和行为描述词（第 II-IV 节）混用**
4. **演绎、情绪和语速写进 `instruct`**——它就是干这个的
5. **为了稳定，优先用"控制类"词**（`silky`、`even`、`precise`、`firm`），而不是"能量类"词（`bright`、`sharp`、`piercing`）
6. **用模糊的词过度指定会叠加不确定性**——简短而清晰的描述可以胜过详细但含糊的描述
7. **在这个模型里，女声天生更一致**，无论用哪种描述风格
