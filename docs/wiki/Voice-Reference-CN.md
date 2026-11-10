# 声音参考

[中文主页](Home-CN.md) · [English](Voice-Reference.md)

描述声音的词汇，以及 Qwen3-TTS VoiceDesign 模型实际会怎么处理它们。

- **第 1 部分**是从表演术语表和配音指导手册中收集的词汇。
- **第 2 部分**是测试显示的 VoiceDesign 如何理解声音描述。

**简版：**

- 把描述**声音本身**的词（音区、质感、音色）放进声音的 **description**——Voice Design、Designer 页和 Dataset Builder 都会用到它。
- 把描述**情绪、演绎和节奏**的词放进该行的**指令**（`instruct`）。
- 不要在描述里混入这两类：那会让声音不稳定（见 [bright tenor 测试](#bright-tenor-调查)）。

末尾的[工作规则](#声音描述的工作规则)做了总结。

---

## 第 1 部分：导演词汇

数百个用于塑造人声表演的标准术语，按它们控制的对象分组。

### 一、质感、音色与品质（"乐器"）

声音的物理听感或"颗粒"。**在 VoiceDesign 的 `description` 里使用这些词。**

| 类别 | 描述词 |
|----------|------------|
| **粗糙/颗粒** | Gravelly, Raspy, Husky, Scratchy, Smoky, Guttural, Coarse, Croaking, Grating, Dry, Cracked, Fractured, Sandpaper, Whiskey-soaked, Growling, Gruff, Hoarse, Ragged, Raw, Rusty, Strained, Throaty |
| **平滑/丰满** | Silky, Velvety, Honeyed, Creamy, Liquid, Orotund, Dulcet, Mellifluous, Polished, Lush, Mellow, Buttery, Soothing, Fluid, Flowing, Seamless, Satiny, Suave, Unctuous |
| **共鸣** | Booming, Chesty, Full-bodied, Deep, Sonorous, Thundering, Rumbling, Hollow, Echoing, Cavernous, Round, Heavy, Grounded, Bassy, Resonant, Bell-like |
| **轻/高** | Airy, Breathy, Feathery, Thin, Reedy, Tinny, Piercing, Shrill, Piping, Wispy, Fluting, Chirpy, Squeaky, Falsetto, Head-voice, Boyish, Girlish, Metallic, Brittle |
| **鼻音/尖锐** | Nasal, Twangy, Whiny, Pinched, Brass, Strident, Cutting, Piercing, Biting, Sharp, Acidic, Astringent, Clamorous, Ear-splitting, Harsh, Jarring, Penetrating, Stingy |
| **发声缺陷** | Fry (Vocal Fry), Sibilant (whistling 'S'), Plosive (popping 'P'), Tremulous (shaking), Wobbling, Quavering, Warbling, Slurred, Mumbled, Incoherent, Thick |

### 二、情绪、态度与语气（"潜台词"）

用于调准心理状态的形容词。**在 `instruct` 中使用这些词，不要放进 `description`。**

| 类别 | 描述词 |
|----------|------------|
| **攻击/敌意** | Acerbic, Antagonistic, Belligerent, Biting, Brusque, Caustic, Combative, Confrontational, Contemptuous, Cutting, Derisive, Dictatorial, Disdainful, Dogmatic, Domineering, Explosive, Fierce, Hard, Imperious, Incendiary, Indignant, Intimidating, Malevolent, Militant, Pugnacious, Savage, Scornful, Seething, Sharp, Spiteful, Stern, Threatening, Truculent, Vengeful, Venomous, Vitriolic, Warlike |
| **悲伤/低落** | Bereft, Bleak, Broken, Crestfallen, Dejected, Desolate, Despondent, Disheartened, Dismal, Doleful, Dour, Forlorn, Gloomy, Grief-stricken, Heartbroken, Heavy-hearted, Hollow, Hopeless, Lachrymose, Melancholy, Miserable, Morose, Mournful, Pathetic, Pessimistic, Plaintive, Regretful, Remorseful, Resigned, Somber, Sorrowful, Tearful, Tragic, Weepy, Wistful, Woe-begone |
| **快乐/积极** | Blissful, Buoyant, Celebratory, Cheerful, Chipper, Contented, Delightful, Ebullient, Ecstatic, Elated, Encouraging, Energetic, Enthusiastic, Euphoric, Exhilarated, Exuberant, Festive, Genial, Giddy, Gleeful, Gratified, Hearty, Hopeful, Jaunty, Jolly, Jovial, Joyous, Jubilant, Lighthearted, Merry, Optimistic, Overjoyed, Peppy, Radiant, Rapturous, Reassured, Sunny, Upbeat, Vibrant, Vivacious, Zestful |
| **恐惧/焦虑** | Agitated, Alarmed, Apprehensive, Cautious, Concerned, Cowardly, Distressed, Edgy, Faint, Frightened, Guarded, Hesitant, Hysterical, Insecure, Jittery, Nervous, Panic-stricken, Paralyzed, Paranoid, Petrified, Quaking, Restless, Scared, Shaky, Skittish, Spooked, Startled, Tense, Terrified, Timid, Trembling, Uneasy, Unsettled, Wary, Worried |
| **平静/中性** | Ambivalent, Apathetic, Balanced, Bland, Blas&eacute;, Bored, Casual, Collected, Composed, Cool, Detached, Dispassionate, Dull, Easygoing, Equable, Even-tempered, Flat, Imperturbable, Indifferent, Laid-back, Listless, Matter-of-fact, Measured, Mediocre, Mild, Moderate, Monotone, Nonchalant, Objective, Pacifistic, Passive, Peaceful, Phlegmatic, Placid, Relaxed, Serene, Steady, Stoic, Tranquil, Unemotional, Unflappable |
| **亲昵/亲密** | Adoring, Amorous, Caring, Cherishing, Comforting, Compassionate, Coquettish, Cuddly, Doting, Endearing, Fond, Gentle, Intimate, Loving, Maternal, Nurturing, Paternal, Protective, Romantic, Seductive, Sensual, Soft, Sultry, Sweet, Sympathetic, Tender, Touching, Warm, Yearning |
| **理智/优越** | Analytical, Arrogant, Authoritative, Condescending, Critical, Cynical, Didactic, Dignified, Dry, Educated, Erudite, Haughty, Informative, Inquisitive, Instructional, Knowledgeable, Lofty, Logical, Patronizing, Pedantic, Philosophical, Pompous, Practical, Pragmatic, Precise, Pretentious, Professional, Rational, Sagacious, Sarcastic, Sardonic, Satirical, Scholarly, Skeptical, Smart, Snobbish, Sophisticated, Supercilious, Superior, Thoughtful, Wise, Wry |
| **俏皮/古怪** | Bantering, Cheeky, Childish, Clownish, Droll, Eccentric, Facetious, Fanciful, Flippant, Flirty, Foolish, Goofy, Humorous, Impish, Ironic, Irreverent, Jocular, Kooky, Mischievous, Mocking, Offbeat, Playful, Prankish, Quirky, Rascally, Ridiculous, Sassy, Saucy, Silly, Snarky, Teasing, Tongue-in-cheek, Whimsical, Witty, Zany |

### 三、演绎、节奏与韵律（"机制"）

速度、流动和清晰度控制。**在 `instruct` 中使用这些词，不要放进 `description`。**

| 风格 | 说明 |
|-------|------------|
| **Staccato** | 短促、断开、有冲击力、截断、突兀、机关枪式 |
| **Legato** | 平滑、连贯、流动、滑行、连续、拉长的元音 |
| **Rapid-fire** | 极快的速度、急促、狂热、拍卖师式、慌乱、气喘 |
| **Measured** | 从容、缓慢、深思、每个词之间留白、沉重 |
| **Halting** | 不确定、频繁停顿、结巴、不连贯、磕绊 |
| **Lilting** | 有节奏的起伏、音乐性、吟唱式、旋律感、跳跃 |
| **Drawl** | 慵懒拉长的元音、缓慢、放松、懒散、把词拉长 |
| **Punchy** | 对关键词的重音高能量、动态、狠狠咬住辅音 |
| **Deadpan** | 零起伏、平音高、冷面喜剧、不动声色 |
| **Cadenced** | 高度有节奏、近乎诗意或莎士比亚式、有格律 |

### 四、行业规格与原型（"角色"）

选角分解表和商业脚本中使用的简写。

| 原型 | 说明 |
|-----------|------------|
| **Authentic** | 真人、非演员、纪录片风格、生涩、未打磨 |
| **Announcer (Classic)** | "Voice of God"（VOG）、展示式、洪亮、精致、电台 DJ |
| **Announcer (Non-Announcery)** | 反推销、对话式、随口带过、无所谓 |
| **Conversational** | 就像跟朋友说话、非正式、边缘含糊、自然的呼吸 |
| **Girl/Guy Next Door** | 有共鸣、友好、中产、值得信任、普通、安全 |
| **Trusted Advisor** | 医生、律师、家长、理财顾问、有见识、平静 |
| **Cool Parent** | 现代、时髦、不严厉、理解人、"Disney Mom/Dad" |
| **The Millennial/Gen Z** | 声音沙哑（vocal fry）、上扬语调、随意、愤世、懂技术、快、反讽 |
| **Promo Voice** | 粗粝、低沉、咆哮、强烈、低语、高戏剧性、电影预告片 |
| **Corporate/Industrial** | 清晰、字正腔圆、专业、中性、信息性强、自信 |
| **Hard Sell** | 大音量、高能量、急促、喊叫、"Car Dealer"、强势 |
| **Soft Sell** | 亲密、气声、奢华、昂贵、安抚、诱惑 |
| **Characters** | 反派、英雄、搭档、生物、怪物、机器人、外星人、女巫、老妪、智者 |

### 五、录音棚指令（"工作流"）

录音过程中的指令。

| 指令 | 含义 |
|---------|---------|
| **A-B-C (Three-in-a-row)** | 用三种不同态度把同一行读三遍 |
| **Billboard** | 强调某个特定的名字或产品（"Hit the brand name harder"） |
| **Button** | 干净、确定地结束句子/take；稳稳落地 |
| **Clean Up** | 重录一行以修正口水声或磕绊 |
| **Cold Read** | 不经排练直接读脚本 |
| **Color** | 给某个特定的词加入更多情绪或个性 |
| **Compression** | 压缩动态范围（安静处更响，响亮处更轻） |
| **Donut** | 开头和结尾有人声，中间留空给音乐/片段 |
| **Eat the Mic** | 极度贴近麦克风以获得近讲效应（低沉/亲密的低音） |
| **Guide Track** | 与预先录制的音频/视频参考对齐时间 |
| **Lay Out** | 在某一段落暂停或停止说话 |
| **Lead-in** | 在第一行之前默念一句，以进入角色 |
| **Level** | 以表演音量朗读，用来设定麦克风增益 |
| **Lift** | 稍微提亮或提高音高，常在广告结尾使用 |
| **Pick-up** | 只录一个特定的句子或短语 |
| **Popping** | "P"/"B" 音上气流过多（修正："Turn slightly off-axis"） |
| **Projection** | 把声音送到房间后方，还是保持在内部 |
| **Punch-in** | 录一小段，插入已有的 take |
| **Safety** | 保险起见再录一遍，以防之前那条有技术问题 |
| **Slate** | 表演前报名字和 take 编号（"John Doe, Take 1"） |
| **Smile** | 朗读时真的微笑（会让音色更亮/更暖） |
| **Sotto Voce** | 舞台低语；压着气说话 |
| **Throw Away** | 弱化一行；让它听起来不重要或随意 |
| **Time** | 加快或放慢以卡到严格时长（例如正好 29.5s） |
| **Wild Line** | 与脚本流程分开录的一行（喊叫、笑声、哼声） |
| **Walla** | 由一组人表演的背景人群噪声/低语声 |

### 六、扩展参考

另外 150 多个术语，分属五个方面。

#### 身体感与氛围

| 维度 | 术语 |
|-----------|-------|
| **临场感** | Close-mic, Distant, Intimate, Expansive, Internalized, Projection |
| **质感** | Silky, Velvety, Honeyed, Mellifluous, Buttery, Creamy, Lush, Liquid, Orotund, Resonant |
| **颗粒感** | Gravelly, Raspy, Smoky, Husky, Guttural, Scratchy, Coarse, Croaking, Grating, Dry, Cracked, Sandpaper, Whiskey-soaked, Growling, Gruff, Hoarse, Ragged, Raw, Rusty, Strained, Throaty |
| **明亮度/重量** | Airy, Breathy, Feathery, Thin, Reedy, Tinny, Piercing, Shrill, Piping, Wispy, Fluting, Chirpy, Squeaky, Metallic, Brittle |
| **温度** | Warm, Sunny, Nurturing, Genial, Toast-like, Cool, Detached, Clinical, Crisp, Distant, Icy, Steel |

#### 情绪（心理意图）

| 类别 | 术语 |
|----------|-------|
| **高能量/积极** | Jubilant, Exuberant, Ebullient, Cheerful, Playful, Whimsical, Contented, Blissful, Optimistic, Encouraging, Radiant, Rapturous, Peppy, Jaunty, Jolly, Jovial |
| **高能量/消极** | Indignant, Seething, Acerbic, Strident, Sharp, Biting, Vitriolic, Threatening, Savage, Belligerent, Antagonistic, Caustic, Combative, Confrontational, Contemptuous, Derisive |
| **低能量/消极** | Melancholy, Despondent, Wistful, Morose, Tearful, Heavy-hearted, Hollow, Defeated, Grief-stricken, Bereft, Bleak, Broken, Crestfallen, Dejected, Desolate, Disheartened, Dismal, Doleful, Dour, Forlorn |
| **脆弱** | Jittery, Tremulous, Quavering, Shaky, Hysterical, Panic-stricken, Hesitant, Guarded, Tense, Agitated, Alarmed, Apprehensive, Faint, Frightened, Skittish, Uneasy |
| **权威/力量** | Commanding, Imperious, Dictatorial, Regal, Stately, Assertive, Dogmatic, Unyielding, Matter-of-fact, Dignified, Pompous, Supercilious, Sagacious |
| **反讽/机智** | Sarcastic, Ironic, Wry, Cynical, Sardonic, Satirical, Facetious, Flippant, Sassy, Saucy, Cheeky, Bantering, Droll |

#### 音高与音乐性（旋律）

| 方面 | 术语 |
|--------|-------|
| **音域** | Bass-heavy, Mid-range, High-frequency, Falsetto, Head-voice, Chest-voice |
| **走向** | Uptalk（音高上扬，不确定）、Down-talk（音高下落，终结/权威）、Singsong（童谣式起伏）、Flat/Monotone（零变化）、Modulated（受控的变化音高） |
| **音区** | Vocal Fry（嘎裂声）、Nasal、Twangy、Resonance、Vibrato、Tremolo |

#### 演绎与机制（节奏与清晰度）

| 方面 | 术语 |
|--------|-------|
| **速度** | Staccato（截断）、Legato（流动）、Rapid-fire、Measured、Deliberate、Halting、Drawling、Languid、Ponderous |
| **咬字** | Crisp, Glassy, Over-enunciated, Slurred, Lazy, Mumbled, Thick, Sibilant (hissing 'S'), Plosive (popping 'P'/'B') |
| **强调** | Billboard（砸出关键词）、Throwaway（弱化）、Punchy（高能量辅音） |
| **韵律** | Cadenced, Syncopated, Naturalistic, Flowing, Breathless, Erratic |

#### 规格与原型（角色模型）

| 类别 | 原型 |
|----------|-----------|
| **商业** | Hard-sell, Soft-sell, Everyman/Everywoman, Girl/Boy Next Door, Hip/Gen-Z, Millennial, Sophisticated/Luxury |
| **叙事** | The Trusted Advisor, The Sage/Professor, The Villain/Antagonist, The Sidekick, The Reluctant Hero |
| **技术/行业** | "Announcery"（经典电台）、"Voice of God"（低沉/洪亮/全知）、"Corporate/Industrial"（精致/专业）、"Sotto Voce"（舞台低语）、"Walla"（背景人群噪声） |

### 七、人声分类

下面第一条工作规则（"以音区开头"）使用的标准音区名称。每一个都定义了音域和音色特征。

#### 标准分类

| 声音类型 | 性别 | 音域（约） | 说明 |
|-----------|--------|-----------------|-------------|
| **Soprano** | 女 | C4 -- C6 | 最高的女声，以明亮、高扬的音质著称。 |
| **Mezzo-Soprano** | 女 | A3 -- A5 | 中音区的女声，音色比女高音更暗、更丰满。 |
| **Contralto** | 女 | F3 -- F5 | 最低的女声。非常罕见，共鸣低沉、有力。 |
| **Countertenor** | 男 | E3 -- E5 | 最高的男声，常用假声演唱与女低音或女中音相近的音域。 |
| **Tenor** | 男 | C3 -- C5 | 最高的"自然"男声类型，在歌剧里常担任英雄或主角。 |
| **Baritone** | 男 | A2 -- A4 | 最常见的男声类型，位于男高音与男低音之间，音色温暖、多面。 |
| **Bass** | 男 | E2 -- E4 | 最低的男声，为人声合奏提供低沉、厚重的基底。 |

#### 专门与补充分类

| 声音类型 | 说明 |
|-----------|-------------|
| **Alto** | 在合唱音乐里，"alto" 指较低的女声声部（SATB），通常由女中音或女低音演唱。 |
| **Bass-Baritone** | 兼具男低音的低频共鸣与男中音的较高音域的男声。 |
| **Treble** | 儿童（男孩和女孩）高而未见变化的声音。 |
| **Sopranista** | 罕见的男歌手，能不用假声唱到女高音音域。 |

---

## 第 2 部分：VoiceDesign 如何处理这些词

关于 VoiceDesign 模型如何理解声音描述的测试，把上面的词汇对应到模型的实际行为。

### 测试方法

- 所有测试在所有声音上都使用相同的 4 句中性句子
- `instruct` 字段留空，以隔离声音描述的效果
- `seed: -1`（随机），测试自然条件下的一致性
- 评估维度：跨行声音一致性、对描述的遵循程度、质量

### 第 1 轮：描述声音的五种方式

测试了 5 种根本不同的描述方式（各男/女，共 40 个样本）。

#### 策略

| 策略 | 方式 | 示例 |
|----------|----------|---------|
| **Anatomy** | 物理/声学特征 | "A deep male baritone with resonant chest voice, smooth timbre, slight vocal fry" |
| **Persona** | 角色身份 | "A kind elderly grandfather who speaks with patience and gentle authority" |
| **Technical** | 音频工程术语 | "A well-projected male voice with crisp diction, moderate pace, controlled dynamic range" |
| **Emotion** | 只写情绪特质 | "A calm, reassuring male voice that sounds confident, trustworthy, and steady" |
| **Context** | 场景/情境 | "A male narrator reading a fantasy novel aloud in a quiet bookshop" |

#### 结果

| 策略 | 一致性 | 关键观察 |
|----------|------------|-----------------|
| **Anatomy** | 最好（男）、差（女） | 物理描述给模型具体的声学目标。女性的 "light soprano, airy" 被推得过高/过薄。 |
| **Persona** | 不稳定 | 当原型强烈暗示声音类型时有效（年轻记者 -> 快而尖）。含义模糊时失败（祖父 -> 性别漂移）。 |
| **Technical** | 低到中 | 模型部分理解音频工程术语，但不可靠。 |
| **Emotion** | 低 | 太抽象。"平静"或"温暖"听起来是什么样，没有物理锚点。 |
| **Context** | 无 | 每一行都是不同的声音。模型把描述当作场景指示，而不是声音身份。 |

#### 结论

**以解剖（声学特征）为先的描述胜出。** 模型需要具体的声学目标（音高、音区、音色、质感）才能产生一致的声音。抽象特质（情绪、人设、情境）给了太多解释自由。

### 第 2 轮：先写声学特征

所有描述都重新组织为：音区 + 音色 + 语气形容词。不用演绎/表演词（那些属于 `instruct`）。

#### 公式

> **[音区] + [音色/质感描述词] + [语气特征形容词]**
>
> 绝不要包含：演绎动词（speaking、reading）、节奏词、场景/情境

#### 结果

| 声音 | 描述 | 一致性 | 质量 | 说明 |
|-------|------------|------------|---------|-------|
| **WARM_BARITONE** | "deep male baritone, rich chest resonance, warm smooth timbre, hint of gravelly texture" | 中高 | 高 | 与描述一致，低沉品质很好，有少量变化 |
| **GROUNDED_ALTO** | "female alto, full grounded mid-range, slightly husky, natural warmth" | 中高 | 高 | 很好地遵循描述，比 baritone 变化更少 |
| **BRIGHT_TENOR** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | 很低 | 高 | 情绪演绎失控，"unhinged" 的感觉——见 Bright Tenor 调查 |
| **SILKY_MEZZO** | "female mezzo-soprano, silky even tone, soft rounded edges, gentle clarity" | 很高 | 很高 | 整体最好。非常耐听。 |
| **COMMANDING_BASS** | "male bass, dark authoritative rumble, heavy low-end, firm tone" | 高 | 高 | 牛仔/西部片式的粗犷感。值得保留。 |
| **CRISP_SOPRANO** | "female soprano, precise crystalline tone, balanced brightness, confident projection" | 很高 | 高 | 语气变化更多但仍一致。有录音室级的感觉。 |

#### 哪些词能让声音保持一致

**高一致性**——模型能可靠锁定的术语：

- 平滑/均匀：`silky`、`even`、`soft`、`rounded`（来自 Smooth/Rich）
- 精确：`precise`、`crystalline`、`balanced`（暗示控制）
- 权威/坚定：`dark`、`authoritative`、`firm`、`commanding`（来自 Resonance）
- 扎实：`grounded`、`full`、`husky`（来自 Rough/Gritty + Resonance）

**高波动**——会让声音不稳定的术语：

- 能量：`bright`、`youthful energy`（来自 Brightness/Weight——见下面的调查）
- 单独使用的模糊温暖词：没有质感锚点的 `warm`、`deep`

**观察：**在所有描述下，女声都比男声更一致。

### Bright Tenor 调查

通过系统性消融（8 个变体、32 个样本）隔离出 BRIGHT_TENOR 不稳定的原因。

#### 变体

| 变体 | 描述 | 改动 |
|---------|------------|-------------|
| **CONTROL** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | 原始——基线 |
| **NO_BRIGHT** | "male tenor, clear tone, light nasal resonance, youthful energy" | 去掉 "bright" |
| **NO_ENERGY** | "male tenor, bright clear tone, light nasal resonance" | 去掉 "youthful energy" |
| **NEITHER** | "male tenor, clear tone, light nasal resonance" | 两者都去掉 |
| **BRIGHT_ONLY** | "A bright male tenor voice" | 极简——只有 "bright" |
| **SHARP_REPLACE** | "male tenor, sharp forward-placed tone, light nasal resonance, crisp articulation" | 用 "sharp" 替换 "bright" |
| **HIGH_HARMONICS** | "male tenor, strong upper harmonics, clear forward projection, light nasal resonance" | 技术性的声学等价表达 |
| **STABILIZED** | "male tenor, steady clean tone, light nasal resonance, even delivery" | 应用一致性公式 |

#### 结果

| 变体 | 一致性 | 表现 | 发现 |
|---------|------------|-----------|---------|
| **CONTROL** | 很低 | 失控、情绪大幅摇摆 | 印证了 R2 的表现 |
| **NO_BRIGHT** | 中 | 有表现力但跟随文本，失控减少 | "Bright" 带来情绪波动 |
| **NO_ENERGY** | 中 | 与 NO_BRIGHT 类似，在戏剧性文本上出现一次拍卖师式的节奏突增 | "Energy" 带来节奏突增 |
| **NEITHER** | 高 | 跟随文本线索，稳定 | 裸露的 tenor 基座本身就稳定 |
| **BRIGHT_ONLY** | 高 | 稳定，响应文本 | 单独用 "Bright" 没问题 |
| **SHARP_REPLACE** | 高 | 恼火/易怒的感觉 | "Sharp/forward-placed" 对应到烦躁的语气 |
| **HIGH_HARMONICS** | 中 | 提高音量、近乎喊叫，非常清晰 | 有用的细分场景：警觉/唤醒效果 |
| **STABILIZED** | 高 | 像旁白、情绪少 | 一致性公式有效，但会压平表现力 |

#### 关键发现：两个各自稳定的词一起用会不稳定

**"Bright" 和 "youthful energy" 各自单独使用都稳定，但一起用会互相破坏。** 这个组合给模型互相矛盾的信号——音调明亮（声学）+ 行为能量（表演）——每次生成都在两种解释之间乱撞。

这很可能是一条通用原则：**在声音描述里把声学描述词（第 I 节）与行为/能量描述词（第 III 节）混在一起会产生歧义。** 声音描述只保留在第 I 节的范围；第 II–IV 节的特质放到 `instruct` 里。

#### 具体词汇如何被理解

| 术语 | 小节 | 模型的解释 | 是否有用 |
|------|---------|---------------------|------------|
| `bright`（单独） | I. Brightness/Weight | 稳定的音调明亮 | 单独使用可以 |
| `bright` + 能量词 | I + III | 情绪混乱 | 避免组合 |
| `sharp` | I. Nasal/Sharp | 烦躁/易怒的语气 | 谨慎使用 |
| `upper harmonics` | I.（技术性） | 提高音量、近乎喊叫 | 细分场景：警觉效果 |
| `steady`、`even` | I. Smooth/Rich | 像旁白般稳定 | 会压平表现力 |
| `silky`、`rounded` | I. Smooth/Rich | 高一致性 + 耐听 | 最适合旁白 |
| `dark`、`authoritative` | I. Resonance | 粗犷/西部片感，稳定 | 适合男性权威感 |
| `crystalline`、`precise` | I. Smooth/Rich | 一致且有表现力范围 | 最佳平衡 |
| `hollow`（在 instruct 里） | I. Resonance | 低语/叹息混合，气息感的空洞 | 避免在 instruct 里用——模型会把它读成声学特征，而不是情绪。改用 "defeated"。 |

### 声音描述的工作规则

1. **以音区开头**（bass、baritone、tenor、alto、mezzo-soprano、soprano）
2. **加 2–3 个质感/音色词**，只来自第 I 节
3. **绝不要**在 description 字段里**混合声学（第 I 节）与行为（第 II–IV 节）描述词**
4. **用 `instruct` 表达演绎、情绪和节奏**——它就是为此存在的
5. 为了一致性，**优先用"控制"类词**（`silky`、`even`、`precise`、`firm`）而不是"能量"类词（`bright`、`sharp`、`piercing`）
6. **用模糊术语过度描述会叠加不确定性**——简短清晰的描述可以胜过冗长模糊的描述
7. 在这个模型里，**女声本身就更一致**，在所有描述风格下都是如此
