# Referencia de voces

[English](Voice-Reference.md) · [日本語](Voice-Reference-JA.md) · [中文](Voice-Reference-CN.md) · Español

Palabras para describir voces, y lo que el modelo VoiceDesign de Qwen3-TTS hace realmente con ellas.

- La **Parte 1** es un vocabulario reunido de glosarios de actuación y guías de dirección de doblaje.
- La **Parte 2** es lo que mostraron las pruebas sobre cómo interpreta VoiceDesign las descripciones de voz.

**La versión corta:**

- Pon las palabras sobre la **voz en sí** (registro, textura, timbre) en la
  **description** de una voz: la usan Voice Design, la pestaña Designer y el Dataset Builder.
- Pon las palabras sobre **emoción, interpretación y ritmo** en la **instruction** de la línea (`instruct`).
- No mezcles las dos en una descripción: eso hace inestables las voces (ver la
  [prueba del tenor brillante](#investigación-del-tenor-brillante)).

Las [reglas de trabajo](#reglas-de-trabajo-para-las-descripciones-de-voz) del final lo resumen.

---

## Parte 1: el vocabulario del director

Cientos de términos estándar para moldear una interpretación vocal, agrupados por lo que controlan.

### I. Textura, timbre y calidad (el "instrumento")

El sonido físico o el "grano" de la voz. **Úsalos en `description` para VoiceDesign.**

| Categoría | Descriptores |
|----------|------------|
| **Áspera/granulosa (Rough/Gritty)** | Gravelly (áspera, rasposa), Raspy (ronca, chirriante), Husky (ronca y grave), Scratchy (raspante), Smoky (ahumada), Guttural (gutural), Coarse (burda), Croaking (como un croar), Grating (chirriante, irritante), Dry (seca), Cracked (quebrada), Fractured (fracturada), Sandpaper (como papel de lija), Whiskey-soaked (curtida en whisky), Growling (gruñente), Gruff (brusca), Hoarse (ronca), Ragged (deshilachada), Raw (cruda), Rusty (oxidada), Strained (forzada), Throaty (de garganta) |
| **Suave/rica (Smooth/Rich)** | Silky (sedosa), Velvety (aterciopelada), Honeyed (melosa), Creamy (cremosa), Liquid (líquida), Orotund (rotunda y sonora), Dulcet (dulce), Mellifluous (meliflua), Polished (pulida), Lush (exuberante), Mellow (suave y madura), Buttery (mantecosa), Soothing (calmante), Fluid (fluida), Flowing (que fluye), Seamless (sin costuras), Satiny (satinada), Suave (afable), Unctuous (untuosa) |
| **Resonancia (Resonance)** | Booming (retumbante), Chesty (de pecho), Full-bodied (de cuerpo pleno), Deep (profunda), Sonorous (sonora), Thundering (atronadora), Rumbling (que retumba), Hollow (hueca), Echoing (con eco), Cavernous (cavernosa), Round (redonda), Heavy (pesada), Grounded (asentada), Bassy (con graves marcados), Resonant (resonante), Bell-like (como una campana) |
| **Ligera/aguda (Light/High)** | Airy (aérea), Breathy (con aire), Feathery (plumosa), Thin (fina), Reedy (como una caña), Tinny (metálica y delgada), Piercing (penetrante), Shrill (estridente), Piping (aguda y aflautada), Wispy (tenue y vaporosa), Fluting (aflautada), Chirpy (gorjeante), Squeaky (chillona), Falsetto (falsete), Head-voice (voz de cabeza), Boyish (de chico), Girlish (de chica), Metallic (metálica), Brittle (quebradiza) |
| **Nasal/afilada (Nasal/Sharp)** | Nasal (nasal), Twangy (nasal y tensa), Whiny (quejumbrosa), Pinched (apretada), Brass (de metal, brillante), Strident (estridente), Cutting (cortante), Piercing (penetrante), Biting (mordaz), Sharp (afilada), Acidic (ácida), Astringent (astringente), Clamorous (clamorosa), Ear-splitting (ensordecedora), Harsh (dura), Jarring (discordante), Penetrating (penetrante), Stingy (punzante) |
| **Defectos vocales (Vocal Defects)** | Fry (Vocal Fry) (crepitante), Sibilant (whistling 'S') (sibilante, ese silbada), Plosive (popping 'P') (plosiva, pe golpeada), Tremulous (shaking) (trémula, temblorosa), Wobbling (ondulante e inestable), Quavering (vacilante), Warbling (con trinos), Slurred (arrastrada), Mumbled (mascullada), Incoherent (incoherente), Thick (espesa) |

### II. Emoción, actitud y tono (el "subtexto")

Adjetivos para ajustar el estado psicológico. **Úsalos en `instruct`, no en `description`.**

| Categoría | Descriptores |
|----------|------------|
| **Agresiva/hostil (Aggressive/Hostile)** | Acerbic (ácida), Antagonistic (antagonista), Belligerent (beligerante), Biting (mordaz), Brusque (brusca), Caustic (cáustica), Combative (combativa), Confrontational (desafiante), Contemptuous (desdeñosa), Cutting (cortante), Derisive (burlona), Dictatorial (dictatorial), Disdainful (despectiva), Dogmatic (dogmática), Domineering (dominante), Explosive (explosiva), Fierce (feroz), Hard (dura), Imperious (imperiosa), Incendiary (incendiaria), Indignant (indignada), Intimidating (intimidante), Malevolent (malévola), Militant (militante), Pugnacious (pugnaz), Savage (salvaje), Scornful (despreciativa), Seething (hirviente de rabia), Sharp (afilada), Spiteful (rencorosa), Stern (severa), Threatening (amenazante), Truculent (truculenta), Vengeful (vengativa), Venomous (venenosa), Vitriolic (vitriólica), Warlike (guerrera) |
| **Triste/deprimida (Sad/Depressed)** | Bereft (desamparada), Bleak (sombría), Broken (quebrada), Crestfallen (alicaída), Dejected (abatida), Desolate (desolada), Despondent (desesperanzada), Disheartened (desanimada), Dismal (lúgubre), Doleful (doliente), Dour (adusta), Forlorn (desamparada), Gloomy (sombría), Grief-stricken (desgarrada por el dolor), Heartbroken (con el corazón roto), Heavy-hearted (con el ánimo pesado), Hollow (vacía), Hopeless (sin esperanza), Lachrymose (llorosa), Melancholy (melancólica), Miserable (miserable), Morose (malhumorada), Mournful (afligida), Pathetic (lastimera), Pessimistic (pesimista), Plaintive (quejumbrosa), Regretful (arrepentida), Remorseful (con remordimiento), Resigned (resignada), Somber (grave), Sorrowful (apenada), Tearful (llorosa), Tragic (trágica), Weepy (llorona), Wistful (nostálgica), Woe-begone (afligida) |
| **Alegre/positiva (Happy/Positive)** | Blissful (dichosa), Buoyant (animosa), Celebratory (festiva), Cheerful (alegre), Chipper (vivaz), Contented (satisfecha), Delightful (encantadora), Ebullient (efervescente), Ecstatic (extática), Elated (exultante), Encouraging (alentadora), Energetic (enérgica), Enthusiastic (entusiasta), Euphoric (eufórica), Exhilarated (exhilarada), Exuberant (exuberante), Festive (festiva), Genial (cordial), Giddy (mareada de alegría), Gleeful (jubilosa), Gratified (complacida), Hearty (campechana), Hopeful (esperanzada), Jaunty (desenvuelta), Jolly (jovial), Jovial (afable y alegre), Joyous (gozosa), Jubilant (jubilosa), Lighthearted (despreocupada), Merry (alegre), Optimistic (optimista), Overjoyed (rebosante de alegría), Peppy (vivaracha), Radiant (radiante), Rapturous (arrobada), Reassured (tranquilizada), Sunny (soleada), Upbeat (animosa), Vibrant (vibrante), Vivacious (vivaz), Zestful (llena de brío) |
| **Temida/ansiosa (Fearful/Anxious)** | Agitated (agitada), Alarmed (alarmada), Apprehensive (aprensiva), Cautious (cautelosa), Concerned (preocupada), Cowardly (cobarde), Distressed (angustiada), Edgy (nerviosa), Faint (débil), Frightened (asustada), Guarded (reservada), Hesitant (vacilante), Hysterical (histérica), Insecure (insegura), Jittery (nerviosa), Nervous (nerviosa), Panic-stricken (presa de pánico), Paralyzed (paralizada), Paranoid (paranoica), Petrified (petrificada), Quaking (temblorosa), Restless (inquieta), Scared (asustada), Shaky (temblorosa), Skittish (asustadiza), Spooked (sobresaltada), Startled (sobresaltada), Tense (tensa), Terrified (aterrada), Timid (tímida), Trembling (temblorosa), Uneasy (incómoda), Unsettled (inquieta), Wary (cauta), Worried (preocupada) |
| **Tranquila/neutral (Calm/Neutral)** | Ambivalent (ambivalente), Apathetic (apática), Balanced (equilibrada), Bland (insulsa), Blasé (blasé), Bored (aburrida), Casual (desenfadada), Collected (serena), Composed (compuesta), Cool (fría), Detached (distante), Dispassionate (desapasionada), Dull (apagada), Easygoing (relajada), Equable (ecuánime), Even-tempered (de temple parejo), Flat (plana), Imperturbable (imperturbable), Indifferent (indiferente), Laid-back (despreocupada), Listless (desganada), Matter-of-fact (objetiva), Measured (mesurada), Mediocre (mediocre), Mild (apacible), Moderate (moderada), Monotone (monótona), Nonchalant (despreocupada), Objective (objetiva), Pacifistic (pacifista), Passive (pasiva), Peaceful (tranquila), Phlegmatic (flemática), Placid (plácida), Relaxed (relajada), Serene (serena), Steady (constante), Stoic (estoica), Tranquil (tranquila), Unemotional (sin emoción), Unflappable (imperturbable) |
| **Afectuosa/íntima (Affectionate/Intimate)** | Adoring (admirativa), Amorous (amorosa), Caring (afectuosa), Cherishing (cariñosa), Comforting (reconfortante), Compassionate (compasiva), Coquettish (coqueta), Cuddly (mimosa), Doting (consentidora), Endearing (entrañable), Fond (cariñosa), Gentle (dulce), Intimate (íntima), Loving (amorosa), Maternal (maternal), Nurturing (protectora), Paternal (paternal), Protective (protectora), Romantic (romántica), Seductive (seductora), Sensual (sensual), Soft (suave), Sultry (cálida y sensual), Sweet (dulce), Sympathetic (comprensiva), Tender (tierna), Touching (conmovedora), Warm (cálida), Yearning (anhelante) |
| **Intelectual/superior (Intellectual/Superior)** | Analytical (analítica), Arrogant (arrogante), Authoritative (autoritaria), Condescending (condescendiente), Critical (crítica), Cynical (cínica), Didactic (didáctica), Dignified (digna), Dry (seca), Educated (culta), Erudite (erudita), Haughty (altiva), Informative (informativa), Inquisitive (inquisitiva), Instructional (instructiva), Knowledgeable (sabia), Lofty (elevada), Logical (lógica), Patronizing (protectora y superior), Pedantic (pedante), Philosophical (filosófica), Pompous (pomposa), Practical (práctica), Pragmatic (pragmática), Precise (precisa), Pretentious (pretenciosa), Professional (profesional), Rational (racional), Sagacious (sagaz), Sarcastic (sarcástica), Sardonic (sardónica), Satirical (satírica), Scholarly (académica), Skeptical (escéptica), Smart (lista), Snobbish (esnob), Sophisticated (sofisticada), Supercilious (altiva y desdeñosa), Superior (superior), Thoughtful (reflexiva), Wise (sabia), Wry (irónica) |
| **Juguetona/peculiar (Playful/Quirky)** | Bantering (bromista), Cheeky (descarada), Childish (infantil), Clownish (payasesca), Droll (graciosa), Eccentric (excéntrica), Facetious (bromista), Fanciful (fantasiosa), Flippant (frívola), Flirty (coqueta), Foolish (tonta), Goofy (payasa), Humorous (humorística), Impish (traviesa), Ironic (irónica), Irreverent (irreverente), Jocular (jocosa), Kooky (chiflada), Mischievous (traviesa), Mocking (burlona), Offbeat (excéntrica), Playful (juguetona), Prankish (bromista), Quirky (peculiar), Rascally (pillastra), Ridiculous (ridícula), Sassy (descarada), Saucy (desenvuelta), Silly (tonta), Snarky (sarcástica), Teasing (provocadora), Tongue-in-cheek (con ironía), Whimsical (caprichosa), Witty (ingeniosa), Zany (alocada) |

### III. Interpretación, ritmo y cadencia (la "mecánica")

Controles de velocidad, fluidez y claridad. **Úsalos en `instruct`, no en `description`.**

| Estilo | Descripción |
|-------|------------|
| **Staccato (picado)** | Corto, separado, con pegada, recortado, abrupto, estilo ametralladora |
| **Legato (ligado)** | Suave, conectado, fluido, deslizante, continuo, vocales alargadas |
| **Rapid-fire (ráfaga)** | Velocidad vertiginosa, urgente, maníaco, estilo subastador, frenético, sin aliento |
| **Measured (mesurado)** | Deliberado, lento, reflexivo, con espacio entre cada palabra, ponderoso |
| **Halting (entrecortado)** | Inseguro, con pausas frecuentes, tartamudeante, inconexo, tropieza |
| **Lilting (cadencioso)** | Sube y baja con ritmo, musical, cantarín, melódico, saltarín |
| **Drawl (arrastrado)** | Vocales alargadas con pereza, lento, relajado, lánguido, estirando las palabras |
| **Punchy (con pegada)** | Énfasis de alta energía en palabras clave, dinámico, golpeando fuerte las consonantes |
| **Deadpan (inexpresivo)** | Cero inflexión, tono plano, comedia seca, interpretación impasible |
| **Cadenced (con cadencia)** | Muy rítmico, casi poético o shakespeariano, medido |

### IV. Especificaciones y arquetipos del sector (el "papel")

Abreviatura usada en desgloses de casting y guiones publicitarios.

| Arquetipo | Descripción |
|-----------|------------|
| **Authentic (auténtico)** | Persona real, no actor, estilo documental, crudo, sin pulir |
| **Announcer (Classic) (locutor clásico)** | "Voice of God" (VOG, voz de Dios), presentacional, retumbante, pulido, DJ de radio |
| **Announcer (Non-Announcery) (locutor sin locución)** | Anti-venta, conversacional, como quien lo deja caer, indiferente |
| **Conversational (conversacional)** | Como hablar con un amigo, informal, finales de palabra arrastrados, respiraciones naturales |
| **Girl/Guy Next Door (la chica/el chico de al lado)** | Cercano, amable, de clase media, fiable, corriente, seguro |
| **Trusted Advisor (asesor de confianza)** | Médico, abogado, padre, planificador financiero, conocedor, tranquilo |
| **Cool Parent (padre/madre moderno)** | Moderno, a la moda, no estricto, comprensivo, "Disney Mom/Dad" |
| **The Millennial/Gen Z (milenial/Gen Z)** | Voz crepitante, entonación ascendente, casual, cínico, tecnológico, rápido, irónico |
| **Promo Voice (voz de tráiler)** | Áspera, grave, gruñente, intensa, susurrante, muy dramática, tráiler de cine |
| **Corporate/Industrial (corporativo/industrial)** | Claro, articulado, profesional, neutro, informativo, seguro |
| **Hard Sell (venta dura)** | Volumen alto, energía alta, urgente, gritando, "Car Dealer," agresivo |
| **Soft Sell (venta suave)** | Íntimo, susurrante, lujoso, caro, reconfortante, seductor |
| **Characters (personajes)** | Villain, Hero, Sidekick, Creature, Monster, Robot, Alien, Witch, Old Crone, Sage |

### V. Comandos de sesión de grabación (el "flujo de trabajo")

Instrucciones para el proceso de grabación.

| Comando | Significado |
|---------|---------|
| **A-B-C (Three-in-a-row) (tres seguidas)** | Leer la misma línea tres veces con tres actitudes distintas |
| **Billboard (destacar)** | Enfatizar un nombre o producto concreto ("Hit the brand name harder") |
| **Button (remate)** | Un final limpio y definitivo de la frase/toma; aterrizar bien |
| **Clean Up (limpiar)** | Regrabar una línea para corregir un ruido de boca o un tropiezo |
| **Cold Read (lectura en frío)** | Leer el guion de inmediato, sin ensayo |
| **Color (dar color)** | Añadir más emoción o personalidad a una palabra concreta |
| **Compression (compresión)** | Reducir el rango dinámico (más fuerte en las partes bajas, más suave en las altas) |
| **Donut (dona)** | Voz al principio y al final, hueco en medio para música/clips |
| **Eat the Mic (comerse el micro)** | Muy cerca del micrófono para el efecto de proximidad (grave/íntimo) |
| **Guide Track (pista guía)** | Igualar el tiempo de una referencia de audio/vídeo pregrabada |
| **Lay Out (callar)** | Hacer una pausa o dejar de hablar durante una sección |
| **Lead-in (entrada)** | Una frase dicha en silencio para entrar en el personaje antes de la primera línea |
| **Level (nivel)** | Leer al volumen de interpretación para fijar la ganancia del micrófono |
| **Lift (levantar)** | Aclarar o subir ligeramente el tono, a menudo al final de un anuncio |
| **Pick-up (regrabar)** | Grabar solo una frase o expresión concreta |
| **Popping (explosión)** | Aire excesivo en los sonidos "P"/"B" (solución: "Turn slightly off-axis") |
| **Projection (proyección)** | Lanzar la voz al fondo de la sala frente a mantenerla interior |
| **Punch-in (insertar)** | Grabar un fragmento pequeño para insertarlo en una toma existente |
| **Safety (seguridad)** | Una última toma por si la anterior buena tenía un fallo técnico |
| **Slate (claqueta)** | Decir el nombre y el número de toma antes de interpretar ("John Doe, Take 1") |
| **Smile (sonreír)** | Sonreír físicamente mientras se lee (cambia el tono a más brillante/cálido) |
| **Sotto Voce (a media voz)** | Un susurro escénico; hablar por debajo del aliento |
| **Throw Away (descartar)** | Quitar énfasis a una línea; hacer que suene poco importante o casual |
| **Time (cronometrar)** | Acelerar o ralentizar para alcanzar una duración estricta (por ejemplo, exactamente 29.5s) |
| **Wild Line (línea suelta)** | Una línea grabada aparte del flujo del guion (gritos, risas, gruñidos) |
| **Walla (murmullo)** | Ruido/murmullo de multitud de fondo interpretado por un grupo |

### VI. Referencia ampliada

Más de 150 términos adicionales en cinco áreas.

#### Fisicalidad y atmósfera

| Dimensión | Términos |
|-----------|-------|
| **Presencia** | Close-mic (micro cercano), Distant (distante), Intimate (íntima), Expansive (expansiva), Internalized (interiorizada), Projection (proyección) |
| **Textura** | Silky (sedosa), Velvety (aterciopelada), Honeyed (melosa), Mellifluous (meliflua), Buttery (mantecosa), Creamy (cremosa), Lush (exuberante), Liquid (líquida), Orotund (rotunda y sonora), Resonant (resonante) |
| **Grano (Grit)** | Gravelly (áspera, rasposa), Raspy (ronca, chirriante), Smoky (ahumada), Husky (ronca y grave), Guttural (gutural), Scratchy (raspante), Coarse (burda), Croaking (como un croar), Grating (chirriante, irritante), Dry (seca), Cracked (quebrada), Sandpaper (como papel de lija), Whiskey-soaked (curtida en whisky), Growling (gruñente), Gruff (brusca), Hoarse (ronca), Ragged (deshilachada), Raw (cruda), Rusty (oxidada), Strained (forzada), Throaty (de garganta) |
| **Brillo/peso (Brightness/Weight)** | Airy (aérea), Breathy (con aire), Feathery (plumosa), Thin (fina), Reedy (como una caña), Tinny (metálica y delgada), Piercing (penetrante), Shrill (estridente), Piping (aguda y aflautada), Wispy (tenue y vaporosa), Fluting (aflautada), Chirpy (gorjeante), Squeaky (chillona), Metallic (metálica), Brittle (quebradiza) |
| **Temperatura** | Warm (cálida), Sunny (soleada), Nurturing (protectora), Genial (cordial), Toast-like (como un brindis), Cool (fría), Detached (distante), Clinical (clínica), Crisp (nítida), Distant (lejana), Icy (gélida), Steel (de acero) |

#### Emociones (intención psicológica)

| Categoría | Términos |
|----------|-------|
| **Alta energía/positiva (High Energy/Positive)** | Jubilant (jubilosa), Exuberant (exuberante), Ebullient (efervescente), Cheerful (alegre), Playful (juguetona), Whimsical (caprichosa), Contented (satisfecha), Blissful (dichosa), Optimistic (optimista), Encouraging (alentadora), Radiant (radiante), Rapturous (arrobada), Peppy (vivaracha), Jaunty (desenvuelta), Jolly (jovial), Jovial (afable y alegre) |
| **Alta energía/negativa (High Energy/Negative)** | Indignant (indignada), Seething (hirviente de rabia), Acerbic (ácida), Strident (estridente), Sharp (afilada), Biting (mordaz), Vitriolic (vitriólica), Threatening (amenazante), Savage (salvaje), Belligerent (beligerante), Antagonistic (antagonista), Caustic (cáustica), Combative (combativa), Confrontational (desafiante), Contemptuous (desdeñosa), Derisive (burlona) |
| **Baja energía/negativa (Low Energy/Negative)** | Melancholy (melancólica), Despondent (desesperanzada), Wistful (nostálgica), Morose (malhumorada), Tearful (llorosa), Heavy-hearted (con el ánimo pesado), Hollow (vacía), Defeated (derrotada), Grief-stricken (desgarrada por el dolor), Bereft (desamparada), Bleak (sombría), Broken (quebrada), Crestfallen (alicaída), Dejected (abatida), Desolate (desolada), Disheartened (desanimada), Dismal (lúgubre), Doleful (doliente), Dour (adusta), Forlorn (desamparada) |
| **Vulnerabilidad** | Jittery (nerviosa), Tremulous (trémula), Quavering (vacilante), Shaky (temblorosa), Hysterical (histérica), Panic-stricken (presa de pánico), Hesitant (vacilante), Guarded (reservada), Tense (tensa), Agitated (agitada), Alarmed (alarmada), Apprehensive (aprensiva), Faint (débil), Frightened (asustada), Skittish (asustadiza), Uneasy (incómoda) |
| **Autoridad/poder (Authority/Power)** | Commanding (con mando), Imperious (imperiosa), Dictatorial (dictatorial), Regal (regia), Stately (majestuosa), Assertive (asertiva), Dogmatic (dogmática), Unyielding (inflexible), Matter-of-fact (objetiva), Dignified (digna), Pompous (pomposa), Supercilious (altiva y desdeñosa), Sagacious (sagaz) |
| **Ironía/ingenio (Irony/Wit)** | Sarcastic (sarcástica), Ironic (irónica), Wry (irónica), Cynical (cínica), Sardonic (sardónica), Satirical (satírica), Facetious (bromista), Flippant (frívola), Sassy (descarada), Saucy (desenvuelta), Cheeky (descarada), Bantering (bromista), Droll (graciosa) |

#### Tono y musicalidad (la melodía)

| Aspecto | Términos |
|--------|-------|
| **Rango** | Bass-heavy (con graves dominantes), Mid-range (rango medio), High-frequency (alta frecuencia), Falsetto (falsete), Head-voice (voz de cabeza), Chest-voice (voz de pecho) |
| **Movimiento** | Uptalk (entonación ascendente, incertidumbre), Down-talk (entonación descendente, finalidad/autoridad), Singsong (sube y baja de canción infantil), Flat/Monotone (variación cero), Modulated (tono variado y controlado) |
| **Registro** | Vocal Fry (crepitante), Nasal (nasal), Twangy (nasal y tensa), Resonance (resonancia), Vibrato (vibrato), Tremolo (trémolo) |

#### Interpretación y mecánica (ritmo y claridad)

| Aspecto | Términos |
|--------|-------|
| **Tempo** | Staccato (picado), Legato (ligado), Rapid-fire (ráfaga), Measured (mesurado), Deliberate (deliberado), Halting (entrecortado), Drawling (arrastrado), Languid (lánguido), Ponderous (ponderoso) |
| **Articulación** | Crisp (nítida), Glassy (vitrea), Over-enunciated (sobreaticulada), Slurred (arrastrada), Lazy (perezosa), Mumbled (mascullada), Thick (espesa), Sibilant (sibilante, ese silbada), Plosive (plosiva, pe/be golpeada) |
| **Énfasis** | Billboard (golpear una palabra clave), Throwaway (quitar énfasis), Punchy (consonantes de alta energía) |
| **Ritmo** | Cadenced (con cadencia), Syncopated (sincopado), Naturalistic (naturalista), Flowing (que fluye), Breathless (sin aliento), Erratic (errático) |

#### Especificaciones y arquetipos (modelos de personaje)

| Categoría | Arquetipos |
|----------|-----------|
| **Publicitario (Commercial)** | Hard-sell (venta dura), Soft-sell (venta suave), Everyman/Everywoman (el hombre/la mujer corriente), Girl/Boy Next Door (la chica/el chico de al lado), Hip/Gen-Z (moderno/Gen Z), Millennial (milenial), Sophisticated/Luxury (sofisticado/lujo) |
| **Narrativo (Narrative)** | The Trusted Advisor (el asesor de confianza), The Sage/Professor (el sabio/el profesor), The Villain/Antagonist (el villano/el antagonista), The Sidekick (el compañero), The Reluctant Hero (el héroe reticente) |
| **Técnico/industrial (Technical/Industry)** | "Announcery" (locución clásica de radio), "Voice of God" (grave/retumbante/omnisciente), "Corporate/Industrial" (pulido/profesional), "Sotto Voce" (susurro escénico), "Walla" (ruido de multitud de fondo) |

### VII. Clasificaciones vocales

Los nombres de registro estándar que usa la primera regla de trabajo de abajo ("empieza por el registro"). Cada uno define un rango de tono y un carácter tonal.

#### Clasificaciones estándar

| Tipo de voz | Género | Rango (aprox.) | Descripción |
|-----------|--------|-----------------|-------------|
| **Soprano** | Femenino | C4 -- C6 | La voz femenina más aguda, conocida por su calidad brillante y elevada. |
| **Mezzo-Soprano** | Femenino | A3 -- A5 | La voz femenina de rango medio, con un tono más oscuro y rico que una soprano. |
| **Contralto** | Femenino | F3 -- F5 | La voz femenina más grave. Muy rara, con una resonancia profunda y potente. |
| **Countertenor** | Masculino | E3 -- E5 | La voz masculina más aguda, que a menudo canta en un rango similar al de una contralto o mezzo-soprano usando falsete. |
| **Tenor** | Masculino | C3 -- C5 | El tipo de voz masculina "natural" más agudo, frecuentemente asignado a papeles heroicos o protagonistas en la ópera. |
| **Baritone** | Masculino | A2 -- A4 | El tipo de voz masculina más común, situado entre tenor y bajo con un tono cálido y versátil. |
| **Bass** | Masculino | E2 -- E4 | La voz masculina más grave, que aporta una base profunda y pesada a los conjuntos vocales. |

#### Clasificaciones especializadas y adicionales

| Tipo de voz | Descripción |
|-----------|-------------|
| **Alto** | En la música coral, "alto" se refiere a la parte femenina más grave (SATB), normalmente cantada por mezzo-sopranos o contraltos. |
| **Bass-Baritone** | Una voz masculina que combina la resonancia grave de un bajo con el rango más agudo de un barítono. |
| **Treble** | Las voces agudas y sin cambio de los niños (tanto niños como niñas). |
| **Sopranista** | Un cantante masculino raro que puede cantar en el rango de soprano sin usar falsete. |

---

## Parte 2: qué hace VoiceDesign con estas palabras

Pruebas de cómo interpreta el modelo VoiceDesign las descripciones de voz, que traducen el vocabulario de arriba al comportamiento real del modelo.

### Cómo se probó

- Todas las pruebas usan las mismas 4 frases neutras en todas las voces
- Campo `instruct` vacío para aislar los efectos de la descripción de voz
- `seed: -1` (aleatoria) para probar la consistencia natural
- Evaluado en: consistencia de la voz entre líneas, adherencia a la descripción, calidad

### Ronda 1: cinco formas de describir una voz

Se probaron 5 enfoques de descripción fundamentalmente distintos (M/F cada uno, 40 muestras en total).

#### Las estrategias

| Estrategia | Enfoque | Ejemplo |
|----------|----------|---------|
| **Anatomy (anatomía)** | Rasgos físicos/acústicos | "A deep male baritone with resonant chest voice, smooth timbre, slight vocal fry" |
| **Persona** | Identidad del personaje | "A kind elderly grandfather who speaks with patience and gentle authority" |
| **Technical (técnica)** | Términos de ingeniería de audio | "A well-projected male voice with crisp diction, moderate pace, controlled dynamic range" |
| **Emotion (emoción)** | Solo cualidades emocionales | "A calm, reassuring male voice that sounds confident, trustworthy, and steady" |
| **Context (contexto)** | Escena/situación | "A male narrator reading a fantasy novel aloud in a quiet bookshop" |

#### Resultados

| Estrategia | Consistencia | Observación clave |
|----------|------------|-----------------|
| **Anatomy (anatomía)** | Mejor (M), pobre (F) | Las descripciones físicas dan al modelo objetivos acústicos concretos. "light soprano, airy" femenina empujó demasiado alto/fino. |
| **Persona** | Variable | Funciona cuando el arquetipo implica fuertemente un tipo vocal (periodista joven -> rápido y chillón). Falla cuando es ambiguo (abuelo -> cambio de género). |
| **Technical (técnica)** | Baja-media | El modelo entiende en parte los términos de ingeniería de audio, pero no de forma fiable. |
| **Emotion (emoción)** | Baja | Demasiado abstracta. Sin ancla física para saber cómo suena "calm" o "warm". |
| **Context (contexto)** | Ninguna | Voz distinta en cada línea. El modelo trata la descripción como dirección de escena, no como identidad de voz. |

#### Conclusión

**Ganan las descripciones que empiezan por la anatomía.** El modelo necesita objetivos acústicos concretos (tono, registro, timbre, textura) para producir voces consistentes. Las cualidades abstractas (emoción, persona, contexto) dan demasiada libertad interpretativa.

### Ronda 2: la anatomía primero

Todas las descripciones se reformularon: registro + timbre + adjetivos tonales. Sin palabras de interpretación/actuación (esas van en `instruct`).

#### La fórmula

> **[registro] + [descriptores de timbre/textura] + [adjetivos de carácter tonal]**
>
> Nunca incluyas: verbos de interpretación (speaking, reading), palabras de ritmo, escena/contexto

#### Resultados

| Voz | Descripción | Consistencia | Calidad | Notas |
|-------|------------|------------|---------|-------|
| **WARM_BARITONE** | "deep male baritone, rich chest resonance, warm smooth timbre, hint of gravelly texture" | Media-alta | Alta | Voz como se describe, muy buena calidad grave, algo de variación |
| **GROUNDED_ALTO** | "female alto, full grounded mid-range, slightly husky, natural warmth" | Media-alta | Alta | Sigue bien la descripción, menos variable que el barítono |
| **BRIGHT_TENOR** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | Muy baja | Alta | Interpretación emocional desatada, calidad "unhinged": ver la investigación del tenor brillante |
| **SILKY_MEZZO** | "female mezzo-soprano, silky even tone, soft rounded edges, gentle clarity" | Muy alta | Muy alta | La mejor en conjunto. Extremadamente fácil de escuchar. |
| **COMMANDING_BASS** | "male bass, dark authoritative rumble, heavy low-end, firm tone" | Alta | Alta | Calidad ronca de vaquero/western. Para quedársela. |
| **CRISP_SOPRANO** | "female soprano, precise crystalline tone, balanced brightness, confident projection" | Muy alta | Alta | Más rango tonal pero sigue siendo consistente. Sensación de estudio. |

#### Qué palabras hacen consistente una voz

**Alta consistencia** — términos a los que el modelo se agarra de forma fiable:
- Suavidad/uniformidad: `silky`, `even`, `soft`, `rounded` (de Smooth/Rich)
- Precisión: `precise`, `crystalline`, `balanced` (implica control)
- Autoridad/firmeza: `dark`, `authoritative`, `firm`, `commanding` (de Resonance)
- Asentamiento: `grounded`, `full`, `husky` (de Rough/Gritty + Resonance)

**Alta variabilidad** — términos que desestabilizan:
- Energía: `bright`, `youthful energy` (de Brightness/Weight: ver la investigación de abajo)
- Calidez ambigua sola: `warm`, `deep` sin una textura que las ancle

**Observación:** las voces femeninas tienden a ser más consistentes que las masculinas en todas las descripciones.

### Investigación del tenor brillante

Se aisló la causa de la inestabilidad de BRIGHT_TENOR mediante ablación sistemática (8 variantes, 32 muestras).

#### Las variantes

| Variante | Descripción | Qué cambió |
|---------|------------|-------------|
| **CONTROL** | "male tenor, bright clear tone, light nasal resonance, youthful energy" | Original: línea base |
| **NO_BRIGHT** | "male tenor, clear tone, light nasal resonance, youthful energy" | Se quitó "bright" |
| **NO_ENERGY** | "male tenor, bright clear tone, light nasal resonance" | Se quitó "youthful energy" |
| **NEITHER** | "male tenor, clear tone, light nasal resonance" | Se quitaron ambas |
| **BRIGHT_ONLY** | "A bright male tenor voice" | Mínima: solo "bright" |
| **SHARP_REPLACE** | "male tenor, sharp forward-placed tone, light nasal resonance, crisp articulation" | Se reemplazó "bright" por "sharp" |
| **HIGH_HARMONICS** | "male tenor, strong upper harmonics, clear forward projection, light nasal resonance" | Equivalente acústico técnico |
| **STABILIZED** | "male tenor, steady clean tone, light nasal resonance, even delivery" | Se aplicó la fórmula de consistencia |

#### Resultados

| Variante | Consistencia | Carácter | Hallazgo |
|---------|------------|-----------|---------|
| **CONTROL** | Muy baja | Desatado, oscilaciones emocionales salvajes | Confirma el comportamiento de R2 |
| **NO_BRIGHT** | Media | Expresivo pero guiado por el texto, menos desatado | "Bright" contribuye a la volatilidad emocional |
| **NO_ENERGY** | Media | Parecido a NO_BRIGHT, un pico de ritmo de subastador en texto dramático | "Energy" contribuye a los estallidos de ritmo |
| **NEITHER** | Alta | Sigue las señales del texto, estable | La base de tenor desnuda es estable de por sí |
| **BRIGHT_ONLY** | Alta | Estable, responde al texto | "Bright" por sí sola está bien |
| **SHARP_REPLACE** | Alta | Calidad molesta/irritable | "Sharp/forward-placed" se traduce en tono irritado |
| **HIGH_HARMONICS** | Media | Voz elevada, casi gritando, muy clara | Nicho útil: efecto de alerta/despertar |
| **STABILIZED** | Alta | De narrador, poca emoción | La fórmula de consistencia funciona pero aplana la expresión |

#### Hallazgo clave: dos palabras estables pueden ser inestables juntas

**"Bright" y "youthful energy" son estables por separado, pero interactúan de forma destructiva.** La combinación da al modelo señales contradictorias — brillo tonal (acústico) + energía conductual (interpretación) — y duda entre interpretaciones en cada generación.

Esto es probablemente un principio general: **mezclar descriptores acústicos (Sección I) con descriptores conductuales/de energía (Sección III) en la descripción de voz crea ambigüedad.** Mantén las descripciones de voz puramente en el territorio de la Sección I; usa `instruct` para las cualidades de las Secciones II-IV.

#### Cómo se interpretan palabras concretas

| Término | Sección | Interpretación del modelo | Utilidad |
|------|---------|---------------------|------------|
| `bright` (sola) | I. Brightness/Weight | Brillo tonal estable | Buena aislada |
| `bright` + términos de energía | I + III | Caos emocional | Evita la combinación |
| `sharp` | I. Nasal/Sharp | Tono molesto/irritable | Úsalo con cuidado |
| `upper harmonics` | I. (técnico) | Volumen elevado, casi grito | Nicho: efecto de alerta |
| `steady`, `even` | I. Smooth/Rich | Estabilidad de narrador | Aplana la expresión |
| `silky`, `rounded` | I. Smooth/Rich | Alta consistencia + facilidad de escucha | Lo mejor para narración |
| `dark`, `authoritative` | I. Resonance | Calidad ronca/western, consistente | Buena para autoridad masculina |
| `crystalline`, `precise` | I. Smooth/Rich | Consistente con rango expresivo | El mejor equilibrio |
| `hollow` (en instruct) | I. Resonance | Híbrido de susurro/suspiro, vacío con aire | Evítalo en instruct: el modelo lo lee como acústico, no emocional. Usa "defeated" en su lugar. |

### Reglas de trabajo para las descripciones de voz

1. **Empieza por el registro** (bass, baritone, tenor, alto, mezzo-soprano, soprano)
2. **Añade 2-3 términos de textura/timbre** solo de la Sección I
3. **Nunca mezcles descriptores acústicos (Sección I) y conductuales (Secciones II-IV)** en el campo description
4. **Usa `instruct` para la interpretación, la emoción y el ritmo** — para eso sirve
5. **Prefiere términos de "control"** (`silky`, `even`, `precise`, `firm`) a términos de "energía" (`bright`, `sharp`, `piercing`) para la consistencia
6. **Especificar de más con términos ambiguos multiplica la incertidumbre** — las descripciones mínimas y claras pueden superar a las detalladas y ambiguas
7. **Las voces femeninas son intrínsecamente más consistentes** en este modelo en todos los estilos de descripción
