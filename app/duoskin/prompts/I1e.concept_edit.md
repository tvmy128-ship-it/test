---
id: I1e.concept_edit
version: 1
kind: image
provider: openai_images
builder: i1
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
background: opaque
must_lines: 4
style_block: HOUSE_STYLE_2D
priming: []
bootstrap: false
size: 1536x1024
images:
- {id: current_concept, text: 'the current concept: the same character seen from the front (left) and from behind (right).'}
- {id: house_style_sheet, text: house style reference; match its rendering only.}
image1_role: edit_target
mask: region_or_figure_boxes
dna_fields: [shape_language, motif_object]
slots:
  fix_sentence:
    source: L7 fix_sentence
    max_words: 25
    lint: [free_text]
  shape_language_line: {source: dna.shape_language}
  motif_object:
    source: dna.motif_object
    max_words: 5
    lint: [free_text]
inputs:
  fix_sentence: {kind: str, required: true}
flags: []
---
PURPOSE: Small change to approved concept art of one original game character, front and back.
IMAGES: {images_line}
SUBJECT: The same character in both views, with one change.
MUST:
1. {fix_sentence}
2. Keep both figures' blocky shape, pose, face and every unmentioned detail exactly as in Image 1.
3. {shape_language_line}
4. Signature detail: {motif_object}, clearly visible in both views where it appears.
STYLE: {style_block}
KEEP: the light grey background, the colour swatches and the spacing of Image 1.
EXCLUDE: text, letters, numbers, logos, watermark, additional people, floor shadow, background objects, background scenery.
