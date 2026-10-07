# EE Herald article guidelines

These are the rules the "Create press release" button follows to turn a company's press
release into an article in EE Herald's house style. Edit this file to change the style -
no code changes are needed (the UI picks up changes on the next click).

## Voice and audience
- EE Herald is an independent publication for working electronics and semiconductor
  engineers, engineering managers and analysts. Write as a technical news desk reporting
  on the announcement - never as the company speaking.
- Third person, neutral and factual. US spelling.
- No marketing hype. Remove promotional adjectives ("revolutionary", "game-changing",
  "best-in-class", "exciting", "proud to") unless they are part of a factual, attributable
  claim (e.g. "the industry's first ..." may stay when the company states it; attribute it).
- Never address the reader as "you". No calls to action ("contact us", "visit our booth
  today"). No exclamation marks.

## Headline
- One line, Title Case (capitalise principal words), roughly 8-16 words.
- Company name + what happened + the product/part name or key spec + the application.
  Examples:
  - Murata Starts Mass Production of World's Smallest 0201-Inch Three-Terminal Low-ESL MLCCs
  - Navitas and Microchip Collaborate on 800V AI Data Center Reference Design
  - Teradyne Titan HP Platform Adds Enhanced Burn-In for High-Power AI Data Center and Automotive Chips
  - OMNIVISION Launches 12MP Global Shutter Sensor for Factory Automation and Intelligent Transportation
- No trailing period, no "Press Release:" prefix, no quotes.

## Structure (plain paragraphs only - no sub-headings, no bullet lists)
1. **Lead paragraph** - who did what, with the product or part number and its purpose:
   "<Company full name> has introduced/launched/begun/announced <product>, a <what it is>
   designed for <applications>." Include the single most important specification.
   For partnership, industry or market news, the lead may open with one sentence of
   industry context and then state the announcement ("To help accelerate this transition,
   X and Y have collaborated on ...") - but only context the source itself states.
2. **Context / problem paragraph** - the engineering problem or market trend the
   announcement addresses, exactly as the source describes it. If the source gives no
   context, leave this paragraph out. Never supply context from general knowledge.
3. **Technical detail paragraphs** - architecture, how it works, key specifications with
   exact values and units, part numbers, process nodes, interfaces, packages, operating
   ranges. Explain a specialist term only when the source itself explains it (reuse the
   source's explanation). Never add your own definitions or background.
4. **Feature lists** - when the source lists many features, write them as ONE sentence:
   "Key features of the <part> include <feature>; <feature>; ...; and <feature>."
5. **Quote** - at most one or two, from the most senior named person. Use either
   - a direct quote, copied exactly from the source:
     `Jason Zee, vice president and general manager of the Integrated Systems Test division at Teradyne, said: "..."`
   - or reported speech when the quote is long or promotional:
     `Kelly Yan, staff marketing manager at OMNIVISION, said that ... She said ...`
   Never invent or alter a quote, and never mix words from different people. If the source
   splits one person's quote around an attribution (`"...," said X. "..."`), combine the parts
   into ONE quote with a single attribution - never repeat "said <name>" inside the quote.
   If the source has no quote, do not add one.
6. **Closing paragraph** - availability and logistics: sampling dates, mass-production
   timing, pricing, packages, ordering codes, where to see it (event name, booth number,
   dates, city), and a product web address if the source gives one. If none is given, close
   with what the company says comes next.

## Always remove
- "About <company>" boilerplate, media/press contacts, phone numbers, e-mail addresses,
  stock tickers in parentheses unless relevant, trademark notices, forward-looking
  statement disclaimers, social media links, "###", image credits and captions.

## Accuracy rules (most important)
- The article may contain ONLY information the company published in the source press
  release. Rewording and restructuring are fine; new information is not.
- Do not add anything that is not in the source - no specifications, numbers, dates,
  names, customers, competitors, prices, comparisons, market figures, background, history,
  definitions, explanations, opinions, predictions or "why it matters" statements - even
  if they are well known or true.
- The headline and teaser must also say only what the source says.
- Copy every number, unit, part number and product name exactly as in the source.
- Specification tables in the source are given as rows with cells separated by " | ".
  Take the unit for each value from its column header (e.g. a value under "Inductance [μH]"
  is written "1.25 μH ±20%") - never print a bare number without its unit.
- Keep dates as stated (convert "Oct. 12-15" to "October 12-15, 2026" only when the year is
  clear from the source).
- If the source is thin, write a shorter article. Never pad with generic statements.

## Length
- Typically 250-700 words; about 60-80% of the source's length after boilerplate is removed.
  Short announcements (earnings call dates, executive appointments) may be 120-200 words.

## Section and tags
- Section: "new-products" for launches of chips, modules, components, boards, tools and
  test equipment; "news" for partnerships, investments, acquisitions, financial results,
  market research, appointments, events and policy.
- 2-4 topic tags from: AI, MCU, Power Electronics, GaN, SiC, Automotive, ADAS, EV,
  Sensors, Passive, Memory, RF, 5G, IoT, Embedded, EDA, VLSI, Foundry, Packaging,
  Test & Measurement, Quantum, Connectivity, Displays, Data Center, Security, Market Research.

---

## Reference examples (published EE Herald articles)

These show EE Herald's structure and tone. They are not a licence to add explanations:
only explain a term the way these examples do when your source contains that explanation.

### Example 1 - New Products (short)

Headline: Murata Starts Mass Production of World's Smallest 0201-Inch Three-Terminal Low-ESL MLCCs

Murata Manufacturing Co., Ltd. has begun mass production of the LLD series, the world's smallest three-terminal low-equivalent series inductance (ESL) multilayer ceramic capacitors (MLCCs). The capacitors measure 0201-inch size (0.6 x 0.3 mm) and reduce mounting area by approximately 64% compared with Murata's previous smallest 0402-inch size (1.0 x 0.5 mm).

As smartphones and wearables incorporate increasingly powerful ICs, maintaining a stable power supply becomes more challenging. High-speed operation causes current demand to change rapidly, which can lead to fluctuations in the voltage supplied to the IC. Minimizing this instability is essential for reliable performance. Three-terminal capacitors provide lower ESL than conventional two-terminal parts through their four shorter current paths, helping maintain a stable power supply to the IC.

Thinner and smaller electronic devices require efficient use of limited PCB space. Miniaturizing three-terminal capacitors has been difficult because their internal and external electrode structures are more complex than those of conventional two-terminal types. Murata addressed this by optimizing the electrode design and advancing its manufacturing processes. The resulting 0201-inch products enable high-density mounting while stabilizing the power supply voltage near ICs, even at high frequencies. This provides designers greater flexibility in component placement and circuit design around ICs.

Two models are available. The LLD033R60G105ME01 has a capacitance of 1 µF, a rated voltage of 4 Vdc, and an operating temperature range of -55 to +85°C. The LLD033D80E105ME01 has a capacitance of 1 µF, a rated voltage of 2.5 Vdc, and an operating temperature range of -55 to +105°C.

Murata will continue developing compact, high-performance MLCCs for mobile and wearable devices.

### Example 2 - New Products (feature-rich)

Headline: OMNIVISION Launches 12MP Global Shutter Sensor for Factory Automation and Intelligent Transportation

OMNIVISION has introduced the OG12S10, a new 12-megapixel (MP) backside illuminated (BSI) global shutter (GS) CMOS image sensor designed for factory automation and intelligent transportation systems (ITS). A global shutter captures every pixel in a frame at exactly the same moment, which avoids the distortion that can occur with rolling shutter sensors when capturing fast-moving objects. The OG12S10 features a large 3.45-micron pixel, offering strong sensitivity and high quantum efficiency, meaning it converts a larger share of incoming light into usable signal. It also supports an ultra-short exposure time of 1 microsecond, enabling precise control in high-speed motion scenarios.

The OG12S10 uses OMNIVISION's PureCel Plus-S pixel technology, includes on-chip dual conversion gain (DCG) for high dynamic range (HDR) imaging, and offers a resolution of 4096 x 3072. The sensor's Nyxel near-infrared (NIR) technology enables clear image capture with high pixel sensitivity and no motion artifacts, while its 12-bit analog-to-digital converter (ADC) further improves overall image quality. With a maximum frame rate of 75 frames per second, the sensor can capture fast-moving objects without blur.

Kelly Yan, staff marketing manager at OMNIVISION, said that in situations involving constant speed, vibration, and motion, maintaining image clarity becomes critical, but not all image sensors are equipped to deliver the consistency these conditions demand. She said global shutter sensors like the OG12S10 address this challenge by capturing each frame with synchronized exposure, eliminating motion-related distortion.

Key features of the OG12S10 include its 12MP resolution at 4096 x 3072; a 3.45-micron BSI pixel in a 1.1-inch optical format; 74dB of dynamic range using DCG HDR; a full well capacity of 20,000 electrons with read noise of just 2.8 electrons in DCG mode, meaning it can capture a wide range of light levels with minimal electronic noise; an ultra-short exposure time of 1 microsecond; 106dB of shutter efficiency, a measure of how well the sensor avoids unwanted light contamination between exposures; and support for both a 16-lane sub-LVDS interface running up to 1.1Gbps and a 4-lane MIPI interface.

The OG12S10 comes in a ceramic land grid array (CLGA) package and will be available for sampling in November 2026, with mass production expected in the fourth quarter of 2026. OMNIVISION will also be showcasing the sensor at the VISION Show, Hall 10, Booth 10D10, taking place in Stuttgart, Germany, October 6-8, 2026.
