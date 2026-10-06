---
id: I4e.hair_edit
version: 1
kind: image
provider: openai_images
builder: edit
route:
  draft: {model: gpt-image-2.5-flare-2026-09-08, quality: low, n: 4}
  final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: high, n: 1}
  ab_final: {model: gpt-image-2.5-sunburst-2026-09-08, quality: xhigh, n: 1}
size: 1024x1536
background: opaque
image1_role: edit_target
mask: hair_box_minus_lower_55pct_of_head_front
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
  fringe_line: {source: phrases.hair}
style_block: HOUSE_STYLE_3D_INPUT
priming: [face, eyes, hat]
inputs:
  fix_sentence: {kind: str, required: true}
  keep: {kind: list}
  style_ref: {kind: flag}
flags: [style_ref]
checks:
  gate_a: [A_SIZE, A_SIL_GUIDE, A_SYMMETRY, A_OCR]
  gate_b: [hr_head_unchanged, hr_no_face, hr_front_ortho, hr_bangs_clear, hr_matches_concept, hr_chunky, hr_volume_readable, ip_no_brand,
    ip_no_known_character, ip_no_text]
bootstrap: false
images:
- {id: current_asset, text: 'the current hairstyle on the bald grey cube head, front view.'}
- {id: style_sheet, text: style sheet; match its colouring only., when: style_ref}
---
PURPOSE: Small change to an approved front-view hairstyle used as the input for 3D modelling.
IMAGES: {images_line}
SUBJECT: The same hairstyle with one change.
MUST:
1. {fix_sentence}
2. Keep everything else exactly as in Image 1.
3. Keep the grey head's exact size, shape and position.
4. Straight-on front view, level, no perspective.
5. {fringe_line}
STYLE: {style_block}
KEEP: {keep_list?}
EXCLUDE: facial features, hat, hair accessories, body, text, watermark.
