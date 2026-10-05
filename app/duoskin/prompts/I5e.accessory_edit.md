---
id: I5e.accessory_edit
version: 1
kind: image
provider: openai_images
builder: edit
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
size: 1024x1024
background: transparent
must_lines: 5
dna_fields: []
slots:
  fix_sentence:
    source: L7 fix_sentence
    max_words: 25
    lint: [free_text]
  keep_list:
    source: L7 keep
    lint: [free_text]
style_block: HOUSE_STYLE_3D_INPUT
bootstrap: false
priming: [character, avatar, hand, shelf]
inputs:
  accessory: {kind: int}
  fix_sentence: {kind: str, required: true}
  keep: {kind: list}
  style_ref: {kind: flag}
flags: [style_ref]
checks:
  gate_a: [A_SIZE, A_ALPHA, A_COMPONENTS, A_MARGIN, A_STROKE, A_SYMMETRY, A_OCR, A_GLYPH, A_PALETTE]
  gate_b: [ac_single_object, ac_front_ortho, ac_no_thin_parts, ac_matches_concept, ac_flat_light, ip_no_brand, ip_no_known_character,
    ip_no_text]
image1_role: edit_target
mask: all_editable
images:
- {id: current_asset, text: the current object.}
- {id: style_sheet, text: style sheet; match its colouring only., when: style_ref}
---
PURPOSE: Small change to an approved reference image of one small toy-like object.
IMAGES: {images_line}
SUBJECT: The same object with one change.
MUST:
1. {fix_sentence}
2. Keep everything else exactly as in Image 1.
3. The whole object centred, seen straight from the front, margin kept.
4. One solid connected object with thick simple parts.
5. Soft even light and flat base colours.
STYLE: {style_block}
KEEP: {keep_list}
OUTPUT: The object alone on a fully transparent background with clean hard alpha edges. Preserve the transparent background.
EXCLUDE: floor, plinth, cast shadow, hands, people, packaging, text, logos, watermark.
