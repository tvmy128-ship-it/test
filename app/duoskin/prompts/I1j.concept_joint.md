---
id: I1j.concept_joint
version: 1
kind: image
provider: openai_images
builder: i1j
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
background: opaque
must_lines: 4
style_block: HOUSE_STYLE_2D
priming: []
bootstrap: false
budget: {max_chars_excl_style: 2400, max_chars_total: 2750}
size: 3072x1024
images:
- {id: guide_concept_joint, text: 'layout guide: four flat-coloured blocky figures from left to right: character one from the front,
    character one from behind, character two from the front, character two from behind, with colour swatches along the bottom edge.'}
- {id: house_style_sheet, text: house style reference; match its rendering only.}
image1_role: edit_target
mask: figure_boxes
dna_fields: []
slots:
  presentation_style_a: {source: both characters}
  presentation_style_b: {source: both characters}
  phrases_a: {source: both characters}
  phrases_b: {source: both characters}
  colour_names_a: {source: both characters}
  colour_names_b: {source: both characters}
  face_phrase_a: {source: both characters}
  face_phrase_b: {source: both characters}
inputs: {}
flags: []
notes: 'A/B arm on pilot day only. No DNA lines: a joint call would mix both characters'' CHARACTER fields.'
---
PURPOSE: Concept art of two original game characters, each seen from the front and from behind, for design approval.
IMAGES: {images_line}
SUBJECT: Character one, a {presentation_style_a} character: {phrases_a}. Character two, a {presentation_style_b} character: {phrases_b}. Main colours of character one: {colour_names_a}. Main colours of character two: {colour_names_b}.
MUST:
1. Keep each figure's exact blocky shape, size and position from Image 1: cube head, box torso, straight box arms and legs; clothing is flat artwork painted on the boxes.
2. The first and third figures show a flat 2D anime-style face on the front of the cube head (character one {face_phrase_a}; character two {face_phrase_b}); the second and fourth figures are seen from directly behind.
3. Take each character's hair, clothing and shoe colours from the matching areas and swatches of Image 1; the two views of one character match in every detail.
4. Each character keeps its own hair, outfit and colours; the two characters share only the drawing style.
STYLE: {style_block}
KEEP: the light grey background, the colour swatches and the spacing of Image 1.
EXCLUDE: text, letters, numbers, logos, watermark, additional people, floor shadow, background objects, background scenery.
