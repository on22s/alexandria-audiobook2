[English](VOICE_REFERENCE.md) | [中文](VOICE_REFERENCE_CN.md) | 日本語 | [Español](VOICE_REFERENCE_ES.md)

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
- 具体的な声域（バリトン baritone）
- 物理的な性質（胸の共鳴、ざらついた質感）
- 話し方のスタイル（ゆっくりと意図的なテンポ）

**悪い：**「An energetic young woman who's always excited」
- 行動的で、音響的ではありません。

**良い：**「Female mezzo-soprano, bright clear timbre, light breathy quality, upbeat rhyth delivery」
- 具体的な声域（メゾソプラノ mezzo-soprano）
- 物理的な性質（明るい、息混じり）
- 話し方のスタイル（陽気でリズミカル）

### 要点

1. **必ず声域から始める：** 男性は bass（バス）／baritone（バリトン）／tenor（テノール）、女性は contralto（コントラルト）／mezzo（メゾ）／soprano（ソプラノ）
2. **質感の言葉を 2〜3 個加える：** smooth（滑らかな）、gravelly（ざらついた）、breathy（息混じりの）、rich（豊かな）、thin（細い）など
3. **話し方の言葉を 1 個加える：** slow（遅い）、fast（速い）、deliberate（意図的な）、rhythmic（リズミカルな）など
4. **音響的な言葉と行動的な言葉を混ぜない：**「warm gravelly baritone」（温かくざらついたバリトン）✓ 対「friendly confident baritone」（親しみやすく自信のあるバリトン）✗
5. **感情の言葉は `description` ではなく `instruct` に書く**

---

# パート 1：演出家の声の語彙

声の演技を細かく調整するための、業界標準の用語を機能別にまとめたものです。

## I. 質感、音色、品質（「楽器」）

声の物理的な響きや「粒」です。**VoiceDesign の `description` ではこれらを使います。**

| 分類 | 記述語 |
|----------|------------|
| **粗い／ざらつき（Rough/Gritty）** | Gravelly（ざらついた）, Raspy（かすれて耳障りな）, Husky（ハスキーな）, Scratchy（ひっかくような）, Smoky（煙をまとったような）, Guttural（喉の奥から出る）, Coarse（粗い）, Croaking（カエルの鳴き声のような）, Grating（耳をこするような）, Dry（乾いた）, Cracked（ひび割れた）, Fractured（砕けた）, Sandpaper（紙やすりのような）, Whiskey-soaked（酒に焼かれた）, Growling（唸るような）, Gruff（ぶっきらぼうな）, Hoarse（しわがれた）, Ragged（ぼろ切れのような）, Raw（生々しい）, Rusty（錆びついた）, Strained（張り詰めた）, Throaty（喉に響く） |
| **滑らか／豊か（Smooth/Rich）** | Silky（絹のような）, Velvety（ビロードのような）, Honeyed（蜂蜜のような）, Creamy（クリーミーな）, Liquid（流れるような）, Orotund（朗々とした）, Dulcet（甘美な）, Mellifluous（甘く流麗な）, Polished（磨かれた）, Lush（豊潤な）, Mellow（まろやかな）, Buttery（バターのような）, Soothing（なだめるような）, Fluid（流動的な）, Flowing（流れのある）, Seamless（継ぎ目のない）, Satiny（サテンのような）, Suave（洗練された）, Unctuous（ねっとりした） |
| **共鳴（Resonance）** | Booming（轟くような）, Chesty（胸に響く）, Full-bodied（厚みのある）, Deep（低い）, Sonorous（響き渡る）, Thundering（雷鳴のような）, Rumbling（ゴロゴロと響く）, Hollow（空洞の）, Echoing（反響する）, Cavernous（洞窟のような）, Round（丸みのある）, Heavy（重い）, Grounded（地に足のついた）, Bassy（低音の強い）, Resonant（よく響く）, Bell-like（鐘のような） |
| **軽い／高い（Light/High）** | Airy（空気感のある）, Breathy（息混じりの）, Feathery（羽のような）, Thin（細い）, Reedy（葦のような）, Tinny（ブリキのような）, Piercing（突き刺さるような）, Shrill（甲高い）, Piping（笛のような）, Wispy（かすかな）, Fluting（フルートのような）, Chirpy（小鳥のような）, Squeaky（キーキーいう）, Falsetto（裏声）, Head-voice（頭声）, Boyish（少年っぽい）, Girlish（少女っぽい）, Metallic（金属的な）, Brittle（硬く割れやすい） |
| **鼻にかかる／鋭い（Nasal/Sharp）** | Nasal（鼻にかかる）, Twangy（鼻音の強い）, Whiny（泣き言のような）, Pinched（つまんだような）, Brass（金管のような）, Strident（耳に障る）, Cutting（切り込むような）, Piercing（突き刺さるような）, Biting（辛辣な）, Sharp（鋭い）, Acidic（酸っぱく辛辣な）, Astringent（収斂性のある）, Clamorous（騒がしい）, Ear-splitting（耳をつんざく）, Harsh（耳ざわりな）, Jarring（不快に響く）, Penetrating（貫くような）, Stingy（ちくちく刺す） |
| **声の欠陥（Vocal Defects）** | Fry (Vocal Fry)（ヴォーカルフライ）, Sibilant (whistling 'S')（歯擦音、S が笛のように鳴る）, Plosive (popping 'P')（破裂音、P がポップする）, Tremulous (shaking)（震える）, Wobbling（揺らぐ）, Quavering（震え声）, Warbling（こぶしのような揺れ）, Slurred（崩れた発音）, Mumbled（もごもごした）, Incoherent（不明瞭な）, Thick（重くこもった） |

## II. 感情、態度、トーン（「サブテキスト」）

心理状態を合わせるための形容詞です。**`description` ではなく `instruct` で使います。**

| 分類 | 記述語 |
|----------|------------|
| **攻撃的／敵対的（Aggressive/Hostile）** | Acerbic（辛辣な）, Antagonistic（敵対的な）, Belligerent（好戦的な）, Biting（辛辣な）, Brusque（無愛想な）, Caustic（痛烈な）, Combative（戦闘的な）, Confrontational（挑発的な）, Contemptuous（軽蔑的な）, Cutting（切り込むような）, Derisive（嘲笑的）, Dictatorial（独裁的な）, Disdainful（軽蔑した）, Dogmatic（独断的な）, Domineering（横暴な）, Explosive（爆発的な）, Fierce（獰猛な）, Hard（強硬な）, Imperious（尊大な）, Incendiary（扇動的な）, Indignant（憤慨した）, Intimidating（威圧的な）, Malevolent（悪意のある）, Militant（過激な）, Pugnacious（けんか腰の）, Savage（野蛮な）, Scornful（さげすむ）, Seething（怒りを煮えたぎらせた）, Sharp（鋭い）, Spiteful（意地の悪い）, Stern（厳しい）, Threatening（威嚇的な）, Truculent（凶暴な）, Vengeful（復讐心に燃える）, Venomous（毒々しい）, Vitriolic（硫酸のような辛辣さ）, Warlike（好戦的な） |
| **悲しい／落ち込んだ（Sad/Depressed）** | Bereft（打ちひしがれた）, Bleak（荒涼とした）, Broken（打ちのめされた）, Crestfallen（意気消沈した）, Dejected（落胆した）, Desolate（荒れ果てた）, Despondent（絶望的な）, Disheartened（気落ちした）, Dismal（陰鬱な）, Doleful（哀れな）, Dour（陰気な）, Forlorn（見捨てられた）, Gloomy（陰気な）, Grief-stricken（悲嘆に暮れた）, Heartbroken（失意の）, Heavy-hearted（心痛む）, Hollow（空虚な）, Hopeless（絶望した）, Lachrymose（涙もろい）, Melancholy（憂鬱な）, Miserable（惨めな）, Morose（不機嫌で陰鬱な）, Mournful（悲しげな）, Pathetic（哀れな）, Pessimistic（悲観的な）, Plaintive（哀調を帯びた）, Regretful（後悔に満ちた）, Remorseful（悔恨の）, Resigned（諦めた）, Somber（もの悲しい）, Sorrowful（悲しみに満ちた）, Tearful（涙ぐんだ）, Tragic（悲劇的な）, Weepy（涙もろい）, Wistful（物思いにふける）, Woe-begone（悲しみに沈んだ） |
| **幸せ／前向き（Happy/Positive）** | Blissful（至福の）, Buoyant（快活な）, Celebratory（祝祭的な）, Cheerful（陽気な）, Chipper（元気な）, Contented（満足した）, Delightful（楽しい）, Ebullient（沸き立つような）, Ecstatic（有頂天の）, Elated（意気揚々たる）, Encouraging（励みになる）, Energetic（精力的な）, Enthusiastic（熱意のある）, Euphoric（陶酔した）, Exhilarated（爽快な）, Exuberant（あふれる活力の）, Festive（お祭り気分の）, Genial（和やかな）, Giddy（浮かれた）, Gleeful（喜びに満ちた）, Gratified（満ち足りた）, Hearty（快活な）, Hopeful（希望に満ちた）, Jaunty（軽快な）, Jolly（陽気な）, Jovial（快活な）, Joyous（歓喜の）, Jubilant（歓喜に沸く）, Lighthearted（気楽な）, Merry（楽しげな）, Optimistic（楽観的な）, Overjoyed（大喜びの）, Peppy（元気はつらつとした）, Radiant（輝くような）, Rapturous（熱狂的な）, Reassured（安心した）, Sunny（明るい）, Upbeat（前向きな）, Vibrant（活気のある）, Vivacious（生き生きとした）, Zestful（熱意にあふれた） |
| **恐れ／不安（Fearful/Anxious）** | Agitated（動揺した）, Alarmed（驚いた）, Apprehensive（不安げな）, Cautious（慎重な）, Concerned（気遣う）, Cowardly（臆病な）, Distressed（苦悩した）, Edgy（神経質な）, Faint（かすかな）, Frightened（怖がった）, Guarded（警戒した）, Hesitant（ためらいがちな）, Hysterical（ヒステリックな）, Insecure（不安定な）, Jittery（そわそわした）, Nervous（神経質な）, Panic-stricken（パニックに陥った）, Paralyzed（凍りついた）, Paranoid（疑心暗鬼の）, Petrified（すくみ上がった）, Quaking（震え上がった）, Restless（落ち着かない）, Scared（恐れた）, Shaky（震える）, Skittish（驚きやすい）, Spooked（びっくりした）, Startled（驚いた）, Tense（緊張した）, Terrified（恐怖に駆られた）, Timid（臆病な）, Trembling（震える）, Uneasy（不安な）, Unsettled（落ち着かない）, Wary（用心深い）, Worried（心配した） |
| **穏やか／中立（Calm/Neutral）** | Ambivalent（両義的な）, Apathetic（無関心な）, Balanced（バランスの取れた）, Bland（平板な）, Blas&eacute;（斜に構えた）, Bored（退屈した）, Casual（何気ない）, Collected（落ち着いた）, Composed（沈着な）, Cool（冷静な）, Detached（距離を置いた）, Dispassionate（冷静な）, Dull（鈍い）, Easygoing（気さくな）, Equable（穏やかな）, Even-tempered（気の穏やかな）, Flat（平板な）, Imperturbable（動じない）, Indifferent（無関心な）, Laid-back（くつろいだ）, Listless（無気力な）, Matter-of-fact（事務的な）, Measured（節度のある）, Mediocre（平凡な）, Mild（穏やかな）, Moderate（適度な）, Monotone（単調な）, Nonchalant（平然とした）, Objective（客観的な）, Pacifistic（平和主義的な）, Passive（受動的な）, Peaceful（平和な）, Phlegmatic（冷静で鈍重な）, Placid（穏やかな）, Relaxed（くつろいだ）, Serene（安らかな）, Steady（安定した）, Stoic（ストイックな）, Tranquil（静かな）, Unemotional（感情のない）, Unflappable（動じない） |
| **情愛／親密（Affectionate/Intimate）** | Adoring（愛情深い）, Amorous（色っぽい）, Caring（思いやりのある）, Cherishing（慈しむ）, Comforting（慰めるような）, Compassionate（思いやりのある）, Coquettish（なまめかしい）, Cuddly（抱きしめたくなる）, Doting（甘やかす）, Endearing（愛らしい）, Fond（好意的な）, Gentle（優しい）, Intimate（親密な）, Loving（愛情に満ちた）, Maternal（母性的な）, Nurturing（育むような）, Paternal（父性的な）, Protective（守るような）, Romantic（ロマンチックな）, Seductive（誘惑的な）, Sensual（官能的な）, Soft（柔らかな）, Sultry（色っぽく低い）, Sweet（甘い）, Sympathetic（同情的な）, Tender（優しい）, Touching（胸を打つ）, Warm（温かな）, Yearning（切望する） |
| **知的／上から目線（Intellectual/Superior）** | Analytical（分析的な）, Arrogant（傲慢な）, Authoritative（権威ある）, Condescending（見下すような）, Critical（批判的な）, Cynical（皮肉な）, Didactic（説教くさい）, Dignified（威厳のある）, Dry（乾いた）, Educated（教養のある）, Erudite（博学な）, Haughty（高慢な）, Informative（情報的な）, Inquisitive（詮索好きな）, Instructional（指導的な）, Knowledgeable（知識豊富な）, Lofty（高踏的な）, Logical（論理的な）, Patronizing（恩着せがましい）, Pedantic（学者ぶった）, Philosophical（哲学的な）, Pompous（尊大な）, Practical（実用的な）, Pragmatic（現実的な）, Precise（正確な）, Pretentious（気取った）, Professional（プロ的な）, Rational（理性的な）, Sagacious（賢明な）, Sarcastic（皮肉な）, Sardonic（冷笑的な）, Satirical（風刺的な）, Scholarly（学者然とした）, Skeptical（懐疑的な）, Smart（利発な）, Snobbish（俗物ぶった）, Sophisticated（洗練された）, Supercilious（横柄な）, Superior（高慢な）, Thoughtful（思慮深い）, Wise（賢明な）, Wry（苦笑いの） |
| **遊び心／風変わり（Playful/Quirky）** | Bantering（からかいの）, Cheeky（生意気な）, Childish（子どもっぽい）, Clownish（道化じみた）, Droll（おかしみのある）, Eccentric（風変わりな）, Facetious（冗談めかした）, Fanciful（空想的な）, Flippant（軽薄な）, Flirty（色っぽくからかう）, Foolish（愚かな）, Goofy（おどけた）, Humorous（ユーモラスな）, Impish（いたずらな）, Ironic（アイロニカルな）, Irreverent（不敬な）, Jocular（おどけた）, Kooky（変わり者の）, Mischievous（いたずら好きな）, Mocking（あざけるような）, Offbeat（型破りな）, Playful（遊び心のある）, Prankish（いたずらな）, Quirky（風変わりな）, Rascally（いたずらな）, Ridiculous（ばかげた）, Sassy（生意気な）, Saucy（生意気で大胆な）, Silly（おどけた）, Snarky（辛辣な）, Teasing（からかうような）, Tongue-in-cheek（冗談めかした）, Whimsical（気まぐれな）, Witty（機知に富んだ）, Zany（奇想天外な） |

## III. 演技、テンポ、リズム（「仕組み」）

速さ、流れ、明瞭さの調整です。**`description` ではなく `instruct` で使います。**

| スタイル | 説明 |
|-------|------------|
| **Staccato（断奏）** | 短く、切り離され、パンチがあり、切り詰めた、唐突な、機関銃のような |
| **Legato（連奏）** | 滑らかで、つながり、流れるようで、滑るように、連続し、母音が伸びる |
| **Rapid-fire（機関銃のように速い）** | 目にも留まらぬ速さ、切迫した、熱狂的な、競売人のような、慌ただしい、息もつかず |
| **Measured（節度のある）** | 意図的で、ゆっくり、思慮深く、一語一語間を置く、重々しい |
| **Halting（途切れがちな）** | 不確かで、頻繁に止まり、どもり、途切れ途切れで、つまずく |
| **Lilting（歌うように弾む）** | リズミカルな上げ下げ、音楽的、歌うようで、旋律的、弾む |
| **Drawl（語尾を伸ばす）** | なまけたように母音を伸ばし、ゆっくりで、くつろいだ、物憂げで、言葉を引き延ばす |
| **Punchy（パンチのある）** | 重要な単語を高エネルギーで強調し、力強く、子音を強く打つ |
| **Deadpan（無表情）** | 抑揚がまったくなく、平板な音程、乾いた喜劇、無表情な語り |
| **Cadenced（韻律のある）** | 非常にリズミカルで、ほとんど詩的かシェイクスピア風、拍子がある |

## IV. 業界の仕様と類型（「役柄」）

キャスティングの内訳や広告の台本で使われる略語です。

| 類型 | 説明 |
|-----------|------------|
| **Authentic（実在感）** | 実在の人物、俳優でない人、ドキュメンタリー風、生の、洗練されていない |
| **Announcer (Classic)（クラシックなアナウンサー）** | 「Voice of God」（VOG）、プレゼン的、とどろく、洗練された、ラジオ DJ |
| **Announcer (Non-Announcery)（アナウンサー風でない）** | 売り込まない、会話的、軽く流す、無関心 |
| **Conversational（会話的）** | 友人と話すだけ、くだけた、語尾が崩れる、自然な息づかい |
| **Girl/Guy Next Door（隣の女の子／男の子）** | 親しみやすい、友好的、中流、信頼できる、普通の、安心できる |
| **Trusted Advisor（信頼できる助言者）** | 医師、弁護士、親、ファイナンシャルプランナー、知識豊富、穏やか |
| **Cool Parent（クールな親）** | 現代的、流行に敏感、厳しくない、理解がある、「Disney Mom/Dad」 |
| **The Millennial/Gen Z（ミレニアル／Z 世代）** | ヴォーカルフライ、上がり調子、くだけた、皮肉的、技術に詳しい、速い、アイロニー |
| **Promo Voice（予告編の声）** | ざらついた、低い、唸る、強烈、ささやく、大げさ、映画の予告編 |
| **Corporate/Industrial（企業／産業向け）** | 明瞭、歯切れよい、プロ的、中立的、情報的、自信がある |
| **Hard Sell（ハードセル）** | 大きな音量、高いエネルギー、切迫、叫ぶ、「Car Dealer」、攻撃的 |
| **Soft Sell（ソフトセル）** | 親密、ささやくよう、高級、値の張る、心地よい、誘惑的 |
| **Characters（役柄）** | 悪役、英雄、相棒、生き物、怪物、ロボット、異星人、魔女、老婆、賢者 |

## V. 録音現場の技術的な指示（「ワークフロー」）

録音の過程への指示です。

| 指示 | 意味 |
|---------|---------|
| **A-B-C (Three-in-a-row)（3 回連続）** | 同じ行を 3 つの異なる態度で 3 回読む |
| **Billboard（看板強調）** | 特定の名前や製品を強調する（「ブランド名をもっと強く」） |
| **Button（着地）** | 文やテイクをきれいに断定的に終える。着地を決める |
| **Clean Up（録り直し）** | 口の音やつまずきを直すために行を録り直す |
| **Cold Read（ぶっつけ本番）** | 練習なしで台本をすぐに読む |
| **Color（色付け）** | 特定の単語にもっと感情や個性を加える |
| **Compression（圧縮）** | ダイナミックレンジを狭める（静かな部分を大きく、大きな部分を小さく） |
| **Donut（ドーナツ）** | 最初と最後に声があり、中間に音楽やクリップ用の穴がある |
| **Eat the Mic（マイクに食いつく）** | 近接効果のためにマイクに非常に近づく（深く親密な低音） |
| **Guide Track（ガイドトラック）** | 録音済みの音声や映像の参照のタイミングに合わせる |
| **Lay Out（休ませる）** | ある区間で話すのをやめる、間を置く |
| **Lead-in（助走）** | 最初の行の前に、役に入るために声に出さずに言う文 |
| **Level（レベル合わせ）** | マイクのゲインを決めるために本番の音量で読む |
| **Lift（リフト）** | 明るく、または音程を少し上げる。しばしば広告の末尾で |
| **Pick-up（追加録り）** | 特定の 1 文やフレーズだけを録音する |
| **Popping（ポップ音）** | 「P」「B」の音で余分な空気が出る（対処：「少し軸を外す」） |
| **Projection（投射）** | 部屋の後ろまで声を届かせるか、内側に保つか |
| **Punch-in（パンチイン）** | 既存のテイクに挿入する小さな断片を録音する |
| **Safety（保険テイク）** | 直前の良いテイクに技術的な問題があった場合のための最後の 1 回 |
| **Slate（スレート）** | 演技の前に名前とテイク番号を言う（「John Doe, Take 1」） |
| **Smile（笑顔）** | 読みながら実際に笑顔になる（明るく温かいトーンに変わる） |
| **Sotto Voce（低声）** | 舞台のささやき。息を潜めて話す |
| **Throw Away（流す）** | 行を強調しない。重要でない、あるいは何気なく聞こえるようにする |
| **Time（尺合わせ）** | 厳密な長さ（たとえばちょうど 29.5 秒）に合わせて速めたり遅めたりする |
| **Wild Line（ワイルドライン）** | 台本の流れから離れて別に録る行（叫び、笑い、うなり） |
| **Walla（ウォーラ、ガヤ）** | 一群で演じる背景の群衆のざわめき |

## VI. 拡張された次元のリファレンス

5 つの主要な領域にわたる、150 以上の追加の専門用語です。

### 次元（身体性と雰囲気）

| 次元 | 用語 |
|-----------|-------|
| **存在感（Presence）** | Close-mic（近接）, Distant（遠い）, Intimate（親密）, Expansive（広がりのある）, Internalized（内にこもった）, Projection（投射） |
| **質感（Texture）** | Silky（絹のような）, Velvety（ビロードのような）, Honeyed（蜂蜜のような）, Mellifluous（甘く流麗な）, Buttery（バターのような）, Creamy（クリーミーな）, Lush（豊潤な）, Liquid（流れるような）, Orotund（朗々とした）, Resonant（よく響く） |
| **ざらつき（Grit）** | Gravelly（ざらついた）, Raspy（かすれて耳障りな）, Smoky（煙をまとったような）, Husky（ハスキーな）, Guttural（喉の奥から出る）, Scratchy（ひっかくような）, Coarse（粗い）, Croaking（カエルの鳴き声のような）, Grating（耳をこするような）, Dry（乾いた）, Cracked（ひび割れた）, Sandpaper（紙やすりのような）, Whiskey-soaked（酒に焼かれた）, Growling（唸るような）, Gruff（ぶっきらぼうな）, Hoarse（しわがれた）, Ragged（ぼろ切れのような）, Raw（生々しい）, Rusty（錆びついた）, Strained（張り詰めた）, Throaty（喉に響く） |
| **明るさ／重さ（Brightness/Weight）** | Airy（空気感のある）, Breathy（息混じりの）, Feathery（羽のような）, Thin（細い）, Reedy（葦のような）, Tinny（ブリキのような）, Piercing（突き刺さるような）, Shrill（甲高い）, Piping（笛のような）, Wispy（かすかな）, Fluting（フルートのような）, Chirpy（小鳥のような）, Squeaky（キーキーいう）, Metallic（金属的な）, Brittle（硬く割れやすい） |
| **温度（Temperature）** | Warm（温かな）, Sunny（明るい）, Nurturing（育むような）, Genial（和やかな）, Toast-like（トーストのような、温かみのある）, Cool（冷静な）, Detached（距離を置いた）, Clinical（臨床的な）, Crisp（歯切れのよい）, Distant（遠い）, Icy（氷のような）, Steel（鋼のような） |

### 感情（心理的な意図）

| 分類 | 用語 |
|----------|-------|
| **高エネルギー／肯定的（High Energy/Positive）** | Jubilant（歓喜に沸く）, Exuberant（あふれる活力の）, Ebullient（沸き立つような）, Cheerful（陽気な）, Playful（遊び心のある）, Whimsical（気まぐれな）, Contented（満足した）, Blissful（至福の）, Optimistic（楽観的な）, Encouraging（励みになる）, Radiant（輝くような）, Rapturous（熱狂的な）, Peppy（元気はつらつとした）, Jaunty（軽快な）, Jolly（陽気な）, Jovial（快活な） |
| **高エネルギー／否定的（High Energy/Negative）** | Indignant（憤慨した）, Seething（怒りを煮えたぎらせた）, Acerbic（辛辣な）, Strident（耳に障る）, Sharp（鋭い）, Biting（辛辣な）, Vitriolic（硫酸のような辛辣さ）, Threatening（威嚇的な）, Savage（野蛮な）, Belligerent（好戦的な）, Antagonistic（敵対的な）, Caustic（痛烈な）, Combative（戦闘的な）, Confrontational（挑発的な）, Contemptuous（軽蔑的な）, Derisive（嘲笑的） |
| **低エネルギー／否定的（Low Energy/Negative）** | Melancholy（憂鬱な）, Despondent（絶望的な）, Wistful（物思いにふける）, Morose（不機嫌で陰鬱な）, Tearful（涙ぐんだ）, Heavy-hearted（心痛む）, Hollow（空虚な）, Defeated（打ち負かされた）, Grief-stricken（悲嘆に暮れた）, Bereft（打ちひしがれた）, Bleak（荒涼とした）, Broken（打ちのめされた）, Crestfallen（意気消沈した）, Dejected（落胆した）, Desolate（荒れ果てた）, Disheartened（気落ちした）, Dismal（陰鬱な）, Doleful（哀れな）, Dour（陰気な）, Forlorn（見捨てられた） |
| **弱さ（Vulnerability）** | Jittery（そわそわした）, Tremulous（震える）, Quavering（震え声）, Shaky（震える）, Hysterical（ヒステリックな）, Panic-stricken（パニックに陥った）, Hesitant（ためらいがちな）, Guarded（警戒した）, Tense（緊張した）, Agitated（動揺した）, Alarmed（驚いた）, Apprehensive（不安げな）, Faint（かすかな）, Frightened（怖がった）, Skittish（驚きやすい）, Uneasy（不安な） |
| **権威／力（Authority/Power）** | Commanding（威風堂々とした）, Imperious（尊大な）, Dictatorial（独裁的な）, Regal（王者のような）, Stately（荘厳な）, Assertive（断固とした）, Dogmatic（独断的な）, Unyielding（不屈の）, Matter-of-fact（事務的な）, Dignified（威厳のある）, Pompous（尊大な）, Supercilious（横柄な）, Sagacious（賢明な） |
| **皮肉／機知（Irony/Wit）** | Sarcastic（皮肉な）, Ironic（アイロニカルな）, Wry（苦笑いの）, Cynical（皮肉な）, Sardonic（冷笑的な）, Satirical（風刺的な）, Facetious（冗談めかした）, Flippant（軽薄な）, Sassy（生意気な）, Saucy（生意気で大胆な）, Cheeky（生意気な）, Bantering（からかいの）, Droll（おかしみのある） |

### 音程と音楽性（メロディー）

| 側面 | 用語 |
|--------|-------|
| **音域（Range）** | Bass-heavy（低音の強い）, Mid-range（中音域）, High-frequency（高周波）, Falsetto（裏声）, Head-voice（頭声）, Chest-voice（胸声） |
| **音の動き（Movement）** | Uptalk（上昇調：音程が上がり、不確かさを示す）, Down-talk（下降調：音程が下がり、終結や権威を示す）, Singsong（歌うように：童謡のような上下）, Flat/Monotone（平板／単調：変化なし）, Modulated（変調：制御された音程の変化） |
| **発声区（Register）** | Vocal Fry（ヴォーカルフライ、ぎしぎしした声）, Nasal（鼻にかかる）, Twangy（鼻音の強い）, Resonance（共鳴）, Vibrato（ビブラート）, Tremolo（トレモロ） |

### 演技と仕組み（テンポと明瞭さ）

| 側面 | 用語 |
|--------|-------|
| **速度（Tempo）** | Staccato（切り詰めた）, Legato（流れるような）, Rapid-fire（目にも留まらぬ速さ）, Measured（節度のある）, Deliberate（意図的な）, Halting（途切れがちな）, Drawling（語尾を伸ばす）, Languid（物憂げな）, Ponderous（重々しい） |
| **発音（Articulation）** | Crisp（歯切れのよい）, Glassy（ガラスのような）, Over-enunciated（過剰に明瞭な）, Slurred（崩れた発音）, Lazy（怠惰な）, Mumbled（もごもごした）, Thick（重くこもった）, Sibilant (hissing 'S')（歯擦音、S が擦れる）, Plosive (popping 'P'/'B')（破裂音、P／B がポップする） |
| **強調（Emphasis）** | Billboard (punching a key word)（看板のようにキーワードを打ち出す）, Throwaway (de-emphasizing)（力を抜いて流す）, Punchy (high-energy consonants)（子音を高エネルギーで打つ） |
| **リズム（Rhythm）** | Cadenced（韻律のある）, Syncopated（シンコペーションの）, Naturalistic（自然な）, Flowing（流れのある）, Breathless（息もつかぬ）, Erratic（不規則な） |

### 仕様と類型（キャラクターモデル）

| 分類 | 類型 |
|----------|-----------|
| **商業（Commercial）** | Hard-sell（ハードセル）, Soft-sell（ソフトセル）, Everyman/Everywoman（普通の人）, Girl/Boy Next Door（隣の女の子／男の子）, Hip/Gen-Z（ヒップ／Z 世代）, Millennial（ミレニアル世代）, Sophisticated/Luxury（洗練／高級） |
| **物語（Narrative）** | The Trusted Advisor（信頼できる助言者）, The Sage/Professor（賢者／教授）, The Villain/Antagonist（悪役／敵役）, The Sidekick（相棒）, The Reluctant Hero（渋る英雄） |
| **技術／産業（Technical/Industry）** | "Announcery"（古典的なラジオ調）, "Voice of God"（低く轟き、全知のような声）, "Corporate/Industrial"（洗練されたプロ向け）, "Sotto Voce"（舞台のささやき）, "Walla"（背景の群衆のざわめき） |

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
| **Anatomy** | 良好（男性）、不良（女性） | 物理的な説明はモデルに具体的な音響の目標を与える。女性の "light soprano, airy" は高すぎ、細すぎになった。 |
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
| `bright`（単独） | I. 明るさ／重さ | 安定したトーンの明るさ | 単独なら良い |
| `bright` ＋エネルギーの用語 | I + III | 感情の混乱 | 組み合わせを避ける |
| `sharp` | I. 鼻にかかる／鋭い | いらだち／不機嫌のトーン | 慎重に使う |
| `upper harmonics` | I.（技術的） | 音量が上がり、ほとんど叫ぶ | 用途：覚醒の効果 |
| `steady`、`even` | I. 滑らか／豊か | ナレーター風の安定 | 表現を平板にする |
| `silky`、`rounded` | I. 滑らか／豊か | 高い一貫性＋聴きやすさ | ナレーションに最良 |
| `dark`、`authoritative` | I. 共鳴 | 荒々しい／西部劇的な質感、一貫 | 男性の権威に良い |
| `crystalline`、`precise` | I. 滑らか／豊か | 表現の幅を保ちつつ一貫 | 最良のバランス |
| `hollow`（instruct 内） | I. Resonance | ささやきとため息のハイブリッド、息の多い空虚さ | instruct では避ける — モデルが感情的ではなく音響的に読む。代わりに "defeated" を使う。 |

## 声の説明のプロンプトの実践則

1. **声域から始める**（bass、baritone、tenor、alto、mezzo-soprano、soprano）
2. **セクション I だけから、質感／音色の用語を 2〜3 個加える**
3. **説明の欄では、音響的な記述語（セクション I）と行動的な記述語（セクション II〜IV）を混ぜない**
4. **演技、感情、テンポには `instruct` を使う** — そのための欄です
5. **一貫性のためには「制御」の用語**（`silky`、`even`、`precise`、`firm`）を「エネルギー」の用語（`bright`、`sharp`、`piercing`）より優先する
6. **曖昧な用語で過剰に指定すると不確かさが積み重なる** — 短く明確な説明が、詳細だが曖昧な説明を上回ることがある
7. **このモデルでは女性の声の方がもともと一貫している**（すべての説明のスタイルで）
