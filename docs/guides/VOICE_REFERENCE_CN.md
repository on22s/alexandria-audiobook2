[English](VOICE_REFERENCE.md) | [中文](VOICE_REFERENCE_CN.md) | [日本語](VOICE_REFERENCE_JA.md) | [Español](VOICE_REFERENCE_ES.md)

# 声音参考

一份综合性的声音表演术语表，汇编自专业表演术语词典和配音指导数据库，并结合了 Qwen3-TTS VoiceDesign 测试的实证发现。

## 本文档怎么用（从这里开始）

本文档分两部分：

### 第一部分：导演术语表——描述声音的词

这是一本描述声音听感的专业术语词典。在以下场合使用这些词：

1. **设计新声音**（Voice Designer 页）——从 **第 I 节：质地、音色与品质** 里选词
   - 例子：不要写 "a nice old man voice"，而写 "male baritone, warm chest resonance, hint of gravelly texture"

2. **指导角色的演绎**（Editor 页、`instruct` 字段）——从 **第 II–IV 节：情绪、演绎、原型** 里选词
   - 例子：不要写 "say it sadly"，而写 "mournful, breathy, slow pace, trembling"

### 第二部分：VoiceDesign 发现——什么才真正有效

这一部分记录了测试 AI 对各类声音描述跟随程度的实验。核心发现是：

> **先描述声音构造（anatomy）的写法效果最好。** 永远从声音的音区开始（男声用 bass/baritone/tenor，女声用 contralto/mezzo/soprano），然后加 2-3 个质感词。绝不要把"听起来怎么样"和"这个人举止如何"混在一起——AI 会糊涂。

### 快速示例：坏的声音描述 vs 好的声音描述

**坏：** "A nice grandpa who reads slowly"
- 太模糊，没有任何声学信息

**好：** "Male baritone, rich chest resonance, warm smooth timbre, hint of gravelly texture, slow deliberate pacing"
- 明确的音区（男中音 baritone）
- 物理品质（胸腔共鸣、沙砾质感）
- 演绎风格（缓慢从容的节奏）

**坏：** "An energetic young woman who's always excited"
- 是行为描述，不是声学描述

**好：** "Female mezzo-soprano, bright clear timbre, light breathy quality, upbeat rhyth delivery"
- 明确的音区（女中音 mezzo-soprano）
- 物理品质（明亮、带气声）
- 演绎风格（轻快有节奏）

### 一句话规则

1. **永远先给音区：** 男声 bass（男低音）/baritone（男中音）/tenor（男高音），女声 contralto（女低音）/mezzo（女中音）/soprano（女高音）
2. **再加 2-3 个质感词：** smooth（平滑）、gravelly（沙砾感）、breathy（气声）、rich（丰润）、thin（单薄）等
3. **再加 1 个演绎词：** slow（缓慢）、fast（快）、deliberate（从容）、rhythmic（有节奏）等
4. **绝不要把声学术语和行为术语混在一起：** "warm gravelly baritone"（温暖沙砾感的男中音）✓ 对；"friendly confident baritone"（友好自信的男中音）✗ 错
5. **情绪词写进 `instruct`，不要写进 `description`**

---

# 第一部分：导演声音术语表

数百个用于微调声音表演的行业标准术语，按功能组织。

## I. 质地、音色与品质（"乐器"）

声音的物理听感或"颗粒感"。**在 VoiceDesign 的 `description` 里使用这些词。**

| 类别 | 描述词 |
|----------|------------|
| **粗糙/沙砾感（Rough/Gritty）** | Gravelly（沙砾感）, Raspy（刺耳沙哑）, Husky（低沉沙哑）, Scratchy（刮擦感）, Smoky（烟熏感）, Guttural（喉音）, Coarse（粗糙）, Croaking（蛙鸣般）, Grating（磨擦刺耳）, Dry（干涩）, Cracked（破裂）, Fractured（碎裂）, Sandpaper（砂纸感）, Whiskey-soaked（酒嗓）, Growling（低吼）, Gruff（粗声粗气）, Hoarse（嘶哑）, Ragged（破布般粗粝）, Raw（原始未打磨）, Rusty（生锈般）, Strained（紧绷费力）, Throaty（喉部共鸣重） |
| **平滑/丰润（Smooth/Rich）** | Silky（丝滑）, Velvety（天鹅绒般）, Honeyed（蜜糖般）, Creamy（奶油般）, Liquid（流动感）, Orotund（圆润洪亮）, Dulcet（悦耳）, Mellifluous（甜美流畅）, Polished（打磨过的）, Lush（丰润）, Mellow（醇厚）, Buttery（黄油般）, Soothing（安抚）, Fluid（流畅）, Flowing（连绵）, Seamless（无缝）, Satiny（缎面般）, Suave（圆滑）, Unctuous（油滑） |
| **共鸣（Resonance）** | Booming（轰隆）, Chesty（胸腔共鸣）, Full-bodied（厚实）, Deep（低沉）, Sonorous（洪亮）, Thundering（雷鸣）, Rumbling（隆隆）, Hollow（空洞）, Echoing（带回声）, Cavernous（洞穴般）, Round（圆润）, Heavy（沉重）, Grounded（扎实）, Bassy（低频重）, Resonant（共鸣强）, Bell-like（钟铃般） |
| **轻/高（Light/High）** | Airy（轻盈飘逸）, Breathy（带气声）, Feathery（羽毛般）, Thin（单薄）, Reedy（芦苇般）, Tinny（金属薄片感）, Piercing（尖锐穿透）, Shrill（尖利）, Piping（笛声般）, Wispy（纤细飘忽）, Fluting（长笛般）, Chirpy（叽喳）, Squeaky（吱吱响）, Falsetto（假声）, Head-voice（头声）, Boyish（少年感）, Girlish（少女感）, Metallic（金属感）, Brittle（脆） |
| **鼻音/锐利（Nasal/Sharp）** | Nasal（鼻音）, Twangy（拨弦般带鼻喉）, Whiny（哀怨）, Pinched（挤压）, Brass（铜管般）, Strident（刺耳响亮）, Cutting（锋利）, Piercing（穿透）, Biting（尖刻）, Sharp（锐利）, Acidic（酸蚀）, Astringent（涩）, Clamorous（喧闹）, Ear-splitting（震耳）, Harsh（粗硬）, Jarring（刺耳不协调）, Penetrating（直穿）, Stingy（尖细扎人） |
| **发声缺陷（Vocal Defects）** | Fry (Vocal Fry)（气泡音）, Sibilant (whistling 'S')（咝音，S 音发哨）, Plosive (popping 'P')（爆破音，P 音爆麦）, Tremulous (shaking)（颤抖）, Wobbling（摇晃不稳）, Quavering（颤音）, Warbling（转音）, Slurred（含糊连音）, Mumbled（嘟囔）, Incoherent（含混不清）, Thick（厚重黏滞） |

## II. 情绪、态度与语气（"潜台词"）

用来调定心理状态的形容词。**在 `instruct` 里使用这些词，不要用在 `description` 里。**

| 类别 | 描述词 |
|----------|------------|
| **攻击/敌意（Aggressive/Hostile）** | Acerbic（尖酸）, Antagonistic（对抗）, Belligerent（好斗）, Biting（尖刻）, Brusque（唐突）, Caustic（刻薄）, Combative（好战）, Confrontational（挑衅）, Contemptuous（轻蔑）, Cutting（锋利）, Derisive（嘲弄）, Dictatorial（独裁）, Disdainful（不屑）, Dogmatic（武断）, Domineering（盛气凌人）, Explosive（爆发）, Fierce（凶猛）, Hard（强硬）, Imperious（专横）, Incendiary（煽动）, Indignant（愤慨）, Intimidating（威吓）, Malevolent（恶意）, Militant（激进）, Pugnacious（好斗）, Savage（野蛮）, Scornful（鄙夷）, Seething（憋着怒）, Sharp（锐利）, Spiteful（恶毒）, Stern（严厉）, Threatening（威胁）, Truculent（凶悍）, Vengeful（报复心重）, Venomous（恶毒）, Vitriolic（硫酸般刻毒）, Warlike（尚武） |
| **悲伤/低落（Sad/Depressed）** | Bereft（失怙般空落）, Bleak（荒凉）, Broken（破碎）, Crestfallen（垂头丧气）, Dejected（沮丧）, Desolate（孤寂）, Despondent（绝望）, Disheartened（灰心）, Dismal（阴郁）, Doleful（哀伤）, Dour（阴沉）, Forlorn（无望孤独）, Gloomy（阴郁）, Grief-stricken（悲恸）, Heartbroken（心碎）, Heavy-hearted（心情沉重）, Hollow（空洞）, Hopeless（绝望）, Lachrymose（泪汪汪）, Melancholy（忧郁）, Miserable（痛苦）, Morose（阴郁易怒）, Mournful（哀悼）, Pathetic（可怜）, Pessimistic（悲观）, Plaintive（哀诉）, Regretful（懊悔）, Remorseful（悔恨）, Resigned（认命）, Somber（肃穆）, Sorrowful（悲伤）, Tearful（含泪）, Tragic（悲剧）, Weepy（爱哭）, Wistful（怅惘）, Woe-begone（愁容满面） |
| **快乐/积极（Happy/Positive）** | Blissful（极乐）, Buoyant（轻快）, Celebratory（欢庆）, Cheerful（愉快）, Chipper（精神抖擞）, Contented（满足）, Delightful（讨喜）, Ebullient（热情四溢）, Ecstatic（狂喜）, Elated（兴高采烈）, Encouraging（鼓励）, Energetic（有活力）, Enthusiastic（热情）, Euphoric（欣快）, Exhilarated（兴奋）, Exuberant（充沛）, Festive（节日般）, Genial（和蔼）, Giddy（飘飘然）, Gleeful（欢欣）, Gratified（满意）, Hearty（爽朗）, Hopeful（充满希望）, Jaunty（轻快潇洒）, Jolly（快活）, Jovial（开朗）, Joyous（喜悦）, Jubilant（欢腾）, Lighthearted（轻松）, Merry（欢乐）, Optimistic（乐观）, Overjoyed（喜出望外）, Peppy（活泼）, Radiant（容光焕发）, Rapturous（狂喜入迷）, Reassured（安心）, Sunny（阳光）, Upbeat（昂扬）, Vibrant（有生机）, Vivacious（活泼）, Zestful（兴致勃勃） |
| **恐惧/焦虑（Fearful/Anxious）** | Agitated（烦躁）, Alarmed（惊惶）, Apprehensive（忐忑）, Cautious（谨慎）, Concerned（担忧）, Cowardly（怯懦）, Distressed（苦恼）, Edgy（神经绷紧）, Faint（微弱）, Frightened（害怕）, Guarded（戒备）, Hesitant（迟疑）, Hysterical（歇斯底里）, Insecure（没安全感）, Jittery（坐立不安）, Nervous（紧张）, Panic-stricken（惊恐）, Paralyzed（僵住）, Paranoid（疑惧）, Petrified（吓呆）, Quaking（发抖）, Restless（不安）, Scared（恐惧）, Shaky（发颤）, Skittish（易惊）, Spooked（受惊）, Startled（一惊）, Tense（紧绷）, Terrified（惊恐万分）, Timid（怯生生）, Trembling（颤抖）, Uneasy（不自在）, Unsettled（心神不宁）, Wary（提防）, Worried（忧虑） |
| **平静/中性（Calm/Neutral）** | Ambivalent（矛盾）, Apathetic（漠然）, Balanced（平衡）, Bland（平淡）, Blas&eacute;（无所谓）, Bored（无聊）, Casual（随意）, Collected（镇定）, Composed（从容）, Cool（冷静）, Detached（疏离）, Dispassionate（超然冷静）, Dull（沉闷）, Easygoing（随和）, Equable（平和）, Even-tempered（脾气温和）, Flat（平）, Imperturbable（不动声色）, Indifferent（冷淡）, Laid-back（松弛）, Listless（无精打采）, Matter-of-fact（就事论事）, Measured（有分寸）, Mediocre（平庸）, Mild（温和）, Moderate（适中）, Monotone（单调）, Nonchalant（满不在乎）, Objective（客观）, Pacifistic（平和）, Passive（被动）, Peaceful（宁静）, Phlegmatic（冷淡迟钝）, Placid（平静）, Relaxed（放松）, Serene（安详）, Steady（平稳）, Stoic（坚忍）, Tranquil（安静）, Unemotional（不带情绪）, Unflappable（处变不惊） |
| **亲爱/亲密（Affectionate/Intimate）** | Adoring（爱慕）, Amorous（多情）, Caring（关切）, Cherishing（珍惜）, Comforting（安慰）, Compassionate（富有同情）, Coquettish（娇媚）, Cuddly（亲昵）, Doting（溺爱）, Endearing（惹人怜爱）, Fond（亲爱）, Gentle（温柔）, Intimate（亲密）, Loving（充满爱意）, Maternal（母性）, Nurturing（呵护）, Paternal（父性）, Protective（保护）, Romantic（浪漫）, Seductive（诱惑）, Sensual（感官）, Soft（柔和）, Sultry（撩人低沉）, Sweet（甜美）, Sympathetic（体谅）, Tender（温存）, Touching（动人）, Warm（温暖）, Yearning（渴望） |
| **智性/优越（Intellectual/Superior）** | Analytical（分析式）, Arrogant（傲慢）, Authoritative（权威）, Condescending（居高临下）, Critical（挑剔）, Cynical（愤世）, Didactic（说教）, Dignified（庄重）, Dry（干巴）, Educated（有教养）, Erudite（博学）, Haughty（高傲）, Informative（信息型）, Inquisitive（好问）, Instructional（指导式）, Knowledgeable（内行）, Lofty（高蹈）, Logical（讲逻辑）, Patronizing（施恩般）, Pedantic（掉书袋）, Philosophical（哲思）, Pompous（浮夸）, Practical（务实）, Pragmatic（实用）, Precise（精确）, Pretentious（装腔）, Professional（专业）, Rational（理性）, Sagacious（睿智）, Sarcastic（挖苦）, Sardonic（冷嘲）, Satirical（讽刺）, Scholarly（学者气）, Skeptical（怀疑）, Smart（机敏）, Snobbish（势利）, Sophisticated（精致老练）, Supercilious（目中无人）, Superior（优越）, Thoughtful（深思）, Wise（明智）, Wry（苦笑式） |
| **俏皮/古怪（Playful/Quirky）** | Bantering（打趣）, Cheeky（没大没小）, Childish（孩子气）, Clownish（小丑般）, Droll（滑稽古怪）, Eccentric（古怪）, Facetious（乱开玩笑）, Fanciful（奇想）, Flippant（轻慢）, Flirty（调情）, Foolish（傻气）, Goofy（憨傻）, Humorous（幽默）, Impish（顽皮）, Ironic（反讽）, Irreverent（不敬）, Jocular（爱开玩笑）, Kooky（怪咖）, Mischievous（捣蛋）, Mocking（嘲仿）, Offbeat（另类）, Playful（俏皮）, Prankish（恶作剧）, Quirky（怪趣）, Rascally（无赖气）, Ridiculous（荒唐）, Sassy（泼辣）, Saucy（大胆轻佻）, Silly（傻乐）, Snarky（尖酸刻薄）, Teasing（逗弄）, Tongue-in-cheek（假正经）, Whimsical（奇思）, Witty（机智）, Zany（疯狂搞笑） |

## III. 演绎、语速与节奏（"机械部分"）

速度、流畅度和清晰度的控制。**在 `instruct` 里使用这些词，不要用在 `description` 里。**

| 风格 | 说明 |
|-------|------------|
| **Staccato（断奏）** | 短促、断开、有冲击力、被切断、突兀、机关枪式 |
| **Legato（连奏）** | 平滑、连贯、流动、滑行、持续、元音拉长 |
| **Rapid-fire（极速连发）** | 极快、急促、狂躁、拍卖师式、慌不择路、喘不上气 |
| **Measured（从容）** | 刻意、缓慢、有思量、每个词之间留空、沉重 |
| **Halting（断续）** | 犹疑、频繁停顿、结巴、不连贯、踉跄 |
| **Lilting（起伏如歌）** | 有节奏的起伏、音乐性、歌谣式、旋律化、跳跃 |
| **Drawl（拖腔）** | 懒洋洋地拉长元音、缓慢、放松、慵懒、把词抻开 |
| **Punchy（冲击式）** | 在关键词上高能强调、有动态、辅音打得重 |
| **Deadpan（冷面）** | 毫无起伏、平调、冷幽默、面瘫式演绎 |
| **Cadenced（有格律）** | 高度有节奏、近乎诗朗诵或莎剧、合拍子 |

## IV. 行业规格与原型（"角色"）

选角资料和广告脚本里用的行话。

| 原型 | 说明 |
|-----------|------------|
| **Authentic（真实感）** | 真人、非演员、纪录片风格、原始、未打磨 |
| **Announcer (Classic)（经典播音员）** | "Voice of God"（VOG，上帝之声）、展示式、洪亮、精致、电台 DJ |
| **Announcer (Non-Announcery)（去播音腔）** | 反推销、对话式、随手扔掉、漫不经心 |
| **Conversational（对话式）** | 就像跟朋友聊天、随意、字尾含糊、自然换气 |
| **Girl/Guy Next Door（邻家女孩/男孩）** | 有共鸣、亲切、中产、可信、普通、安全 |
| **Trusted Advisor（可信顾问）** | 医生、律师、家长、理财顾问、懂行、沉着 |
| **Cool Parent（酷家长）** | 现代、时髦、不严厉、善解人意、"Disney Mom/Dad" |
| **The Millennial/Gen Z（千禧/Z 世代）** | 气泡音、升调、随意、愤世、懂技术、语速快、反讽 |
| **Promo Voice（预告片嗓）** | 粗粝、低沉、低吼、强烈、耳语、高戏剧性、电影预告片 |
| **Corporate/Industrial（企业/工业）** | 清晰、咬字准、专业、中性、信息型、自信 |
| **Hard Sell（硬推销）** | 大音量、高能量、紧迫、喊、汽车销售腔、攻击性 |
| **Soft Sell（软推销）** | 亲密、气声、奢华、昂贵、抚慰、诱惑 |
| **Characters（角色）** | 反派、英雄、跟班、生物、怪物、机器人、外星人、女巫、老妪、智者 |

## V. 技术性现场指令（"工作流"）

关于录音过程的指令。

| 指令 | 含义 |
|---------|---------|
| **A-B-C (Three-in-a-row)（连读三遍）** | 用三种不同的态度把同一句读三遍 |
| **Billboard（招牌重音）** | 强调某个专名或产品（"品牌名再打重一点"） |
| **Button（收尾）** | 句子/这条录音干净利落地收住；稳稳落地 |
| **Clean Up（清理）** | 重录一句，修掉嘴部杂音或磕绊 |
| **Cold Read（冷读）** | 不预热、不排练，直接读脚本 |
| **Color（上色）** | 给某个词加更多情绪或个性 |
| **Compression（压缩）** | 缩小动态范围（安静处更响，响亮处更轻） |
| **Donut（甜甜圈）** | 首尾有人声，中间留空给音乐/素材 |
| **Eat the Mic（吃麦）** | 贴麦克风极近取近讲效应（低沉亲密的低频） |
| **Guide Track（参考轨）** | 对齐一段预先录好的音频/视频参考的时序 |
| **Lay Out（留空）** | 某一段停下不说话 |
| **Lead-in（带戏）** | 在正式台词前默念一句进入角色 |
| **Level（试音）** | 用表演音量读一段，以设定麦克风增益 |
| **Lift（提亮）** | 略微提亮或升调，常在广告结尾 |
| **Pick-up（补录）** | 只录某一特定的句子或短语 |
| **Popping（爆麦）** | P/B 音上气流过多（修法："稍微偏轴"） |
| **Projection（投射）** | 把声音送到房间后排，而不是收在内部 |
| **Punch-in（插入录音）** | 录一小段，插进已有的录音里 |
| **Safety（保险条）** | 最后再录一条，以防上一条好的有技术瑕疵 |
| **Slate（报板）** | 表演前报名字和条数（"John Doe, Take 1"） |
| **Smile（微笑）** | 读的时候真的微笑（音色会变亮变暖） |
| **Sotto Voce（低声）** | 舞台耳语；压着气说话 |
| **Throw Away（随口带过）** | 弱化一句，让它听起来不重要或很随意 |
| **Time（卡时长）** | 加速或减速以卡准严格时长（例如正好 29.5 秒） |
| **Wild Line（散录条）** | 脱离脚本流程单独录的一句（喊叫、笑声、哼声） |
| **Walla（人群底噪）** | 由一群人表演的背景人群噪声/嗡鸣 |

## VI. 扩展维度参考

涵盖五个关键领域的 150+ 个额外专业术语。

### 维度（物理性与氛围）

| 维度 | 术语 |
|-----------|-------|
| **在场感（Presence）** | Close-mic（近讲）, Distant（遥远）, Intimate（亲密）, Expansive（开阔）, Internalized（内在化）, Projection（投射） |
| **质地（Texture）** | Silky（丝滑）, Velvety（天鹅绒般）, Honeyed（蜜糖般）, Mellifluous（甜美流畅）, Buttery（黄油般）, Creamy（奶油般）, Lush（丰润）, Liquid（流动）, Orotund（圆润洪亮）, Resonant（共鸣强） |
| **沙砾感（Grit）** | Gravelly（沙砾感）, Raspy（刺耳沙哑）, Smoky（烟熏感）, Husky（低沉沙哑）, Guttural（喉音）, Scratchy（刮擦感）, Coarse（粗糙）, Croaking（蛙鸣般）, Grating（磨擦刺耳）, Dry（干涩）, Cracked（破裂）, Sandpaper（砂纸感）, Whiskey-soaked（酒嗓）, Growling（低吼）, Gruff（粗声粗气）, Hoarse（嘶哑）, Ragged（破布般粗粝）, Raw（原始未打磨）, Rusty（生锈般）, Strained（紧绷费力）, Throaty（喉部共鸣重） |
| **亮度/重量（Brightness/Weight）** | Airy（轻盈飘逸）, Breathy（带气声）, Feathery（羽毛般）, Thin（单薄）, Reedy（芦苇般）, Tinny（金属薄片感）, Piercing（尖锐穿透）, Shrill（尖利）, Piping（笛声般）, Wispy（纤细飘忽）, Fluting（长笛般）, Chirpy（叽喳）, Squeaky（吱吱响）, Metallic（金属感）, Brittle（脆） |
| **温度（Temperature）** | Warm（温暖）, Sunny（阳光）, Nurturing（呵护）, Genial（和蔼）, Toast-like（烘烤般）, Cool（冷）, Detached（疏离）, Clinical（临床般冷静）, Crisp（清脆）, Distant（遥远）, Icy（冰冷）, Steel（钢铁般） |

### 情绪（心理意图）

| 类别 | 术语 |
|----------|-------|
| **高能量/积极（High Energy/Positive）** | Jubilant（欢腾）, Exuberant（充沛）, Ebullient（热情四溢）, Cheerful（愉快）, Playful（俏皮）, Whimsical（奇思）, Contented（满足）, Blissful（极乐）, Optimistic（乐观）, Encouraging（鼓励）, Radiant（容光焕发）, Rapturous（狂喜入迷）, Peppy（活泼）, Jaunty（轻快潇洒）, Jolly（快活）, Jovial（开朗） |
| **高能量/消极（High Energy/Negative）** | Indignant（愤慨）, Seething（憋着怒）, Acerbic（尖酸）, Strident（刺耳响亮）, Sharp（锐利）, Biting（尖刻）, Vitriolic（硫酸般刻毒）, Threatening（威胁）, Savage（野蛮）, Belligerent（好斗）, Antagonistic（对抗）, Caustic（刻薄）, Combative（好战）, Confrontational（挑衅）, Contemptuous（轻蔑）, Derisive（嘲弄） |
| **低能量/消极（Low Energy/Negative）** | Melancholy（忧郁）, Despondent（绝望）, Wistful（怅惘）, Morose（阴郁易怒）, Tearful（含泪）, Heavy-hearted（心情沉重）, Hollow（空洞）, Defeated（被击垮）, Grief-stricken（悲恸）, Bereft（失怙般空落）, Bleak（荒凉）, Broken（破碎）, Crestfallen（垂头丧气）, Dejected（沮丧）, Desolate（孤寂）, Disheartened（灰心）, Dismal（阴郁）, Doleful（哀伤）, Dour（阴沉）, Forlorn（无望孤独） |
| **脆弱（Vulnerability）** | Jittery（坐立不安）, Tremulous（颤抖）, Quavering（颤音）, Shaky（发颤）, Hysterical（歇斯底里）, Panic-stricken（惊恐）, Hesitant（迟疑）, Guarded（戒备）, Tense（紧绷）, Agitated（烦躁）, Alarmed（惊惶）, Apprehensive（忐忑）, Faint（微弱）, Frightened（害怕）, Skittish（易惊）, Uneasy（不自在） |
| **权威/力量（Authority/Power）** | Commanding（有威势）, Imperious（专横）, Dictatorial（独裁）, Regal（王者气）, Stately（庄严）, Assertive（坚定自信）, Dogmatic（武断）, Unyielding（不屈）, Matter-of-fact（就事论事）, Dignified（庄重）, Pompous（浮夸）, Supercilious（目中无人）, Sagacious（睿智） |
| **反讽/机智（Irony/Wit）** | Sarcastic（挖苦）, Ironic（反讽）, Wry（苦笑式）, Cynical（愤世）, Sardonic（冷嘲）, Satirical（讽刺）, Facetious（乱开玩笑）, Flippant（轻慢）, Sassy（泼辣）, Saucy（大胆轻佻）, Cheeky（没大没小）, Bantering（打趣）, Droll（滑稽古怪） |

### 音高与音乐性（旋律）

| 方面 | 术语 |
|--------|-------|
| **音域（Range）** | Bass-heavy（低频重）, Mid-range（中音区）, High-frequency（高频）, Falsetto（假声）, Head-voice（头声）, Chest-voice（胸声） |
| **走向（Movement）** | Uptalk（升调：音高上扬，表示不确定）, Down-talk（降调：音高下落，表示终结/权威）, Singsong（歌谣式：童谣般的起伏）, Flat/Monotone（平/单调：毫无变化）, Modulated（有调节：受控的音高变化） |
| **发声区（Register）** | Vocal Fry（气泡音，嘎吱声）, Nasal（鼻音）, Twangy（拨弦感）, Resonance（共鸣）, Vibrato（颤吟）, Tremolo（颤音） |

### 演绎与机械（语速与清晰度）

| 方面 | 术语 |
|--------|-------|
| **速度（Tempo）** | Staccato（断奏、切分短促）, Legato（连奏、流动）, Rapid-fire（极速连发）, Measured（从容）, Deliberate（刻意）, Halting（断续）, Drawling（拖腔）, Languid（慵懒）, Ponderous（沉重） |
| **咬字（Articulation）** | Crisp（清脆）, Glassy（玻璃般）, Over-enunciated（过度咬字）, Slurred（含糊）, Lazy（懒散）, Mumbled（嘟囔）, Thick（黏滞）, Sibilant（咝音，S 发嘶）, Plosive（爆破音，P/B 爆麦） |
| **强调（Emphasis）** | Billboard（招牌重音：把关键词砸出来）, Throwaway（随口带过：弱化）, Punchy（冲击式：辅音高能） |
| **节奏（Rhythm）** | Cadenced（有格律）, Syncopated（切分）, Naturalistic（自然）, Flowing（流动）, Breathless（喘息）, Erratic（无规律） |

### 规格与原型（角色模型）

| 类别 | 原型 |
|----------|-----------|
| **商业（Commercial）** | Hard-sell（硬推销）, Soft-sell（软推销）, Everyman/Everywoman（普通人）, Girl/Boy Next Door（邻家女孩/男孩）, Hip/Gen-Z（潮人/Z 世代）, Millennial（千禧一代）, Sophisticated/Luxury（精致/奢华） |
| **叙事（Narrative）** | The Trusted Advisor（可信顾问）, The Sage/Professor（智者/教授）, The Villain/Antagonist（反派/对手）, The Sidekick（跟班）, The Reluctant Hero（不情愿的英雄） |
| **技术/工业（Technical/Industry）** | "Announcery"（经典电台腔）, "Voice of God"（上帝之声：低沉、洪亮、无所不知）, "Corporate/Industrial"（企业/工业：精致、专业）, "Sotto Voce"（舞台耳语）, "Walla"（背景人群噪声） |

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
| **Anatomy（声音构造式）** | 物理/声学特征 | "A deep male baritone with resonant chest voice, smooth timbre, slight vocal fry" |
| **Persona（人设式）** | 角色身份 | "A kind elderly grandfather who speaks with patience and gentle authority" |
| **Technical（技术式）** | 音频工程术语 | "A well-projected male voice with crisp diction, moderate pace, controlled dynamic range" |
| **Emotion（情绪式）** | 只讲情绪品质 | "A calm, reassuring male voice that sounds confident, trustworthy, and steady" |
| **Context（场景式）** | 场景/情境 | "A male narrator reading a fantasy novel aloud in a quiet bookshop" |

### 结果

| 策略 | 一致性 | 关键观察 |
|----------|------------|-----------------|
| **Anatomy** | 男声最好，女声差 | 物理描述给了模型具体的声学目标。女声的 "light soprano, airy" 被推得太高、太薄。 |
| **Persona** | 不稳定 | 当原型强烈暗示某种音色时有效（年轻记者 → 又快又尖）。含糊时就失效（祖父 → 性别都变了）。 |
| **Technical** | 低到中 | 模型对音频工程术语只理解一部分，不可靠。 |
| **Emotion** | 低 | 太抽象。对 "calm" 或 "warm" 听起来是什么样，没有任何物理锚点。 |
| **Context** | 无 | 每一行都是不同的声音。模型把描述当成了场景指导，而不是声音身份。 |

### 第一轮结论

**先描述声音构造的写法胜出。** 模型需要具体的声学目标（音高、音区、音色、质地）才能产出稳定的声音。抽象的品质（情绪、人设、场景）给了它太多解释空间。

## 第二轮：声音构造优先的混合写法

所有描述都改写为：音区 + 音色 + 语气形容词。不含演绎/表演类的词（那些属于 `instruct`）。

### 公式

> **[音区 register] + [音色/质地描述词] + [语气性格形容词]**
>
> 绝不包含：演绎类动词（speaking、reading）、语速词、场景/上下文

### 结果

| 声音 | 描述 | 一致性 | 质量 | 备注 |
|-------|------------|------------|---------|-------|
| **WARM_BARITONE** | "deep male baritone, rich chest resonance, warm smooth timbre, hint of gravelly texture" | 中高 | 高 | 声音和描述一致，低音品质很好，略有波动 |
| **GROUNDED_ALTO** | "female alto, full grounded mid-range, slightly husky, natural warmth" | 中高 | 高 | 对描述跟随得好，比男中音更稳 |
| **BRIGHT_TENOR** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | 很低 | 高 | 情绪演绎狂野，有 "失常" 感——见 Bright Tenor 调查 |
| **SILKY_MEZZO** | "female mezzo-soprano, silky even tone, soft rounded edges, gentle clarity" | 很高 | 很高 | 整体最好。听起来极其舒服。 |
| **COMMANDING_BASS** | "male bass, dark authoritative rumble, heavy low-end, firm tone" | 高 | 高 | 牛仔/西部片式的粗犷。值得留用。 |
| **CRISP_SOPRANO** | "female soprano, precise crystalline tone, balanced brightness, confident projection" | 很高 | 高 | 音色范围更大但仍稳定。有录音棚质感。 |

### 一致性的驱动因素（来自第 I 节的术语）

**高一致性**——模型能可靠抓住的词：
- 平滑/均匀：`silky`（丝滑）、`even`（均匀）、`soft`（柔和）、`rounded`（圆润）（来自 Smooth/Rich）
- 精确：`precise`（精确）、`crystalline`（水晶般）、`balanced`（平衡）（暗示受控）
- 权威/坚定：`dark`（暗色）、`authoritative`（权威）、`firm`（坚定）、`commanding`（有威势）（来自 Resonance）
- 扎实：`grounded`（扎实）、`full`（厚实）、`husky`（低沉沙哑）（来自 Rough/Gritty + Resonance）

**高波动**——会让结果不稳定的词：
- 能量：`bright`（明亮）、`youthful energy`（年轻的活力）（来自 Brightness/Weight——见下方调查）
- 单纯的模糊暖意：`warm`（温暖）、`deep`（低沉），没有锚定的质感词

**观察：** 在所有描述下，女声的一致性总体优于男声。

## Bright Tenor 调查

通过系统性消融（8 个变体，32 个样本），隔离出 BRIGHT_TENOR 不稳定的原因。

### 测试矩阵

| 变体 | 描述 | 改了什么 |
|---------|------------|-------------|
| **CONTROL** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | 原始版本——基线 |
| **NO_BRIGHT** | "male tenor, clear tone, light nasal resonance, youthful energy" | 去掉了 "bright" |
| **NO_ENERGY** | "male tenor, bright clear tone, light nasal resonance" | 去掉了 "youthful energy" |
| **NEITHER** | "male tenor, clear tone, light nasal resonance" | 两个都去掉 |
| **BRIGHT_ONLY** | "A bright male tenor voice" | 最小化——只有 "bright" |
| **SHARP_REPLACE** | "male tenor, sharp forward-placed tone, light nasal resonance, crisp articulation" | 用 "sharp" 替换 "bright" |
| **HIGH_HARMONICS** | "male tenor, strong upper harmonics, clear forward projection, light nasal resonance" | 技术性的声学等价说法 |
| **STABILIZED** | "male tenor, steady clean tone, light nasal resonance, even delivery" | 套用一致性公式 |

### 结果

| 变体 | 一致性 | 角色感 | 发现 |
|---------|------------|-----------|---------|
| **CONTROL** | 很低 | 失常，情绪大幅摇摆 | 确认了第二轮的观察 |
| **NO_BRIGHT** | 中 | 有表现力但受文本引导，没那么失常 | "bright" 贡献了情绪上的易变 |
| **NO_ENERGY** | 中 | 与 NO_BRIGHT 类似，在戏剧性文本上出现一次拍卖师式的语速飙升 | "energy" 贡献了节奏上的突进 |
| **NEITHER** | 高 | 跟着文本线索走，稳定 | 光秃秃的男高音基座本身就稳定 |
| **BRIGHT_ONLY** | 高 | 稳定，随文本响应 | 单用 "bright" 没问题 |
| **SHARP_REPLACE** | 高 | 带烦躁/易怒的品质 | "sharp/forward-placed" 映射成了恼怒的语气 |
| **HIGH_HARMONICS** | 中 | 提高音量，近乎喊，非常清楚 | 有用的细分场景：警醒/叫醒效果 |
| **STABILIZED** | 高 | 像旁白，情绪很淡 | 一致性公式有效，但把表现力压平了 |

### 关键发现：交互效应

**"bright" 和 "youthful energy" 单独用都稳定，合在一起却会互相破坏。** 这个组合给了模型互相矛盾的信号——音色上的明亮（声学）+ 行为上的活力（表演）——于是它每次生成都在两种解释之间摇摆。

这很可能是条通用原则：**在声音描述里把声学描述词（第 I 节）和行为/能量描述词（第 III 节）混在一起，会产生歧义。** 声音描述要纯粹待在第 I 节的范围内；第 II–IV 节的品质放到 `instruct` 里。

### 逐词映射

| 词 | 节 | 模型的解释 | 有用程度 |
|------|---------|---------------------|------------|
| `bright`（单用） | I. 亮度/重量 | 稳定的音色亮度 | 单独用没问题 |
| `bright` + 能量类词 | I + III | 情绪混乱 | 避免组合 |
| `sharp` | I. 鼻音/锐利 | 烦躁/易怒的语气 | 谨慎使用 |
| `upper harmonics` | I.（技术类） | 音量提高，近乎喊叫 | 细分场景：警醒效果 |
| `steady`、`even` | I. 平滑/丰润 | 旁白般的稳定 | 会压平表现力 |
| `silky`、`rounded` | I. 平滑/丰润 | 高一致性 + 耐听 | 叙述的最佳选择 |
| `dark`、`authoritative` | I. 共鸣 | 粗犷/西部片质感，稳定 | 适合男性权威 |
| `crystalline`、`precise` | I. 平滑/丰润 | 稳定且保有表现力范围 | 平衡最好 |
| `hollow`（写在 instruct 里） | I. 共鸣 | 耳语/叹息的混合，带气声的空洞 | 别用在 instruct 里——模型会按声学理解，而不是情绪。改用 "defeated"（被击垮）。 |

## 声音描述提示词的工作规则

1. **先给音区**（bass、baritone、tenor、alto、mezzo-soprano、soprano）
2. **再加 2-3 个只来自第 I 节的质感/音色词**
3. **绝不要在 description 字段里把声学描述词（第 I 节）和行为描述词（第 II–IV 节）混用**
4. **演绎、情绪和语速写进 `instruct`**——它就是干这个的
5. **为了稳定，优先用"控制类"词**（`silky`、`even`、`precise`、`firm`），而不是"能量类"词（`bright`、`sharp`、`piercing`）
6. **用模糊的词过度指定会叠加不确定性**——简短而清晰的描述可以胜过详细但含糊的描述
7. **在这个模型里，女声天生更一致**，无论用哪种描述风格
