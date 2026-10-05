---
id: I1b.concept_back
version: 1
kind: image
provider: openai_images
builder: i1
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
background: opaque
must_lines: 5
style_block: HOUSE_STYLE_2D
priming: []
bootstrap: false
budget: {max_chars_excl_style: 1880, max_chars_total: 2200}
size: 768x1024
images:
- {id: guide_concept_back, text: 'layout guide: one flat-coloured blocky figure seen from behind, with colour swatches along the bottom
    edge.'}
- {id: approved_front, text: the approved front view of the same character; match its design and rendering.}
image1_role: edit_target
mask: figure_box
dna_fields: [shape_language, motif_object]
slots:
  presentation_style: {source: phrases.presentation_style}
  hair_phrase:
    source: kit+spec hair
    max_words: 16
    lint: [free_text]
  top_phrase:
    source: kit+spec top
    max_words: 20
    lint: [free_text]
  bottom_phrase:
    source: kit+spec bottom
    max_words: 16
    lint: [free_text]
  shoe_phrase: {source: kit shoe, max_words: 6}
  accessory_phrase:
    source: spec accessories
    max_words: 36
    lint: [free_text]
  colour_names: {source: palette roles -> colour_names.json}
  face_phrase: {source: face grammar phrase map, max_words: 16}
  shape_language_line: {source: dna.shape_language}
  motif_object:
    source: dna.motif_object
    max_words: 5
    lint: [free_text]
inputs: {}
flags: []
notes: Image 2 = the chosen I1f front of this character, which also carries the house rendering.
---
PURPOSE: Concept art of the back view of an approved original game character.
IMAGES: {images_line}
SUBJECT: Paint the figure from directly behind as the same character: {hair_phrase}; {top_phrase}; {bottom_phrase}; {shoe_phrase}[[; {accessory_phrase}]].
MUST:
1. Keep the figure's exact blocky shape, size and position from Image 1; clothing is flat artwork painted on the boxes.
2. The same character seen from directly behind, showing the back of the hair and clothing.
3. Every detail that shows on both sides matches Image 2 exactly; colours come from the matching areas and swatches of Image 1.
4. {shape_language_line}
5. Signature detail: the motif {motif_object}, shown only where it can be seen from behind.
STYLE: {style_block}
KEEP: the light grey background, the colour swatches and the spacing of Image 1.
EXCLUDE: text, letters, numbers, logos, watermark, additional people, floor shadow, background objects, background scenery.
