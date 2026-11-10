[English](VOICE_REFERENCE.md) | [中文](VOICE_REFERENCE_CN.md) | 日本語

# 声のリファレンス

プロの演技用語集やボイスオーバーの演出データベースからまとめた、声の演技指示の語彙と、Qwen3-TTS の VoiceDesign を試して得た実測の知見を合わせたものです。

## この文書の使い方（ここから）

この文書は 2 つの部分からなります。

### パート 1：演出家の語彙 — 声を描写する言葉

声の響きを描写するための専門用語の辞書です。次の場面で使います。

1. **新しい声を設計する**（Voice Designer タブ） — **セクション I：質感、音色、品質**から言葉を選びます。
   - 例：「a nice old man voice」ではなく「male baritone, warm chest resonance, hint of gravelly texture」と書きます。

2. **登場人物の演技を指示する**（Editor タブ、`instruct` 欄） — **セクション II〜IV：感情、演技、類型**から言葉を選びます。
   - 例：「say it sadly」ではなく「mournful, breathy, slow pace, trembling」と書きます。

### パート 2：VoiceDesign の知見 — 実際にうまくいくもの

この部分は、AI がさまざまな種類の声の説明をどれだけうまく扱うかを試した実験を記録しています。主な知見は次のとおりです。

> **解剖学的な説明が最もよく効きます。** 必ず声域（男性なら bass/baritone/tenor、女性なら contralto/mezzo/soprano）から始め、次に質感の言葉を 2〜3 個加えます。「どう響くか」と「その人がどう振る舞うか」を混ぜてはいけません。AI が混乱します。

### 簡単な例：悪い説明と良い説明

**悪い：**「A nice grandpa who reads slowly」
- 曖昧すぎて、音響的な情報がありません。

**良い：**「Male baritone, rich chest resonance, warm smooth timbre, hint of gravelly texture, slow deliberate pacing」
- 具体的な声域（baritone）
- 物理的な性質（胸の共鳴、ざらついた質感）
- 話し方のスタイル（ゆっくりと意図的なテンポ）

**悪い：**「An energetic young woman who's always excited」
- 行動的で、音響的ではありません。

**良い：**「Female mezzo-soprano, bright clear timbre, light breathy quality, upbeat rhyth delivery」
- 具体的な声域（mezzo-soprano）
- 物理的な性質（明るい、息混じり）
- 話し方のスタイル（陽気でリズミカル）

### 要点

1. **必ず声域から始める：** bass/baritone/tenor（男性）、contralto/mezzo/soprano（女性）
2. **質感の言葉を 2〜3 個加える：** smooth、gravelly、breathy、rich、thin など
3. **話し方の言葉を 1 個加える：** slow、fast、deliberate、rhythmic など
4. **音響的な言葉と行動的な言葉を混ぜない：**「warm gravelly baritone」✓ 対「friendly confident baritone」✗
5. **感情の言葉は `description` ではなく `instruct` に書く**

---

# パート 1：演出家の声の語彙

声の演技を細かく調整するための、業界標準の用語を機能別にまとめたものです。

## I. 質感、音色、品質（「楽器」）

声の物理的な響きや「粒」です。**VoiceDesign の `description` ではこれらを使います。**

| 分類 | 記述語 |
|----------|------------|
| **粗い／ざらつく** | Gravelly, Raspy, Husky, Scratchy, Smoky, Guttural, Coarse, Croaking, Grating, Dry, Cracked, Fractured, Sandpaper, Whiskey-soaked, Growling, Gruff, Hoarse, Ragged, Raw, Rusty, Strained, Throaty |
| **滑らか／豊か** | Silky, Velvety, Honeyed, Creamy, Liquid, Orotund, Dulcet, Mellifluous, Polished, Lush, Mellow, Buttery, Soothing, Fluid, Flowing, Seamless, Satiny, Suave, Unctuous |
| **共鳴** | Booming, Chesty, Full-bodied, Deep, Sonorous, Thundering, Rumbling, Hollow, Echoing, Cavernous, Round, Heavy, Grounded, Bassy, Resonant, Bell-like |
| **軽い／高い** | Airy, Breathy, Feathery, Thin, Reedy, Tinny, Piercing, Shrill, Piping, Wispy, Fluting, Chirpy, Squeaky, Falsetto, Head-voice, Boyish, Girlish, Metallic, Brittle |
| **鼻にかかる／鋭い** | Nasal, Twangy, Whiny, Pinched, Brass, Strident, Cutting, Piercing, Biting, Sharp, Acidic, Astringent, Clamorous, Ear-splitting, Harsh, Jarring, Penetrating, Stingy |
| **声の欠陥** | Fry (Vocal Fry), Sibilant (whistling 'S'), Plosive (popping 'P'), Tremulous (shaking), Wobbling, Quavering, Warbling, Slurred, Mumbled, Incoherent, Thick |

## II. 感情、態度、トーン（「サブテキスト」）

心理状態を合わせるための形容詞です。**`description` ではなく `instruct` で使います。**

| 分類 | 記述語 |
|----------|------------|
| **攻撃的／敵対的** | Acerbic, Antagonistic, Belligerent, Biting, Brusque, Caustic, Combative, Confrontational, Contemptuous, Cutting, Derisive, Dictatorial, Disdainful, Dogmatic, Domineering, Explosive, Fierce, Hard, Imperious, Incendiary, Indignant, Intimidating, Malevolent, Militant, Pugnacious, Savage, Scornful, Seething, Sharp, Spiteful, Stern, Threatening, Truculent, Vengeful, Venomous, Vitriolic, Warlike |
| **悲しい／落ち込んだ** | Bereft, Bleak, Broken, Crestfallen, Dejected, Desolate, Despondent, Disheartened, Dismal, Doleful, Dour, Forlorn, Gloomy, Grief-stricken, Heartbroken, Heavy-hearted, Hollow, Hopeless, Lachrymose, Melancholy, Miserable, Morose, Mournful, Pathetic, Pessimistic, Plaintive, Regretful, Remorseful, Resigned, Somber, Sorrowful, Tearful, Tragic, Weepy, Wistful, Woe-begone |
| **幸せ／前向き** | Blissful, Buoyant, Celebratory, Cheerful, Chipper, Contented, Delightful, Ebullient, Ecstatic, Elated, Encouraging, Energetic, Enthusiastic, Euphoric, Exhilarated, Exuberant, Festive, Genial, Giddy, Gleeful, Gratified, Hearty, Hopeful, Jaunty, Jolly, Jovial, Joyous, Jubilant, Lighthearted, Merry, Optimistic, Overjoyed, Peppy, Radiant, Rapturous, Reassured, Sunny, Upbeat, Vibrant, Vivacious, Zestful |
| **恐れ／不安** | Agitated, Alarmed, Apprehensive, Cautious, Concerned, Cowardly, Distressed, Edgy, Faint, Frightened, Guarded, Hesitant, Hysterical, Insecure, Jittery, Nervous, Panic-stricken, Paralyzed, Paranoid, Petrified, Quaking, Restless, Scared, Shaky, Skittish, Spooked, Startled, Tense, Terrified, Timid, Trembling, Uneasy, Unsettled, Wary, Worried |
| **穏やか／中立** | Ambivalent, Apathetic, Balanced, Bland, Blas&eacute;, Bored, Casual, Collected, Composed, Cool, Detached, Dispassionate, Dull, Easygoing, Equable, Even-tempered, Flat, Imperturbable, Indifferent, Laid-back, Listless, Matter-of-fact, Measured, Mediocre, Mild, Moderate, Monotone, Nonchalant, Objective, Pacifistic, Passive, Peaceful, Phlegmatic, Placid, Relaxed, Serene, Steady, Stoic, Tranquil, Unemotional, Unflappable |
| **情愛／親密** | Adoring, Amorous, Caring, Cherishing, Comforting, Compassionate, Coquettish, Cuddly, Doting, Endearing, Fond, Gentle, Intimate, Loving, Maternal, Nurturing, Paternal, Protective, Romantic, Seductive, Sensual, Soft, Sultry, Sweet, Sympathetic, Tender, Touching, Warm, Yearning |
| **知的／上から目線** | Analytical, Arrogant, Authoritative, Condescending, Critical, Cynical, Didactic, Dignified, Dry, Educated, Erudite, Haughty, Informative, Inquisitive, Instructional, Knowledgeable, Lofty, Logical, Patronizing, Pedantic, Philosophical, Pompous, Practical, Pragmatic, Precise, Pretentious, Professional, Rational, Sagacious, Sarcastic, Sardonic, Satirical, Scholarly, Skeptical, Smart, Snobbish, Sophisticated, Supercilious, Superior, Thoughtful, Wise, Wry |
| **遊び心／風変わり** | Bantering, Cheeky, Childish, Clownish, Droll, Eccentric, Facetious, Fanciful, Flippant, Flirty, Foolish, Goofy, Humorous, Impish, Ironic, Irreverent, Jocular, Kooky, Mischievous, Mocking, Offbeat, Playful, Prankish, Quirky, Rascally, Ridiculous, Sassy, Saucy, Silly, Snarky, Teasing, Tongue-in-cheek, Whimsical, Witty, Zany |

## III. 演技、テンポ、リズム（「仕組み」）

速さ、流れ、明瞭さの調整です。**`description` ではなく `instruct` で使います。**

| スタイル | 説明 |
|-------|------------|
| **Staccato** | 短く、切り離され、パンチがあり、切り詰めた、唐突な、機関銃のような |
| **Legato** | 滑らかで、つながり、流れるようで、滑るように、連続し、母音が伸びる |
| **Rapid-fire** | 目にも留まらぬ速さ、切迫した、熱狂的な、競売人のような、慌ただしい、息もつかず |
| **Measured** | 意図的で、ゆっくり、思慮深く、一語一語間を置く、重々しい |
| **Halting** | 不確かで、頻繁に止まり、どもり、途切れ途切れで、つまずく |
| **Lilting** | リズミカルな上げ下げ、音楽的、歌うようで、旋律的、弾む |
| **Drawl** | なまけたように母音を伸ばし、ゆっくりで、くつろいだ、物憂げで、言葉を引き延ばす |
| **Punchy** | 重要な単語を高エネルギーで強調し、力強く、子音を強く打つ |
| **Deadpan** | 抑揚がまったくなく、平板な音程、乾いた喜劇、無表情な語り |
| **Cadenced** | 非常にリズミカルで、ほとんど詩的かシェイクスピア風、拍子がある |

## IV. 業界の仕様と類型（「役柄」）

キャスティングの内訳や広告の台本で使われる略語です。

| 類型 | 説明 |
|-----------|------------|
| **Authentic** | 実在の人物、俳優でない人、ドキュメンタリー風、生の、洗練されていない |
| **Announcer (Classic)** | 「Voice of God」（VOG）、プレゼン的、とどろく、洗練された、ラジオ DJ |
| **Announcer (Non-Announcery)** | 売り込まない、会話的、軽く流す、無関心 |
| **Conversational** | 友人と話すだけ、くだけた、語尾が崩れる、自然な息づかい |
| **Girl/Guy Next Door** | 親しみやすい、友好的、中流、信頼できる、普通の、安心できる |
| **Trusted Advisor** | 医師、弁護士、親、ファイナンシャルプランナー、知識豊富、穏やか |
| **Cool Parent** | 現代的、流行に敏感、厳しくない、理解がある、「Disney Mom/Dad」 |
| **The Millennial/Gen Z** | 声のフライ、上がり調子、くだけた、皮肉的、技術に詳しい、速い、アイロニー |
| **Promo Voice** | ざらついた、低い、唸る、強烈、ささやく、大げさ、映画の予告編 |
| **Corporate/Industrial** | 明瞭、歯切れよい、プロ的、中立的、情報的、自信がある |
| **Hard Sell** | 大きな音量、高いエネルギー、切迫、叫ぶ、「Car Dealer」、攻撃的 |
| **Soft Sell** | 親密、ささやくよう、高級、値の張る、心地よい、誘惑的 |
| **Characters** | Villain, Hero, Sidekick, Creature, Monster, Robot, Alien, Witch, Old Crone, Sage |

## V. 録音現場の技術的な指示（「ワークフロー」）

録音の過程への指示です。

| 指示 | 意味 |
|---------|---------|
| **A-B-C (Three-in-a-row)** | 同じ行を 3 つの異なる態度で 3 回読む |
| **Billboard** | 特定の名前や製品を強調する（「ブランド名をもっと強く」） |
| **Button** | 文やテイクをきれいに断定的に終える。着地を決める |
| **Clean Up** | 口の音やつまずきを直すために行を録り直す |
| **Cold Read** | 練習なしで台本をすぐに読む |
| **Color** | 特定の単語にもっと感情や個性を加える |
| **Compression** | ダイナミックレンジを狭める（静かな部分を大きく、大きな部分を小さく） |
| **Donut** | 最初と最後に声があり、中間に音楽やクリップ用の穴がある |
| **Eat the Mic** | 近接効果のためにマイクに非常に近づく（深く親密な低音） |
| **Guide Track** | 録音済みの音声や映像の参照のタイミングに合わせる |
| **Lay Out** | ある区間で話すのをやめる、間を置く |
| **Lead-in** | 最初の行の前に、役に入るため無言で言う文 |
| **Level** | マイクのゲインを決めるために本番の音量で読む |
| **Lift** | 明るく、または音程を少し上げる。しばしば広告の末尾で |
| **Pick-up** | 特定の 1 文やフレーズだけを録音する |
| **Popping** | 「P」「B」の音で余分な空気が出る（対処：「少し軸を外す」） |
| **Projection** | 部屋の後ろまで声を届かせるか、内側に保つか |
| **Punch-in** | 既存のテイクに挿入する小さな断片を録音する |
| **Safety** | 直前の良いテイクに技術的な問題があった場合のための最後の 1 回 |
| **Slate** | 演技の前に名前とテイク番号を言う（「John Doe, Take 1」） |
| **Smile** | 読みながら実際に笑顔になる（明るく温かいトーンに変わる） |
| **Sotto Voce** | 舞台のささやき。息を潜めて話す |
| **Throw Away** | 行を強調しない。重要でない、あるいは何気なく聞こえるようにする |
| **Time** | 厳密な長さ（たとえばちょうど 29.5 秒）に合わせて速めたり遅めたりする |
| **Wild Line** | 台本の流れから離れて別に録る行（叫び、笑い、うなり） |
| **Walla** | 一群で演じる背景の群衆のざわめき |

## VI. 拡張された次元のリファレンス

5 つの主要な領域にわたる、150 以上の追加の専門用語です。

### 次元（身体性と雰囲気）

| 次元 | 用語 |
|-----------|-------|
| **Presence** | Close-mic, Distant, Intimate, Expansive, Internalized, Projection |
| **Texture** | Silky, Velvety, Honeyed, Mellifluous, Buttery, Creamy, Lush, Liquid, Orotund, Resonant |
| **Grit** | Gravelly, Raspy, Smoky, Husky, Guttural, Scratchy, Coarse, Croaking, Grating, Dry, Cracked, Sandpaper, Whiskey-soaked, Growling, Gruff, Hoarse, Ragged, Raw, Rusty, Strained, Throaty |
| **Brightness/Weight** | Airy, Breathy, Feathery, Thin, Reedy, Tinny, Piercing, Shrill, Piping, Wispy, Fluting, Chirpy, Squeaky, Metallic, Brittle |
| **Temperature** | Warm, Sunny, Nurturing, Genial, Toast-like, Cool, Detached, Clinical, Crisp, Distant, Icy, Steel |

### 感情（心理的な意図）

| 分類 | 用語 |
|----------|-------|
| **高エネルギー／肯定的** | Jubilant, Exuberant, Ebullient, Cheerful, Playful, Whimsical, Contented, Blissful, Optimistic, Encouraging, Radiant, Rapturous, Peppy, Jaunty, Jolly, Jovial |
| **高エネルギー／否定的** | Indignant, Seething, Acerbic, Strident, Sharp, Biting, Vitriolic, Threatening, Savage, Belligerent, Antagonistic, Caustic, Combative, Confrontational, Contemptuous, Derisive |
| **低エネルギー／否定的** | Melancholy, Despondent, Wistful, Morose, Tearful, Heavy-hearted, Hollow, Defeated, Grief-stricken, Bereft, Bleak, Broken, Crestfallen, Dejected, Desolate, Disheartened, Dismal, Doleful, Dour, Forlorn |
| **弱さ** | Jittery, Tremulous, Quavering, Shaky, Hysterical, Panic-stricken, Hesitant, Guarded, Tense, Agitated, Alarmed, Apprehensive, Faint, Frightened, Skittish, Uneasy |
| **権威／力** | Commanding, Imperious, Dictatorial, Regal, Stately, Assertive, Dogmatic, Unyielding, Matter-of-fact, Dignified, Pompous, Supercilious, Sagacious |
| **皮肉／機知** | Sarcastic, Ironic, Wry, Cynical, Sardonic, Satirical, Facetious, Flippant, Sassy, Saucy, Cheeky, Bantering, Droll |

### 音程と音楽性（メロディー）

| 側面 | 用語 |
|--------|-------|
| **Range** | Bass-heavy, Mid-range, High-frequency, Falsetto, Head-voice, Chest-voice |
| **Movement** | Uptalk (rising pitch, uncertainty), Down-talk (dropping pitch, finality/authority), Singsong (nursery-rhyme rise and fall), Flat/Monotone (zero variation), Modulated (controlled varied pitch) |
| **Register** | Vocal Fry (creaky), Nasal, Twangy, Resonance, Vibrato, Tremolo |

### 演技と仕組み（テンポと明瞭さ）

| 側面 | 用語 |
|--------|-------|
| **Tempo** | Staccato (clipped), Legato (flowing), Rapid-fire, Measured, Deliberate, Halting, Drawling, Languid, Ponderous |
| **Articulation** | Crisp, Glassy, Over-enunciated, Slurred, Lazy, Mumbled, Thick, Sibilant (hissing 'S'), Plosive (popping 'P'/'B') |
| **Emphasis** | Billboard (punching a key word), Throwaway (de-emphasizing), Punchy (high-energy consonants) |
| **Rhythm** | Cadenced, Syncopated, Naturalistic, Flowing, Breathless, Erratic |

### 仕様と類型（キャラクターモデル）

| 分類 | 類型 |
|----------|-----------|
| **Commercial** | Hard-sell, Soft-sell, Everyman/Everywoman, Girl/Boy Next Door, Hip/Gen-Z, Millennial, Sophisticated/Luxury |
| **Narrative** | The Trusted Advisor, The Sage/Professor, The Villain/Antagonist, The Sidekick, The Reluctant Hero |
| **Technical/Industry** | "Announcery" (classic radio), "Voice of God" (deep/booming/omniscient), "Corporate/Industrial" (polished/professional), "Sotto Voce" (stage whisper), "Walla" (background crowd noise) |

---

# パート 2：Qwen3-TTS VoiceDesign の実験結果

VoiceDesign のモデルが声の説明のプロンプトをどう解釈するかを試した実測の結果です。上のプロの語彙を、実際のモデルの振る舞いに対応付けます。

## 方法

- すべてのテストで、すべての声に同じ 4 つの中立的な文を使う
- 声の説明の効果を切り分けるため、`instruct` 欄は空にする
- `seed: -1`（ランダム）で自然な一貫性を試す
- 評価の観点：行をまたいだ声の一貫性、説明への従い方、品質
- テストの台本は `scripts/voice_design_strategies*.json` に保存

## ラウンド 1：方針の比較

根本的に異なる 5 つの説明の方法を試しました（男女それぞれ、合計 40 サンプル）。

### 試した方針

| 方針 | 方法 | 例 |
|----------|----------|---------|
| **Anatomy** | 物理的／音響的な特性 | "A deep male baritone with resonant chest voice, smooth timbre, slight vocal fry" |
| **Persona** | 人物のアイデンティティ | "A kind elderly grandfather who speaks with patience and gentle authority" |
| **Technical** | 音声工学の用語 | "A well-projected male voice with crisp diction, moderate pace, controlled dynamic range" |
| **Emotion** | 感情の性質だけ | "A calm, reassuring male voice that sounds confident, trustworthy, and steady" |
| **Context** | 場面／状況 | "A male narrator reading a fantasy novel aloud in a quiet bookshop" |

### 結果

| 方針 | 一貫性 | 主な観察 |
|----------|------------|-----------------|
| **Anatomy** | 最良（男性）、不良（女性） | 物理的な説明はモデルに具体的な音響の目標を与える。女性の "light soprano, airy" は高すぎ、細すぎになった。 |
| **Persona** | 可変 | 類型が声の種類を強く示す場合（若い記者 → 速く甲高い）は効く。曖昧な場合（祖父 → 性別が入れ替わる）は失敗する。 |
| **Technical** | 低〜中 | モデルは音声工学の用語を部分的に理解するが、確実ではない。 |
| **Emotion** | 低 | 抽象的すぎる。「calm」や「warm」がどう響くかの物理的な手がかりがない。 |
| **Context** | なし | 行ごとに違う声。モデルは説明を場面の指示と受け取り、声のアイデンティティとは受け取らない。 |

### ラウンド 1 の結論

**解剖学を先にする説明が勝ちます。** モデルが一貫した声を出すには、具体的な音響の目標（音程、声域、音色、質感）が必要です。抽象的な性質（感情、ペルソナ、文脈）は解釈の自由を与えすぎます。

## ラウンド 2：解剖学を先にするハイブリッド

すべての説明を、声域＋音色＋トーンの形容詞の形に作り直しました。演技やパフォーマンスの言葉は入れません（それらは `instruct` に属します）。

### 公式

> **[声域] + [音色／質感の記述語] + [トーンの性格の形容詞]**
>
> 決して入れない：演技の動詞（speaking、reading）、テンポの言葉、場面／文脈

### 結果

| 声 | 説明 | 一貫性 | 品質 | 備考 |
|-------|------------|------------|---------|-------|
| **WARM_BARITONE** | "deep male baritone, rich chest resonance, warm smooth timbre, hint of gravelly texture" | 中〜高 | 高 | 説明どおりの声、非常に良い深い品質、多少のばらつき |
| **GROUNDED_ALTO** | "female alto, full grounded mid-range, slightly husky, natural warmth" | 中〜高 | 高 | 説明によく従い、baritone よりばらつきが少ない |
| **BRIGHT_TENOR** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | 非常に低い | 高 | 激しい感情の演技、「常軌を逸した」品質 — Bright Tenor の調査を参照 |
| **SILKY_MEZZO** | "female mezzo-soprano, silky even tone, soft rounded edges, gentle clarity" | 非常に高い | 非常に高い | 全体で最良。非常に聴きやすい。 |
| **COMMANDING_BASS** | "male bass, dark authoritative rumble, heavy low-end, firm tone" | 高 | 高 | カウボーイ／西部劇のような荒々しさ。採用候補。 |
| **CRISP_SOPRANO** | "female soprano, precise crystalline tone, balanced brightness, confident projection" | 非常に高い | 高 | トーンの幅はより広いが一貫している。スタジオ品質の感触。 |

### 一貫性を決める要素（セクション I の用語から）

**高い一貫性** — モデルが確実に捉える用語：

- 滑らかさ／均一さ：`silky`、`even`、`soft`、`rounded`（Smooth/Rich から）
- 精密さ：`precise`、`crystalline`、`balanced`（制御を含意する）
- 権威／強さ：`dark`、`authoritative`、`firm`、`commanding`（Resonance から）
- 地に足のついた感じ：`grounded`、`full`、`husky`（Rough/Gritty＋Resonance から）

**高いばらつき** — 不安定にする用語：

- エネルギー：`bright`、`youthful energy`（Brightness/Weight から — 下の調査を参照）
- 手がかりとなる質感のない、温かさだけ、深さだけの曖昧な指定：`warm`、`deep`

**観察：** すべての説明で、女性の声は男性より一貫する傾向があります。

## Bright Tenor の調査

体系的な切り分け（8 つの変種、32 サンプル）で、BRIGHT_TENOR の不安定さの原因を特定しました。

### テストの組み合わせ

| 変種 | 説明 | 変更点 |
|---------|------------|-------------|
| **CONTROL** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | 元の説明 — 基準 |
| **NO_BRIGHT** | "male tenor, clear tone, light nasal resonance, youthful energy" | "bright" を削除 |
| **NO_ENERGY** | "male tenor, bright clear tone, light nasal resonance" | "youthful energy" を削除 |
| **NEITHER** | "male tenor, clear tone, light nasal resonance" | 両方を削除 |
| **BRIGHT_ONLY** | "A bright male tenor voice" | 最小限 — "bright" だけ |
| **SHARP_REPLACE** | "male tenor, sharp forward-placed tone, light nasal resonance, crisp articulation" | "bright" を "sharp" に置き換え |
| **HIGH_HARMONICS** | "male tenor, strong upper harmonics, clear forward projection, light nasal resonance" | 技術的な音響の等価物 |
| **STABILIZED** | "male tenor, steady clean tone, light nasal resonance, even delivery" | 一貫性の公式を適用 |

### 結果

| 変種 | 一貫性 | 性格 | 知見 |
|---------|------------|-----------|---------|
| **CONTROL** | 非常に低い | 常軌を逸した、激しい感情の揺れ | R2 の振る舞いを確認 |
| **NO_BRIGHT** | 中 | 表現豊かだがテキストに導かれ、常軌を逸することが少ない | "Bright" が感情の不安定さに寄与する |
| **NO_ENERGY** | 中 | NO_BRIGHT に似るが、劇的なテキストで競売人のようなテンポの急上昇が 1 回 | "Energy" がテンポの急な変化に寄与する |
| **NEITHER** | 高 | テキストの手がかりに従い、安定 | tenor の素の土台はもともと安定している |
| **BRIGHT_ONLY** | 高 | 安定し、テキストに反応 | "Bright" だけなら問題ない |
| **SHARP_REPLACE** | 高 | いらだった質感 | "Sharp/forward-placed" はいらだちのトーンに対応する |
| **HIGH_HARMONICS** | 中 | 声が大きくなり、ほとんど叫ぶ、非常に明瞭 | 有用な用途：覚醒／目覚めの効果 |
| **STABILIZED** | 高 | ナレーター風、感情が薄い | 一貫性の公式は効くが表現を平板にする |

### 主な知見：相互作用

**"Bright" と "youthful energy" はそれぞれ単独なら安定しますが、一緒にすると破壊的に作用します。** この組み合わせはモデルに矛盾する信号 — トーンの明るさ（音響的）＋行動のエネルギー（演技的）— を与え、生成のたびに解釈の間で揺れ動きます。

これはおそらく一般的な原則です。**声の説明の中で、音響的な記述語（セクション I）と行動的／エネルギーの記述語（セクション III）を混ぜると曖昧になります。** 声の説明はセクション I の領域に純粋に保ち、セクション II〜IV の性質は `instruct` で使ってください。

### 用語ごとの対応

| 用語 | セクション | モデルの解釈 | 有用性 |
|------|---------|---------------------|------------|
| `bright`（単独） | I. Brightness/Weight | 安定したトーンの明るさ | 単独なら良い |
| `bright` ＋エネルギーの用語 | I + III | 感情の混乱 | 組み合わせを避ける |
| `sharp` | I. Nasal/Sharp | いらだち／不機嫌のトーン | 慎重に使う |
| `upper harmonics` | I.（技術的） | 音量が上がり、ほとんど叫ぶ | 用途：覚醒の効果 |
| `steady`、`even` | I. Smooth/Rich | ナレーター風の安定 | 表現を平板にする |
| `silky`、`rounded` | I. Smooth/Rich | 高い一貫性＋聴きやすさ | ナレーションに最良 |
| `dark`、`authoritative` | I. Resonance | 荒々しい／西部劇的な質感、一貫 | 男性の権威に良い |
| `crystalline`、`precise` | I. Smooth/Rich | 表現の幅を保ちつつ一貫 | 最良のバランス |
| `hollow`（instruct 内） | I. Resonance | ささやきとため息のハイブリッド、息の多い空虚さ | instruct では避ける — モデルが感情的ではなく音響的に読む。代わりに "defeated" を使う。 |

## 声の説明のプロンプトの実践則

1. **声域から始める**（bass、baritone、tenor、alto、mezzo-soprano、soprano）
2. **セクション I だけから、質感／音色の用語を 2〜3 個加える**
3. **説明の欄では、音響的な記述語（セクション I）と行動的な記述語（セクション II〜IV）を混ぜない**
4. **演技、感情、テンポには `instruct` を使う** — そのための欄です
5. **一貫性のためには「制御」の用語**（`silky`、`even`、`precise`、`firm`）を「エネルギー」の用語（`bright`、`sharp`、`piercing`）より優先する
6. **曖昧な用語で過剰に指定すると不確かさが積み重なる** — 短く明確な説明が、詳細だが曖昧な説明を上回ることがある
7. **このモデルでは女性の声の方がもともと一貫している**（すべての説明のスタイルで）
