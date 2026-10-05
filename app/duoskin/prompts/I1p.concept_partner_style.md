---
id: I1p.concept_partner_style
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
size: 1536x1024
images:
- {id: guide_concept_char, text: 'layout guide: two flat-coloured blocky figures (left = front view, right = back view) with colour
    swatches along the bottom edge.'}
- {id: house_style_sheet, text: house style reference; match its rendering only.}
- {id: partner_front, text: 'partner character; match its rendering style only, not its hair, face, colours or outfit.'}
image1_role: edit_target
mask: figure_boxes
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
notes: I1 with Image 3 = A's chosen front figure; the mood image is dropped (U8 allows 2 references besides the edited image).
---
PURPOSE: Concept art of one original game character, front and back, for design approval.
IMAGES: {images_line}
SUBJECT: Paint both figures as the same {presentation_style} character: {hair_phrase}; {top_phrase}; {bottom_phrase}; {shoe_phrase}[[; {accessory_phrase}]]. Main colours: {colour_names}.
MUST:
1. Keep each figure's exact blocky shape, size and position from Image 1: cube head, box torso, straight box arms and legs; clothing is flat artwork painted on the boxes.
2. Left figure: a flat 2D anime-style face on the front of the cube head, {face_phrase}. Right figure: the same character seen from directly behind, showing the back of the hair and clothing.
3. Take hair, clothing and shoe colours from the matching areas and swatches of Image 1; both figures match in every detail.
4. {shape_language_line}
5. Signature detail: {motif_object}, clearly visible in both views where it appears.
STYLE: {style_block}
KEEP: the light grey background, the colour swatches and the spacing of Image 1.
EXCLUDE: text, letters, numbers, logos, watermark, additional people, floor shadow, background objects, background scenery.
